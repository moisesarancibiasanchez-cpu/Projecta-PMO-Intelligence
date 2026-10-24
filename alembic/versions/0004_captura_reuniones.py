"""Captura de reuniones de Teams — tabla de reuniones capturadas.

Añade ``reuniones_capturadas``, donde queda lo que se ha traído de
Microsoft 365: la transcripción con su hablante, los participantes y la
referencia al audio guardado en el almacenamiento de objetos.

La tabla está pensada para el **barrido periódico**: la tarea de captura
la interroga por rango de fechas, y la restricción de unicidad sobre
``(organizador, reunion_id)`` hace que reejecutarla no duplique nada.

La migración es **idempotente**, como las anteriores: aplicarla dos
veces no cambia el esquema y no falla.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existe(tabla: str) -> bool:
    """Indica si una tabla ya existe."""
    inspector = sa.inspect(op.get_bind())
    return tabla in inspector.get_table_names()


def _existe_indice(tabla: str, indice: str) -> bool:
    """Indica si un índice ya existe en la tabla indicada."""
    inspector = sa.inspect(op.get_bind())
    if tabla not in inspector.get_table_names():
        return False
    return any(i["name"] == indice for i in inspector.get_indexes(tabla))


def upgrade() -> None:
    """Crea la tabla de reuniones capturadas."""
    if _existe("reuniones_capturadas"):
        return

    op.create_table(
        "reuniones_capturadas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organizacion_id", sa.Uuid(), nullable=False),
        sa.Column("proyecto_id", sa.Uuid(), nullable=True),
        sa.Column("organizador", sa.String(length=256), nullable=False),
        sa.Column("reunion_id", sa.String(length=256), nullable=False),
        sa.Column("asunto", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("transcripcion_id", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("segmentos", sa.JSON(), nullable=False),
        sa.Column("participantes", sa.JSON(), nullable=False),
        sa.Column("audio_objeto", sa.String(length=512), nullable=True),
        sa.Column("formato_audio", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("audio_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("segmentos_totales", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("huella", sa.String(length=64), nullable=False),
        sa.Column("inicio", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fin", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "capturado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organizacion_id"],
            ["organizaciones.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proyecto_id"],
            ["proyectos.id"],
            ondelete="SET NULL",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organizador", "reunion_id", name="uq_reuniones_capturadas_organizador_reunion"
        ),
    )

    for columna in ("organizacion_id", "proyecto_id", "huella"):
        op.create_index(
            f"ix_reuniones_capturadas_{columna}",
            "reuniones_capturadas",
            [columna],
            unique=False,
        )
    op.create_index(
        "ix_reuniones_capturadas_organizacion_proyecto",
        "reuniones_capturadas",
        ["organizacion_id", "proyecto_id"],
        unique=False,
    )


def downgrade() -> None:
    """Retira la tabla de reuniones capturadas.

    La tabla no tiene filas que otras tablas referencien —es una tabla
    de intercambio, no una entidad—, así que se puede retirar sin
    romper nada. Aun así, se comprueba antes, porque el `downgrade` de
    una migración que ya se aplicó no debe surprises.
    """
    if not _existe("reuniones_capturadas"):
        return

    for indice in (
        "ix_reuniones_capturadas_organizacion_proyecto",
        "ix_reuniones_capturados_huella",
        "ix_reuniones_capturadas_huella",
        "ix_reuniones_capturadas_proyecto_id",
        "ix_reuniones_capturadas_organizacion_id",
    ):
        if _existe_indice("reuniones_capturadas", indice):
            op.drop_index(indice, table_name="reuniones_capturadas")

    op.drop_table("reuniones_capturadas")
