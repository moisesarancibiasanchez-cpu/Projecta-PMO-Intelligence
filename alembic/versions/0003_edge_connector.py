"""Edge Connector de Projecta PMO Intelligence — tablas de la prueba de concepto.

Añade las tres tablas que el conector necesita:

* ``dispositivos_edge`` — registro de identidad criptográfica. Guarda la
  clave pública Ed25519, nunca la privada.
* ``consentimientos_edge`` — el registro que la puerta consulta antes de
  dejar leer. Es la extensión del registro de HU_11 a los demás recursos.
* ``eventos_edge`` — los eventos ya firmados, minimizados y aceptados.

La migración es **idempotente**, como las anteriores: aplicarla dos veces
no cambia el esquema y no falla.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
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
    """Aplica el esquema del Edge Connector de forma idempotente."""
    # --- dispositivos_edge ---------------------------------------------
    if not _existe("dispositivos_edge"):
        op.create_table(
            "dispositivos_edge",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("organizacion_id", sa.Uuid(), nullable=False),
            sa.Column("nombre", sa.String(length=160), nullable=False),
            sa.Column("huella_clave", sa.String(length=64), nullable=False),
            sa.Column("clave_publica", sa.String(length=64), nullable=False),
            sa.Column("version", sa.String(length=32), nullable=False, server_default="M0"),
            sa.Column("estado", sa.String(length=32), nullable=False, server_default="activo"),
            sa.Column("modo", sa.String(length=32), nullable=False, server_default="READ_ONLY"),
            sa.Column("sistemas", sa.JSON(), nullable=False),
            sa.Column(
                "registrado_en",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("revocado_en", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ultimo_latido_en", sa.DateTime(timezone=True), nullable=True),
            sa.Column("credencial_expira_en", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["organizacion_id"],
                ["organizaciones.id"],
                name="fk_dispositivos_edge_organizacion",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_dispositivos_edge"),
            sa.UniqueConstraint("huella_clave", name="uq_dispositivos_edge_huella"),
        )
    if not _existe_indice("dispositivos_edge", "ix_dispositivos_edge_organizacion"):
        op.create_index(
            "ix_dispositivos_edge_organizacion",
            "dispositivos_edge",
            ["organizacion_id", "estado"],
        )

    # --- consentimientos_edge ------------------------------------------
    if not _existe("consentimientos_edge"):
        op.create_table(
            "consentimientos_edge",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("organizacion_id", sa.Uuid(), nullable=False),
            sa.Column("recurso", sa.String(length=320), nullable=False),
            sa.Column("recurso_tipo", sa.String(length=64), nullable=False),
            sa.Column("finalidad", sa.String(length=320), nullable=False),
            sa.Column("base_legal", sa.String(length=64), nullable=False),
            sa.Column("destinatarios", sa.JSON(), nullable=False),
            sa.Column("proyecto_id", sa.Uuid(), nullable=True),
            sa.Column("otorgado_por", sa.String(length=320), nullable=False),
            sa.Column(
                "otorgado_en",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("revocado_en", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expira_en", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["organizacion_id"],
                ["organizaciones.id"],
                name="fk_consentimientos_edge_organizacion",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["proyecto_id"],
                ["proyectos.id"],
                name="fk_consentimientos_edge_proyecto",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_consentimientos_edge"),
        )
    if not _existe_indice("consentimientos_edge", "ix_consentimientos_edge_organizacion"):
        op.create_index(
            "ix_consentimientos_edge_organizacion",
            "consentimientos_edge",
            ["organizacion_id", "recurso"],
        )
    if not _existe_indice("consentimientos_edge", "ix_consentimientos_edge_proyecto_id"):
        op.create_index(
            "ix_consentimientos_edge_proyecto_id",
            "consentimientos_edge",
            ["proyecto_id"],
        )

    # --- eventos_edge ---------------------------------------------------
    if not _existe("eventos_edge"):
        op.create_table(
            "eventos_edge",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("organizacion_id", sa.Uuid(), nullable=False),
            sa.Column("dispositivo_id", sa.Uuid(), nullable=False),
            sa.Column("proyecto_id", sa.Uuid(), nullable=True),
            sa.Column("tipo", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("huella", sa.String(length=64), nullable=False),
            sa.Column("decision", sa.String(length=32), nullable=False, server_default="aceptado"),
            sa.Column("motivo", sa.Text(), nullable=True),
            sa.Column("correlation_id", sa.String(length=64), nullable=True),
            sa.Column(
                "recibido_en",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["organizacion_id"],
                ["organizaciones.id"],
                name="fk_eventos_edge_organizacion",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["dispositivo_id"],
                ["dispositivos_edge.id"],
                name="fk_eventos_edge_dispositivo",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["proyecto_id"],
                ["proyectos.id"],
                name="fk_eventos_edge_proyecto",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_eventos_edge"),
            sa.UniqueConstraint(
                "dispositivo_id", "huella", name="uq_eventos_edge_huella"
            ),
        )
    if not _existe_indice("eventos_edge", "ix_eventos_edge_organizacion_tipo"):
        op.create_index(
            "ix_eventos_edge_organizacion_tipo",
            "eventos_edge",
            ["organizacion_id", "tipo"],
        )
    if not _existe_indice("eventos_edge", "ix_eventos_edge_dispositivo_id"):
        op.create_index(
            "ix_eventos_edge_dispositivo_id", "eventos_edge", ["dispositivo_id"]
        )
    if not _existe_indice("eventos_edge", "ix_eventos_edge_proyecto_id"):
        op.create_index("ix_eventos_edge_proyecto_id", "eventos_edge", ["proyecto_id"])
    if not _existe_indice("eventos_edge", "ix_eventos_edge_correlation_id"):
        op.create_index(
            "ix_eventos_edge_correlation_id", "eventos_edge", ["correlation_id"]
        )


def downgrade() -> None:
    """Revierte el esquema del Edge Connector."""
    inspector = sa.inspect(op.get_bind())
    for tabla, indices in (
        ("eventos_edge", ["ix_eventos_edge_correlation_id", "ix_eventos_edge_proyecto_id",
                          "ix_eventos_edge_dispositivo_id",
                          "ix_eventos_edge_organizacion_tipo"]),
        ("consentimientos_edge", ["ix_consentimientos_edge_proyecto_id",
                                  "ix_consentimientos_edge_organizacion"]),
        ("dispositivos_edge", ["ix_dispositivos_edge_organizacion"]),
    ):
        if tabla not in inspector.get_table_names():
            continue
        for indice in indices:
            if _existe_indice(tabla, indice):
                op.drop_index(indice, table_name=tabla)
    for tabla in ("eventos_edge", "consentimientos_edge", "dispositivos_edge"):
        if _existe(tabla):
            op.drop_table(tabla)
