"""API del Edge Connector de Projecta PMO Intelligence (prueba de concepto).

Seis grupos de operaciones, que son los seis puntos donde el servidor
actúa como autoridad:

· **Dispositivos** — registro, consulta y revocación. El registro guarda
  la clave pública Ed25519 del dispositivo; la privada nunca sale del
  perímetro del banco.
· **Consentimientos** — el registro que la puerta consulta. Otorgar,
  consultar y revocar.
· **Eventos** — la ingesta. Aquí es donde se verifica la firma, se pasa
  la puerta del consentimiento y se minimizan los datos.
· **Salud** — el estado de cada dispositivo, con las caducidades
  anticipadas.

Los roles se resuelven con el RBAC del proyecto: el registro de
dispositivos y el de consentimientos exigen nivel de administrador, y
la ingesta no exige sesión de usuario, porque la firma del dispositivo
es su credencial.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DependenciaActor, DependenciaSesion
from app.core import auditoria, edge, rbac
from app.core.db.models import ConsentimientoEdge, DispositivoEdge, EventoEdge

LOG = logging.getLogger("projecta.edge.api")

router = APIRouter(prefix="/edge", tags=["edge connector"])


# ─────────────────────────── esquemas ───────────────────────────


class DispositivoCrear(BaseModel):
    """Alta de un dispositivo del Edge Connector."""

    nombre: str = Field(min_length=3, max_length=160)
    #: Clave pública Ed25519 en hexadecimal. La privada no se transmite.
    clave_publica: str = Field(min_length=64, max_length=64)
    sistemas: list[str] = Field(default_factory=list)
    modo: str = Field(default="READ_ONLY", max_length=32)
    credencial_expira_en: datetime | None = None


class ConsentimientoCrear(BaseModel):
    """Registro de consentimiento que sostiene la puerta."""

    recurso: str = Field(min_length=1, max_length=320)
    recurso_tipo: str = Field(min_length=2, max_length=64)
    #: La finalidad declarada. Sin finalidad, la puerta no abre.
    finalidad: str = Field(min_length=3, max_length=320)
    base_legal: str = Field(min_length=2, max_length=64)
    destinatarios: list[str] = Field(default_factory=list)
    proyecto_id: str | None = None
    otorgado_por: str = Field(min_length=3, max_length=320)
    expira_en: datetime | None = None


class EventoRecibir(BaseModel):
    """Evento firmado que envía el conector."""

    #: SHA-256 de la clave pública del dispositivo.
    huella_clave: str = Field(min_length=64, max_length=64)
    tipo: str = Field(min_length=3, max_length=64)
    #: Contenido del evento, firmado tal cual.
    contenido: dict[str, Any]
    #: Firma Ed25519 del contenido canónico, en hexadecimal.
    firma: str = Field(min_length=128, max_length=128)
    marca_tiempo: str = Field(min_length=10, max_length=40)
    #: El uso previsto. Se compara con la finalidad registrada.
    uso_previsto: str = Field(min_length=3, max_length=320)
    recurso: str = Field(min_length=1, max_length=320)
    recurso_tipo: str = Field(min_length=2, max_length=64)
    proyecto_id: str | None = None
    destinatarios_previstos: list[str] = Field(default_factory=list)
    correlation_id: str | None = None


class IngestaTeams(BaseModel):
    """Petición de una pasada de ingesta de Microsoft Teams."""

    #: Identificador del chat, si lo que se lee es un chat.
    chat_id: str | None = None
    #: Identificador del equipo, si lo que se lee es un canal.
    equipo_id: str | None = None
    #: Identificador del canal dentro del equipo.
    canal_id: str | None = None
    #: Proyecto al que se asocia lo leído.
    proyecto_id: str | None = None


# ─────────────────────────── dispositivos ───────────────────────────


def _exigir_administrador(actor: rbac.Actor) -> None:
    """Exige nivel de administrador de organización."""
    decision = rbac.evaluar(actor, rbac.administrar)
    if not decision.permitido:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=decision.motivo,
            headers={"X-Regla": decision.regla},
        )


@router.post("/dispositivos", status_code=status.HTTP_201_CREATED, summary="Registra un dispositivo")
async def registrar_dispositivo(
    cuerpo: DispositivoCrear,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Registra un dispositivo del Edge Connector.

    La clave privada **no** se transmite: el servidor solo guarda la
    pública, que es lo que permite verificar las firmas.
    """
    _exigir_administrador(actor)
    try:
        dispositivo = await edge.registrar_dispositivo(
            sesion,
            organizacion_id=actor.organizacion_id,
            nombre=cuerpo.nombre,
            clave_publica_hex=cuerpo.clave_publica,
            sistemas=cuerpo.sistemas,
            modo=cuerpo.modo,
            credencial_expira_en=cuerpo.credencial_expira_en,
        )
    except edge.ErrorDeEdge as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await sesion.commit()
    return {
        "id": str(dispositivo.id),
        "nombre": dispositivo.nombre,
        "huella_clave": dispositivo.huella_clave,
        "modo": dispositivo.modo,
        "sistemas": dispositivo.sistemas,
        "estado": dispositivo.estado,
    }


