"""Projecta PMO Intelligence — Projecta Capture: tablas del MVP de captura en navegador.

Añade las entidades del MVP de captura:

* ``sesiones_de_captura`` — la sesión, con su estado de captura y sus dos
  modos (solicitado y efectivo).
* ``participantes_de_sesion`` — los participantes, declarados a mano.
* ``consentimientos_de_captura`` — la declaración de autorización del
  operador y el registro de información a los participantes.
* ``artefactos_de_audio`` — la grabación, referenciada, no incrustada.
* ``fragmentos_de_subida`` — los fragmentos, con su secuencia y su huella.
* ``trabajos_de_procesamiento`` — los trabajos de transcripción y análisis.
* ``transcripciones`` y ``segmentos_de_transcripcion`` — el texto y sus
  tramos con hablante y tiempo.
* ``mapeos_de_hablante`` — el vínculo entre etiqueta e identidad, hecho a
  mano.
* ``notas_de_reunion`` — las notas manuales con su marca de tiempo.
* ``elementos_extraidos`` y ``evidencias_de_item`` — los resultados del
  análisis y su respaldo.
* ``acciones_de_revision`` — el historial de las correcciones humanas.
* ``politicas_de_retencion`` — la conservación y el borrado.

La entidad de auditoría **no** se crea aquí: se reutiliza
``eventos_auditoria``, que ya tiene cadena de hash y tipos validados.

La migración es **idempotente**, como las anteriores: aplicarla dos veces
no cambia el esquema y no falla.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
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


#: Todas las tablas de esta migración, en el orden en que se crean. El
#: orden importa: las tablas referenciadas se crean antes que las que las
#: referencian.
TABLAS: tuple[str, ...] = (
    "sesiones_de_captura",
    "participantes_de_sesion",
    "consentimientos_de_captura",
    "artefactos_de_audio",
    "fragmentos_de_subida",
    "trabajos_de_procesamiento",
    "transcripciones",
    "segmentos_de_transcripcion",
    "mapeos_de_hablante",
    "notas_de_reunion",
    "elementos_extraidos",
    "evidencias_de_item",
    "acciones_de_revision",
    "politicas_de_retencion",
)


def upgrade() -> None:
    """Crea las tablas de la captura."""
    if _existe("sesiones_de_captura"):
        return

    op.create_table(
        "sesiones_de_captura",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organizacion_id", sa.Uuid(), nullable=False),
        sa.Column("proyecto_id", sa.Uuid(), nullable=False),
        sa.Column("titulo", sa.String(length=200), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=False, server_default=""),
        sa.Column("finalidad", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("estado", sa.String(length=32), nullable=False, server_default="DRAFT"),
        sa.Column("modo_solicitado", sa.String(length=32), nullable=False, server_default="MIC_ONLY"),
        sa.Column("modo_efectivo", sa.String(length=32), nullable=True),
        sa.Column("captura_completa", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("detencion_inmediata", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("inicio_captura", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fin_captura", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duracion_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("creado_por", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "actualizado_en",
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
            ["proyecto_id"], ["proyectos.id"], ondelete="CASCADE", onupdate="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["creado_por"], ["usuarios.id"], ondelete="SET NULL", onupdate="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Los estados son un conjunto **cerrado**: la base de datos no
        # admite un estado inventado, ni siquiera por SQL directo.
        sa.CheckConstraint(
            "estado IN ('DRAFT', 'READY', 'RECORDING', 'PAUSED', "
            "'STOPPED', 'CANCELLED', 'FAILED')",
            name="estado",
        ),
    )

    op.create_table(
        "participantes_de_sesion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("nombre", sa.String(length=160), nullable=False),
        sa.Column("rol", sa.String(length=80), nullable=True),
        sa.Column("identidad", sa.String(length=256), nullable=True),
        sa.Column("organizador", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("informado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sesion_id", "nombre", name="uq_participantes_sesion_nombre"),
    )

    op.create_table(
        "consentimientos_de_captura",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=48), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("aceptado", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("declarado_por", sa.Uuid(), nullable=True),
        sa.Column("declarado_por_nombre", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["declarado_por"], ["usuarios.id"], ondelete="SET NULL", onupdate="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "artefactos_de_audio",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("formato", sa.String(length=16), nullable=False),
        sa.Column("codec", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("estado", sa.String(length=24), nullable=False, server_default="pendiente"),
        sa.Column("clave_objeto", sa.String(length=512), nullable=True),
        sa.Column("tamano_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duracion_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fragmentos_totales", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("huella", sa.String(length=64), nullable=True),
        sa.Column("finalizado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "fragmentos_de_subida",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artefacto_id", sa.Uuid(), nullable=False),
        sa.Column("indice", sa.Integer(), nullable=False),
        sa.Column("tamano_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("huella", sa.String(length=64), nullable=False),
        sa.Column("clave_objeto", sa.String(length=512), nullable=True),
        sa.Column("verificado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "recibido_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["artefacto_id"],
            ["artefactos_de_audio.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artefacto_id", "indice", name="uq_fragmentos_artefacto_indice"),
    )

    op.create_table(
        "trabajos_de_procesamiento",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("artefacto_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=32), nullable=False),
        sa.Column("estado", sa.String(length=32), nullable=False, server_default="NOT_STARTED"),
        sa.Column("proveedor", sa.String(length=64), nullable=False, server_default="simulado"),
        sa.Column("id_externo", sa.String(length=128), nullable=True),
        sa.Column("progreso", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("tarea_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artefacto_id"],
            ["artefactos_de_audio.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        # El estado del trabajo es un conjunto **cerrado**, igual que
        # el de la sesión: la base de datos no admite un estado
        # inventado, ni siquiera por SQL directo.
        sa.CheckConstraint(
            "estado IN ('NOT_STARTED', 'UPLOADING', 'UPLOADED', "
            "'TRANSCRIBING', 'TRANSCRIBED', 'ANALYZING', "
            "'REVIEW_REQUIRED', 'APPROVED', 'FAILED')",
            name="estado",
        ),
        sa.UniqueConstraint("artefacto_id", "tipo", name="uq_trabajos_artefacto_tipo"),
    )

    op.create_table(
        "transcripciones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("artefacto_id", sa.Uuid(), nullable=False),
        sa.Column("idioma", sa.String(length=8), nullable=False, server_default="es"),
        sa.Column("texto", sa.Text(), nullable=False, server_default=""),
        sa.Column("revisada", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("editada", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confianza", sa.Float(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artefacto_id"],
            ["artefactos_de_audio.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artefacto_id", name="uq_transcripciones_artefacto"),
    )

    op.create_table(
        "segmentos_de_transcripcion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transcripcion_id", sa.Uuid(), nullable=False),
        sa.Column("indice", sa.Integer(), nullable=False),
        sa.Column("hablante", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("inicio_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fin_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("texto", sa.Text(), nullable=False, server_default=""),
        sa.Column("confianza", sa.Float(), nullable=True),
        sa.Column("corregido", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["transcripcion_id"],
            ["transcripciones.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transcripcion_id", "indice", name="uq_segmentos_transcripcion_indice"),
    )

    op.create_table(
        "mapeos_de_hablante",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transcripcion_id", sa.Uuid(), nullable=False),
        sa.Column("hablante", sa.String(length=80), nullable=False),
        sa.Column("identidad", sa.String(length=256), nullable=True),
        sa.Column("nombre", sa.String(length=160), nullable=True),
        sa.Column("validado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("validado_por", sa.Uuid(), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["transcripcion_id"],
            ["transcripciones.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["validado_por"], ["usuarios.id"], ondelete="SET NULL", onupdate="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "transcripcion_id", "hablante", name="uq_mapeos_transcripcion_hablante"
        ),
    )

    op.create_table(
        "notas_de_reunion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("marca_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("autor_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["autor_id"], ["usuarios.id"], ondelete="SET NULL", onupdate="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "elementos_extraidos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("clave", sa.String(length=64), nullable=False),
        sa.Column("tipo", sa.String(length=32), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=False),
        sa.Column("responsable_id", sa.Uuid(), nullable=True),
        sa.Column("hablante", sa.String(length=80), nullable=True),
        sa.Column("fecha_propuesta", sa.String(length=32), nullable=True),
        sa.Column("marca_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("segmentos_evidencia", sa.JSON(), nullable=False),
        sa.Column("requiere_revision", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("motivo_revision", sa.Text(), nullable=True),
        sa.Column("estado_revision", sa.String(length=24), nullable=False, server_default="pendiente"),
        sa.Column("aprobado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("texto_corregido", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["responsable_id"],
            ["participantes_de_sesion.id"],
            ondelete="SET NULL",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "evidencias_de_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("elemento_id", sa.Uuid(), nullable=False),
        sa.Column("segmento_indice", sa.Integer(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("marca_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("desactualizada", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["elemento_id"],
            ["elementos_extraidos.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "acciones_de_revision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("entidad_tipo", sa.String(length=24), nullable=False),
        sa.Column("entidad_id", sa.Uuid(), nullable=False),
        sa.Column("accion", sa.String(length=24), nullable=False),
        sa.Column("texto_anterior", sa.Text(), nullable=True),
        sa.Column("texto_nuevo", sa.Text(), nullable=True),
        sa.Column("motivo", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entidad_id"],
            ["elementos_extraidos.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["usuarios.id"], ondelete="SET NULL", onupdate="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "politicas_de_retencion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("dias_transcripcion", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("dias_elementos", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("dias_audio", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dias_notas", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("conservar_audio", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("purga_programada_para", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultima_purga_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["sesion_id"],
            ["sesiones_de_captura.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sesion_id", name="uq_politicas_retencion_sesion"),
    )

    # --- índices ------------------------------------------------------------
    indices: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("ix_sesiones_captura_organizacion_estado", ("sesiones_de_captura", "organizacion_id", "estado")),
        ("ix_sesiones_captura_proyecto_creado", ("sesiones_de_captura", "proyecto_id", "creado_en")),
        ("ix_consentimientos_sesion_tipo", ("consentimientos_de_captura", "sesion_id", "tipo")),
        ("ix_artefactos_sesion_estado", ("artefactos_de_audio", "sesion_id", "estado")),
        ("ix_trabajos_estado", ("trabajos_de_procesamiento", "estado")),
        ("ix_notas_sesion_creado", ("notas_de_reunion", "sesion_id", "creado_en")),
        ("ix_elementos_sesion_tipo", ("elementos_extraidos", "sesion_id", "tipo")),
        ("ix_elementos_revision", ("elementos_extraidos", "requiere_revision")),
        ("ix_evidencias_item", ("evidencias_de_item", "elemento_id")),
        ("ix_acciones_revision_entidad", ("acciones_de_revision", "entidad_tipo", "entidad_id")),
    )
    for nombre, (tabla, *columnas) in indices:
        op.create_index(nombre, tabla, list(columnas), unique=False)

    for tabla, columna in (
        ("sesiones_de_captura", "organizacion_id"),
        ("sesiones_de_captura", "proyecto_id"),
        ("sesiones_de_captura", "creado_por"),
        ("participantes_de_sesion", "sesion_id"),
        ("consentimientos_de_captura", "sesion_id"),
        ("artefactos_de_audio", "sesion_id"),
        ("fragmentos_de_subida", "artefacto_id"),
        ("trabajos_de_procesamiento", "sesion_id"),
        ("trabajos_de_procesamiento", "artefacto_id"),
        ("transcripciones", "sesion_id"),
        ("transcripciones", "artefacto_id"),
        ("segmentos_de_transcripcion", "transcripcion_id"),
        ("mapeos_de_hablante", "transcripcion_id"),
        ("notas_de_reunion", "sesion_id"),
        ("elementos_extraidos", "sesion_id"),
        ("evidencias_de_item", "elemento_id"),
        ("acciones_de_revision", "sesion_id"),
        ("acciones_de_revision", "entidad_id"),
        ("politicas_de_retencion", "sesion_id"),
    ):
        op.create_index(
            f"ix_{tabla}_{columna}", tabla, [columna], unique=False
        )


def downgrade() -> None:
    """Retira las tablas de la captura.

    El orden es el inverso al de creación: primero las que referencian a
    otras, después las referenciadas.

    **No se borran ni los índices ni las restricciones a mano**, y el motivo
    es doble:

    * En PostgreSQL, una ``UniqueConstraint`` crea un índice único *propio*,
      y ese índice es de lo que depende la restricción. Intentar borrarlo
      con ``DROP INDEX`` falla con ``DependentObjectsStillExistError`` y
      deja la migración a medias.
    * En SQLite, ``ALTER TABLE ... DROP CONSTRAINT`` no existe: lanza
      ``NotImplementedError`` y pide el modo *batch*.

    No hace falta ninguna de las dos cosas. Las claves foráneas de estas
    tablas apuntan **hacia** otras (a ``organizaciones``, ``proyectos``,
    ``usuarios``) o **entre** tablas de la propia captura, y ninguna tabla
    externa apunta hacia estas. Al borrar la tabla se van sus índices, sus
    restricciones y sus claves foráneas con ella, y como el borrado es en
    orden inverso al de creación, ninguna se queda referencing a una tabla
    que ya no existe.
    """
    for tabla in reversed(TABLAS):
        if not _existe(tabla):
            continue
        op.drop_table(tabla)
