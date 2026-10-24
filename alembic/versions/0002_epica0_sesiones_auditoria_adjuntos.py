"""Sesiones, membresías, auditoría y adjuntos.

Completa el esquema de la Épica 0 (HU_02 a HU_08):

* ``roles.nivel`` — nivel jerárquico que resuelve el RBAC en runtime.
* ``roles.ambito`` — ámbito del rol: ``organización`` o ``proyecto``.
* ``membresias_proyecto`` — ámbito de proyecto del RBAC.
* ``sesiones`` — refresh tokens rotativos agrupados por familia.
* ``eventos_auditoria`` — registro append-only con cadena de hash.
* ``adjuntos`` — metadato de los ficheros del almacenamiento de objetos.
* ``proyectos`` — código único por organización y borrado lógico.

Además, siembra el **catálogo de los seis roles normativos**. No es un
extra: el RBAC resuelve el nivel del rol contra la tabla ``roles``, así que
un despliegue sin catálogo no puede autorizar nada. Como el catálogo está
fijado por el TO-BE v1.1 §6.1, tiene que viajar con el esquema: si se
sembrara a mano, un despliegue olvidado arrancaría sin roles y el primer
inicio de sesión fallaría.

La migración es **idempotente**: aplicarla dos veces no cambia el esquema y
no falla. Usa ``IF NOT EXISTS`` en las comprobaciones de existencia, de modo
que un despliegue que se reintenta tras un fallo parcial converge.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22
"""

from __future__ import annotations

import uuid

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existe(tabla: str) -> bool:
    """Indica si una tabla ya existe en la base de datos."""
    inspector = sa.inspect(op.get_bind())
    return tabla in inspector.get_table_names()


def _existe_indice(tabla: str, indice: str) -> bool:
    """Indica si un índice ya existe en la tabla indicada."""
    inspector = sa.inspect(op.get_bind())
    if tabla not in inspector.get_table_names():
        return False
    return any(i["name"] == indice for i in inspector.get_indexes(tabla))


def _existe_columna(tabla: str, columna: str) -> bool:
    """Indica si una columna ya existe en la tabla indicada."""
    inspector = sa.inspect(op.get_bind())
    if tabla not in inspector.get_table_names():
        return False
    return any(c["name"] == columna for c in inspector.get_columns(tabla))


#: Catálogo de los seis roles normativos del TO-BE v1.1 §6.1.
#:
#: ``(nombre, nivel, ambito, puede, no_puede)``. El nivel es lo que resuelve
#: el RBAC en runtime: ampliar el catálogo es insertar una fila, no desplegar
#: código.
CATALOGO_ROLES: tuple[tuple[str, int, str, str, str], ...] = (
    (
        "observador",
        0,
        "organización",
        "Ver proyectos, compromisos, informes y auditoría",
        "Crear, corregir, aprobar ni transicionar",
    ),
    (
        "colaborador",
        1,
        "membresía de proyecto",
        "Cargar fuentes, adjuntar evidencia y actualizar sus compromisos",
        "Aprobar borradores ni aplicar cambios",
    ),
    (
        "analista",
        2,
        "proyecto",
        "Analizar evidencia y proponer borradores",
        "Aprobar ni transicionar estados",
    ),
    (
        "gerente",
        3,
        "proyecto",
        "Aprobar, rechazar y transicionar en su proyecto",
        "Administrar la organización ni sus roles",
    ),
    (
        "pmo",
        4,
        "organización",
        "Configurar reglas, plantillas y semáforos; auditar toda la organización",
        "Administrar usuarios ni la configuración de la plataforma",
    ),
    (
        "administrador",
        5,
        "organización",
        "Administrar usuarios, roles e integraciones de la organización",
        "Modificar la configuración global de la plataforma",
    ),
)


#: Tabla ``roles`` tipada, para que SQLAlchemy convierta los valores al
#: dialecto de cada motor. Con ``sa.text()`` el UUID se pasaría como
#: parámetro Python crudo, y SQLite no admite ese tipo: la migración
#: fallaría contra el round-trip en archivo.
TABLA_ROLES = sa.table(
    "roles",
    sa.column("id", sa.Uuid()),
    sa.column("nombre", sa.String()),
    sa.column("nivel", sa.Integer()),
    sa.column("ambito", sa.String()),
    sa.column("descripcion", sa.Text()),
)