@router.get("/dispositivos", summary="Lista los dispositivos registrados")
async def listar_dispositivos(
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Devuelve los dispositivos de la organización del actor."""
    resultado = await sesion.execute(
        select(DispositivoEdge).where(
            DispositivoEdge.organizacion_id == uuid.UUID(actor.organizacion_id)
        )
    )
    dispositivos = resultado.scalars().all()
    return {
        "total": len(dispositivos),
        "elementos": [
            {
                "id": str(d.id),
                "nombre": d.nombre,
                "huella_clave": d.huella_clave,
                "modo": d.modo,
                "sistemas": d.sistemas,
                "estado": d.estado,
                "ultimo_latido_en": d.ultimo_latido_en,
                "credencial_expira_en": d.credencial_expira_en,
            }
            for d in dispositivos
        ],
    }


@router.delete("/dispositivos/{dispositivo_id}", summary="Revoca un dispositivo")
async def revocar_dispositivo(
    dispositivo_id: uuid.UUID,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    motivo: Annotated[str, Query(min_length=3, max_length=320)],
) -> dict[str, Any]:
    """Revoca un dispositivo. A partir de aquí, no puede enviar eventos."""
    _exigir_administrador(actor)
    resultado = await sesion.execute(
        select(DispositivoEdge).where(
            DispositivoEdge.id == dispositivo_id,
            DispositivoEdge.organizacion_id == uuid.UUID(actor.organizacion_id),
        )
    )
    dispositivo = resultado.scalar_one_or_none()
    if dispositivo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="El dispositivo no existe."
        )
    await edge.revoocar_dispositivo(sesion, dispositivo, motivo=motivo)
    await sesion.commit()
    return {"id": str(dispositivo.id), "estado": dispositivo.estado}


@router.get("/dispositivos/{dispositivo_id}/salud", summary="Estado de salud del dispositivo")
async def salud_dispositivo(
    dispositivo_id: uuid.UUID,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Estado de salud del dispositivo, con las caducidades anticipadas."""
    resultado = await sesion.execute(
        select(DispositivoEdge).where(
            DispositivoEdge.id == dispositivo_id,
            DispositivoEdge.organizacion_id == uuid.UUID(actor.organizacion_id),
        )
    )
    dispositivo = resultado.scalar_one_or_none()
    if dispositivo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="El dispositivo no existe."
        )
    return edge.estado_salud(dispositivo)


# ─────────────────────────── consentimientos ───────────────────────────


