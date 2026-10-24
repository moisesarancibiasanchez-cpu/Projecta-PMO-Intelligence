"""Modelos ORM de Projecta PMO Intelligence.

Entidades del núcleo implementadas en la Épica 0 (HU_01 a HU_08):

* :class:`Organizacion` — organización cliente (tenant).
* :class:`Rol` — catálogo de roles con **nivel jerárquico**, que es la
  fuente de verdad del RBAC. El nivel se resuelve en runtime, nunca en el
  esquema.
* :class:`Usuario` — persona de la organización, con su rol.
* :class:`Proyecto` — proyecto, con código único dentro de la organización.
* :class:`MembresiaProyecto` — pertenencia de un usuario a un proyecto, con
  el rol que ejerce **dentro** de ese proyecto. Sin esta tabla, el ámbito
  de proyecto del RBAC no se puede resolver.
* :class:`Sesion` — refresh tokens rotativos, agrupados por familia para
  poder revocar la familia entera ante una reutilización.
* :class:`EventoAuditoria` — registro **append-only** con cadena de hash.
* :class:`Adjunto` — metadato de un fichero en el almacenamiento de objetos,
  con su SHA-256 y su disponibilidad.

Todas usan tipado ``Mapped[]`` de SQLAlchemy 2, nombres de tabla en
``snake_case`` plural y declaran de forma explícita la política
``ondelete`` de cada clave foránea.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import Base


class Organizacion(Base):
    """Organización cliente: contiene proyectos y usuarios."""

    __tablename__ = "organizaciones"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    proyectos: Mapped[list["Proyecto"]] = relationship(
        back_populates="organizacion",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    usuarios: Mapped[list["Usuario"]] = relationship(
        back_populates="organizacion",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<Organizacion {self.slug}>"


class Rol(Base):
    """Rol del catálogo normativo.

    ``nivel`` es el nivel jerárquico (0 a 5) que resuelve el RBAC en
    runtime. Ampliar el catálogo es insertar una fila, no desplegar código.
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    nivel: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    ambito: Mapped[str] = mapped_column(String(32), nullable=False, default="organizacion")
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="rol")

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<Rol {self.nombre} nivel {self.nivel}>"


