"""API de captura de reuniones de Teams en Projecta PMO Intelligence.

Tres grupos de operaciones:

· **Captura** — encolar un barrido de reuniones, o capturar una
  concreta. Es lo que dispara el ciclo de captura.
· **Consulta** — ver las reuniones ya capturadas y leer su
  transcripción.
· **Salud** — el estado de la integración: si está configurada, qué
  se ha capturado y cuándo.

Todo lo que **escribe** exige nivel de administrador de organización:
traer reuniones es una operación de lectura sobre el tenant de
Microsoft 365, y quien la autoriza es la misma figura que autoriza
cualquier acceso a los datos de la organización.

Lo que **consulta** exige un actor autenticado, y el alcance lo dan los
roles: quien no puede ver un proyecto no ve sus reuniones.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, false, func, or_, select

from app.api.deps import DependenciaActor, DependenciaSesion
from app.core import rbac
from app.core.db.models import ReunionCapturada

LOG = logging.getLogger("projecta.captura.api")

router = APIRouter(prefix="/reuniones", tags=["captura de reuniones"])


# ─────────────────────────── esquemas ───────────────────────────


class CapturaSolicitada(BaseModel):
    """Petición de una pasada de captura de reuniones."""

    #: Proyecto al que se asocian las reuniones capturadas.
    proyecto_id: str | None = None
    #: Cuántas horas hacia atrás se mira. Por defecto, la de la
    #: configuración.
    horas: int | None = Field(default=None, ge=1, le=720)
    #: Capturar una reunión concreta en vez de barrer el rango.
    reunion_id: str | None = None


# ─────────────────────────── utilidades ───────────────────────────


def _exigir_administrador(actor: rbac.Actor) -> None:
    """Exige nivel de administrador de organización."""
    decision = rbac.evaluar(actor, rbac.administrar)
    if not decision.permitido:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=decision.motivo,
        )


def _organizacion(actor: rbac.Actor) -> uuid.UUID:
    """Devuelve la organización del actor."""
    return uuid.UUID(str(actor.organizacion_id))


def _puede_ver(actor: rbac.Actor, proyecto_id: uuid.UUID | None) -> bool:
    """Comprueba si el actor puede ver lo que pertenece a un proyecto.

    Una reunión sin proyecto pertenece a la organización, y se ven con
    la política de ámbito de organización. Una reunión con proyecto
    exige además que el actor sea miembro de él: el alcance lo dan los
    roles, no el identificador de la reunión.
    """
    if proyecto_id is None:
        decision = rbac.evaluar(actor, rbac.ambito_organizacion)
        return decision.permitido
    decision = rbac.evaluar(
        actor, rbac.cargar_evidencia, proyecto_id=str(proyecto_id)
    )
    return decision.permitido


def _alcance_sql(actor: rbac.Actor) -> Any:
    """Traduce el alcance del actor a una condición SQL.

    Es la **misma regla** que aplica `_puede_ver`, pero expresada en SQL
    para que el filtro vaya **antes** del recorte. Si el filtro se
    aplicara después, con más reuniones que el límite las que caían
    fuera de la ventana serían inalcanzables, y las que el actor sí
    podía ver quedarían fuera porque otras ocupaban la página.

    * Una reunión **sin proyecto** es de la organización: la ve quien
      tenga el ámbito de organización.
    * Una reunión **con proyecto** exige nivel para cargar evidencia y,
      salvo que el actor tenga ámbito de organización, ser miembro del
      proyecto.

    La matriz de equivalencia con `_puede_ver` la comprueba la prueba
    `test_13_el_alcance_sql_y_el_de_python_coinciden`.
    """
    condiciones = []

    if actor.nivel >= rbac.ambito_organizacion.nivel_requerido:
        condiciones.append(ReunionCapturada.proyecto_id.is_(None))

    if actor.nivel >= rbac.cargar_evidencia.nivel_requerido:
        if actor.ambito_organizacion:
            # Con ámbito de organización, cualquier proyecto de la
            # organización es visible.
            condiciones.append(ReunionCapturada.proyecto_id.is_not(None))
        elif actor.proyectos:
            condiciones.append(
                ReunionCapturada.proyecto_id.in_(
                    [uuid.UUID(str(proyecto)) for proyecto in actor.proyectos]
                )
            )

    if not condiciones:
        return false()
    if len(condiciones) == 1:
        return condiciones[0]
    return or_(*condiciones)


async def _validar_proyecto(
    sesion: Any, actor: rbac.Actor, proyecto_id: str | None
) -> str | None:
    """Comprueba que el proyecto pertenece a la organización del actor.

    Una reunión puede quedar colgada de un proyecto, o no. Si lo lleva,
    ese proyecto tiene que ser **de la misma organización** que el actor:
    la clave foránea solo ata la reunión al proyecto, no el proyecto a la
    organización, y sin esta comprobación una reunión de la organización A
    puede apuntar a un proyecto de la B. La fila resultante no la ve
    nadie, ni siquiera quien la creó, y no hay forma de saber por qué
    desapareció.

    Un 404 y no un 403: lo que se protege es la **existencia** del
    proyecto en esta organización, y confirmar que existe en otra ya es
    filtrar información.
    """
    if proyecto_id is None:
        return None

    try:
        identificador = uuid.UUID(str(proyecto_id))
    except (ValueError, AttributeError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"El proyecto {proyecto_id!r} no es un identificador válido.",
        ) from error

    from app.core.db.models import Proyecto

    resultado = await sesion.execute(
        select(Proyecto).where(
            Proyecto.id == identificador,
            Proyecto.organizacion_id == _organizacion(actor),
        )
    )
    if resultado.scalars().first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el proyecto {identificador} en esta organización.",
        )
    return str(identificador)


# ─────────────────────────── captura ───────────────────────────


@router.post(
    "/capturar",
    summary="Encola la captura de reuniones de Teams",
)
async def encolar_captura(
    cuerpo: CapturaSolicitada,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Encola un barrido de las reuniones del organizador.

    La captura va a la cola `integraciones`, que el contrato del
    proyecto ya define. No hay cola nueva.

    Si se indica ``reunion_id``, captura esa reunión concreta. Si no,
    barre la ventana temporal configurada.
    """
    _exigir_administrador(actor)

    # El proyecto tiene que ser **de la organización del actor**. Sin esta
    # comprobación, una reunión de la organización A puede quedar colgada
    # de un proyecto de la organización B: la fila deja de ser visible
    # para los dos (cada uno la deniega por su alcance) y desaparece sin
    # dejar rastro de por qué.
    proyecto_id = await _validar_proyecto(sesion, actor, cuerpo.proyecto_id)

    from app.workers.tasks.captura_teams import (
        TAREA_CAPTURA_TEAMS,
        capturar_reuniones,
    )

    tarea = capturar_reuniones.apply_async(
        kwargs={
            "organizacion_id": actor.organizacion_id,
            "proyecto_id": proyecto_id,
            "horas": cuerpo.horas,
            "reunion_id": cuerpo.reunion_id,
        },
        queue="integraciones",
    )
    return {
        "tarea": TAREA_CAPTURA_TEAMS,
        "id": tarea.id,
        "cola": "integraciones",
        "organizacion_id": actor.organizacion_id,
        "reunion_id": cuerpo.reunion_id,
    }


