"""Máquina de estados de Projecta Capture.

Son **dos** máquinas, y no una. El MVP las separa de forma explícita,
y la razón es que confundirlas produce un error que no se ve:

* **El estado de captura** es el del dispositivo: si se está grabando,
  si está en pausa, si se ha parado.
* **El estado de procesamiento** es el del servidor: si la transcripción
  se está haciendo, si ya está, si hay que revisarla.

«Grabación detenida» **no** es «transcripción completada». Una sesión
puede estar ``STOPPED`` y su transcripción ``NOT_STARTED``, y las dos
cosas son ciertas a la vez. Si se mezclaran en un único campo, o bien
se perdería el estado real de la captura, o bien se anunciaría una
transcripción que no existe.

Las transiciones son **cerradas**. Una operación que no esté en la
tabla se rechaza, y el error dice cuál era el estado y cuál se pedía.
Eso permite que el navegador y el servidor coincidan: el navegador
dibuja el botón de «iniciar» solo cuando el servidor lo permitiría.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ESTADOS_DE_CAPTURA",
    "ESTADOS_DE_PROCESAMIENTO",
    "ErrorDeTransicion",
    "MODOS_DE_CAPTURA",
    "transicion_captura_permitida",
    "transicion_procesamiento_permitida",
    "transiciones_de_captura",
    "transiciones_de_procesamiento",
]


class ErrorDeTransicion(Exception):
    """La transición solicitada no está permitida.

    Lleve los dos estados: el que había y el que se pedía. Un error que
    solo dice «no se puede» no ayuda a depurar ni a explicar al
    operador qué tiene que hacer.
    """

    def __init__(self, estado_actual: str, estado_destino: str, maquina: str) -> None:
        self.estado_actual = estado_actual
        self.estado_destino = estado_destino
        self.maquina = maquina
        super().__init__(
            f"En la máquina de {maquina} no se puede pasar de "
            f"'{estado_actual}' a '{estado_destino}'."
        )


#: Estados de la captura.
ESTADOS_DE_CAPTURA: Final[frozenset[str]] = frozenset(
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

#: Estados del procesamiento.
ESTADOS_DE_PROCESAMIENTO: Final[frozenset[str]] = frozenset(
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

#: Modos de captura: el solicitado y el efectivamente obtenido.
MODOS_DE_CAPTURA: Final[frozenset[str]] = frozenset(
    {
        "MIC_ONLY",
        "SHARED_AUDIO_ONLY",
        "MIC_AND_SHARED_AUDIO",
    }
)

#: Transiciones válidas de la **captura**.
#:
#: Las reglas que hay detrás, en una línea cada una:
#:
#: * ``DRAFT`` → ``READY`` cuando la sesión tiene sus datos y la
#:   declaración de autorización está registrada.
#: * ``READY`` → ``RECORDING`` cuando el operador pulsa iniciar. No se
#:   puede grabar sin pasar por ``READY``: es la puerta.
#: * ``RECORDING`` ↔ ``PAUSED`` son el pausa y la reanudación.
#: * ``RECORDING``/``PAUSED`` → ``STOPPED`` al terminar.
#: * ``DRAFT``/``READY`` → ``CANCELLED`` al cancelar antes de empezar.
#: * Cualquier estado activo → ``FAILED`` si el dispositivo o la red
#:   fallan.
#: * De ``STOPPED`` no se sale. Una grabación terminada no se reanuda:
#:   reanudarla exigiría un artefacto nuevo, y mezclar dos en uno es
#:   justo lo que no se quiere.
_TRANSICIONES_CAPTURA: Final[dict[str, frozenset[str]]] = {
    "DRAFT": frozenset({"READY", "CANCELLED", "FAILED"}),
    "READY": frozenset({"RECORDING", "CANCELLED", "FAILED"}),
    "RECORDING": frozenset({"PAUSED", "STOPPED", "FAILED", "CANCELLED"}),
    "PAUSED": frozenset({"RECORDING", "STOPPED", "FAILED", "CANCELLED"}),
    "STOPPED": frozenset(),
    "CANCELLED": frozenset(),
    "FAILED": frozenset({"CANCELLED"}),
}

#: Transiciones válidas del **procesamiento**.
#:
#: El procesamiento es una cadena: se sube, se transcribe, se analiza,
#: se revisa y se aprueba. ``FAILED`` es un estado terminal con salida
#: a ``TRANSCRIBING``, porque un trabajo que falló por un error de red
#: se puede reintentar; uno que falló por un audio corrupto, no, y eso
#: lo decide el trabajo, no la máquina de estados.
_TRANSICIONES_PROCESAMIENTO: Final[dict[str, frozenset[str]]] = {
    "NOT_STARTED": frozenset({"UPLOADING", "FAILED"}),
    "UPLOADING": frozenset({"UPLOADED", "FAILED"}),
    "UPLOADED": frozenset({"TRANSCRIBING", "FAILED"}),
    "TRANSCRIBING": frozenset({"TRANSCRIBED", "FAILED"}),
    "TRANSCRIBED": frozenset({"ANALYZING", "FAILED"}),
    "ANALYZING": frozenset({"REVIEW_REQUIRED", "APPROVED", "FAILED"}),
    "REVIEW_REQUIRED": frozenset({"APPROVED", "ANALYZING", "FAILED"}),
    "APPROVED": frozenset(),
    "FAILED": frozenset({"TRANSCRIBING", "ANALYZING"}),
}


def transiciones_de_captura(estado: str) -> frozenset[str]:
    """Los estados a los que se puede pasar desde un estado de captura."""
    return _TRANSICIONES_CAPTURA.get(estado, frozenset())


def transiciones_de_procesamiento(estado: str) -> frozenset[str]:
    """Los estados a los que se puede pasar desde un estado de proceso."""
    return _TRANSICIONES_PROCESAMIENTO.get(estado, frozenset())


def transicion_captura_permitida(desde: str, hacia: str) -> bool:
    """¿Se puede pasar de un estado de captura a otro?

    Un estado que no existe en la tabla **no** tiene transiciones: la
    respuesta es que no. Es el fallo cerrado, y evita que un estado
    inventado abra la puerta a cualquier otro.
    """
    if desde not in ESTADOS_DE_CAPTURA or hacia not in ESTADOS_DE_CAPTURA:
        return False
    return hacia in _TRANSICIONES_CAPTURA.get(desde, frozenset())


def transicion_procesamiento_permitida(desde: str, hacia: str) -> bool:
    """¿Se puede pasar de un estado de procesamiento a otro?"""
    if desde not in ESTADOS_DE_PROCESAMIENTO or hacia not in ESTADOS_DE_PROCESAMIENTO:
        return False
    return hacia in _TRANSICIONES_PROCESAMIENTO.get(desde, frozenset())
