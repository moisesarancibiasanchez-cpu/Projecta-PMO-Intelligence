"""Adjuntos y evidencia de Projecta PMO Intelligence (HU_08).

El fichero vive en el almacenamiento de objetos; en la base de datos solo
queda su metadato. Esa separación es deliberada: la base de datos guarda
lo que se **consulta y audita**, el bucket guarda lo que se **almacena**.

Reglas que se aplican:

* **Idempotencia por contenido.** La clave del objeto deriva del SHA-256.
  Subir dos veces el mismo fichero no crea dos objetos: la segunda fila
  apunta al mismo checksum y se marca ``ya_existia``.
* **La indisponibilidad no rompe el listado.** Si el bucket no responde,
  el adjunto se marca ``disponible: false`` y se sigue devolviendo. Perder
  el inventario por una caída del almacenamiento sería peor que perder la
  descarga.
* **El acceso exige permiso sobre el proyecto.** Un adjunto de un proyecto
  ajeno no se descarga, aunque se conozca su identificador.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select

from app.api.deps import DependenciaActor, DependenciaSesion
from app.core import auditoria, rbac
from app.core.db.models import Adjunto
from app.core.storage import (
    ErrorDeAlmacenamiento,
    ObjetoNoDisponible,
    almacenamiento,
)

LOG = logging.getLogger("projecta.adjuntos")

router = APIRouter(prefix="/adjuntos", tags=["adjuntos"])

#: Tipos MIME admitidos. Se listan de forma explícita: aceptar cualquier
#: tipo es aceptar la carga de ejecutables.
TIPOS_ADMITIDOS: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "text/csv",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "image/tiff",
        "application/xml",
        "text/xml",
        "application/json",
    }
)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Sube un fichero")
async def subir(
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    fichero: Annotated[UploadFile, File(description="Fichero de evidencia")],
    proyecto_id: Annotated[str, Query(description="Proyecto al que pertenece la evidencia")],
) -> dict[str, Any]:
    """Sube un fichero al almacenamiento de objetos y guarda su metadato.

    El proyecto es **obligatorio**, y no por comodidad: la política de
    RBAC `cargar_evidencia` tiene ámbito de proyecto, así que sin él la
    operación no se puede autorizar. Declarar el parámetro como
    opcional y luego rechazarlo siempre por permisos sería una
    contradicción — y una evidencia sin proyecto es un hueco de
    gobernanza en un entorno regulado.
    """
    decision = rbac.evaluar(
        actor, rbac.cargar_evidencia, proyecto_id=proyecto_id
    )
    if not decision.permitido:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=decision.motivo,
            headers={"X-Regla": decision.regla},
        )

    content_type = (fichero.content_type or "application/octet-stream").split(";")[0].strip()
    if content_type not in TIPOS_ADMITIDOS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Tipo de fichero no admitido: '{content_type}'. "
                f"Admitidos: {', '.join(sorted(TIPOS_ADMITIDOS))}."
            ),
        )

    datos = await fichero.read()
    if not datos:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El fichero está vacío.",
        )

    nombre = fichero.filename or "sin-nombre"
    cliente = almacenamiento()
    try:
        cliente.asegurar_bucket()
        resultado = cliente.subir(
            datos,
            nombre=nombre,
            content_type=content_type,
            extension=nombre.rsplit(".", 1)[-1] if "." in nombre else "",
        )
    except ErrorDeAlmacenamiento as exc:
        LOG.warning("no se pudo guardar el adjunto: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El almacenamiento de objetos no está disponible en este momento.",
        ) from exc

    adjunto = Adjunto(
        organizacion_id=uuid.UUID(actor.organizacion_id),
        proyecto_id=uuid.UUID(proyecto_id) if proyecto_id else None,
        nombre=nombre,
        content_type=content_type,
        tamano_bytes=len(datos),
        sha256=resultado.sha256,
        clave_objeto=resultado.clave_objeto,
        version=resultado.version,
        disponible=True,
        subido_por_id=uuid.UUID(actor.usuario_id),
    )
    sesion.add(adjunto)
    await sesion.flush()

    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="ADJUNTO_CARGADO",
            entidad=f"adjunto:{adjunto.id}",
            actor=actor.usuario_id,
            organizacion_id=actor.organizacion_id,
            proyecto_id=proyecto_id,
            detalle=(
                f"'{nombre}' cargado ({len(datos)} bytes, sha256 "
                f"{resultado.sha256[:12]}…)"
            ),
        ),
    )
    await sesion.commit()
    return {
        "id": str(adjunto.id),
        "nombre": nombre,
        "sha256": resultado.sha256,
        "tamano_bytes": len(datos),
        "clave_objeto": resultado.clave_objeto,
        "disponible": True,
        "ya_existia": resultado.ya_existia,
    }


@router.get("", summary="Lista los adjuntos")
async def listar(
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    proyecto_id: Annotated[str | None, None] = None,
) -> dict[str, Any]:
    """Devuelve los adjuntos, marcando los que ya no están disponibles."""
    condiciones = [Adjunto.organizacion_id == uuid.UUID(actor.organizacion_id)]
    if proyecto_id:
        condiciones.append(Adjunto.proyecto_id == uuid.UUID(proyecto_id))
    if not actor.ambito_organizacion and actor.proyectos:
        condiciones.append(
            Adjunto.proyecto_id.in_([uuid.UUID(p) for p in actor.proyectos])
        )

    resultado = await sesion.execute(
        select(Adjunto)
        .where(*condiciones)
        .order_by(Adjunto.creado_en.desc())
        .limit(200)
    )
    adjuntos = resultado.scalars().all()

    cliente = almacenamiento()
    almacenamiento_vivo = cliente.disponible()
    elementos: list[dict[str, Any]] = []
    for adjunto in adjuntos:
        disponible = adjunto.disponible
        if disponible and almacenamiento_vivo and not cliente.existe(adjunto.clave_objeto):
            disponible = False
            adjunto.disponible = False
            await auditoria.registrar(
                sesion,
                auditoria.Evento(
                    tipo="ADJUNTO_NO_DISPONIBLE",
                    entidad=f"adjunto:{adjunto.id}",
                    actor=actor.usuario_id,
                    organizacion_id=actor.organizacion_id,
                    proyecto_id=str(adjunto.proyecto_id) if adjunto.proyecto_id else None,
                    detalle=f"El objeto de '{adjunto.nombre}' ya no está en el bucket",
                ),
            )
        elementos.append(
            {
                "id": str(adjunto.id),
                "nombre": adjunto.nombre,
                "content_type": adjunto.content_type,
                "tamano_bytes": adjunto.tamano_bytes,
                "sha256": adjunto.sha256,
                "proyecto_id": str(adjunto.proyecto_id) if adjunto.proyecto_id else None,
                "disponible": disponible,
                "creado_en": adjunto.creado_en,
            }
        )
    await sesion.commit()
    return {"total": len(elementos), "elementos": elementos}


@router.get("/{adjunto_id}/contenido", summary="Descarga un fichero")
async def descargar(
    adjunto_id: uuid.UUID,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> Response:
    """Devuelve el contenido del fichero.

    Si el objeto ya no está en el bucket, se responde 410 y no 404: el
    adjunto **existe**, lo que no existe es su contenido. La distinción
    importa en una auditoría.
    """
    resultado = await sesion.execute(
        select(Adjunto).where(
            Adjunto.id == adjunto_id,
            Adjunto.organizacion_id == uuid.UUID(actor.organizacion_id),
        )
    )
    adjunto = resultado.scalar_one_or_none()
    if adjunto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="El adjunto no existe."
        )
    decision = rbac.evaluar(
        actor,
        rbac.ambito_organizacion,
        proyecto_id=str(adjunto.proyecto_id) if adjunto.proyecto_id else None,
    )
    if not decision.permitido:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=decision.motivo,
            headers={"X-Regla": decision.regla},
        )

    cliente = almacenamiento()
    try:
        datos = cliente.leer(adjunto.clave_objeto)
    except ObjetoNoDisponible as exc:
        adjunto.disponible = False
        await sesion.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "El adjunto existe pero su contenido ya no está en el "
                "almacenamiento de objetos."
            ),
        ) from exc
    except ErrorDeAlmacenamiento as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El almacenamiento de objetos no está disponible en este momento.",
        ) from exc

    return Response(
        content=datos,
        media_type=adjunto.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{adjunto.nombre}"',
            "X-SHA256": adjunto.sha256,
        },
    )