# ─────────────────────────── consulta ───────────────────────────


@router.get(
    "",
    summary="Lista las reuniones capturadas",
)
async def listar_reuniones(
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    proyecto_id: str | None = Query(default=None),
    limite: int = Query(default=50, ge=1, le=500),
    desplazamiento: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Lista las reuniones capturadas de la organización.

    Filtra por proyecto cuando se indica, y el alcance por rol se
    comprueba **en la consulta**, antes del recorte.

    ``desplazamiento`` es lo que hace falta para paginar: sin él, con
    más reuniones que el límite, las que quedaban fuera de la ventana
    eran inalcanzables.
    """
    filtros = [
        ReunionCapturada.organizacion_id == _organizacion(actor),
        _alcance_sql(actor),
    ]
    if proyecto_id:
        filtros.append(
            ReunionCapturada.proyecto_id == uuid.UUID(str(proyecto_id))
        )

    # El total es el de lo que le corresponde al actor, no el número de
    # la página. Se dice explícitamente, para que quien lo lea no lo
    # confunda.
    total = await sesion.execute(
        select(func.count()).select_from(ReunionCapturada).where(and_(*filtros))
    )
    total_real = total.scalar() or 0

    consulta = (
        select(ReunionCapturada)
        .where(and_(*filtros))
        .order_by(ReunionCapturada.capturado_en.desc())
        .limit(limite)
        .offset(desplazamiento)
    )
    resultado = await sesion.execute(consulta)
    filas = list(resultado.scalars())

    visibles = [
        {
            "id": str(fila.id),
            "reunion_id": fila.reunion_id,
            "asunto": fila.asunto,
            "organizador": fila.organizador,
            "segmentos": fila.segmentos_totales,
            "participantes": len(fila.participantes or []),
            "tiene_audio": bool(fila.audio_objeto),
            "audio_bytes": fila.audio_bytes,
            "huella": fila.huella,
            "capturado_en": fila.capturado_en.isoformat() if fila.capturado_en else None,
        }
        for fila in filas
    ]
    return {
        "reuniones": visibles,
        "total": total_real,
        "total_en_la_pagina": len(visibles),
        "limite": limite,
        "desplazamiento": desplazamiento,
        "hay_mas": desplazamiento + len(visibles) < total_real,
    }


@router.get(
    "/{reunion_id}",
    summary="Lee la transcripción de una reunión capturada",
)
async def leer_reunion(
    reunion_id: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Devuelve la transcripción completa de una reunión, con su hablante.

    Devuelve también el texto corrido, que es lo que se analiza.
    """
    resultado = await sesion.execute(
        select(ReunionCapturada).where(
            ReunionCapturada.organizacion_id == _organizacion(actor),
            ReunionCapturada.reunion_id == reunion_id,
        )
    )
    fila = resultado.scalars().first()
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No hay ninguna reunión capturada con el identificador {reunion_id}.",
        )
    if not _puede_ver(actor, fila.proyecto_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El actor no tiene acceso a los datos de esta reunión.",
        )

    lineas = [
        f"[{segmento.get('hablante', 'hablante desconocido')}] {segmento.get('texto', '')}"
        for segmento in (fila.segmentos or [])
    ]
    return {
        "id": str(fila.id),
        "reunion_id": fila.reunion_id,
        "asunto": fila.asunto,
        "organizador": fila.organizador,
        "transcripcion_id": fila.transcripcion_id,
        "segmentos": fila.segmentos,
        "participantes": fila.participantes,
        "texto": "\n".join(lineas),
        "tiene_audio": bool(fila.audio_objeto),
        "audio_bytes": fila.audio_bytes,
        "huella": fila.huella,
        "capturado_en": fila.capturado_en.isoformat() if fila.capturado_en else None,
    }


