"""Entorno de Alembic para Projecta PMO Intelligence.

Soporte async: la URL de la conexión se toma de la configuración de la
aplicación (``settings.database_url``), que es la URL **asíncrona**
(``postgresql+asyncpg`` en producción, ``sqlite+aiosqlite`` en desarrollo).

El motivo de usar la URL asíncrona y no la síncrona es que este entorno crea el
motor con ``async_engine_from_config`` y lo consume con ``run_sync``. Ese motor
exige un driver asíncrono: ``postgresql+psycopg`` funcionaría por casualidad
(psycopg 3 tiene modo asíncrono), pero ``sqlite://`` —pysqlite— no lo es y
fallaría con «The asyncio extension requires an async driver». La propiedad
``settings.database_url_sync`` sigue existiendo para las herramientas que
necesiten una URL puramente síncrona (volcados, administración, Alembic en
modo síncrono).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.core.db import models  # noqa: F401  (importa los modelos en el metadata)
from app.core.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `configparser` interpreta los `%`, así que hay que escaparlos.
_url = settings.database_url.replace("%", "%%")
config.set_main_option("sqlalchemy.url", _url)

#: Metadata con todas las tablas del proyecto.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Ejecuta las migraciones sin conexión (genera el SQL)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Ejecuta las migraciones sobre una conexión síncrona."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Crea el motor asíncrono y ejecuta las migraciones."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Punto de entrada para las migraciones en línea."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
