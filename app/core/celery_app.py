"""Instancia de Celery de **Projecta PMO Intelligence**.

Este módulo es el punto único de configuración de la capa asíncrona: broker,
backend de resultados, colas, enrutamientos, límites de ejecución y
planificador (beat).

Contrato de referencia: ``docs/skeleton/design.md`` — sección «Contrato de
Celery». Los nombres de la aplicación, de las seis colas y de las tareas
registradas son normativos y no deben cambiar sin actualizar el contrato.

Uso habitual::

    celery -A app.core.celery_app:celery_app worker -Q ia,ingesta
    celery -A app.core.celery_app:celery_app beat
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Final

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identidad de la aplicación
# ---------------------------------------------------------------------------

#: Identificador técnico de la app Celery (usado en el nombre de la instancia).
NOMBRE_APP: Final[str] = "projecta"

#: Nombre visible de la marca. Nunca se abrevia a «Projecta» a secas.
NOMBRE_MARCA: Final[str] = "Projecta PMO Intelligence"

# ---------------------------------------------------------------------------
# Colas del contrato (exactamente seis)
# ---------------------------------------------------------------------------

COLA_INGESTA: Final[str] = "ingesta"
COLA_DOCUMENTOS: Final[str] = "documentos"
COLA_IA: Final[str] = "ia"
COLA_INTEGRACIONES: Final[str] = "integraciones"
COLA_REPORTES: Final[str] = "reportes"
COLA_MANTENIMIENTO: Final[str] = "mantenimiento"

#: Las seis colas del contrato, en el orden documentado.
COLAS: Final[tuple[str, ...]] = (
    COLA_INGESTA,
    COLA_DOCUMENTOS,
    COLA_IA,
    COLA_INTEGRACIONES,
    COLA_REPORTES,
    COLA_MANTENIMIENTO,
)

# ---------------------------------------------------------------------------
# Tareas registradas (nombres exactos del contrato)
# ---------------------------------------------------------------------------

TAREA_SMOKE: Final[str] = "projecta.smoke"
TAREA_MANTENIMIENTO_PING: Final[str] = "projecta.mantenimiento.ping"

# ---------------------------------------------------------------------------
# Valores por defecto (desarrollo local)
# ---------------------------------------------------------------------------

#: Broker por defecto: base 1 de Redis.
BROKER_POR_DEFECTO: Final[str] = "redis://localhost:6379/1"
#: Backend de resultados por defecto: base 2 de Redis.
BACKEND_POR_DEFECTO: Final[str] = "redis://localhost:6379/2"
#: Redis de aplicación por defecto: base 0 de Redis.
REDIS_POR_DEFECTO: Final[str] = "redis://localhost:6379/0"

# ---------------------------------------------------------------------------
# Parámetros de robustez obligatorios del contrato
# ---------------------------------------------------------------------------

TASK_ACKS_LATE: Final[bool] = True
TASK_REJECT_ON_WORKER_LOST: Final[bool] = True
WORKER_PREFETCH_MULTIPLIER: Final[int] = 1
TASK_SOFT_TIME_LIMIT: Final[int] = 600  # 10 minutos
TASK_TIME_LIMIT: Final[int] = 900  # 15 minutos
BROKER_CONNECTION_RETRY_ON_STARTUP: Final[bool] = True

#: Segundos que un mensaje puede permanecer invisible en el broker. Debe ser
#: mayor que ``task_time_limit`` para no perder tareas con ``acks_late``.
VISIBILITY_TIMEOUT: Final[int] = 1800


# ---------------------------------------------------------------------------
# Resolución de configuración
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _ajustes() -> Any | None:
    """Devuelve ``app.core.config.settings`` o ``None`` si no está disponible.

    La importación es tolerante a fallo: el worker y el beat deben poder
    arrancar aunque la configuración tipada todavía no exista o falle al
    validarse. En ese caso se recurre a las variables de entorno y, en último
    término, a los valores por defecto de desarrollo.
    """
    try:
        from app.core.config import settings  # noqa: PLC0415 (import diferido a propósito)
    except Exception as exc:  # pragma: no cover - depende del entorno
        detalle = str(exc).strip().splitlines()
        logger.warning(
            "No se pudo cargar 'app.core.config' (%s: %s). "
            "Se usarán las variables de entorno y los valores por defecto.",
            type(exc).__name__,
            detalle[0] if detalle else "sin detalle",
        )
        return None
    return settings


def _resolver_url(*, atributo: str, variable: str, por_defecto: str) -> str:
    """Resuelve una URL con la precedencia: settings > entorno > por defecto."""
    ajustes = _ajustes()
    valor = getattr(ajustes, atributo, None) if ajustes is not None else None
    if isinstance(valor, str) and valor.strip():
        return valor.strip()
    valor_entorno = os.environ.get(variable, "").strip()
    if valor_entorno:
        return valor_entorno
    return por_defecto


def url_broker() -> str:
    """URL del broker de Celery."""
    return _resolver_url(
        atributo="celery_broker_url",
        variable="CELERY_BROKER_URL",
        por_defecto=BROKER_POR_DEFECTO,
    )


def url_backend() -> str:
    """URL del backend de resultados de Celery."""
    return _resolver_url(
        atributo="celery_result_backend",
        variable="CELERY_RESULT_BACKEND",
        por_defecto=BACKEND_POR_DEFECTO,
    )


def url_redis(*, por_defecto: str = REDIS_POR_DEFECTO) -> str:
    """URL de Redis de aplicación (caché, locks y comprobaciones de estado)."""
    return _resolver_url(
        atributo="redis_url",
        variable="REDIS_URL",
        por_defecto=por_defecto,
    )


def nombre_app_visible() -> str:
    """Nombre visible de la aplicación, tal y como lo publica la configuración."""
    ajustes = _ajustes()
    valor = getattr(ajustes, "app_name", None) if ajustes is not None else None
    if isinstance(valor, str) and valor.strip():
        return valor.strip()
    return NOMBRE_MARCA


# ---------------------------------------------------------------------------
# Colas, enrutamientos y planificador
# ---------------------------------------------------------------------------

#: Exchange directo de la aplicación. El routing key de cada cola coincide
#: con el nombre de la cola, de modo que publicar en «ia» llega a «ia».
EXCHANGE: Final[Exchange] = Exchange(NOMBRE_APP, type="direct", durable=True)

#: Las seis colas del contrato, declaradas de forma explícita para que el
#: broker las cree y para que los workers puedan hacer ``-Q`` con ellas.
COLAS_KOMBU: Final[tuple[Queue, ...]] = tuple(
    Queue(nombre, EXCHANGE, routing_key=nombre, durable=True) for nombre in COLAS
)

#: Enrutamiento explícito por tarea. Las claves con ``*`` son patrones glob
#: soportados nativamente por el enrutador de Celery y cubren las tareas
#: futuras de cada carril; las claves exactas mandan sobre los patrones.
TASK_ROUTES: Final[dict[str, dict[str, str]]] = {
    TAREA_SMOKE: {"queue": COLA_IA},
    TAREA_MANTENIMIENTO_PING: {"queue": COLA_MANTENIMIENTO},
    "projecta.ingesta.*": {"queue": COLA_INGESTA},
    "projecta.documentos.*": {"queue": COLA_DOCUMENTOS},
    "projecta.ia.*": {"queue": COLA_IA},
    "projecta.integraciones.*": {"queue": COLA_INTEGRACIONES},
    "projecta.reportes.*": {"queue": COLA_REPORTES},
    "projecta.mantenimiento.*": {"queue": COLA_MANTENIMIENTO},
}

#: Cola por defecto para las tareas sin enrutamiento explícito. «ia» es el
#: carril de procesamiento general (clasificación, extracción, artefactos).
TASK_DEFAULT_QUEUE: Final[str] = COLA_IA

#: Planificador: comprobación de mantenimiento cada cinco minutos.
BEAT_SCHEDULE: Final[dict[str, dict[str, Any]]] = {
    "mantenimiento-ping-cada-5-minutos": {
        "task": TAREA_MANTENIMIENTO_PING,
        "schedule": crontab(minute="*/5"),
        "options": {"queue": COLA_MANTENIMIENTO},
    },
}

#: Módulos de tareas que se autocargan al importar la app.
#:
#: `ingesta_teams` es la ingesta del Edge Connector en la Variante A: el
#: adaptador de Teams corre **dentro del worker**, en la red del banco,
#: sin producto aparte y sin puerto. Usa la cola `integraciones`, que
#: este mismo contrato ya define y ya enruta.
TASK_MODULES: Final[list[str]] = [
    "app.workers.tasks.smoke",
    "app.workers.tasks.maintenance",
    "app.workers.tasks.ingesta_teams",
    "app.workers.tasks.captura_teams",
    "app.workers.tasks.procesa_captura",
]


# ---------------------------------------------------------------------------
# Instancia de Celery
# ---------------------------------------------------------------------------

BROKER_URL: Final[str] = url_broker()
BACKEND_URL: Final[str] = url_backend()

celery_app = Celery(
    NOMBRE_APP,
    broker=BROKER_URL,
    backend=BACKEND_URL,
    include=TASK_MODULES,
)

celery_app.conf.update(
    # --- Broker y backend -------------------------------------------------
    broker_url=BROKER_URL,
    result_backend=BACKEND_URL,
    broker_connection_retry_on_startup=BROKER_CONNECTION_RETRY_ON_STARTUP,
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT},
    # --- Colas y enrutamiento --------------------------------------------
    task_default_queue=TASK_DEFAULT_QUEUE,
    task_queues=COLAS_KOMBU,
    task_routes=TASK_ROUTES,
    # Solo existen las seis colas del contrato: no se crean colas a medida.
    task_create_missing_queues=False,
    # --- Robustez de los workers -----------------------------------------
    task_acks_late=TASK_ACKS_LATE,
    task_reject_on_worker_lost=TASK_REJECT_ON_WORKER_LOST,
    worker_prefetch_multiplier=WORKER_PREFETCH_MULTIPLIER,
    task_soft_time_limit=TASK_SOFT_TIME_LIMIT,
    task_time_limit=TASK_TIME_LIMIT,
    # --- Extras operativos ------------------------------------------------
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    result_expires=86400,
    task_track_started=True,
    task_send_sent_event=True,
    worker_send_task_events=True,
    # --- Planificador -----------------------------------------------------
    beat_schedule=BEAT_SCHEDULE,
)

# Autocarga de los módulos de tareas declarados en ``include``. Sin esta
# llamada, las tareas solo se registrarían al arrancar un worker o el beat
# (``celery`` las importa en su propio arranque) y el registro quedaría vacío
# en cualquier proceso que solo importe la app (API, pruebas, scripts).
celery_app.loader.import_default_modules()

logger.info(
    "Celery '%s' configurado: broker=%s, backend=%s, colas=%s, tarea por defecto=%s",
    NOMBRE_APP,
    BROKER_URL,
    BACKEND_URL,
    ", ".join(COLAS),
    TASK_DEFAULT_QUEUE,
)

__all__ = [
    "BACKEND_URL",
    "BACKEND_POR_DEFECTO",
    "BEAT_SCHEDULE",
    "BROKER_URL",
    "BROKER_POR_DEFECTO",
    "COLAS",
    "COLAS_KOMBU",
    "COLA_DOCUMENTOS",
    "COLA_IA",
    "COLA_INGESTA",
    "COLA_INTEGRACIONES",
    "COLA_MANTENIMIENTO",
    "COLA_REPORTES",
    "EXCHANGE",
    "NOMBRE_APP",
    "NOMBRE_MARCA",
    "REDIS_POR_DEFECTO",
    "TASK_DEFAULT_QUEUE",
    "TASK_MODULES",
    "TASK_ROUTES",
    "TAREA_MANTENIMIENTO_PING",
    "TAREA_SMOKE",
    "celery_app",
    "nombre_app_visible",
    "url_backend",
    "url_broker",
    "url_redis",
]
