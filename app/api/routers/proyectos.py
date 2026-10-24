"""Proyectos de Projecta PMO Intelligence (HU_03).

CRUD básico de proyectos con las reglas que fija el TO-BE:

* El **código es único dentro de la organización**, no globalmente. Dos
  organizaciones distintas pueden tener un proyecto con el mismo código.
  Un duplicado devuelve 409.
* El **borrado es lógico**: el proyecto se marca con ``eliminado_en`` y
  desaparece de los listados, pero se conserva porque su historial —y su
  trazabilidad— forman parte de la evidencia.
* Toda creación y todo borrado dejan un evento de auditoría, porque un
  proyecto que aparece sin rastro no es explicable.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DependenciaActor, DependenciaSesion
from app.core import auditoria, rbac
from app.core.db.models import MembresiaProyecto, Proyecto, Usuario

LOG = logging.getLogger("projecta.proyectos")

router = APIRouter(prefix="/proyectos", tags=["proyectos"])

#: Estados en los que puede estar un proyecto. El semáforo del TO-BE se
#: calcula a partir de ellos, no se almacena.
ESTADOS_PROYECTO: tuple[str, ...] = (
    "planificacion",
    "en_curso",
    "en_riesgo",
    "pausado",
    "cerrado",
)


# ─────────────────────────── esquemas ───────────────────────────


class MiembroProyecto(BaseModel):
    """Miembro inicial de un proyecto."""

    usuario_id: str
    rol: str = "colaborador"


class ProyectoCrear(BaseModel):
    """Datos de alta de un proyecto."""

    nombre: str = Field(min_length=3, max_length=200)
    codigo: str = Field(min_length=2, max_length=64)
    descripcion: str | None = Field(default=None, max_length=4000)
    estado: str = Field(default="planificacion", max_length=32)
    plantilla: str | None = Field(default=None, max_length=64)
    #: Miembros iniciales con su rol dentro del proyecto.
    miembros: list[MiembroProyecto] = Field(default_factory=list)


class ProyectoResumen(BaseModel):
    """Vista de un proyecto en los listados."""

    id: str
    nombre: str
    codigo: str
    estado: str
    organizacion_id: str
    creado_en: datetime


class ProyectoDetalle(BaseModel):
    """Vista completa de un proyecto."""

    id: str
    nombre: str
    codigo: str
    descripcion: str | None
    estado: str
    plantilla: str | None
    organizacion_id: str
    creado_en: datetime
    actualizado_en: datetime
    #: Número de miembros del proyecto.
    miembros: int


# ─────────────────────────── helpers ───────────────────────────


async def _exigir_actor(actor: rbac.Actor, sesion: Any, operacion: str) -> None:
    """Comprueba que el actor puede crear proyectos en su organización.

    Un rechazo **no es silencio**: no modifica ningún dato, pero el
    intento se registra con la regla que lo rechazó y el motivo. Es lo que
    fija el TO-BE para HU_05, y sin este registro un intento denegado
    sería indistinguible de un intento que nunca ocurrió.
    """
    decision = rbac.evaluar(actor, rbac.configurar)
    if decision.permitido:
        return
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="TRANSICION_RECHAZADA",
            entidad=f"operacion:{operacion}",
            actor=actor.usuario_id,
            organizacion_id=actor.organizacion_id,
            usuario_id=actor.usuario_id,
            detalle=(
                f"Operación '{operacion}' rechazada por permisos. "
                f"Regla: {decision.regla}. Motivo: {decision.motivo}"
            ),
        ),
    )
    await sesion.commit()
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=decision.motivo,
        headers={"X-Regla": decision.regla},
    )


async def _obtener_proyecto(
    sesion: Any, proyecto_id: uuid.UUID, organizacion_id: str
) -> Proyecto:
    """Carga un proyecto no eliminado de una organización."""
    resultado = await sesion.execute(
        select(Proyecto).where(
            Proyecto.id == proyecto_id,
            Proyecto.organizacion_id == uuid.UUID(str(organizacion_id)),
            Proyecto.eliminado_en.is_(None),
        )
    )
    proyecto = resultado.scalar_one_or_none()
    if proyecto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="El proyecto no existe."
        )
    return proyecto


# ─────────────────────────── endpoints ───────────────────────────


@router.get("", summary="Lista los proyectos visibles para el actor")
async def listar(
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
    desplazamiento: Annotated[int, Query(ge=0)] = 0,
    estado: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Devuelve los proyectos de la organización, filtrados por ámbito.

    Un actor de ámbito de proyecto ve solo los proyectos de los que es
    miembro; un actor de ámbito de organización los ve todos.
    """
    condiciones = [
        Proyecto.organizacion_id == uuid.UUID(actor.organizacion_id),
        Proyecto.eliminado_en.is_(None),
    ]
    if estado:
        condiciones.append(Proyecto.estado == estado)
    if not actor.ambito_organizacion:
        if not actor.proyectos:
            return {"total": 0, "elementos": []}
        condiciones.append(
            Proyecto.id.in_([uuid.UUID(p) for p in actor.proyectos])
        )

    total = await sesion.execute(
        select(func.count()).select_from(Proyecto).where(*condiciones)
    )
    resultado = await sesion.execute(
        select(Proyecto)
        .where(*condiciones)
        .order_by(Proyecto.creado_en.desc())
        .limit(limite)
        .offset(desplazamiento)
    )
    proyectos = resultado.scalars().all()
    return {
        "total": total.scalar_one(),
        "elementos": [
            ProyectoResumen(
                id=str(p.id),
                nombre=p.nombre,
                codigo=p.codigo,
                estado=p.estado,
                organizacion_id=str(p.organizacion_id),
                creado_en=p.creado_en,
            )
            for p in proyectos
        ],
    }


