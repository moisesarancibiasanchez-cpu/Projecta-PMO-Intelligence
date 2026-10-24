"""Procesamiento de las capturas de Projecta Capture.

La tarea que hace el trabajo pesado: transcribe el audio de una sesión
y extrae los elementos PMO. Vive en la cola ``integraciones``, que el
contrato del proyecto ya define y ya enruta.

Por qué una tarea y no una petición HTTP
-----------------------------------------

Una transcripción tarda. Una petición HTTP que espera a que termine se
corta, y el cliente se queda sin saber qué pasó. Aquí el trabajo se
encola, se consulta por su identificador, y el estado se guarda en la
base de datos: el cliente pregunta cada cuánto y sabe dónde está.

La idempotencia la da el trabajo, que es único por
``(artefacto_id, tipo)``. Si se encola dos veces, la segunda no crea
otro: devuelve el que ya hay.

Y hay una cosa que este módulo hace **explícitamente**: el audio no
sale del perímetro. Si el proveedor de transcripción es el simulado, no
sale nada. Si es un motor real, sale **solo** porque alguien lo
configuró, y el trabajo registra qué proveedor se usó, para poder
auditarlo.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Task

from app.core.asyncio_bridge import ejecutar
from app.core.celery_app import celery_app
from app.core.captura.estados import ErrorDeTransicion
from app.core.captura.servicio import ErrorDePeticion
from app.core.db.session import AsyncSessionLocal

LOG = logging.getLogger("projecta.captura.procesa")

#: Nombre exacto de la tarea.
TAREA_PROCESA_CAPTURA = "projecta.captura.procesa"


class ErrorDeProcesamiento(Exception):
    """El procesamiento no se pudo completar."""


def _configuracion() -> Any:
    """Devuelve la configuración de la aplicación."""
    from app.core.config import settings

    return settings


async def _procesar_una_vez(trabajo_id: str) -> dict[str, Any]:
    """Procesa un trabajo. Una pasada.

    El trabajo puede ser de dos tipos: ``transcripcion`` o ``analisis``.
    Los dos se ejecutan contra el mismo artefacto, y el segundo usa la
    transcripción que produjo el primero.
    """
    from sqlalchemy import select

    from app.core.captura import servicio
    from app.core.captura.extraccion import SegmentoDeEntrada, extraer
    from app.core.captura.proveedores import construir_proveedor
    from app.core.db.models_captura import (
        ArtefactoDeAudio,
        ElementoExtraido,
        EvidenciaDeItem,
        SegmentoDeTranscripcion,
        TrabajoDeProcesamiento,
        Transcripcion,
    )

    async with AsyncSessionLocal() as sesion:
        resultado = await sesion.execute(
            select(TrabajoDeProcesamiento).where(
                TrabajoDeProcesamiento.id == trabajo_id
            )
        )
        trabajo = resultado.scalars().first()
        if trabajo is None:
            raise ErrorDeProcesamiento(f"No existe el trabajo {trabajo_id}.")

        if trabajo.estado in ("TRANSCRIBED", "ANALYZING", "REVIEW_REQUIRED", "APPROVED"):
            # Ya está hecho. Reencolar no vuelve a hacerlo.
            return {
                "trabajo_id": str(trabajo.id),
                "estado": trabajo.estado,
                "ya_procesado": True,
            }

        resultado = await sesion.execute(
            select(ArtefactoDeAudio).where(ArtefactoDeAudio.id == trabajo.artefacto_id)
        )
        artefacto = resultado.scalars().first()
        if artefacto is None:
            raise ErrorDeProcesamiento(
                f"No existe el artefacto de audio {trabajo.artefacto_id}."
            )

        if trabajo.tipo == "transcripcion":
            return await _transcribir(sesion, trabajo, artefacto)
        return await _analizar(sesion, trabajo)


async def _transcribir(
    sesion: Any, trabajo: Any, artefacto: Any
) -> dict[str, Any]:
    """Transcribe el artefacto y guarda la transcripción con sus segmentos."""
    from app.core.captura import servicio
    from app.core.captura.proveedores import construir_proveedor, ErrorDeTranscripcion
    from app.core.db.models_captura import SegmentoDeTranscripcion, Transcripcion

    config = _configuracion()
    proveedor = construir_proveedor(
        url=config.captura_motor_transcripcion_url,
        token=config.captura_motor_transcripcion_token,
    )

    await servicio.transicionar(
        sesion, trabajo_id=trabajo.id, destino="TRANSCRIBING", progreso=10
    )
    await sesion.commit()

    audio = b""
    if artefacto.clave_objeto:
        try:
            from app.core.storage import almacenamiento

            almacen = almacenamiento()
            if almacen.disponible():
                audio = almacen.leer(artefacto.clave_objeto)
        except Exception as error:  # noqa: BLE001 - sin audio no hay transcripción
            LOG.error("no se pudo leer el audio del artefacto %s: %s", artefacto.id, error)

    if not audio:
        await servicio.transicionar(
            sesion,
            trabajo_id=trabajo.id,
            destino="FAILED",
            error="No hay audio disponible para transcribir.",
        )
        await sesion.commit()
        return {"trabajo_id": str(trabajo.id), "estado": "FAILED", "error": "sin audio"}

    try:
        resultado = proveedor.transcribe(audio, language="es")
    except ErrorDeTranscripcion as error:
        await servicio.transicionar(
            sesion, trabajo_id=trabajo.id, destino="FAILED", error=str(error)
        )
        await sesion.commit()
        return {"trabajo_id": str(trabajo.id), "estado": "FAILED", "error": str(error)}

    # Una transcripción por artefacto. Si ya había una, se sustituye:
    # el historial de correcciones vive en `acciones_de_revision`.
    from sqlalchemy import delete, select

    existente = await sesion.execute(
        select(Transcripcion).where(Transcripcion.artefacto_id == artefacto.id)
    )
    previa = existente.scalars().first()
    if previa is not None:
        await sesion.execute(
            delete(SegmentoDeTranscripcion).where(
                SegmentoDeTranscripcion.transcripcion_id == previa.id
            )
        )
        await sesion.delete(previa)
        await sesion.flush()

    transcripcion = Transcripcion(
        sesion_id=artefacto.sesion_id,
        artefacto_id=artefacto.id,
        idioma=resultado.idioma,
        texto=resultado.texto,
        confianza=resultado.confianza,
        correlation_id=trabajo.correlation_id,
    )
    sesion.add(transcripcion)
    await sesion.flush()

    for indice, segmento in enumerate(resultado.segmentos):
        sesion.add(
            SegmentoDeTranscripcion(
                transcripcion_id=transcripcion.id,
                indice=indice,
                hablante=segmento.hablante,
                inicio_ms=segmento.inicio_ms,
                fin_ms=segmento.fin_ms,
                texto=segmento.texto,
                confianza=segmento.confianza,
            )
        )
    await sesion.flush()

    await servicio.transicionar(
        sesion, trabajo_id=trabajo.id, destino="TRANSCRIBED", progreso=100
    )
    await sesion.commit()
    return {
        "trabajo_id": str(trabajo.id),
        "estado": "TRANSCRIBED",
        "segmentos": len(resultado.segmentos),
        "proveedor": resultado.proveedor,
    }


async def _analizar(sesion: Any, trabajo: Any) -> dict[str, Any]:
    """Extrae los elementos PMO de la transcripción y los guarda."""
    from sqlalchemy import select

    from app.core.captura import servicio
    from app.core.captura.extraccion import SegmentoDeEntrada, extraer
    from app.core.db.models_captura import (
        ElementoExtraido,
        EvidenciaDeItem,
        SegmentoDeTranscripcion,
        Transcripcion,
    )

    await servicio.transicionar(
        sesion, trabajo_id=trabajo.id, destino="ANALYZING", progreso=10
    )
    await sesion.commit()

    resultado = await sesion.execute(
        select(Transcripcion).where(Transcripcion.sesion_id == trabajo.sesion_id)
    )
    transcripcion = resultado.scalars().first()
    if transcripcion is None:
        await servicio.transicionar(
            sesion,
            trabajo_id=trabajo.id,
            destino="FAILED",
            error="No hay transcripción que analizar.",
        )
        await sesion.commit()
        return {
            "trabajo_id": str(trabajo.id),
            "estado": "FAILED",
            "error": "sin transcripción",
        }

    resultado = await sesion.execute(
        select(SegmentoDeTranscripcion)
        .where(SegmentoDeTranscripcion.transcripcion_id == transcripcion.id)
        .order_by(SegmentoDeTranscripcion.indice)
    )
    segmentos = [
        SegmentoDeEntrada(
            indice=s.indice,
            hablante=s.hablante,
            inicio_ms=s.inicio_ms,
            fin_ms=s.fin_ms,
            texto=s.texto,
        )
        for s in resultado.scalars().all()
    ]

    extraccion = extraer(segmentos)

    for elemento in extraccion.elementos:
        fila = ElementoExtraido(
            sesion_id=trabajo.sesion_id,
            clave=elemento.clave,
            tipo=elemento.tipo,
            descripcion=elemento.descripcion,
            hablante=elemento.hablante,
            fecha_propuesta=elemento.fecha_propuesta,
            marca_ms=elemento.marca_ms,
            segmentos_evidencia=list(elemento.segmentos_evidencia),
            requiere_revision=elemento.requiere_revision,
            motivo_revision=elemento.motivo_revision,
            estado_revision="pendiente",
            correlation_id=trabajo.correlation_id,
        )
        sesion.add(fila)
        await sesion.flush()
        for indice in elemento.segmentos_evidencia:
            sesion.add(
                EvidenciaDeItem(
                    elemento_id=fila.id,
                    segmento_indice=indice,
                    texto=elemento.texto_evidencia,
                    marca_ms=elemento.marca_ms,
                )
            )
    await sesion.flush()

    destino = "REVIEW_REQUIRED" if extraccion.requiere_revision else "APPROVED"
    await servicio.transicionar(
        sesion, trabajo_id=trabajo.id, destino=destino, progreso=100
    )
    await sesion.commit()
    return {
        "trabajo_id": str(trabajo.id),
        "estado": destino,
        "elementos": len(extraccion.elementos),
        "requiere_revision": extraccion.requiere_revision,
    }


@celery_app.task(
    bind=True,
    name=TAREA_PROCESA_CAPTURA,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def procesa_captura(self: Task, trabajo_id: str) -> dict[str, Any]:
    """Tarea de Celery que transcribe y analiza una captura.

    Los errores de configuración no se reintentan: reintentar no
    arregla un certificado que falta, y solo retrasa el aviso.
    """
    try:
        return ejecutar(_procesar_una_vez(trabajo_id))
    except ErrorDePeticion as error:
        LOG.error("Procesamiento de captura no ejecutado: %s", error)
        return {"trabajo_id": trabajo_id, "estado": "FAILED", "error": str(error)}
    except Exception as error:  # noqa: BLE001
        LOG.warning("Procesamiento de captura fallido: %s", error)
        raise self.retry(exc=error)
