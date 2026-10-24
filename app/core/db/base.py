"""Base declarativa de SQLAlchemy 2 para Projecta PMO Intelligence."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Convención de nombres para índices, restricciones y claves foráneas.
#: Mantenerla explícita permite que Alembic genere migraciones reversibles
#: también en SQLite, donde las restricciones no se pueden modificar.
CONVENCION_NOMBRES = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarativa común a todos los modelos ORM del proyecto."""

    metadata = MetaData(naming_convention=CONVENCION_NOMBRES)
