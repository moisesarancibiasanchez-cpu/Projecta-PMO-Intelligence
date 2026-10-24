"""Esquema inicial de Projecta PMO Intelligence.

Crea las cuatro entidades del núcleo: organizaciones, roles, usuarios y
proyectos, con sus claves foráneas y su política `ondelete` explícita.

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea el esquema inicial del proyecto."""
    op.create_table(
        "organizaciones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("nombre", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_organizaciones"),
        sa.UniqueConstraint("slug", name="uq_organizaciones_slug"),
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("nombre", sa.String(length=80), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("nombre", name="uq_roles_nombre"),
    )

    op.create_table(
        "usuarios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organizacion_id", sa.Uuid(), nullable=False),
        sa.Column("rol_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("nombre", sa.String(length=200), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organizacion_id"],
            ["organizaciones.id"],
            name="fk_usuarios_organizacion_id_organizaciones",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rol_id"],
            ["roles.id"],
            name="fk_usuarios_rol_id_roles",
            ondelete="RESTRICT",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usuarios"),
        sa.UniqueConstraint("email", name="uq_usuarios_email"),
    )
    op.create_index("ix_usuarios_organizacion_id", "usuarios", ["organizacion_id"], unique=False)
    op.create_index("ix_usuarios_rol_id", "usuarios", ["rol_id"], unique=False)

    op.create_table(
        "proyectos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organizacion_id", sa.Uuid(), nullable=False),
        sa.Column("nombre", sa.String(length=200), nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("estado", sa.String(length=32), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organizacion_id"],
            ["organizaciones.id"],
            name="fk_proyectos_organizacion_id_organizaciones",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proyectos"),
        sa.UniqueConstraint("codigo", name="uq_proyectos_codigo"),
    )
    op.create_index("ix_proyectos_organizacion_id", "proyectos", ["organizacion_id"], unique=False)


def downgrade() -> None:
    """Elimina el esquema inicial del proyecto (reverso exacto de `upgrade`)."""
    op.drop_index("ix_proyectos_organizacion_id", table_name="proyectos")
    op.drop_table("proyectos")

    op.drop_index("ix_usuarios_rol_id", table_name="usuarios")
    op.drop_index("ix_usuarios_organizacion_id", table_name="usuarios")
    op.drop_table("usuarios")

    op.drop_table("roles")

    op.drop_table("organizaciones")