# ─────────────────────────── salud ───────────────────────────


@router.get(
    "/salud/integracion",
    summary="Estado de la integración con Microsoft 365",
)
async def salud_integracion(actor: DependenciaActor) -> dict[str, Any]:
    """Dice si la captura está configurada y qué falta, si falta algo.

    Es la ruta que hay que mirar cuando algo no funciona: si el
    tenant tiene las transcripciones desactivadas, o si falta la
    política de acceso de aplicación, aquí se ve.
    """
    from app.core.config import settings

    _exigir_administrador(actor)
    faltan = []
    if not settings.graph_tenant_id:
        faltan.append("GRAPH_TENANT_ID")
    if not settings.graph_client_id:
        faltan.append("GRAPH_CLIENT_ID")
    if not settings.graph_certificado:
        faltan.append("GRAPH_CERTIFICADO")
    if not settings.graph_clave_privada:
        faltan.append("GRAPH_CLAVE_PRIVADA")
    if not settings.teams_organizador:
        faltan.append("TEAMS_ORGANIZADOR")

    return {
        "configurada": not faltan,
        "faltan": faltan,
        "organizador": settings.teams_organizador,
        "ventana_horas": settings.teams_captura_ventana_horas,
        "con_audio": settings.teams_captura_con_audio,
        "cola": "integraciones",
    }