class Usuario(Base):
    """Usuario de la plataforma, siempre ligado a una organización y a un rol."""

    __tablename__ = "usuarios"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    rol_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Hash bcrypt de la contraseña. Nunca el texto plano.
    password_hash: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Identificador del proveedor OIDC del que procede la identidad.
    proveedor_identidad: Mapped[str | None] = mapped_column(String(120), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organizacion: Mapped["Organizacion"] = relationship(back_populates="usuarios")
    rol: Mapped["Rol"] = relationship(back_populates="usuarios")
    membresias: Mapped[list["MembresiaProyecto"]] = relationship(
        back_populates="usuario", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<Usuario {self.email}>"


class Proyecto(Base):
    """Proyecto perteneciente a una organización.

    El código es único **dentro de la organización**, no globalmente: dos
    organizaciones distintas pueden tener un proyecto con el mismo código.
    """

    __tablename__ = "proyectos"
    __table_args__ = (
        UniqueConstraint("organizacion_id", "codigo", name="uq_proyectos_organizacion_codigo"),
        Index("ix_proyectos_organizacion_estado", "organizacion_id", "estado"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    codigo: Mapped[str] = mapped_column(String(64), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    estado: Mapped[str] = mapped_column(String(32), nullable=False, default="planificacion")
    #: Identificador de la plantilla de fases con la que se creó.
    plantilla: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Borrado lógico. Un proyecto eliminado no aparece en los listados.
    eliminado_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organizacion: Mapped["Organizacion"] = relationship(back_populates="proyectos")
    membresias: Mapped[list["MembresiaProyecto"]] = relationship(
        back_populates="proyecto", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<Proyecto {self.codigo}>"


class MembresiaProyecto(Base):
    """Pertenencia de un usuario a un proyecto, con su rol dentro de él.

    Es lo que resuelve el ámbito de proyecto del RBAC: un gerente de
    proyecto en X no opera sobre Y porque no hay fila aquí.
    """

    __tablename__ = "membresias_proyecto"
    __table_args__ = (
        UniqueConstraint("usuario_id", "proyecto_id", name="uq_membresias_usuario_proyecto"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("usuarios.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    proyecto_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proyectos.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    rol_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    usuario: Mapped["Usuario"] = relationship(back_populates="membresias")
    proyecto: Mapped["Proyecto"] = relationship(back_populates="membresias")
    rol: Mapped["Rol"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<MembresiaProyecto usuario={self.usuario_id} proyecto={self.proyecto_id}>"


class Sesion(Base):
    """Refresh token rotativo.

    Cada fila es un token. ``familia_id`` agrupa los tokens que descienden
    del mismo inicio de sesión: si uno de ellos se reutiliza, se revoca
    la familia entera, que es la señal de robo.
    """

    __tablename__ = "sesiones"
    __table_args__ = (
        Index("ix_sesiones_familia", "familia_id"),
        Index("ix_sesiones_usuario", "usuario_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    usuario_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("usuarios.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    familia_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 del token. El token en claro no se almacena nunca.
    hash_refresh: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Marca de uso: un token ya usado no puede volver a usarse.
    usado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<Sesion {self.id} familia={self.familia_id}>"


class EventoAuditoria(Base):
    """Evento de auditoría **append-only** con cadena de hash.

    Cada evento guarda el hash del anterior, de forma que alterar uno
    rompe la cadena y se detecta. La columna ``hash_anterior`` es la que
    encadena; ``hash`` es la huella del contenido de este evento.
    """

    __tablename__ = "eventos_auditoria"
    __table_args__ = (
        Index("ix_auditoria_organizacion_tipo", "organizacion_id", "tipo"),
        Index("ix_auditoria_entidad", "entidad"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    proyecto_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proyectos.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )
    tipo: Mapped[str] = mapped_column(String(64), nullable=False)
    entidad: Mapped[str] = mapped_column(String(160), nullable=False)
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
    )
    #: Texto legible del evento.
    detalle: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Huella SHA-256 del contenido del evento.
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Huella del evento anterior: es lo que encadena la cadena.
    hash_anterior: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cuando: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<EventoAuditoria {self.tipo} {self.entidad}>"


class Adjunto(Base):
    """Metadato de un fichero almacenado en el almacenamiento de objetos.

    El contenido vive en el bucket; aquí queda su SHA-256, su clave de
    objeto y su disponibilidad. ``sha256`` es lo que hace idempotente la
    carga: dos ficheros con el mismo contenido no se duplican.
    """

    __tablename__ = "adjuntos"
    __table_args__ = (
        Index("ix_adjuntos_sha256", "sha256"),
        Index("ix_adjuntos_proyecto", "proyecto_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: La evidencia **siempre** pertenece a un proyecto. La política de
    #: RBAC que autoriza la carga tiene ámbito de proyecto, así que
    #: admitir un adjunto sin proyecto dejaría una fila que ninguna
    #: consulta de permisos podría acotar.
    proyecto_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proyectos.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    nombre: Mapped[str] = mapped_column(String(320), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    tamano_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Huella SHA-256 del contenido, en hexadecimal.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Clave del objeto dentro del bucket.
    clave_objeto: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Versión del objeto, para el versionado del bucket.
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ``False`` cuando el objeto ya no está en el almacenamiento. El
    #: metadato se conserva y el listado no se rompe.
    disponible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    subido_por_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<Adjunto {self.nombre} sha256={self.sha256[:12]}…>"


__all__ = [
    "Adjunto",
    "ConsentimientoEdge",
    "DispositivoEdge",
    "EventoAuditoria",
    "EventoEdge",
    "MembresiaProyecto",
    "Organizacion",
    "Proyecto",
    "Rol",
    "Sesion",
    "Usuario",
]


# ══════════════════════════════════════════════════════════════════════════
# Edge Connector — prueba de concepto
# ══════════════════════════════════════════════════════════════════════════


class DispositivoEdge(Base):
    """Dispositivo del Edge Connector registrado en la organización.

    El dispositivo es la **identidad criptográfica** del conector: una
    clave pública Ed25519 con la que se verifica la firma de cada
    evento. El registro guarda la clave pública, nunca la privada: la
    privada no sale del perímetro del banco.

    ``huella_clave`` es el SHA-256 de la clave pública, y es lo que
    identifica al dispositivo de forma estable y publicly auditable.
    """

    __tablename__ = "dispositivos_edge"
    __table_args__ = (
        UniqueConstraint("huella_clave", name="uq_dispositivos_edge_huella"),
        Index("ix_dispositivos_edge_organizacion", "organizacion_id", "estado"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    #: SHA-256 en hexadecimal de la clave pública Ed25519.
    huella_clave: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Clave pública Ed25519 en hexadecimal. Nunca la privada.
    clave_publica: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="M0")
    #: ``activo`` o ``revocado``. Un dispositivo revocado no puede enviar.
    estado: Mapped[str] = mapped_column(String(32), nullable=False, default="activo")
    #: Modo de operación declarado. M0 solo admite ``READ_ONLY``.
    modo: Mapped[str] = mapped_column(String(32), nullable=False, default="READ_ONLY")
    #: Sistemas fuente habilitados, como lista.
    sistemas: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    registrado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revocado_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Último latido recibido. Lo usa el health agent.
    ultimo_latido_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Caducidad de la credencial, si la tiene. El health agent la avisa
    #: **antes** de que caduque, no cuando ya ha caducado.
    credencial_expira_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<DispositivoEdge {self.nombre} {self.estado}>"


class ConsentimientoEdge(Base):
    """Registro de consentimiento, el que la puerta consulta antes de leer.

    Es el registro que HU_11 exige para las transcripciones, extendido a
    cualquier recurso que el Edge Connector vaya a leer. Cada registro
    declara **la finalidad**: un uso distinto del consentido cierra la
    puerta, aunque el consentimiento exista.

    La ausencia de registro se trata como **ausencia de consentimiento**:
    la puerta falla cerrada, nunca por omisión.
    """

    __tablename__ = "consentimientos_edge"
    __table_args__ = (
        Index("ix_consentimientos_edge_organizacion", "organizacion_id", "recurso"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Identificador del recurso externo (por ejemplo, el identificador de
    #: una reunión en el sistema fuente).
    recurso: Mapped[str] = mapped_column(String(320), nullable=False)
    #: Tipo de recurso: ``reunion``, ``ticket``, ``canal``, ``documento``.
    recurso_tipo: Mapped[str] = mapped_column(String(64), nullable=False)
    #: **La finalidad declarada.** Es lo que se compara con el uso
    #: previsto: sin finalidad, no hay puerta que valga.
    finalidad: Mapped[str] = mapped_column(String(320), nullable=False)
    #: Base legal del tratamiento: ``interes_legitimo``, ``consentimiento``,
    #: ``contrato`` o ``obligacion_legal``.
    base_legal: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Destinatarios del dato, como lista. Un destinatario no previsto
    #: cierra la puerta igual que una finalidad distinta.
    destinatarios: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Proyecto de Projecta PMO Intelligence al que se refiere el
    #: consentimiento. Puede ser nulo para consentimientos de organización.
    proyecto_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proyectos.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )
    otorgado_por: Mapped[str] = mapped_column(String(320), nullable=False)
    otorgado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revocado_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: El consentimiento caduca. El health agent avisa antes.
    expira_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<ConsentimientoEdge {self.recurso} {self.finalidad}>"


class EventoEdge(Base):
    """Evento recibido del Edge Connector, ya firmado, minimizado y aceptado.

    La **huella** es el SHA-256 del contenido canónico del evento. Sirve
    para dos cosas: para la **idempotencia** —el mismo evento recibido dos
    veces no se procesa dos veces— y para la **trazabilidad**: cualquier
    auditor puede recalcularla y comprobar que el contenido no cambió.
    """

    __tablename__ = "eventos_edge"
    __table_args__ = (
        UniqueConstraint("dispositivo_id", "huella", name="uq_eventos_edge_huella"),
        Index("ix_eventos_edge_organizacion_tipo", "organizacion_id", "tipo"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    dispositivo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dispositivos_edge.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    proyecto_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proyectos.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )
    #: Tipo de evento. La lista es **cerrada**: un tipo fuera del catálogo
    #: se rechaza, no se acepta «por si acaso».
    tipo: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Contenido minimizado. Lo que se guardó es lo que se minimizó: los
    #: campos bloqueados no llegan aquí.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    #: SHA-256 del contenido canónico.
    huella: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``aceptado`` o ``rechazado``.
    decision: Mapped[str] = mapped_column(String(32), nullable=False, default="aceptado")
    #: Motivo del rechazo, si lo hubo.
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    recibido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<EventoEdge {self.tipo} {self.decision}>"


class ReunionCapturada(Base):
    """Una reunión de Teams capturada: su transcripción, su audio y su asistencia.

    La **idempotencia** la da la combinación de ``organizador`` y
    ``reunion_id``: la misma reunión no se vuelve a guardar dos veces,
    aunque la tarea se ejecute dos veces o el doble se reinicie.

    La **huella** es el SHA-256 del contenido canónico de la
    transcripción. Sirve para lo mismo que en el resto del proyecto: para
    saber si el contenido cambió, y para no reprocesar lo mismo.

    El audio **no** se guarda en la base de datos. Se guarda en el
    almacenamiento de objetos, y aquí queda solo su referencia y su
    tamaño: el binario en la base sería una decisión que no le
    corresponde a la base de datos.
    """

    __tablename__ = "reuniones_capturadas"
    __table_args__ = (
        UniqueConstraint(
            "organizador", "reunion_id", name="uq_reuniones_capturadas_organizador_reunion"
        ),
        Index("ix_reuniones_capturadas_organizacion_proyecto", "organizacion_id", "proyecto_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    proyecto_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proyectos.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )
    #: UPN o id del organizador de la reunión en Microsoft 365.
    organizador: Mapped[str] = mapped_column(String(256), nullable=False)
    #: Identificador de la reunión en Teams.
    reunion_id: Mapped[str] = mapped_column(String(256), nullable=False)
    #: Asunto de la reunión, si se conoce.
    asunto: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    #: Identificador de la transcripción en Graph.
    transcripcion_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    #: Segmentos de la transcripción, con hablante y marcas de tiempo.
    segmentos: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: Participantes, con su entrada y su salida.
    participantes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: Clave del audio en el almacenamiento de objetos, si se guardó.
    audio_objeto: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Formato del audio.
    formato_audio: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    #: Tamaño del audio en bytes.
    audio_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Número de segmentos de la transcripción.
    segmentos_totales: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: SHA-256 del contenido canónico de la transcripción.
    huella: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Inicio de la reunión.
    inicio: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Fin de la reunión.
    fin: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Instante de la captura.
    capturado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<ReunionCapturada {self.reunion_id} seg={self.segmentos_totales}>"


# Las entidades de Projecta Capture viven en su propio módulo, pero se
# registran aquí para que `Base.metadata` las conozca. Sin este import,
# `create_all` crearía el esquema sin las tablas de captura y las
# pruebas pasarían sin tocar la base de datos.
from app.core.db.models_captura import (  # noqa: E402,F401
    AccionDeRevision,
    ArtefactoDeAudio,
    ConsentimientoDeCaptura,
    ElementoExtraido,
    EvidenciaDeItem,
    FragmentoDeSubida,
    MapeoHablante,
    NotaDeReunion,
    ParticipanteDeSesion,
    PoliticaDeRetencion,
    SesionDeCaptura,
    SegmentoDeTranscripcion,
    TrabajoDeProcesamiento,
    Transcripcion,
)
