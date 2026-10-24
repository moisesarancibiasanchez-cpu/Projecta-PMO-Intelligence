"""Proveedores de transcripción de Projecta Capture.

Una interfaz **desacoplada** del motor concreto, con dos
implementaciones:

* :class:`ProveedorSimulado` — genera una transcripción sintética y
  **determinista**. Sirve para desarrollo y para las pruebas, y no
  necesita credenciales ni conexión. Es lo que permite que el entorno
  de desarrollo funcione entero sin tocar ningún servicio externo.
* :class:`AdaptadorMotorReal` — habla con un motor de transcripción
  **configurable por URL**, que es lo que permite que el MVP se
  despliegue sin enviar audio a Internet: si no hay URL configurada, el
  adaptador falla con un error claro en lugar de enviar nada.

Lo que el módulo **no** hace, y por qué:

* No inventa precisión. La confianza solo se rellena si el proveedor
  la entrega. Un ``0.87`` inventado sería una afirmación falsa sobre
  la calidad de una transcripción, y alguien la usaría.
* No inventa proveedores. No hay un «motor por defecto»: hay una URL
  en la configuración, o no hay integración.
* No manda audio a ningún sitio sin que la configuración lo diga
  explícitamente. Sin URL, el adaptador falla antes de abrir una
  conexión.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

__all__ = [
    "AdaptadorMotorReal",
    "EstadoDeTrabajo",
    "ErrorDeTranscripcion",
    "ProveedorSimulado",
    "ResultadoDeTranscripcion",
    "SegmentoTranscrito",
    "TranscriptionProvider",
    "construir_proveedor",
]


class ErrorDeTranscripcion(Exception):
    """El proveedor no pudo transcribir.

    Lleve el motivo: quien lo lee tiene que saber si fue un problema
    de configuración, de red o del propio audio.
    """


@dataclass(frozen=True)
class SegmentoTranscrito:
    """Un segmento de la transcripción.

    El ``hablante`` es una **etiqueta** («Hablante 1»), no una
    identidad. Quien quiera saber quién es, lo valida a mano.
    """

    hablante: str
    inicio_ms: int
    fin_ms: int
    texto: str
    #: Confianza del segmento, **solo** si el proveedor la entrega.
    confianza: float | None = None


@dataclass(frozen=True)
class ResultadoDeTranscripcion:
    """El resultado completo de una transcripción."""

    segmentos: list[SegmentoTranscrito] = field(default_factory=list)
    idioma: str = "es"
    proveedor: str = ""
    id_trabajo: str = ""
    #: Confianza global, solo si el proveedor la entrega.
    confianza: float | None = None

    @property
    def texto(self) -> str:
        """La transcripción como texto corrido."""
        return "\n".join(
            f"[{s.hablante}] {s.texto}" for s in self.segmentos
        )

    def duracion_ms(self) -> int:
        """La duración cubierta por la transcripción."""
        return max((s.fin_ms for s in self.segmentos), default=0)


@dataclass(frozen=True)
class EstadoDeTrabajo:
    """El estado de un trabajo en un proveedor asíncrono."""

    estado: str
    progreso: int | None = None
    id_trabajo: str = ""
    error: str | None = None


@runtime_checkable
class TranscriptionProvider(Protocol):
    """La interfaz que debe cumplir un proveedor de transcripción."""

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "es",
        options: dict[str, Any] | None = None,
    ) -> ResultadoDeTranscripcion:
        """Transcribe un audio y devuelve la transcripción."""
        ...

    def get_status(self, job_id: str) -> EstadoDeTrabajo:
        """Consulta el estado de un trabajo asíncrono."""
        ...


#: El texto sintético que usa el proveedor simulado. Es una reunión de
#: proyecto ficticia, y sirve para que el resto del sistema tenga algo
#: con lo que trabajar sin recording real.
TEXTO_SIMULADO: Final[tuple[tuple[str, str], ...]] = (
    (
        "Hablante 1",
        "Buenos días, arrancamos con el comité de seguimiento del "
        "programa. El primer punto es el estado de la integración con el "
        "núcleo.",
    ),
    (
        "Hablante 2",
        "El primer punto va con retraso. Necesitamos dos semanas más de "
        "lo previsto para cerrar la validación.",
    ),
    (
        "Hablante 1",
        "Lo declaramos como riesgo en abierto y lo revisamos en el "
        "próximo comité.",
    ),
    (
        "Hablante 2",
        "Me lo quedo. Lo cierro antes del viernes y lo subo al tablero.",
    ),
    (
        "Hablante 1",
        "Perfecto. El segundo punto es la migración de datos históricos, "
        "que ya está planificada.",
    ),
)


class ProveedorSimulado:
    """Proveedor sintético, determinista y sin conexión.

    Genera una transcripción a partir de los bytes del audio, de forma
    que **el mismo audio produce siempre la misma transcripción**. La
    determinismo no es un capricho: es lo que permite que una prueba
    verifique un resultado concreto, y que el análisis sea
    reproducible.

    No es una transcripción real. Lo dice el ``proveedor`` del
    resultado, para que nadie confunda una simulación con una
    transcripción de verdad.
    """

    nombre: str = "simulado"

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "es",
        options: dict[str, Any] | None = None,
    ) -> ResultadoDeTranscripcion:
        """Genera la transcripción sintética del audio."""
        if not audio:
            raise ErrorDeTranscripcion("No hay audio que transcribir.")

        # El desplazamiento depende del contenido del audio, para que
        # dos audios distintos no produzcan la misma transcripción.
        digest = hashlib.sha256(audio).hexdigest()
        desplazamiento = int(digest[:4], 16) % 5000

        segmentos: list[SegmentoTranscrito] = []
        cursor = desplazamiento
        for indice, (hablante, texto) in enumerate(TEXTO_SIMULADO):
            duracion = 2500 + (int(digest[indice * 2 : indice * 2 + 2], 16) % 4000)
            segmentos.append(
                SegmentoTranscrito(
                    hablante=hablante,
                    inicio_ms=cursor,
                    fin_ms=cursor + duracion,
                    texto=texto,
                    # El simulador **no** entrega confianza. Se deja en
                    # None en lugar de inventar un número.
                    confianza=None,
                )
            )
            cursor += duracion

        return ResultadoDeTranscripcion(
            segmentos=segmentos,
            idioma=language,
            proveedor=self.nombre,
            id_trabajo=f"sim-{digest[:16]}",
            # Tampoco hay confianza global. Un número inventado sería
            # una afirmación falsa sobre la calidad del audio.
            confianza=None,
        )

    def get_status(self, job_id: str) -> EstadoDeTrabajo:
        """El simulado es síncrono: el trabajo siempre está terminado."""
        return EstadoDeTrabajo(
            estado="TRANSCRIBED", progreso=100, id_trabajo=job_id, error=None
        )


class AdaptadorMotorReal:
    """Adaptador de un motor de transcripción real, configurable por URL.

    La URL viene de la configuración. Si no hay URL, el adaptador
    **falla antes de abrir cualquier conexión**: el MVP tiene que poder
    desplegarse sin enviar audio a Internet, y un valor por defecto
    que apunte a un servicio externo violaría eso.
    """

    nombre: str = "motor_real"

    def __init__(
        self,
        *,
        url: str = "",
        token: str = "",
        timeout: float = 120.0,
        intentos: int = 2,
        http: Any | None = None,
    ) -> None:
        """Prepara el adaptador.

        Args:
            url: URL del motor de transcripción.
            token: token de acceso, si el motor lo pide.
            timeout: segundos de espera por petición.
            intentos: cuántas veces se reintenta un error transitorio.
            http: cliente HTTP, para las pruebas.
        """
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.intentos = max(1, intentos)
        if http is None:
            import httpx

            http = httpx.Client(timeout=timeout)
        self.http = http

    def _exigir_configuracion(self) -> None:
        """Falla si no hay URL configurada."""
        if not self.url:
            raise ErrorDeTranscripcion(
                "No hay motor de transcripción configurado. El MVP funciona "
                "con el proveedor simulado, que no necesita conexión. Para "
                "usar un motor real hay que configurar su URL."
            )

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "es",
        options: dict[str, Any] | None = None,
    ) -> ResultadoDeTranscripcion:
        """Transcribe el audio llamando al motor configurado."""
        self._exigir_configuracion()
        if not audio:
            raise ErrorDeTranscripcion("No hay audio que transcribir.")

        import base64

        cuerpo: dict[str, Any] = {
            "audio_base64": base64.b64encode(audio).decode("ascii"),
            "language": language,
        }
        if options:
            cuerpo["options"] = options

        cabeceras = {"Content-Type": "application/json"}
        if self.token:
            cabeceras["Authorization"] = f"Bearer {self.token}"

        ultimo_error: Exception | None = None
        for intento in range(self.intentos):
            try:
                respuesta = self.http.post(
                    f"{self.url}/transcribe",
                    json=cuerpo,
                    headers=cabeceras,
                    timeout=self.timeout,
                )
            except Exception as error:  # noqa: BLE001 - se reintenta y se informa
                ultimo_error = error
                if intento + 1 < self.intentos:
                    time.sleep(2 ** intento)
                    continue
                raise ErrorDeTranscripcion(
                    f"No se pudo contactar con el motor de transcripción: {error}"
                ) from error

            if respuesta.status_code == 200:
                return self._interpretar(respuesta.json())

            if respuesta.status_code in (429, 500, 502, 503, 504):
                ultimo_error = ErrorDeTranscripcion(
                    f"El motor devolvió {respuesta.status_code}: "
                    f"{respuesta.text[:200]}"
                )
                if intento + 1 < self.intentos:
                    espera = int(respuesta.headers.get("Retry-After", "2"))
                    time.sleep(max(1, espera))
                    continue

            raise ErrorDeTranscripcion(
                f"El motor devolvió {respuesta.status_code}: {respuesta.text[:200]}"
            )

        raise ErrorDeTranscripcion(
            f"El motor de transcripción falló: {ultimo_error}"
        )

    def _interpretar(self, cuerpo: dict[str, Any]) -> ResultadoDeTranscripcion:
        """Convierte la respuesta del motor en un resultado normalizado.

        Se aceptan dos formas de respuesta: una lista de segmentos con
        ``speaker``/``start``/``end``/``text``, o un texto plano. La
        confianza se copia **solo si viene**.
        """
        segmentos: list[SegmentoTranscrito] = []
        confianza_global: float | None = None

        if isinstance(cuerpo, dict):
            confianza_global = cuerpo.get("confidence")
            crudo = cuerpo.get("segments") or []
        else:
            crudo = []

        if crudo:
            for indice, segmento in enumerate(crudo):
                segmentos.append(
                    SegmentoTranscrito(
                        hablante=str(
                            segmento.get("speaker")
                            or segmento.get("hablante")
                            or f"Hablante {indice + 1}"
                        ),
                        inicio_ms=int(segmento.get("start", segmento.get("inicio_ms", 0))),
                        fin_ms=int(segmento.get("end", segmento.get("fin_ms", 0))),
                        texto=str(segmento.get("text", "")),
                        confianza=segmento.get("confidence"),
                    )
                )
        else:
            texto = ""
            if isinstance(cuerpo, dict):
                texto = str(cuerpo.get("text", ""))
            if texto:
                segmentos.append(
                    SegmentoTranscrito(
                        hablante="Hablante 1",
                        inicio_ms=0,
                        fin_ms=0,
                        texto=texto,
                    )
                )

        return ResultadoDeTranscripcion(
            segmentos=segmentos,
            idioma=str(cuerpo.get("language", "es")) if isinstance(cuerpo, dict) else "es",
            proveedor=self.nombre,
            id_trabajo=str(cuerpo.get("id", "")) if isinstance(cuerpo, dict) else "",
            confianza=confianza_global,
        )

    def get_status(self, job_id: str) -> EstadoDeTrabajo:
        """Consulta el estado de un trabajo en el motor."""
        self._exigir_configuracion()
        cabeceras = {}
        if self.token:
            cabeceras["Authorization"] = f"Bearer {self.token}"
        respuesta = self.http.get(
            f"{self.url}/jobs/{job_id}",
            headers=cabeceras,
            timeout=self.timeout,
        )
        if respuesta.status_code != 200:
            raise ErrorDeTranscripcion(
                f"El motor devolvió {respuesta.status_code} al consultar el trabajo."
            )
        cuerpo = respuesta.json()
        return EstadoDeTrabajo(
            estado=str(cuerpo.get("status", "UNKNOWN")),
            progreso=cuerpo.get("progress"),
            id_trabajo=str(cuerpo.get("id", job_id)),
            error=cuerpo.get("error"),
        )


def construir_proveedor(
    *,
    url: str = "",
    token: str = "",
    http: Any | None = None,
) -> TranscriptionProvider:
    """Devuelve el proveedor que hay que usar.

    Sin URL configurada, el **simulado**. Es lo que permite que el
    entorno de desarrollo y las pruebas funcionen sin credenciales, y
    que el MVP se despliegue sin enviar audio a Internet.
    """
    if url:
        return AdaptadorMotorReal(url=url, token=token, http=http)
    return ProveedorSimulado()
