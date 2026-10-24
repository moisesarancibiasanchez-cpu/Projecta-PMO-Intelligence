"""Motor asíncrono, sessionmaker y dependencia de sesión de la aplicación."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.db.base import Base

logger = logging.getLogger("projecta.db")


def _opciones_motor() -> dict[str, Any]:
    """Opciones comunes del motor asíncrono."""
    opciones: dict[str, Any] = {
        "echo": settings.debug,
        "future": True,
        # `pool_pre_ping` evita usar conexiones muertas tras un reinicio del
        # servidor de base de datos.
        "pool_pre_ping": True,
    }
    if settings.database_url.startswith("sqlite"):
        # SQLite no admite conexiones simultáneas entre hilos con la
        # comprobación por defecto en modo archivo.
        opciones["connect_args"] = {"check_same_thread": False}
    return opciones


#: Motor asíncrono compartido por la aplicación.
engine: AsyncEngine = create_async_engine(settings.database_url, **_opciones_motor())

#: Fábrica de sesiones asíncronas. `expire_on_commit=False` evita que los
#: objetos queden expirados tras un commit dentro de la misma petición.
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependencia de FastAPI que entrega una sesión de base de datos.

    Realiza ``commit`` cuando la dependencia termina sin error y ``rollback``
    ante cualquier excepción, y cierra siempre la sesión.
    """
    sesion = AsyncSessionLocal()
    try:
        yield sesion
        await sesion.commit()
    except Exception:
        await sesion.rollback()
        raise
    finally:
        await sesion.close()


async def dispose_engine() -> None:
    """Cierra el pool de conexiones del motor asíncrono."""
    await engine.dispose()
    logger.info("Pool de conexiones de base de datos cerrado.")


async def crear_tablas() -> None:
    """Crea las tablas definidas en los modelos (solo para desarrollo)."""
    async with engine.begin() as conexion:
        await conexion.run_sync(Base.metadata.create_all)


__all__ = [
    "AsyncSessionLocal",
    "Base",
    "crear_tablas",
    "dispose_engine",
    "engine",
    "get_session",
]