@router.post(
    "/consentimientos",
    status_code=status.HTTP_201_CREATED,
    summary="Registra un consentimiento",
)
async def otorgar_consentimiento(
    cuerpo: ConsentimientoCrear,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Registra un consentimiento para un recurso y una finalidad.

    Es el registro que la puerta consulta. Sin él, la puerta falla
    cerrada.
    """
    _exigir_administrador(actor)
    consentimiento = ConsentimientoEdge(
        organizacion_id=uuid.UUID(actor.organizacion_id),
        recurso=cuerpo.recurso,
        recurso_tipo=cuerpo.recurso_tipo,
        finalidad=cuerpo.finalidad,
        base_legal=cuerpo.base_legal,
        destinatarios=list(cuerpo.destinatarios),
        proyecto_id=uuid.UUID(cuerpo.proyecto_id) if cuerpo.proyecto_id else None,
        otorgado_por=cuerpo.otorgado_por,
        expira_en=cuerpo.expira_en,
    )
    sesion.add(consentimiento)
    await sesion.flush()
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="EDGE_CONSENTIMIENTO_OTORGADO",
            entidad=f"consentimiento:{consentimiento.id}",
            actor=actor.usuario_id,
            organizacion_id=actor.organizacion_id,
            proyecto_id=cuerpo.proyecto_id,
            detalle=(
                f"Consentimiento '{cuerpo.recurso_tipo}:{cuerpo.recurso}' con "
                f"finalidad '{cuerpo.finalidad}' y base legal '{cuerpo.base_legal}'."
            ),
        ),
    )
    await sesion.commit()
    return {
        "id": str(consentimiento.id),
        "recurso": consentimiento.recurso,
        "finalidad": consentimiento.finalidad,
        "otorgado_en": consentimiento.otorgado_en,
    }


@router.get("/consentimientos", summary="Lista los consentimientos registrados")
async def listar_consentimientos(
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Devuelve los consentimientos de la organización del actor."""
    resultado = await sesion.execute(
        select(ConsentimientoEdge).where(
            ConsentimientoEdge.organizacion_id == uuid.UUID(actor.organizacion_id)
        )
    )
    registros = resultado.scalars().all()
    return {
        "total": len(registros),
        "elementos": [
            {
                "id": str(c.id),
                "recurso": c.recurso,
                "recurso_tipo": c.recurso_tipo,
                "finalidad": c.finalidad,
                "base_legal": c.base_legal,
                "destinatarios": c.destinatarios,
                "proyecto_id": str(c.proyecto_id) if c.proyecto_id else None,
                "otorgado_por": c.otorgado_por,
                "otorgado_en": c.otorgado_en,
                "revocado_en": c.revocado_en,
                "expira_en": c.expira_en,
            }
            for c in registros
        ],
    }


@router.delete("/consentimientos/{consentimiento_id}", summary="Revoca un consentimiento")
async def revocar_consentimiento(
    consentimiento_id: uuid.UUID,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Revoca un consentimiento. La puerta se cierra de inmediato."""
    _exigir_administrador(actor)
    resultado = await sesion.execute(
        select(ConsentimientoEdge).where(
            ConsentimientoEdge.id == consentimiento_id,
            ConsentimientoEdge.organizacion_id == uuid.UUID(actor.organizacion_id),
        )
    )
    consentimiento = resultado.scalar_one_or_none()
    if consentimiento is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="El consentimiento no existe."
        )
    from datetime import datetime as _dt, timezone as _tz

    consentimiento.revocado_en = _dt.now(_tz.utc)
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="EDGE_CONSENTIMIENTO_REVOCADO",
            entidad=f"consentimiento:{consentimiento.id}",
            actor=actor.usuario_id,
            organizacion_id=actor.organizacion_id,
            detalle=f"Consentimiento '{consentimiento.recurso}' revocado.",
        ),
    )
    await sesion.commit()
    return {"id": str(consentimiento.id), "revocado": True}


# ─────────────────────────── eventos ───────────────────────────


@router.post(
    "/eventos",
    summary="Ingesta un evento firmado del conector",
    responses={
        202: {"description": "Evento aceptado y persistido."},
        403: {"description": "Evento rechazado; el motivo viaja en el cuerpo."},
    },
)
async def recibir_evento(
    cuerpo: EventoRecibir,
    sesion: DependenciaSesion,
    respuesta: Response,
) -> dict[str, Any]:
    """Recibe un evento firmado del Edge Connector.

    No exige sesión de usuario: la firma del dispositivo es su credencial.
    El dispositivo tiene que estar registrado y activo.

    La respuesta dice **qué pasó**, no solo el código HTTP: si el evento
    se rechazó, el motivo viaja en el cuerpo, para que el conector
    pueda distinguir «no autorizado» de «fallo de transporte».

    El **código HTTP es el canal de verdad**: 202 si el evento se aceptó
    y 403 si se rechazó. Antes el cuerpo anunciaba un campo ``codigo``
    con esos mismos valores mientras la respuesta HTTP era siempre 200:
    dos fuentes de verdad que pueden divergir, y el conector que leyera
    el código equivocado creería que un rechazo fue una aceptación.
    """
    resultado = await edge.ingerir_evento(
        sesion,
        organizacion_id=await _organizacion_del_dispositivo(
            sesion, cuerpo.huella_clave
        ),
        huella_clave=cuerpo.huella_clave,
        tipo=cuerpo.tipo,
        contenido=cuerpo.contenido,
        firma=cuerpo.firma,
        marca_tiempo=cuerpo.marca_tiempo,
        uso_previsto=cuerpo.uso_previsto,
        recurso=cuerpo.recurso,
        recurso_tipo=cuerpo.recurso_tipo,
        proyecto_id=cuerpo.proyecto_id,
        destinatarios_previstos=cuerpo.destinatarios_previstos,
        correlation_id=cuerpo.correlation_id,
    )
    await sesion.commit()
    respuesta.status_code = (
        status.HTTP_202_ACCEPTED
        if resultado.aceptado
        else status.HTTP_403_FORBIDDEN
    )
    return {
        "decision": resultado.decision,
        "motivo": resultado.motivo,
        "evento_id": resultado.evento_id,
        "huella": resultado.huella,
        "duplicado": resultado.duplicado,
    }


async def _organizacion_del_dispositivo(sesion: Any, huella_clave: str) -> str:
    """Resuelve la organización a la que pertenece un dispositivo."""
    resultado = await sesion.execute(
        select(DispositivoEdge).where(DispositivoEdge.huella_clave == huella_clave)
    )
    dispositivo = resultado.scalar_one_or_none()
    if dispositivo is None:
        # Se usa una organización inexistente: la ingesta se rechazará y
        # el rechazo no podrá registrarse contra ninguna organización real.
        return "00000000-0000-0000-0000-000000000000"
    return str(dispositivo.organizacion_id)


@router.get("/eventos", summary="Lista los eventos ingeridos")
async def listar_eventos(
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Devuelve los eventos recibidos, del más reciente al más antiguo."""
    resultado = await sesion.execute(
        select(EventoEdge)
        .where(EventoEdge.organizacion_id == uuid.UUID(actor.organizacion_id))
        .order_by(EventoEdge.recibido_en.desc())
        .limit(limite)
    )
    eventos = resultado.scalars().all()
    return {
        "total": len(eventos),
        "elementos": [
            {
                "id": str(e.id),
                "tipo": e.tipo,
                "decision": e.decision,
                "motivo": e.motivo,
                "huella": e.huella,
                "payload": e.payload,
                "proyecto_id": str(e.proyecto_id) if e.proyecto_id else None,
                "recibido_en": e.recibido_en,
            }
            for e in eventos
        ],
    }


# ─────────────────────────── puerta (diagnóstico) ───────────────────────────


@router.get("/puerta/{recurso_tipo}/{recurso}", summary="Consulta la puerta del consentimiento")
async def consultar_puerta(
    recurso_tipo: str,
    recurso: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    uso_previsto: Annotated[str | None, Query(max_length=320)] = None,
) -> dict[str, Any]:
    """Consulta la puerta sin enviar un evento.

    Es la operación que responde a la pregunta operativa «¿puedo leer
    este recurso?». Devuelve la decisión, el motivo y, si hay
    consentimiento, su finalidad.
    """
    consentimiento = await edge.consultar_consentimiento(
        sesion, actor.organizacion_id, recurso, recurso_tipo
    )
    if uso_previsto is None:
        return {
            "recurso": recurso,
            "recurso_tipo": recurso_tipo,
            "consentimiento_registrado": consentimiento is not None,
            "finalidad": consentimiento.finalidad if consentimiento else None,
            "base_legal": consentimiento.base_legal if consentimiento else None,
            "nota": (
                "Sin uso previsto no se puede decidir: la puerta compara el uso "
                "previsto con la finalidad registrada."
            ),
        }
    decision = edge.evaluar_consentimiento(
        consentimiento, uso_previsto=uso_previsto
    )
    return {
        "recurso": recurso,
        "recurso_tipo": recurso_tipo,
        "permitido": decision.permitido,
        "motivo": decision.motivo,
        "finalidad": decision.finalidad,
        "consentimiento_id": decision.consentimiento_id,
    }


# ─────────────────────────── ingesta de origen ───────────────────────────


@router.post(
    "/integraciones/teams/ingesta",
    summary="Encola la ingesta de Microsoft Teams (Variante A)",
)
async def encolar_ingesta_teams(
    cuerpo: IngestaTeams,
    actor: DependenciaActor,
) -> dict[str, Any]:
    """Encola una pasada de la ingesta de Teams.

    Esta ruta es la que sustituye al **agente desplegado** de la
    Variante B. En la Variante A no hay ningún proceso connector fuera
    de la aplicación: la ingesta es una tarea del worker, en la cola
    `integraciones`, y la dispara quien tiene autoridad para hacerlo.

    El ritmo lo decide el banco. Aquí se dispara a demanda, que es lo
    que hace falta para un piloto; para producción, lo habitual es
    programarla con el `beat` de Celery del propio banco.
    """
    _exigir_administrador(actor)
    from app.workers.tasks.ingesta_teams import TAREA_INGESTA_TEAMS, ingesta_teams

    tarea = ingesta_teams.apply_async(
        kwargs={
            "organizacion_id": actor.organizacion_id,
            "chat_id": cuerpo.chat_id,
            "canal_id": cuerpo.canal_id,
            "equipo_id": cuerpo.equipo_id,
            "proyecto_id": cuerpo.proyecto_id,
        },
        queue="integraciones",
    )
    return {
        "tarea": TAREA_INGESTA_TEAMS,
        "id": tarea.id,
        "cola": "integraciones",
        "organizacion_id": actor.organizacion_id,
    }
