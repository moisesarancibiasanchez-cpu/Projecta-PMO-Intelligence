"""Fábrica de la aplicación FastAPI de Projecta PMO Intelligence.

Ejemplo de arranque:

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import (
    adjuntos,
    auth,
    captura,
    edge,
    health,
    index,
    proyectos,
    reuniones,
)
from app.core.config import APP_VERSION, settings
from app.core.db.session import dispose_engine
from app.core.observability import instalar as instalar_observabilidad
from app.core.observability import middleware_correlacion

logger = logging.getLogger("projecta")

DESCRIPCION = (
    "Projecta PMO Intelligence — la capa inteligente para gobernar proyectos, "
    "portafolios y decisiones. Decide con evidencia. Avanza con control."
)


def _configurar_logging() -> None:
    """Ajusta el logging raíz al nivel configurado."""
    nivel = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=nivel,
        format="%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
        force=True,
    )


def _registrar_configuracion() -> None:
    """Registra la configuración efectiva al arrancar, sin secretos."""
    logger.info("Arrancando %s v%s", settings.app_name, APP_VERSION)
    logger.info("Entorno: %s · Depuración: %s", settings.app_env, settings.debug)
    logger.info("Base de datos: %s", settings.database_url_enmascarada)
    logger.info("Redis: %s", settings.redis_url_enmascarada)
    logger.info("Almacenamiento: %s (bucket %s)", settings.minio_endpoint, settings.minio_bucket)
    logger.info("Orígenes CORS permitidos: %s", settings.cors_origins or "ninguno")
    logger.info("Nivel de log: %s", settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ciclo de vida de la aplicación: registra la configuración y libera recursos."""
    _configurar_logging()
    _registrar_configuracion()
    logger.info("%s operativo.", settings.app_name)
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("%s detenido: recursos liberados.", settings.app_name)


def create_app() -> FastAPI:
    """Crea y configura la aplicación FastAPI."""
    _configurar_logging()

    app = FastAPI(
        title=settings.app_name,
        version=APP_VERSION,
        description=DESCRIPCION,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS configurable: solo se permite lo que aparece en CORS_ORIGINS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Identificador de correlación en cada petición: es el hilo que une el
    # log, la auditoría, las métricas y las trazas.
    middleware_correlacion(app)

    # Trazas y métricas. Nunca rompen el arranque: si la instrumentación
    # falla, la aplicación sigue operativas.
    instalar_observabilidad(app)

    app.include_router(health.router)
    app.include_router(index.router)
    app.include_router(auth.router)
    app.include_router(proyectos.router)
    app.include_router(adjuntos.router)
    app.include_router(edge.router)
    app.include_router(reuniones.router)
    app.include_router(captura.router)

    app.state.settings = settings
    return app


#: Instancia lista para ``uvicorn app.main:app``.
app = create_app()
