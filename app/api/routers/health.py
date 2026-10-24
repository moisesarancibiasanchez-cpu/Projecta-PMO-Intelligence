"""Routers de salud de Projecta PMO Intelligence.

* ``GET /health``  -> liveness puro. No toca ninguna dependencia.
* ``GET /ready``   -> readiness. Verifica base de datos y Redis (críticos) y el
  almacenamiento (no crítico) con un timeout corto. Responde 503 si alguna
  dependencia crítica no responde.
* ``GET /salud``   -> detalle de salud en JSON.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import APP_VERSION, settings
from app.core.db.session import engine

logger = logging.getLogger("projecta.salud")

router = APIRouter(tags=["salud"])

#: Timeout corto para los chequeos de dependencias (segundos).
TIMEOUT_SEGUNDOS = 2.0

#: Dependencias críticas: si alguna falla, la instancia no está lista.
DEPENDENCIAS_CRITICAS = ("database", "redis")


class ChequeoDependencia(BaseModel):
    """Resultado de un chequeo individual de dependencia."""

    ok: bool
    latency_ms: float = 0.0
    error: str | None = Field(default=None, description="Motivo del fallo, si lo hubo.")


class ReporteReadiness(BaseModel):
    """Shape exacto de la respuesta de ``GET /ready``."""

    status: Literal["ok", "degraded"]
    checks: dict[str, ChequeoDependencia]


class ReporteSalud(BaseModel):
    """Detalle de salud de la aplicación (``GET /salud``)."""

    status: Literal["ok", "degraded"]
    checks: dict[str, ChequeoDependencia]
    aplicacion: str
    version: str
    entorno: str


def _milisegundos(inicio: float) -> float:
    """Transforma un instante inicial en milisegundos transcurridos."""
    return round((time.perf_counter() - inicio) * 1000, 2)


def _mensaje_error(error: Exception) -> str:
    """Convierte una excepción en un mensaje breve y legible."""
    texto = str(error).strip() or error.__class__.__name__
    return f"{error.__class__.__name__}: {texto}"


async def _chequeo_base_datos() -> ChequeoDependencia:
    """Comprueba que la base de datos responde a una consulta trivial."""
    inicio = time.perf_counter()
    try:
        async with engine.connect() as conexion:
            await conexion.execute(text("SELECT 1"))
    except Exception as error:  # noqa: BLE001 - el fallo se reporta, no se propaga
        logger.warning("Chequeo de base de datos fallido: %s", error)
        return ChequeoDependencia(ok=False, latency_ms=_milisegundos(inicio), error=_mensaje_error(error))
    return ChequeoDependencia(ok=True, latency_ms=_milisegundos(inicio))


async def _chequeo_redis() -> ChequeoDependencia:
    """Comprueba que Redis responde a un PING."""
    inicio = time.perf_counter()
    cliente: Redis | None = None
    try:
        cliente = Redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=TIMEOUT_SEGUNDOS,
            socket_timeout=TIMEOUT_SEGUNDOS,
        )
        await cliente.ping()
    except Exception as error:  # noqa: BLE001 - el fallo se reporta, no se propaga
        logger.warning("Chequeo de Redis fallido: %s", error)
        return ChequeoDependencia(ok=False, latency_ms=_milisegundos(inicio), error=_mensaje_error(error))
    finally:
        if cliente is not None:
            try:
                await cliente.aclose()
            except Exception:  # pragma: no cover - cierre best effort
                pass
    return ChequeoDependencia(ok=True, latency_ms=_milisegundos(inicio))


async def _chequeo_storage() -> ChequeoDependencia:
    """Comprueba que el almacenamiento de objetos responde.

    El almacenamiento es **no crítico**: cuando no forma parte del perfil de
    despliegue se informa ``ok: false`` con el motivo ``no disponible en este
    perfil`` sin degradar el estado global (ver el ejemplo del contrato).
    """
    inicio = time.perf_counter()
    endpoint = (settings.minio_endpoint or "").strip()
    if not endpoint:
        return ChequeoDependencia(ok=False, latency_ms=0.0, error="no disponible en este perfil")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
            await cliente.get(endpoint)
    except Exception as error:  # noqa: BLE001 - el fallo se reporta, no se propaga
        logger.info("Almacenamiento no disponible (%s): %s", endpoint, error)
        return ChequeoDependencia(
            ok=False, latency_ms=_milisegundos(inicio), error="no disponible en este perfil"
        )
    return ChequeoDependencia(ok=True, latency_ms=_milisegundos(inicio))


async def _chequeo_con_timeout(
    funcion: Any, nombre: str
) -> ChequeoDependencia:
    """Ejecuta un chequeo con timeout corto; nunca propaga excepciones."""
    inicio = time.perf_counter()
    try:
        return await asyncio.wait_for(funcion(), timeout=TIMEOUT_SEGUNDOS)
    except asyncio.TimeoutError:
        logger.warning("Chequeo de %s agotó el tiempo de espera.", nombre)
        return ChequeoDependencia(
            ok=False,
            latency_ms=_milisegundos(inicio),
            error=f"tiempo de espera agotado ({TIMEOUT_SEGUNDOS:g} s)",
        )
    except Exception as error:  # noqa: BLE001 - el fallo se reporta, no se propaga
        logger.warning("Chequeo de %s fallido: %s", nombre, error)
        return ChequeoDependencia(
            ok=False, latency_ms=_milisegundos(inicio), error=_mensaje_error(error)
        )


async def _ejecutar_chequeos() -> dict[str, ChequeoDependencia]:
    """Ejecuta todos los chequeos en paralelo y devuelve el detalle por dependencia."""
    resultados = await asyncio.gather(
        _chequeo_con_timeout(_chequeo_base_datos, "base de datos"),
        _chequeo_con_timeout(_chequeo_redis, "redis"),
        _chequeo_con_timeout(_chequeo_storage, "almacenamiento"),
        return_exceptions=True,
    )
    chequeos: dict[str, ChequeoDependencia] = {}
    for nombre, resultado in zip(("database", "redis", "storage"), resultados, strict=True):
        if isinstance(resultado, ChequeoDependencia):
            chequeos[nombre] = resultado
        else:  # pragma: no cover - salvaguarda ante errores inesperados
            logger.error("Chequeo de %s devolvió un resultado inesperado: %r", nombre, resultado)
            chequeos[nombre] = ChequeoDependencia(
                ok=False, latency_ms=0.0, error="error inesperado en el chequeo"
            )
    return chequeos


def _estado_global(chequeos: dict[str, ChequeoDependencia]) -> Literal["ok", "degraded"]:
    """Calcula el estado global a partir de las dependencias críticas."""
    criticas_ok = all(chequeos.get(nombre, ChequeoDependencia(ok=False)).ok for nombre in DEPENDENCIAS_CRITICAS)
    return "ok" if criticas_ok else "degraded"


@router.get("/health", response_class=Response, summary="Liveness de la aplicación")
async def health() -> Response:
    """Liveness puro: responde ``OK`` sin tocar ninguna dependencia."""
    return Response(content="OK", media_type="text/plain; charset=utf-8", status_code=status.HTTP_200_OK)


@router.get(
    "/ready",
    response_model=ReporteReadiness,
    response_model_exclude_none=True,
    summary="Readiness de la aplicación",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReporteReadiness,
            "description": "Alguna dependencia crítica no responde.",
        }
    },
)
async def ready(response: Response) -> ReporteReadiness:
    """Readiness: verifica dependencias reales y devuelve 503 si alguna crítica falla."""
    chequeos = await _ejecutar_chequeos()
    estado = _estado_global(chequeos)
    if estado != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReporteReadiness(status=estado, checks=chequeos)


@router.get(
    "/salud",
    response_model=ReporteSalud,
    response_model_exclude_none=True,
    summary="Detalle de salud en JSON",
)
async def salud() -> ReporteSalud:
    """Detalle de salud de la aplicación y de sus dependencias."""
    chequeos = await _ejecutar_chequeos()
    return ReporteSalud(
        status=_estado_global(chequeos),
        checks=chequeos,
        aplicacion=settings.app_name,
        version=APP_VERSION,
        entorno=settings.app_env,
    )