@router.post("", status_code=status.HTTP_201_CREATED, summary="Crea un proyecto")
async def crear(
    cuerpo: ProyectoCrear,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Crea un proyecto y registra el evento de auditoría."""
    await _exigir_actor(actor, sesion, "crear_proyecto")
    if cuerpo.estado not in ESTADOS_PROYECTO:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Estado desconocido: '{cuerpo.estado}'. "
                f"Estados válidos: {', '.join(ESTADOS_PROYECTO)}."
            ),
        )

    codigo = cuerpo.codigo.strip().upper()
    proyecto = Proyecto(
        organizacion_id=uuid.UUID(actor.organizacion_id),
        nombre=cuerpo.nombre.strip(),
        codigo=codigo,
        descripcion=cuerpo.descripcion,
        estado=cuerpo.estado,
        plantilla=cuerpo.plantilla,
    )
    sesion.add(proyecto)
    try:
        await sesion.flush()
    except IntegrityError as exc:
        await sesion.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ya existe un proyecto con el código '{codigo}' en esta "
                f"organización."
            ),
        ) from exc

    await _anadir_miembros(sesion, proyecto, cuerpo.miembros, actor)
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="PROYECTO_CREADO",
            entidad=f"proyecto:{proyecto.id}",
            actor=actor.usuario_id,
            organizacion_id=actor.organizacion_id,
            proyecto_id=str(proyecto.id),
            detalle=f"Proyecto '{proyecto.codigo}' creado en estado {proyecto.estado}",
        ),
    )
    await sesion.commit()
    return {
        "id": str(proyecto.id),
        "nombre": proyecto.nombre,
        "codigo": proyecto.codigo,
        "estado": proyecto.estado,
        "organizacion_id": str(proyecto.organizacion_id),
    }


@router.get("/{proyecto_id}", summary="Detalle de un proyecto")
async def obtener(
    proyecto_id: uuid.UUID,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Devuelve el detalle de un proyecto."""
    proyecto = await _obtener_proyecto(sesion, proyecto_id, actor.organizacion_id)
    decision = rbac.evaluar(actor, rbac.ambito_organizacion, proyecto_id=str(proyecto.id))
    if not decision.permitido:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=decision.motivo,
            headers={"X-Regla": decision.regla},
        )
    total = await sesion.execute(
        select(func.count())
        .select_from(MembresiaProyecto)
        .where(MembresiaProyecto.proyecto_id == proyecto.id)
    )
    return {
        "id": str(proyecto.id),
        "nombre": proyecto.nombre,
        "codigo": proyecto.codigo,
        "descripcion": proyecto.descripcion,
        "estado": proyecto.estado,
        "plantilla": proyecto.plantilla,
        "organizacion_id": str(proyecto.organizacion_id),
        "creado_en": proyecto.creado_en,
        "actualizado_en": proyecto.actualizado_en,
        "miembros": total.scalar_one(),
    }


@router.delete("/{proyecto_id}", summary="Elimina un proyecto (borrado lógico)")
async def eliminar(
    proyecto_id: uuid.UUID,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Marca el proyecto como eliminado. No borra su historial."""
    await _exigir_actor(actor, sesion, "eliminar_proyecto")
    proyecto = await _obtener_proyecto(sesion, proyecto_id, actor.organizacion_id)
    proyecto.eliminado_en = datetime.now(timezone.utc)
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="PROYECTO_ESTADO_CAMBIADO",
            entidad=f"proyecto:{proyecto.id}",
            actor=actor.usuario_id,
            organizacion_id=actor.organizacion_id,
            proyecto_id=str(proyecto.id),
            detalle=f"Proyecto '{proyecto.codigo}' eliminado (borrado lógico)",
        ),
    )
    await sesion.commit()
    return {"id": str(proyecto.id), "eliminado": True}


# ─────────────────────────── apoyo ───────────────────────────


async def _anadir_miembros(
    sesion: Any, proyecto: Proyecto, miembros: list[MiembroProyecto], actor: rbac.Actor
) -> None:
    """Añade los miembros iniciales de un proyecto.

    Cada miembro recibe el rol que tenga **dentro** del proyecto, que puede
    no ser su rol en la organización.
    """
    from app.core.db.models import Rol

    for miembro in miembros:
        try:
            usuario_id = uuid.UUID(miembro.usuario_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Identificador de usuario no válido: '{miembro.usuario_id}'.",
            ) from exc
        resultado = await sesion.execute(select(Rol).where(Rol.nombre == miembro.rol))
        rol = resultado.scalar_one_or_none()
        if rol is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Rol desconocido: '{miembro.rol}'.",
            )
        existe = await sesion.execute(
            select(Usuario).where(Usuario.id == usuario_id)
        )
        if existe.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"El usuario '{miembro.usuario_id}' no existe.",
            )
        sesion.add(
            MembresiaProyecto(
                usuario_id=usuario_id,
                proyecto_id=proyecto.id,
                rol_id=rol.id,
            )
        )
    await sesion.flush()
