"""Modelo de datos de Projecta Capture.

Este módulo contiene las entidades del MVP de captura de reuniones en
navegador: la sesión, sus participantes, la declaración de autorización,
los artefactos de audio, los fragmentos de subida, los trabajos de
procesamiento, la transcripción con sus segmentos, el mapeo de
hablantes, las notas manuales, los elementos extraídos, las referencias
de evidencia, las acciones de revisión y la política de retención.

Por qué en un módulo aparte
---------------------------

``app.core.db.models`` ya tiene las entidades del núcleo: organización,
usuarios, roles, proyectos, compromisos, auditoría. Este módulo **no las
duplica**: las reutiliza. Las entidades de captura se separan porque
tienen un ciclo de vida distinto —nacen y mueren con la sesión, y se
borran según una política de retención— y porque son Mucho más
numerosas.

La entidad de auditoría **no** está aquí: se reutiliza
``eventos_auditoria``, que ya tiene la cadena de hash, el
``correlation_id`` y los tipos validados. La captura no necesita otra
cadena: necesita la misma.

Las tres separaciones del MVP, respetadas aquí
-----------------------------------------------

1. **El estado de captura y el estado de procesamiento son cosas
   distintas.** ``SesionDeCaptura.estado`` es el de la captura
   (``DRAFT``…``FAILED``) y ``TrabajoDeProcesamiento.estado`` es el del
   procesamiento (``NOT_STARTED``…``FAILED``). Parar la grabación **no**
   significa que la transcripción esté hecha.

2. **El modo solicitado y el modo obtenido son dos campos.** Se guarda
   lo que se pidió (``modo_solicitado``) y lo que se consiguió
   (``modo_efectivo``). Una sesión en la que solo se obtuvo el micrófono
   no es una captura completa, aunque se pidieran las dos fuentes.

3. **La etiqueta de hablante no es la identidad de la persona.** La
   transcripción habla de «Hablante 1»; el mapeo a una identidad real
   vive aparte, en ``MapeoHablante``, y se rellena **a mano**. Nadie
   deduce que «Hablante 1» es Ana solo porque Ana estaba en la lista de
   asistentes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base

__all__ = [
    "ACCIONES_DE_REVISION",
    "ESTADOS_DE_CAPTURA",
    "ESTADOS_DE_PROCESAMIENTO",
    "MODOS_DE_CAPTURA",
    "TIPOS_DE_ITEM",
    "ArtefactoDeAudio",
    "ConsentimientoDeCaptura",
    "ElementoExtraido",
    "EvidenciaDeItem",
    "FragmentoDeSubida",
    "MapeoHablante",
    "NotaDeReunion",
    "ParticipanteDeSesion",
    "PoliticaDeRetencion",
    "SesionDeCaptura",
    "TrabajoDeProcesamiento",
    "Transcripcion",
    "SegmentoDeTranscripcion",
]


# ─────────────────────────── catálogos ───────────────────────────

#: Estados de la **captura**. Son los del MVP, y son cerrados: un estado
#: fuera de esta lista no se acepta.
ESTADOS_DE_CAPTURA: frozenset[str] = frozenset(
    {
        "DRAFT",
        "READY",
        "RECORDING",
        "PAUSED",
        "STOPPED",
        "CANCELLED",
        "FAILED",
    }
)

#: Estados del **procesamiento**. Deliberadamente aparte de los de
#: captura: una grabación detenida no es una transcripción completada.
ESTADOS_DE_PROCESAMIENTO: frozenset[str] = frozenset(
    {
        "NOT_STARTED",
        "UPLOADING",
        "UPLOADED",
        "TRANSCRIBING",
        "TRANSCRIBED",
        "ANALYZING",
        "REVIEW_REQUIRED",
        "APPROVED",
        "FAILED",
    }
)

#: Modos de captura. El MVP define tres, y distingue el pedido del
#: obtenido.
MODOS_DE_CAPTURA: frozenset[str] = frozenset(
    {
        "MIC_ONLY",
        "SHARED_AUDIO_ONLY",
        "MIC_AND_SHARED_AUDIO",
    }
)

#: Tipos de elemento que la extracción puede producir. Cerrado, igual
#: que los estados: un tipo fuera de la lista no se acepta.
TIPOS_DE_ITEM: frozenset[str] = frozenset(
    {
        "acuerdo",
        "decision",
        "compromiso",
        "riesgo",
        "bloqueo",
        "dependencia",
        "pregunta_abierta",
        "tema",
    }
)

#: Acciones de revisión humana sobre un elemento extraído.
ACCIONES_DE_REVISION: frozenset[str] = frozenset(
    {
        "aprobado",
        "rechazado",
        "corregido",
        "pendiente",
    }
)


# ─────────────────────────── sesión ───────────────────────────


class SesionDeCaptura(Base):
    """Una sesión de captura: una reunión que se está grabando o se grabó.

    El **proyecto** es obligatorio: una sesión de captura siempre pertenece
    a un proyecto, porque su resultado son compromisos, riesgos y
    decisiones de ese proyecto. La evidencia siempre pertenece a un
    proyecto, y esa regla no se rompe aquí.

    El **estado** es el de la captura. El estado del procesamiento está
    en :class:`TrabajoDeProcesamiento`, porque son dos máquinas
    distintas.
    """

    __tablename__ = "sesiones_de_captura"
    __table_args__ = (
        Index("ix_sesiones_captura_organizacion_estado", "organizacion_id", "estado"),
        Index("ix_sesiones_captura_proyecto_creado", "proyecto_id", "creado_en"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizaciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    proyecto_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proyectos.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Título de la sesión.
    titulo: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Descripción o Purpose de la sesión.
    descripcion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Para qué se graba. Se registra porque el propósito determina qué
    #: se puede hacer con el resultado.
    finalidad: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    #: Estado de la **captura**.
    estado: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    #: Modo **solicitado**: el que el operador pidió.
    modo_solicitado: Mapped[str] = mapped_column(String(32), nullable=False, default="MIC_ONLY")
    #: Modo **efectivo**: el que se consiguió. Puede ser distinto, y en
    #: ese caso la captura **no** es completa.
    modo_efectivo: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: `True` solo si se obtuvieron **todas** las fuentes pedidas. Una
    #: sesión sin audio compartido no es una captura completa aunque se
    #: haya pedido.
    captura_completa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Si la captura se ha detenido de forma inmediata y forzada.
    detencion_inmediata: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Momento en que empezó a grabarse.
    inicio_captura: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Momento en que se detuvo.
    fin_captura: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Duración acumulada de la grabación, en milisegundos. Se acumula en
    #: las pausas, porque el tiempo grabado no es el tiempo transcurrido.
    duracion_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Constructor de la sesión, para la trazabilidad.
    creado_por: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<SesionDeCaptura {self.id} {self.estado}>"


class ParticipanteDeSesion(Base):
    """Alguien que participa en la sesión.

    Los participantes se registran **a mano**, antes de empezar. Nadie los
    deduce de una lista de asistentes, y de su nombre no se deduce su
    identidad: por eso hay una columna explícita de identidad opcional.
    """

    __tablename__ = "participantes_de_sesion"
    __table_args__ = (
        UniqueConstraint("sesion_id", "nombre", name="uq_participantes_sesion_nombre"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Nombre que escribe el operador.
    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Rol en la reunión. Lo pone el operador; no se deduce.
    rol: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Identidad de la plataforma, si se conoce. **Opcional y explícita**:
    #: el nombre escrito no es una identidad.
    identidad: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: `True` si es el organizador de la sesión.
    organizador: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: `True` si se ha informado de la grabación. Lo declara el
    #: organizador; el sistema no lo puede saber.
    informado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<ParticipanteDeSesion {self.nombre}>"


class ConsentimientoDeCaptura(Base):
    """La declaración de autorización del operador.

    Esto es lo que el MVP exige antes de empezar a grabar: el operador
    **declara** que la sesión está autorizada. No es un consentimiento
    automático de los participantes, y el propio campo lo dice: se
    llama ``declaracion_autorizacion`` y no ``consentimiento_participantes``.

    La distinción importa y no es un matiz: un sistema que presentara
    esta declaración como «los participantes han consentido» estaría
    afirmando algo que no ha comprobado.
    """

    __tablename__ = "consentimientos_de_captura"
    __table_args__ = (
        Index("ix_consentimientos_sesion_tipo", "sesion_id", "tipo"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: `declaracion_autorizacion` o `informacion_participantes`.
    tipo: Mapped[str] = mapped_column(String(48), nullable=False)
    #: Texto exacto que se aceptó, para poder auditarlo.
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    #: `True` si se aceptó; `False` si se retiró.
    aceptado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Quién declaró la autorización.
    declarado_por: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
    )
    #: Nombre del declarante, para el registro legible aunque el usuario
    #: se borre.
    declarado_por_nombre: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<ConsentimientoDeCaptura {self.tipo} aceptado={self.aceptado}>"


# ─────────────────────────── audio ───────────────────────────


class ArtefactoDeAudio(Base):
    """Una grabación de una sesión.

    El audio **no** se guarda en la base de datos: se guarda en el
    almacenamiento de objetos, y aquí queda su referencia, su tamaño y
    su huella. Un binario en la base de datos es una decisión que no le
    corresponde a la base de datos.

    Los **fragmentos** son las partes de la subida. Un fragmento de
    ``MediaRecorder`` no es un fichero reproducible por sí solo, y por
    eso hay una tabla de fragmentos con su secuencia y su huella: la
    reconstrucción se hace en el servidor, no en el navegador.
    """

    __tablename__ = "artefactos_de_audio"
    __table_args__ = (
        Index("ix_artefactos_sesion_estado", "sesion_id", "estado"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Formato declarado por el navegador: `webm`, `mp4`, `ogg`.
    formato: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Códec declarado: `opus`, `aac`, `vorbis`...
    codec: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: Estado de la subida: `pendiente`, `subiendo`, `completo`, `fallido`.
    estado: Mapped[str] = mapped_column(String(24), nullable=False, default="pendiente")
    #: Clave del objeto en el almacenamiento, cuando la subida termina.
    clave_objeto: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Tamaño total en bytes.
    tamano_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Duración en milisegundos.
    duracion_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Número de fragmentos esperados. Cero mientras la captura sigue.
    fragmentos_totales: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: SHA-256 del artefacto completo, para verificar la integridad.
    huella: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: `True` cuando la subida se ha finalizado explícitamente. Sin
    #: esto, el artefacto queda «en curso» y no se puede procesar.
    finalizado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<ArtefactoDeAudio {self.id} {self.estado} {self.tamano_bytes}B>"


class FragmentoDeSubida(Base):
    """Un fragmento de la grabación, con su secuencia y su huella.

    La **idempotencia** la da la restricción de unicidad sobre
    ``(artefacto_id, indice)``: un fragmento reenviado no se guarda dos
    veces. Y la **integridad** la comprueba la huella: el servidor
    verifica cada fragmento al recibirlo y puede detectar huecos y
    duplicados al final.
    """

    __tablename__ = "fragmentos_de_subida"
    __table_args__ = (
        UniqueConstraint("artefacto_id", "indice", name="uq_fragmentos_artefacto_indice"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    artefacto_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artefactos_de_audio.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Posición del fragmento en la secuencia, desde cero.
    indice: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Tamaño del fragmento en bytes.
    tamano_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: SHA-256 del fragmento, verificado al recibirlo.
    huella: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Clave del objeto en el almacenamiento, si se guardó el fragmento.
    clave_objeto: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: `True` si el fragmento se ha verificado contra su huella.
    verificado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recibido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<FragmentoDeSubida #{self.indice} {self.tamano_bytes}B>"


# ─────────────────────────── procesamiento ───────────────────────────


class TrabajoDeProcesamiento(Base):
    """Un trabajo de transcripción y análisis de un artefacto de audio.

    Es un trabajo **desacoplado**: se encola y se consulta. Nunca se
    procesa dentro de una petición HTTP síncrona, porque una transcripción
    tarda y una petición que espera minutes se corta.

    La **idempotencia** la da la restricción de unicidad sobre
    ``(artefacto_id, tipo)``: pedir dos veces el mismo trabajo no crea
    dos.
    """

    __tablename__ = "trabajos_de_procesamiento"
    __table_args__ = (
        UniqueConstraint("artefacto_id", "tipo", name="uq_trabajos_artefacto_tipo"),
        Index("ix_trabajos_estado", "estado"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    artefacto_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artefactos_de_audio.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: `transcripcion` o `analisis`.
    tipo: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Estado del procesamiento.
    estado: Mapped[str] = mapped_column(String(32), nullable=False, default="NOT_STARTED")
    #: Proveedor que hace el trabajo, para poder auditar quién transcribió.
    proveedor: Mapped[str] = mapped_column(String(64), nullable=False, default="simulado")
    #: Identificador del trabajo en el proveedor.
    id_externo: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Progreso de 0 a 100. `None` cuando el proveedor no lo da.
    progreso: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Mensaje de error, si lo hubo.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Identificador de la tarea de Celery que lo ejecuta.
    tarea_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<TrabajoDeProcesamiento {self.tipo} {self.estado}>"


class Transcripcion(Base):
    """La transcripción de un artefacto de audio.

    Una sola transcripción por artefacto: si se vuelve a transcribir, se
    sustituye, y el texto anterior se conserva en el historial de
    revisiones, no aquí.
    """

    __tablename__ = "transcripciones"
    __table_args__ = (
        UniqueConstraint("artefacto_id", name="uq_transcripciones_artefacto"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    artefacto_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artefactos_de_audio.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Idioma de la transcripción.
    idioma: Mapped[str] = mapped_column(String(8), nullable=False, default="es")
    #: Texto completo, para la búsqueda y la exportación.
    texto: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: `True` si alguien ha revisado y corregido el texto.
    revisada: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: `True` si el texto se ha editado a mano.
    editada: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Confianza global, solo si el proveedor la entrega.
    confianza: Mapped[float | None] = mapped_column(nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<Transcripcion {self.id} {self.idioma}>"


class SegmentoDeTranscripcion(Base):
    """Un segmento de la transcripción, con su hablante y su tiempo.

    El ``hablante`` es una **etiqueta** («Hablante 1»), no una
    identidad. El mapeo a una identidad real está en
    :class:`MapeoHablante` y se establece a mano.
    """

    __tablename__ = "segmentos_de_transcripcion"
    __table_args__ = (
        UniqueConstraint("transcripcion_id", "indice", name="uq_segmentos_transcripcion_indice"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transcripcion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcripciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Posición del segmento en la transcripción, desde cero.
    indice: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Etiqueta del hablante: «Hablante 1», «Hablante 2»...
    hablante: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    #: Inicio del segmento, en milisegundos desde el principio.
    inicio_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Fin del segmento, en milisegundos desde el principio.
    fin_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Texto del segmento.
    texto: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Confianza del segmento, solo si el proveedor la entrega.
    confianza: Mapped[float | None] = mapped_column(nullable=True)
    #: `True` si una persona ha corregido este segmento.
    corregido: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<SegmentoDeTranscripcion #{self.indice} {self.hablante}>"


class MapeoHablante(Base):
    """El vínculo entre una etiqueta de hablante y una identidad.

    Lo establece **una persona**, revisando la transcripción. Nadie
    deduce que «Hablante 1» es alguien concreto, y el sistema no lo
    hace ni aunque la lista de participantes tenga una sola persona.
    """

    __tablename__ = "mapeos_de_hablante"
    __table_args__ = (
        UniqueConstraint("transcripcion_id", "hablante", name="uq_mapeos_transcripcion_hablante"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transcripcion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcripciones.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: La etiqueta, tal y como aparece en la transcripción.
    hablante: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Identidad del participante, si se ha confirmado.
    identidad: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: Nombre legible del participante, si se ha confirmado.
    nombre: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: `True` si alguien ha validado el vínculo.
    validado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Quién lo validó.
    validado_por: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<MapeoHablante {self.hablante} -> {self.nombre or 'sin validar'}>"


class NotaDeReunion(Base):
    """Una nota manual escrita durante la sesión, con su marca de tiempo.

    Las notas son **humanas**: no las genera el modelo, las escribe la
    persona que está en la reunión. Por eso guardan la marca de tiempo en
    la que se escribieron, que es lo que las hace útiles después.
    """

    __tablename__ = "notas_de_reunion"
    __table_args__ = (
        Index("ix_notas_sesion_creado", "sesion_id", "creado_en"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Texto de la nota.
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    #: Marca de tiempo en la que se escribió, en milisegundos.
    marca_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Quién la escribió.
    autor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<NotaDeReunion @{self.marca_ms}ms>"


# ─────────────────────────── extracción ───────────────────────────


class ElementoExtraido(Base):
    """Un elemento extraído de la transcripción: un acuerdo, un riesgo...

    Cada elemento guarda **la evidencia** que lo sustenta: qué segmentos
    lo respaldan y en qué momento se dijo. Un elemento sin evidencia no
    es un elemento: es una suposición.

    Y guarda la **incertidumbre**: cuando el responsable o la fecha no
    están claramente en el texto, van a ``None`` y ``requiere_revision``
    queda a ``True`` con su motivo. El sistema **no rellena** lo que no
    sabe.
    """

    __tablename__ = "elementos_extraidos"
    __table_args__ = (
        Index("ix_elementos_sesion_tipo", "sesion_id", "tipo"),
        Index("ix_elementos_revision", "requiere_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Identificador **estable y determinista** del elemento. Es lo que
    #: permite deduplicar dos ejecuciones del análisis.
    clave: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Tipo del elemento: de :data:`TIPOS_DE_ITEM`.
    tipo: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Descripción del elemento.
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    #: Identificador del participante propuesto como responsable. ``None``
    #: si el texto no lo sostiene de forma unívoca.
    responsable_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("participantes_de_sesion.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
    )
    #: Etiqueta del hablante al que se atribuye, no la identidad.
    hablante: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Fecha propuesta, en ISO 8601. ``None`` si no está en el texto.
    fecha_propuesta: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Marca de tiempo del respaldo, en milisegundos.
    marca_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Índices de los segmentos que lo sustentan.
    segmentos_evidencia: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    #: `True` si necesita revisión humana antes de darse por bueno.
    requiere_revision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Por qué necesita revisión, si la necesita.
    motivo_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Estado de la revisión: de :data:`ACCIONES_DE_REVISION`.
    estado_revision: Mapped[str] = mapped_column(String(24), nullable=False, default="pendiente")
    #: `True` si se ha aprobado.
    aprobado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Texto corregido por una persona, si lo hubo.
    texto_corregido: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<ElementoExtraido {self.tipo} {self.clave[:12]}>"


class EvidenciaDeItem(Base):
    """El vínculo explícito entre un elemento extraído y su evidencia.

    La tabla guarda el **texto exacto** del segmento que lo sustenta, no
    solo su índice. Si la transcripción se corrige, el texto guardado
    deja de coincidir, y eso es detectable: la evidencia deja de
    corresponder a lo que el elemento dice.
    """

    __tablename__ = "evidencias_de_item"
    __table_args__ = (
        Index("ix_evidencias_item", "elemento_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    elemento_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elementos_extraidos.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Índice del segmento de la transcripción.
    segmento_indice: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Texto exacto del segmento en el momento de la extracción.
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    #: Marca de tiempo del segmento, en milisegundos.
    marca_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: `True` si el texto guardado ya no coincide con el segmento
    #: actual, porque la transcripción se editó después.
    desactualizada: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<EvidenciaDeItem seg={self.segmento_indice}>"


class AccionDeRevision(Base):
    """Una acción de revisión humana sobre un elemento o un segmento.

    Es el **historial**: quién aprobó, quién rechazó, quién corrigió y
    cuándo. Sin historial, una corrección es indistinguible de un
    error.
    """

    __tablename__ = "acciones_de_revision"
    __table_args__ = (
        Index("ix_acciones_revision_entidad", "entidad_tipo", "entidad_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: `elemento` o `segmento`.
    entidad_tipo: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Identificador del elemento o del segmento revisado.
    entidad_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elementos_extraidos.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Acción: de :data:`ACCIONES_DE_REVISION`.
    accion: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Texto corregido, si la acción fue una corrección.
    texto_anterior: Mapped[str | None] = mapped_column(Text, nullable=True)
    texto_nuevo: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Motivo de la corrección o del rechazo.
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Quién revisó.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL", onupdate="CASCADE"),
        nullable=True,
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<AccionDeRevision {self.accion} {self.entidad_tipo}>"


# ─────────────────────────── retención ───────────────────────────


class PoliticaDeRetencion(Base):
    """La política de conservación que aplica a las sesiones de captura.

    Los valores son **días**, y el borrado se hace con una tarea
    periódica que busca lo que ha caducado. La política se guarda por
    sesión, con una política de organización por defecto, para que el
    borrado sea una decisión explícita y no un comportamiento
    implícito.

    ``conservar_audio`` es lo que decide si el audio original se
    conserva o se elimina al transcribir. Por defecto **no** se
    conserva: el valor por defecto de un sistema de captura es no dejar
    el audio original dando vueltas.
    """

    __tablename__ = "politicas_de_retencion"
    __table_args__ = (
        UniqueConstraint("sesion_id", name="uq_politicas_retencion_sesion"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sesion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sesiones_de_captura.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Días que se conserva la transcripción.
    dias_transcripcion: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    #: Días que se conservan los elementos extraídos.
    dias_elementos: Mapped[int] = mapped_column(Integer, nullable=False, default=365)
    #: Días que se conserva el audio original. Cero = se elimina al
    #: transcribir.
    dias_audio: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Días que se conservan las notas manuales.
    dias_notas: Mapped[int] = mapped_column(Integer, nullable=False, default=365)
    #: `True` si el audio original se conserva tras transcribir.
    conservar_audio: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Momento en que se calculó la próxima purga.
    purga_programada_para: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    #: Momento de la última purga efectiva.
    ultima_purga_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<PoliticaDeRetencion audio={self.dias_audio}d tx={self.dias_transcripcion}d>"
