"""Detección y control de escucha de reuniones — capa de decisión.

Este módulo implementa la **decisión** de si una aplicación debe
capturar audio en un momento dado. No captura nada: decide, y deja
constancia de por qué.

Por qué la decisión está separada de la captura
------------------------------------------------

Porque **la decisión es la parte legalmente crítica**, y la captura es
la parte fácil. En cada sistema operativo se puede enumerar el audio
que se está reproduciendo y capturando, y se puede capturar el audio de
un proceso concreto. Lo que **no** es una cuestión de sistema operativo
es decidir si esa captura está autorizada.

Y la jurisprudencia es clara al respecto:

* **AEPD, PS-00412-2020 (16-11-2021).** Una sesión de evaluación
  grabada telemáticamente para preparar el acta fue declarada
  **ilícita** por el artículo 6.1 del RGPD. Y dice algo que corta de
  raíz la excusa más común: *«no se estima suficiente para salvar la
  ilicitud del tratamiento el hecho de que los integrantes de la junta
  de evaluación conocieran que la sesión sería grabada»*. Saber que se
  graba **no** legitima la grabación.
* **AEPD, informe jurídico sobre grabación de imagen y voz.** La
  grabación de la voz es un tratamiento con su propia justificación, y
  *«las grabaciones indiscriminadas de voz y conversaciones de los
  empleados y público en general... no cumpliría el principio de
  proporcionalidad»*, por ser una medida intrusiva para la intimidad.
* **AEPD, EXP202407307 (sanción de 3.000 euros).** La grabación de un
  comité **sí** tenía base legal: se informó, y hubo consentimiento. Lo
  que no la tenía fue **convertir la grabación en el acta** y
  **comunicarla a terceros** ajenos a la reunión. El cambio de finalidad
  es *«una operación cualitativamente diferente»*.

De ahí sale la regla que este módulo implementa:

> **«Hay una reunión» no es un motivo para grabar. Lo que lo es es
> «esta reunión concreta está consentida».**

Y por eso la entrada no es un detector de voz, sino un **registro de
reuniones consentidas** que se consulta **antes** de abrir el micrófono
y el audio del sistema. Un micrófono abierto no significa reunión de
proyecto: significa, como mínimo, que hay alguien con el micrófono
abierto. La 1:1 con el mando, una formación o una llamada personal son
«reuniones» en el sentido del detector, y suelen ser las más sensibles.

Lo que sale de aquí
-------------------

Un sensor de reuniones hace tres cosas, y solo tres:

1. **Detectar** qué aplicación de reunión está activa. Eso es nativo del
   sistema operativo y lo hacen las tres plataformas principales
   (Windows: `IAudioSessionManager2` · macOS: los *process objects* de
   Core Audio · Linux: los *sink inputs* de PipeWire). Aquí se
   normaliza.
2. **Decidir** si esa reunión está autorizada. Eso es la política del
   banco, y es este módulo.
3. **Emitir la señal derivada**, no el audio. El audio se transcribe en
   el perímetro y **se descarta**; lo que sale es el compromiso, el
   riesgo y su marca temporal, por el camino normal del Edge Connector.

El audio crudo **no sale** del perímetro, y por defecto **no se
conserva**: se consume en la transcripción y se tira. Eso es lo que
diferencia este diseño del que la AEPD sancionaría.

Sobre la frontera del AI Act
----------------------------

Entre los campos bloqueados está `emocion`, y no es un detalle técnico:
el artículo 5.1.f) del Reglamento europeo de IA prohíbe inferir
emociones en el ámbito laboral. Un sensor que enviara estados
emocionales de empleados a un sistema de gestión de proyectos estaría
haciendo exactamente eso. Aquí la frontera se respeta desde el diseño:
el sensor transporta y filtra, y la interpretación es trabajo del
servidor, que aplica la misma lista de bloqueados.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "DecisionDeEscucha",
    "DetectorDeReunion",
    "MOTIVOS",
    "ReunionConsentida",
    "SesionDeAudio",
]

#: Motivos por los que se decide **no** escuchar. Cada uno es un
#: enunciado auditable: «no se escuchó porque X», con X de esta lista.
#:
#: No es una lista de estados internos del programa: es la lista de
#: respuestas que el banco tiene que poder dar a la AEPD, a un
#: inspector, o a un empleado que pregunta qué se ha grabado.
MOTIVOS: Final[dict[str, str]] = {
    "reunion_no_consentida": (
        "La reunión en curso no figura en el registro de reuniones "
        "consentidas. No se ha capturado audio."
    ),
    "proceso_no_habilitado": (
        "La aplicación que está en curso no está habilitada por la "
        "política local. No se ha capturado audio."
    ),
    "sin_senal_de_reunion": (
        "La aplicación habilitada no está reproduciendo audio ni capturando "
        "micrófono: no hay reunión en curso. No se ha capturado audio."
    ),
    "sin_consentimiento_de_reunion": (
        "No se ha registrado el consentimiento de la reunión. No se ha "
        "capturado audio."
    ),
}


@dataclass(frozen=True)
class SesionDeAudio:
    """Una sesión de audio activa, en un lenguaje independiente del sistema.

    Es la **normalización** de lo que cada sistema operativo cuenta de
    forma distinta. Windows enumera *audio sessions* con un PID;
    macOS tiene *process objects* con un bundle id y un PID; Linux tiene
    *sink inputs* con un nombre de aplicación.

    Y hay un detalle de seguridad que no es menor: en Linux, las
    propiedades `application.name` e `application.process.id` **las
    declara el propio cliente** y se pueden falsear. Solo las
    propiedades `pipewire.*` son fiables. Por eso esta clase lleva el
    proceso **como dato no verificado**, y por eso la verificación de
    identidad marca esa fuente como no fiable.
    """

    #: Nombre del proceso o identificador de bundle. **No verificado.**
    proceso: str
    #: Identificador del proceso en el sistema, si se conoce.
    identificador_proceso: str = ""
    #: `True` si la sesión está capturando (micrófono).
    captura: bool = False
    #: `True` si la sesión está reproduciendo (salida del sistema).
    reproduccion: bool = False
    #: `False` cuando el proceso de la sesión no ha podido verificarse.
    identidad_verificada: bool = True
    #: Observación libre para la traza local.
    detalle: str = ""

    @property
    def activa(self) -> bool:
        """¿La sesión está en curso?"""
        return self.captura or self.reproduccion


@dataclass(frozen=True)
class ReunionConsentida:
    """Una reunión que el registro del banco autoriza a escuchar.

    Es el **único** motivo admisible para capturar audio. No hay otras
    entradas en el registro: si una reunión no está aquí, no se escucha.
    """

    #: Identificador de la reunión: el *meeting id* de Teams, el
    #: identificador del evento de calendario, o el que use la
    #: plataforma de la reunión.
    identificador: str
    #: Proyecto al que se asocia.
    proyecto_id: str
    #: Participantes **autorizados**. Una lista vacía significa «cualquier
    #: participante de esta reunión».
    participantes_autorizados: tuple[str, ...] = ()
    #: Identificadores de proceso o bundle que pueden producir el audio
    #: de esta reunión. Vacío = los que la política global habilite.
    procesos_permitidos: tuple[str, ...] = ()
    #: Si `True`, el audio crudo puede conservarse. Por defecto no.
    conservar_audio: bool = False


@dataclass(frozen=True)
class DecisionDeEscucha:
    """El resultado de decidir, con su motivo y su traza.

    Una decisión **siempre** lleva motivo, también cuando es positiva.
    «Se escuchó porque la reunión R-7 está consentida y Teams está
    activo» es una respuesta; un `True` a secas no lo es.
    """

    #: ¿Se puede capturar audio?
    escuchar: bool
    #: Clave de `MOTIVOS` cuando no se escucha; `None` cuando sí.
    motivo: str | None = None
    #: Texto legible del motivo.
    explicacion: str = ""
    #: Reunión sobre la que se ha decidido, si se ha identificado.
    reunion: ReunionConsentida | None = None
    #: Aplicación cuya salida de audio se capturaría.
    proceso_origen: str = ""
    #: Sesiones que han pensado en la decisión, para la traza.
    sesiones: tuple[SesionDeAudio, ...] = field(default_factory=tuple)

    def resumen(self) -> str:
        """Una línea apta para el registro local y para la auditoría."""
        if self.escuchar:
            return (
                f"ESCUCHA autorizada · reunión={self.reunion.identificador if self.reunion else '?'} "
                f"· proceso={self.proceso_origen}"
            )
        return f"NO ESCUCHA · {self.motivo}"


class DetectorDeReunion:
    """Decide si se puede escuchar una reunión, y por qué.

    La clase **no captura**. Recibe la lista de sesiones de audio
    activas, ya normalizada por el adaptador del sistema operativo, y
    decide. La decisión es una función pura de su entrada: el mismo
    estado da siempre la misma decisión, y eso es lo que hace que se
    pueda probar.
    """

    def __init__(
        self,
        *,
        registro: tuple[ReunionConsentida, ...] = (),
        procesos_habilitados: tuple[str, ...] = (),
        identificador_reunion: str | None = None,
        permitir_audio_no_verificado: bool = False,
    ) -> None:
        """Prepara el detector.

        Args:
            registro: las reuniones consentidas. Si está vacío, **no se
                escucha nada**: es el fallo cerrado.
            procesos_habilitados: procesos o bundle ids que la política
                local permite. Si está vacío, se usa el que declara
                cada reunión.
            identificador_reunion: la reunión en curso, si se sabe. Si
                no se sabe, se decide por el proceso, y la decisión
                queda marcada como **no verificada**.
            permitir_audio_no_verificado: si se acepta que la identidad
                del proceso no se haya podido verificar. En Linux, las
                propiedades de aplicación las declara el cliente y se
                pueden falsear, así que esto está **desactivado por
                defecto**.
        """
        self.registro = registro
        self.procesos_habilitados = procesos_habilitados
        self.identificador_reunion = identificador_reunion
        self.permitir_audio_no_verificado = permitir_audio_no_verificado

    # -- utilidades --------------------------------------------------
    def _buscar_reunion(self) -> ReunionConsentida | None:
        """Localiza la reunión en curso en el registro."""
        if self.identificador_reunion:
            for reunion in self.registro:
                if reunion.identificador == self.identificador_reunion:
                    return reunion
            return None
        # Sin identificador solo se puede señalar una reunión si el
        # registro tiene exactamente una: adivinar entre varias sería
        # justo el error que este módulo existe para evitar.
        if len(self.registro) == 1:
            return self.registro[0]
        return None

    def _procesos_permitidos(self, reunion: ReunionConsentida | None) -> tuple[str, ...]:
        """Procesos que la política permite para esta reunión."""
        if reunion is not None and reunion.procesos_permitidos:
            return reunion.procesos_permitidos
        return self.procesos_habilitados

    # -- decisión ----------------------------------------------------
    def decidir(
        self, sesiones: tuple[SesionDeAudio, ...] = ()
    ) -> DecisionDeEscucha:
        """Decide si se puede escuchar, y devuelve el motivo.

        El orden de las comprobaciones es el orden en que un inspector
        las leería, y el orden importa: si la reunión no está
        consentida, **no tiene sentido** preguntar si el micrófono está
        abierto. Se responde por la razón principal.
        """
        # 1. ¿Hay alguna reunión consentida? Sin registro, no se escucha.
        if not self.registro:
            return self._no("sin_consentimiento_de_reunion", sesiones)

        # 2. ¿La reunión en curso es una de las consentidas?
        reunion = self._buscar_reunion()
        if reunion is None:
            return self._no("reunion_no_consentida", sesiones)

        # 3. ¿Hay alguna aplicación de reunión activa y permitida?
        permitidos = self._procesos_permitidos(reunion)
        candidatas = [
            sesion
            for sesion in sesiones
            if sesion.activa
            and sesion.proceso in permitidos
            and (sesion.identidad_verificada or self.permitir_audio_no_verificado)
        ]
        if not candidatas:
            return self._no("sin_senal_de_reunion", sesiones, reunion)

        # 4. ¿Hay señal de reunión? Reproducción y captura a la vez.
        reproducing = [s for s in candidatas if s.reproduccion]
        capturando = [s for s in candidatas if s.captura]
        if not reproducing and not capturando:
            return self._no("sin_senal_de_reunion", sesiones, reunion)

        # 5. Se escucha. La aplicación de la que se toma el audio es la
        #    que reproduce, que es la que trae la voz de los demás.
        origen = (reproducing or capturando)[0].proceso
        return DecisionDeEscucha(
            escuchar=True,
            motivo=None,
            explicacion=(
                f"Reunión '{reunion.identificador}' consentida para el proyecto "
                f"{reunion.proyecto_id}. Aplicación activa: {origen}."
            ),
            reunion=reunion,
            proceso_origen=origen,
            sesiones=sesiones,
        )

    def _no(
        self,
        motivo: str,
        sesiones: tuple[SesionDeAudio, ...],
        reunion: ReunionConsentida | None = None,
    ) -> DecisionDeEscucha:
        """Construye una decisión negativa, con su motivo."""
        return DecisionDeEscucha(
            escuchar=False,
            motivo=motivo,
            explicacion=MOTIVOS.get(motivo, motivo),
            reunion=reunion,
            sesiones=sesiones,
        )
