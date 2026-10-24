"""Tarea de humo (*smoke*) de **Projecta PMO Intelligence**.

Sirve para comprobar de extremo a extremo que el worker de Celery está vivo,
que las rutas de la aplicación se importan bien y que el enrutamiento hacia la
cola ``ia`` funciona.

La tarea es **idempotente** y **no tiene efectos secundarios**: no escribe en
la base de datos, no publica mensajes y no llama a servicios externos. Solo
lee el estado del proceso y devuelve un diccionario.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.celery_app import celery_app, nombre_app_visible


def _ahora() -> str:
    """Marca de tiempo actual en ISO 8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat()


@celery_app.task(name="projecta.smoke", queue="ia")
def smoke(echo: str | None = None) -> dict[str, Any]:
    """Devuelve el estado del proceso sin tocar nada.

    Args:
        echo: Valor opcional que se devuelve tal cual, útil para verificar que
            los argumentos viajan correctamente hasta el worker.

    Returns:
        dict con las claves ``status``, ``app``, ``timestamp`` y ``echo``.
    """
    return {
        "status": "ok",
        "app": nombre_app_visible(),
        "timestamp": _ahora(),
        "echo": echo,
    }


# Alias con el nombre completo de la tarea en snake_case, para poder invocarla
# directamente desde Python sin pasar por el registro de Celery.
projecta_smoke = smoke

__all__ = ["projecta_smoke", "smoke"]