def _sembrar_roles() -> None:
    """Inserta el catálogo de roles normativos si aún no está.

    Idempotente: un rol que ya existe no se toca. Si alguien ha cambiado
    su nivel a mano, el cambio se respeta —la migración no pisa decisiones
    de quien administra la plataforma— y la herramienta
    ``scripts/sembrar_roles.py`` avisa de la discrepancia.
    """
    conexion = op.get_bind()
    existentes = {
        fila[0]
        for fila in conexion.execute(sa.select(TABLA_ROLES.c.nombre)).fetchall()
    }
    for nombre, nivel, ambito, puede, no_puede in CATALOGO_ROLES:
        if nombre in existentes:
            continue
        conexion.execute(
            sa.insert(TABLA_ROLES).values(
                id=uuid.uuid4(),
                nombre=nombre,
                nivel=nivel,
                ambito=ambito,
                descripcion=f"{puede}. No puede: {no_puede}.",
            )
        )


def _roles_en_uso() -> set[str]:
    """Nombres de los roles que están referenciados y no se pueden borrar.

    Un rol asignado a un usuario o a una membresía de proyecto no se
    puede eliminar sin romper la integridad referencial. La migración
    0002 siembra el catálogo, pero un usuario puede haberse asignado el
    rol «administrador» entre el ``upgrade`` y el ``downgrade``: si la
    reversión intentara borrarlo, la restricción de clave foránea
    reventaría **después** de que las tablas ya se han soltado, dejando
    el esquema a medias.
    """
    conexion = op.get_bind()
    inspector = sa.inspect(conexion)
    en_uso: set[str] = set()

    if "usuarios" in inspector.get_table_names():
        filas = conexion.execute(
            sa.text(
                "SELECT DISTINCT r.nombre FROM usuarios u "
                "JOIN roles r ON r.id = u.rol_id"
            )
        ).fetchall()
        en_uso.update(fila[0] for fila in filas)

    if "membresias_proyecto" in inspector.get_table_names():
        filas = conexion.execute(
            sa.text(
                "SELECT DISTINCT r.nombre FROM membresias_proyecto m "
                "JOIN roles r ON r.id = m.rol_id"
            )
        ).fetchall()
        en_uso.update(fila[0] for fila in filas)

    return en_uso


def _retirar_roles() -> None:
    """Retira el catálogo sembrado por esta migración.

    Solo se borran los roles normativos que siguen con su descripción de
    origen: si alguien los ha modificado, se dejan intactos, porque
    reverterlos destruiría trabajo administrativo.

    Los roles **en uso** tampoco se borran, por mucho que conserven su
    descripción de origen: eliminarlos violaría la integridad
    referencial y abortaría la reversión a mitad. Un rol es un dato, no
    una columna: dejarlo puesto no deja el esquema incoherente, y la
    reversión puede seguir adelante.
    """
    conexion = op.get_bind()
    en_uso = _roles_en_uso()
    for nombre, _nivel, _ambito, puede, no_puede in CATALOGO_ROLES:
        if nombre in en_uso:
            continue
        conexion.execute(
            sa.delete(TABLA_ROLES).where(
                sa.and_(
                    TABLA_ROLES.c.nombre == nombre,
                    TABLA_ROLES.c.descripcion == f"{puede}. No puede: {no_puede}.",
                )
            )
        )


