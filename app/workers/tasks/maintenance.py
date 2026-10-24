"""Tareas de mantenimiento de **Projecta PMO Intelligence**.

La tarea ``projecta.mantenimiento.ping`` comprueba la conectividad con Redis
y devuelve el resultado de la comprobación. Está pensada para que ``beat``
la ejecute cada cinco minutos y para que un humano pueda dispararla a mano
como diagnóstico rápido de la infraestructura.

La comprobación **nunca propaga la excepción**: si Redis no responde, la tarea
devuelve un estado ``error`` con el detalle del fallo. Así, un incidente de
Redis queda registrado sin generar reintentos ni ruido en el worker.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import redis

from app.core.celery_app import celery_app, nombre_app_visible, url_broker, url_redis

#: Tiempos máximos de conexión y de lectura contra Redis (segundos).
TIMEOUT_CONEXION: float = 2.0
TIMEOUT_LECTURA: float = 2.0


def _ahora() -> str:
    """Marca de tiempo actual en ISO 8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def _comprobar_redis(url: str) -> dict[str, Any]:
    """Intenta un ``PING`` a Redis y devuelve el detalle de la comprobación."""
    inicio = time.perf_counter()
    cliente: redis.Redis | None = None
    pong = False
    error: str | None = None

    try:
        cliente = redis.Redis.from_url(
            url,
            socket_connect_timeout=TIMEOUT_CONEXION,
            socket_timeout=TIMEOUT_LECTURA,
        )
        pong = bool(cliente.ping())
        if not pong:
            error = "Redis respondió sin PONG"
    except Exception as exc:  # noqa: BLE001 - el detalle se devuelve, no se propaga
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if cliente is not None:
            try:
                cliente.close()
            except Exception:  # noqa: BLE001 - cerrar el pool nunca debe romper la tarea
                pass

    latencia_ms = round((time.perf_counter() - inicio) * 1000, 2)
    return {
        "ok": pong,
        "url": url,
        "latencia_ms": latencia_ms,
        "pong": pong,
        "error": error,
    }


@celery_app.task(name="projecta.mantenimiento.ping", queue="mantenimiento")
def ping() -> dict[str, Any]:
    """Comprueba la conectividad con Redis y devuelve el resultado.

    Returns:
        dict con las claves ``status``, ``ok``, ``app``, ``redis`` y
        ``timestamp``. ``status`` vale ``"ok"`` cuando Redis responde y
        ``"error"`` cuando no.
    """
    # Se usa el Redis de aplicación; si no está configurado, se recurre al
    # broker de Celery, que en los despliegues habituales es el mismo servidor.
    url = url_redis(por_defecto=url_broker())
    comprobacion = _comprobar_redis(url)
    ok = bool(comprobacion["ok"])

    return {
        "status": "ok" if ok else "error",
        "ok": ok,
        "app": nombre_app_visible(),
        "redis": comprobacion,
        "timestamp": _ahora(),
    }


# Alias con el nombre completo de la tarea en snake_case, para poder invocarla
# directamente desde Python sin pasar por el registro de Celery.
projecta_mantenimiento_ping = ping

__all__ = ["ping", "projecta_mantenimiento_ping"]