def upgrade() -> None:
    """Aplica el esquema de la Épica 0 de forma idempotente."""
    # --- roles: nivel jerárquico y ámbito -----------------------------
    if _existe_columna("roles", "nivel"):
        pass
    else:
        op.add_column(
            "roles",
            sa.Column("nivel", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _existe_indice("roles", "ix_roles_nivel"):
        op.create_index("ix_roles_nivel", "roles", ["nivel"])
    # El ámbito del rol (`organización` o `proyecto`) lo declara el modelo
    # pero faltaba en el esquema. Sin esta columna, la siembra de roles
    # falla contra PostgreSQL con «column ambito does not exist», aunque
    # las pruebas, que se apoyan en SQLite y crean el esquema desde el
    # modelo, pasarían.
    if _existe_columna("roles", "ambito"):
        pass
    else:
        op.add_column(
            "roles",
            sa.Column(
                "ambito", sa.String(length=32), nullable=False, server_default="organización"
            ),
        )

    # --- usuarios: hash de contraseña y proveedor de identidad -------
    if not _existe_columna("usuarios", "password_hash"):
        op.add_column("usuarios", sa.Column("password_hash", sa.String(length=120), nullable=True))
    if not _existe_columna("usuarios", "proveedor_identidad"):
        op.add_column(
            "usuarios", sa.Column("proveedor_identidad", sa.String(length=120), nullable=True)
        )

    # --- proyectos: código único por organización y borrado lógico ----
    if not _existe_columna("proyectos", "plantilla"):
        op.add_column("proyectos", sa.Column("plantilla", sa.String(length=64), nullable=True))
    if not _existe_columna("proyectos", "eliminado_en"):
        op.add_column(
            "proyectos", sa.Column("eliminado_en", sa.DateTime(timezone=True), nullable=True)
        )

    # --- catálogo de roles normativos ---------------------------------
    _sembrar_roles()

    # --- membresias_proyecto ------------------------------------------
    if not _existe("membresias_proyecto"):
        op.create_table(
            "membresias_proyecto",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("usuario_id", sa.Uuid(), nullable=False),
            sa.Column("proyecto_id", sa.Uuid(), nullable=False),
            sa.Column("rol_id", sa.Uuid(), nullable=False),
            sa.Column(
                "creado_en",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["rol_id"], ["roles.id"], name="fk_membresias_proyecto_rol", ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["proyecto_id"],
                ["proyectos.id"],
                name="fk_membresias_proyecto_proyecto",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["usuario_id"],
                ["usuarios.id"],
                name="fk_membresias_proyecto_usuario",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_membresias_proyecto"),
            sa.UniqueConstraint(
                "usuario_id", "proyecto_id", name="uq_membresias_usuario_proyecto"
            ),
        )
    if not _existe_indice("membresias_proyecto", "ix_membresias_proyecto_usuario_id"):
        op.create_index("ix_membresias_proyecto_usuario_id", "membresias_proyecto", ["usuario_id"])
    if not _existe_indice("membresias_proyecto", "ix_membresias_proyecto_proyecto_id"):
        op.create_index(
            "ix_membresias_proyecto_proyecto_id", "membresias_proyecto", ["proyecto_id"]
        )
    if not _existe_indice("membresias_proyecto", "ix_membresias_proyecto_rol_id"):
        op.create_index("ix_membresias_proyecto_rol_id", "membresias_proyecto", ["rol_id"])

    # --- sesiones -----------------------------------------------------
    if not _existe("sesiones"):
        op.create_table(
            "sesiones",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("usuario_id", sa.Uuid(), nullable=False),
            sa.Column("organizacion_id", sa.Uuid(), nullable=False),
            sa.Column("familia_id", sa.String(length=64), nullable=False),
            sa.Column("hash_refresh", sa.String(length=64), nullable=False),
            sa.Column("expira_en", sa.DateTime(timezone=True), nullable=False),
            sa.Column("usado_en", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revocado_en", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "creado_en",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["organizacion_id"],
                ["organizaciones.id"],
                name="fk_sesiones_organizacion",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["usuario_id"], ["usuarios.id"], name="fk_sesiones_usuario", ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id", name="pk_sesiones"),
            sa.UniqueConstraint("hash_refresh", name="uq_sesiones_hash_refresh"),
        )
    if not _existe_indice("sesiones", "ix_sesiones_familia"):
        op.create_index("ix_sesiones_familia", "sesiones", ["familia_id"])
    if not _existe_indice("sesiones", "ix_sesiones_usuario"):
        op.create_index("ix_sesiones_usuario", "sesiones", ["usuario_id"])

    # --- eventos_auditoria ---------------------------------------------
    if not _existe("eventos_auditoria"):
        op.create_table(
            "eventos_auditoria",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("organizacion_id", sa.Uuid(), nullable=False),
            sa.Column("proyecto_id", sa.Uuid(), nullable=True),
            sa.Column("tipo", sa.String(length=64), nullable=False),
            sa.Column("entidad", sa.String(length=160), nullable=False),
            sa.Column("actor", sa.String(length=160), nullable=False),
            sa.Column("usuario_id", sa.Uuid(), nullable=True),
            sa.Column("detalle", sa.Text(), nullable=True),
            sa.Column("hash", sa.String(length=64), nullable=False),
            sa.Column("hash_anterior", sa.String(length=64), nullable=False),
            sa.Column("correlation_id", sa.String(length=64), nullable=True),
            sa.Column(
                "cuando",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["organizacion_id"],
                ["organizaciones.id"],
                name="fk_auditoria_organizacion",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["proyecto_id"], ["proyectos.id"], name="fk_auditoria_proyecto", ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["usuario_id"], ["usuarios.id"], name="fk_auditoria_usuario", ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id", name="pk_eventos_auditoria"),
        )
    if not _existe_indice("eventos_auditoria", "ix_auditoria_organizacion_tipo"):
        op.create_index(
            "ix_auditoria_organizacion_tipo", "eventos_auditoria", ["organizacion_id", "tipo"]
        )
    if not _existe_indice("eventos_auditoria", "ix_auditoria_entidad"):
        op.create_index("ix_auditoria_entidad", "eventos_auditoria", ["entidad"])
    if not _existe_indice("eventos_auditoria", "ix_eventos_auditoria_correlation_id"):
        op.create_index(
            "ix_eventos_auditoria_correlation_id", "eventos_auditoria", ["correlation_id"]
        )

    # --- adjuntos -------------------------------------------------------
    if not _existe("adjuntos"):
        op.create_table(
            "adjuntos",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("organizacion_id", sa.Uuid(), nullable=False),
            # La evidencia siempre pertenece a un proyecto: la política de
            # RBAC que autoriza la carga tiene ámbito de proyecto, así que
            # admitir un adjunto huérfano dejaría filas que ninguna
            # consulta de permisos podría acotar.
            sa.Column("proyecto_id", sa.Uuid(), nullable=False),
            sa.Column("nombre", sa.String(length=320), nullable=False),
            sa.Column("content_type", sa.String(length=160), nullable=True),
            sa.Column("tamano_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("clave_objeto", sa.String(length=512), nullable=False),
            sa.Column("version", sa.String(length=64), nullable=True),
            sa.Column("disponible", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("subido_por_id", sa.Uuid(), nullable=True),
            sa.Column(
                "creado_en",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["organizacion_id"],
                ["organizaciones.id"],
                name="fk_adjuntos_organizacion",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["proyecto_id"], ["proyectos.id"], name="fk_adjuntos_proyecto", ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["subido_por_id"],
                ["usuarios.id"],
                name="fk_adjuntos_usuario",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_adjuntos"),
        )
    if not _existe_indice("adjuntos", "ix_adjuntos_sha256"):
        op.create_index("ix_adjuntos_sha256", "adjuntos", ["sha256"])
    if not _existe_indice("adjuntos", "ix_adjuntos_proyecto"):
        op.create_index("ix_adjuntos_proyecto", "adjuntos", ["proyecto_id"])

    # --- índice compuesto de proyectos --------------------------------
    if not _existe_indice("proyectos", "ix_proyectos_organizacion_estado"):
        op.create_index(
            "ix_proyectos_organizacion_estado", "proyectos", ["organizacion_id", "estado"]
        )


def downgrade() -> None:
    """Revierte el esquema de la Épica 0."""
    inspector = sa.inspect(op.get_bind())
    for tabla, indices in (
        ("proyectos", ["ix_proyectos_organizacion_estado"]),
        ("adjuntos", ["ix_adjuntos_proyecto", "ix_adjuntos_sha256"]),
        ("eventos_auditoria", ["ix_eventos_auditoria_correlation_id",
                               "ix_auditoria_entidad", "ix_auditoria_organizacion_tipo"]),
        ("sesiones", ["ix_sesiones_usuario", "ix_sesiones_familia"]),
        ("membresias_proyecto", ["ix_membresias_proyecto_rol_id",
                                 "ix_membresias_proyecto_proyecto_id",
                                 "ix_membresias_proyecto_usuario_id"]),
    ):
        if tabla not in inspector.get_table_names():
            continue
        for indice in indices:
            if _existe_indice(tabla, indice):
                op.drop_index(indice, table_name=tabla)
    for tabla in ("adjuntos", "eventos_auditoria", "sesiones", "membresias_proyecto"):
        if _existe(tabla):
            op.drop_table(tabla)
    _retirar_roles()
    for columna in ("eliminado_en", "plantilla"):
        if _existe_columna("proyectos", columna):
            op.drop_column("proyectos", columna)
    for columna in ("proveedor_identidad", "password_hash"):
        if _existe_columna("usuarios", columna):
            op.drop_column("usuarios", columna)
    if _existe_indice("roles", "ix_roles_nivel"):
        op.drop_index("ix_roles_nivel", table_name="roles")
    for columna in ("nivel", "ambito"):
        if _existe_columna("roles", columna):
            op.drop_column("roles", columna)
