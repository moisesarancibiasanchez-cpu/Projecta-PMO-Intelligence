"""Cliente de Microsoft Graph para capturar reuniones de Teams.

Este módulo habla con Graph y trae a Projecta PMO Intelligence **el
audio y el texto** de las reuniones de Teams: la transcripción con
atribución de hablantes y la grabación de la llamada.

Por qué Graph y no el micrófono del sistema
--------------------------------------------

Porque Graph es donde el audio **ya está**, capturado y guardado por
Microsoft, con el consentimiento del administrador del tenant y el de
los participantes. No hay que escuchar el micrófono del equipo, ni
capturar el audio del sistema operativo, ni montar un driver de
audio. El banco ya tiene Microsoft 365: la reunión ya se grabó, y lo
que hay que hacer es **traerla**.

La diferencia no es de comodidad, es de exactitud: la transcripción de
Graph sabe quién habló. Un bucle de audio del sistema no lo sabe, y
reconstruirlo exige separación de hablantes sobre audio mezclado.

Los tres artefactos de una reunión
----------------------------------

* **Transcripción** (`.vtt`): el texto, con hablante y marcas de
  tiempo. Es el artefacto principal.
* **Grabación**: el audio y el vídeo de la llamada, en MP4.
* **Informe de asistencia**: quién estuvo y cuándo entró y salió.

Y las tres cosas que hay que tener en el tenant
-----------------------------------------------

``OnlineMeetingTranscript.Read.All``
    Permiso de aplicación para leer transcripciones.

``OnlineMeetingRecording.Read.All``
    Permiso de aplicación para leer grabaciones.

``OnlineMeetingArtifact.Read.All``
    Permiso de aplicación para leer los informes de asistencia.

Y, además de los permisos, una **política de acceso de aplicación** en
Teams, sin la cual Graph responde 403 aunque el consentimiento del
administrador esté concedido:

    New-CsApplicationAccessPolicy -Identity ProjectaPMO -AppIds {client-id}
    Grant-CsApplicationAccessPolicy -PolicyName ProjectaPMO -Global

El detalle fino, y explica por qué hay dos formas de listar
------------------------------------------------------------

``getAllTranscripts`` — el barrido por rango de fechas. Devuelve
todas las transcripciones del organizador en un intervalo, y es lo que
usa la tarea de captura. Es cómodo, pero **no** cubre las reuniones
privadas de chat, que necesitan un consentimiento específico del
recurso.

``/onlineMeetings/{id}/transcripts`` — la lista por reunión. Entra
reunión a reunión, y sí cubre las reuniones privadas de chat.

La captura usa las dos: barrido por rango para no dejar pasar nada, y
luego la lista por reunión para completar las que el barrido no
trajo.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Final

from app.core.edge_teams import ErrorDeGraph

__all__ = [
    "ClienteReuniones",
    "ErrorDeGraph",
    "ErrorDeTranscripcionDeshabilitada",
    "Segmento",
    "parsear_vtt",
]

#: Base de Graph. Configurable para poder apuntar a un doble de prueba.
GRAPH_BASE: Final[str] = "https://graph.microsoft.com/v1.0"

#: Formato de fecha que espera Graph en los parámetros de fecha.
FORMATO_FECHA: Final[str] = "%Y-%m-%dT%H:%M:%SZ"


class ErrorDeTranscripcionDeshabilitada(ErrorDeGraph):
    """El administrador del tenant ha desactivado el acceso a transcripciones.

    Graph responde 403 con el código interno ``GraphAccessToTranscripts-
    Disabled``. No hay forma de sortearlo desde el lado de la
    aplicación: o el administrador lo reactiva, o no hay transcripción.
    """


@dataclass(frozen=True)
class Segmento:
    """Un fragmento de la transcripción, con su hablante y su tiempo.

    Los tiempos van en segundos desde el inicio de la reunión, que es
    como los usa Graph y como los entiende cualquier reproductor.
    """

    #: Identificador del hablante, tal y como lo da Graph.
    hablante: str
    #: Inicio del fragmento, en segundos desde el inicio.
    inicio: float
    #: Fin del fragmento, en segundos desde el inicio.
    fin: float
    #: Texto dicho.
    texto: str

    def como_dict(self) -> dict[str, Any]:
        """Representación serializable, para la API y para la base de datos."""
        return {
            "hablante": self.hablante,
            "inicio": self.inicio,
            "fin": self.fin,
            "texto": self.texto,
        }


#: Marca de tiempo de WebVTT: ``hh:mm:ss.mmm`` o ``mm:ss.mmm``.
RE_MARCA = re.compile(
    r"(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{2})[.,](?P<ms>\d{1,3})"
)


def _segundos(marca: str) -> float:
    """Convierte una marca de tiempo de WebVTT en segundos."""
    m = RE_MARCA.search(marca)
    if not m:
        return 0.0
    horas = float(m.group("h") or 0)
    minutos = float(m.group("m"))
    segundos = float(m.group("s"))
    milis = float(m.group("ms").ljust(3, "0"))
    return horas * 3600 + minutos * 60 + segundos + milis / 1000


def parsear_vtt(contenido: str) -> list[Segmento]:
    """Convierte una transcripción en WebVTT en segmentos con hablante.

    El formato que devuelve Graph es WebVTT estándar, y el hablante
    viene en la línea de voz, con esta forma:

    .. code-block:: vtt

        WEBVTT

        00:00:01.000 --> 00:00:04.000
        <v Speaker Name>Lo primero que dijo.

    Se admiten también las variantes de Microsoft, que incluyen la
    marca del hablante dentro del propio texto.

    La función es **tolerante**: un VTT truncado, con líneas raras o
    con una cabecera que no se reconoce, devuelve los segmentos que sí
    se pueden leer en lugar de fallar. Una transcripción a medio
    descargar es un caso real, no un caso de laboratorio.
    """
    segmentos: list[Segmento] = []
    bloque: list[str] = []
    marcas: tuple[float, float] | None = None
    hablante_actual = ""

    def cerrar() -> None:
        nonlocal bloque, marcas, hablante_actual
        if marcas is not None and bloque:
            texto = " ".join(linea.strip() for linea in bloque if linea.strip())
            texto = texto.strip()
            if texto:
                segmentos.append(
                    Segmento(
                        hablante=hablante_actual,
                        inicio=marcas[0],
                        fin=marcas[1],
                        texto=_limpiar_texto(texto),
                    )
                )
        bloque = []
        marcas = None
        hablante_actual = ""

    for linea in contenido.splitlines():
        if "-->" in linea:
            cerrar()
            izquierda, _, derecha = linea.partition("-->")
            marcas = (_segundos(izquierda), _segundos(derecha))
            # La etiqueta del hablante puede venir en la propia línea de
            # tiempo, entre el texto y las marcas: ``<v Ana> 00:00 -->``.
            etiqueta = re.search(r"<v[ >]([^>]*)>", izquierda)
            if etiqueta:
                hablante_actual = etiqueta.group(1).strip()
            continue
        if marcas is not None:
            if linea.strip() == "":
                cerrar()
                continue
            # ``<v Hablante>`` en su propia línea.
            etiqueta = re.match(r"^\s*<v[ >]([^>]*)>\s*(.*)$", linea)
            if etiqueta:
                hablante_actual = etiqueta.group(1).strip()
                resto = etiqueta.group(2).strip()
                if resto:
                    bloque.append(resto)
                continue
            bloque.append(linea)
        elif linea.strip() == "":
            cerrar()

    cerrar()
    return segmentos


def _limpiar_texto(texto: str) -> str:
    """Quita las etiquetas de voz y los dobles espacios del texto."""
    texto = re.sub(r"</?v[^>]*>", "", texto)
    texto = re.sub(r"<[^>]+>", "", texto)
    return re.sub(r"\s+", " ", texto).strip()


@dataclass
class ResultadoDeReunion:
    """Lo que se ha capturado de una reunión.

    Los tres artefactos son opcionales porque Teams no los produce
    todos: una reunión sin grabación no tiene grabación, y una
    transcripción puede existir sin informe de asistencia.
    """

    #: Identificador de la reunión en Teams.
    reunion_id: str
    #: Asunto de la reunión, si se conoce.
    asunto: str = ""
    #: Identificador de la transcripción.
    transcripcion_id: str = ""
    #: Segmentos de la transcripción.
    segmentos: list[Segmento] = field(default_factory=list)
    #: Participantes y sus marcas de entrada y salida.
    participantes: list[dict[str, Any]] = field(default_factory=list)
    #: Grabación descargada, si se pidió y se pudo.
    audio: bytes | None = None
    #: Formato de la grabación.
    formato_audio: str = ""
    #: Identificador del informe de asistencia.
    informe_id: str = ""
    #: Inicio de la reunión, en ISO 8601.
    inicio: str = ""
    #: Fin de la reunión, en ISO 8601.
    fin: str = ""

    @property
    def texto_plano(self) -> str:
        """La transcripción como texto corrido, con el hablante delante."""
        lineas = []
        for segmento in self.segmentos:
            quien = segmento.hablante or "hablante desconocido"
            lineas.append(f"[{quien}] {segmento.texto}")
        return "\n".join(lineas)

    def como_dict(self) -> dict[str, Any]:
        """Representación serializable."""
        return {
            "reunion_id": self.reunion_id,
            "asunto": self.asunto,
            "transcripcion_id": self.transcripcion_id,
            "segmentos": [s.como_dict() for s in self.segmentos],
            "participantes": self.participantes,
            "tiene_audio": self.audio is not None,
            "formato_audio": self.formato_audio,
            "informe_id": self.informe_id,
            "inicio": self.inicio,
            "fin": self.fin,
        }


class ClienteReuniones:
    """Cliente de Graph para las reuniones de Teams.

    Se instancia con un token ya válido, o con la configuración del
    tenant para que se lo pida él mismo. Todas las peticiones salen
    **hacia fuera**: no hay ningún endpoint público y ningún puerto
    entrante, que es lo que permite que esto funcione dentro del
    perímetro del banco.
    """

    def __init__(
        self,
        token: str,
        *,
        http: Any | None = None,
        base: str = GRAPH_BASE,
    ) -> None:
        """Prepara el cliente.

        Args:
            token: token de acceso de Graph ya válido.
            http: cliente HTTP. Si no se pasa, se crea uno con `httpx`.
            base: base de la API, para poder apuntar a un doble.
        """
        if http is None:
            import httpx

            http = httpx.Client(timeout=60.0)
            # El cliente se ha creado aquí, así que es **nuestro** y
            # nos toca cerrarlo. Si lo recibe quien lo pasa, el cierre
            # es de quien lo pasó.
            self._http_propio = True
        else:
            self._http_propio = False
        self.token = token
        self.http = http
        self.base = base.rstrip("/")

    def close(self) -> None:
        """Cierra el cliente HTTP, si es de este objeto.

        Cada cliente de `httpx` se lleva por delante su pool de
        conexiones y sus sockets TLS. Sin cerrar, en un worker de larga
        vida la ejecución acumulativa de la tarea fuga descriptores de
        fichero, y el síntoma (agotamiento) aparece mucho después, sin
        relación aparente con la causa.
        """
        if not getattr(self, "_http_propio", False):
            return
        cerrar = getattr(self.http, "close", None)
        if cerrar is not None:
            try:
                cerrar()
            except Exception as error:  # noqa: BLE001
                LOG.warning("no se pudo cerrar el cliente HTTP de Graph: %s", error)

    def __enter__(self) -> "ClienteReuniones":
        return self

    def __exit__(self, *_excepcion: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Red de seguridad: si alguien crea un cliente y no lo cierra,
        # al recogerse el objeto se sueltan los sockets igualmente.
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # -- infraestructura ------------------------------------------------
    def _url(self, ruta: str) -> str:
        """Construye la URL absoluta a partir de una ruta de Graph."""
        if ruta.startswith("http"):
            return ruta
        return f"{self.base}/{ruta.lstrip('/')}"

    def _peticion(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Llama a Graph y devuelve el JSON.

        Gestiona el límite de peticiones (`429` con `Retry-After`) y
        traduce el 403 de transcripciones deshabilitadas a una
        excepción propia, que es un problema de configuración del
        tenant y no un fallo de la aplicación.
        """
        intentos = 0
        while True:
            intentos += 1
            respuesta = self.http.get(
                self._url(url),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=60.0,
            )
            if respuesta.status_code == 429:
                if intentos >= 5:
                    raise ErrorDeGraph(
                        f"Graph sigue limitando la petición tras {intentos} intentos"
                    )
                espera = int(respuesta.headers.get("Retry-After", "5"))
                time.sleep(max(1, espera))
                continue
            if respuesta.status_code == 403:
                cuerpo = _cuerpo_json(respuesta)
                codigo = _codigo_error(cuerpo)
                if codigo == "GraphAccessToTranscriptsDisabled":
                    raise ErrorDeTranscripcionDeshabilitada(
                        "El administrador del tenant ha desactivado el acceso a "
                        "transcripciones de Teams (GraphAccessToTranscriptsDisabled). "
                        "No hay forma de obtenerlas desde la aplicación."
                    )
                raise ErrorDeGraph(
                    f"Graph devolvió 403: {cuerpo.get('error', {}).get('message', '')[:200]}"
                )
            if respuesta.status_code == 404:
                raise ErrorDeGraph(
                    f"Graph no encontró el recurso: {url}"
                )
            if respuesta.status_code != 200:
                raise ErrorDeGraph(
                    f"Graph devolvió {respuesta.status_code} para {url}: "
                    f"{respuesta.text[:200]}"
                )
            return _cuerpo_json(respuesta)

    def _descarga(self, url: str) -> bytes:
        """Descarga contenido binario (la grabación)."""
        respuesta = self.http.get(
            self._url(url),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/octet-stream",
            },
            timeout=300.0,
        )
        if respuesta.status_code != 200:
            raise ErrorDeGraph(
                f"Graph devolvió {respuesta.status_code} al descargar {url}: "
                f"{respuesta.text[:200]}"
            )
        return respuesta.content

    # -- reuniones -------------------------------------------------------
    def listar_reuniones(
        self,
        usuario: str,
        *,
        desde: str | None = None,
        hasta: str | None = None,
    ) -> list[dict[str, Any]]:
        """Lista las reuniones en línea del organizador.

        Args:
            usuario: UPN o id del organizador.
            desde: fecha de inicio, en formato ISO 8601 UTC.
            hasta: fecha de fin, en formato ISO 8601 UTC.
        """
        params: dict[str, str] = {}
        if desde:
            params["$filter"] = f"startDateTime ge {desde}"
        if hasta:
            params["$filter"] = (
                f"startDateTime ge {desde} and startDateTime lt {hasta}"
                if desde
                else f"startDateTime lt {hasta}"
            )
        cuerpo = self._peticion(f"/users/{usuario}/onlineMeetings", params=params)
        return list(cuerpo.get("value", []))

    def listar_transcripciones(
        self,
        usuario: str,
        *,
        desde: str,
        hasta: str,
    ) -> list[dict[str, Any]]:
        """Lista **todas** las transcripciones del organizador en un rango.

        Este es el endpoint que hace viable la captura periódica: no
        hace falta saber de antemano qué reuniones hubo. Devuelve
        objetos con ``meetingId``, ``id`` y ``transcriptContentUrl``.
        """
        ruta = (
            f"/users/{usuario}/onlineMeetings/getAllTranscripts("
            f"meetingOrganizerUserId='{usuario}',"
            f"startDateTime={desde},"
            f"endDateTime={hasta})"
        )
        cuerpo = self._peticion(ruta)
        return list(cuerpo.get("value", []))

    def listar_transcripciones_de_reunion(
        self, usuario: str, reunion_id: str
    ) -> list[dict[str, Any]]:
        """Lista las transcripciones de una reunión concreta.

        Esta vía sí cubre las reuniones privadas de chat, que el
        barrido por rango no trae.
        """
        cuerpo = self._peticion(f"/users/{usuario}/onlineMeetings/{reunion_id}/transcripts")
        return list(cuerpo.get("value", []))

    def descargar_transcripcion(
        self,
        usuario: str,
        reunion_id: str,
        transcripcion_id: str,
    ) -> str:
        """Descarga la transcripción en WebVTT."""
        ruta = (
            f"/users/{usuario}/onlineMeetings/{reunion_id}"
            f"/transcripts/{transcripcion_id}/content"
        )
        respuesta = self.http.get(
            self._url(ruta),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "text/vtt",
            },
            timeout=120.0,
        )
        if respuesta.status_code == 403:
            cuerpo = _cuerpo_json(respuesta)
            if _codigo_error(cuerpo) == "GraphAccessToTranscriptsDisabled":
                raise ErrorDeTranscripcionDeshabilitada(
                    "El administrador del tenant ha desactivado el acceso a "
                    "transcripciones de Teams."
                )
        if respuesta.status_code != 200:
            raise ErrorDeGraph(
                f"Graph devolvió {respuesta.status_code} al descargar la "
                f"transcripción: {respuesta.text[:200]}"
            )
        return respuesta.text

    def reunion(self, usuario: str, reunion_id: str) -> dict[str, Any]:
        """Lee los metadatos de una reunión: asunto, inicio y fin.

        La transcripción no lleva el asunto. Si se quiere saber de qué
        reunión se trata, hay que preguntárselo a la reunión.
        """
        return self._peticion(f"/users/{usuario}/onlineMeetings/{reunion_id}")

    def listar_grabaciones(
        self, usuario: str, reunion_id: str
    ) -> list[dict[str, Any]]:
        """Lista las grabaciones de una reunión.

        Los *call records* de la API v1.0 **no** devuelven las
        grabaciones: hay que ir a la propia reunión.
        """
        cuerpo = self._peticion(f"/users/{usuario}/onlineMeetings/{reunion_id}/recordings")
        return list(cuerpo.get("value", []))

    def descargar_grabacion(
        self,
        usuario: str,
        reunion_id: str,
        grabacion_id: str,
    ) -> bytes:
        """Descarga la grabación de la reunión (MP4 con audio y vídeo)."""
        ruta = (
            f"/users/{usuario}/onlineMeetings/{reunion_id}"
            f"/recordings/{grabacion_id}/content"
        )
        return self._descarga(ruta)

    def participantes(
        self, usuario: str, reunion_id: str
    ) -> list[dict[str, Any]]:
        """Lee el informe de asistencia de la reunión.

        Solo existe **después** de que la reunión ha terminado.
        """
        cuerpo = self._peticion(
            f"/users/{usuario}/onlineMeetings/{reunion_id}/attendanceReports"
        )
        return list(cuerpo.get("value", []))

    # -- captura ---------------------------------------------------------
    def capturar(
        self,
        usuario: str,
        reunion_id: str,
        *,
        con_audio: bool = True,
    ) -> ResultadoDeReunion:
        """Captura una reunión: transcripción, participantes y grabación.

        Los tres artefactos son opcionales y la captura no falla si
        falta alguno: Teams no siempre los produce. Lo que **sí** es
        un fallo es que la transcripción no exista, porque sin ella
        no hay nada que analizar.
        """
        resultado = ResultadoDeReunion(reunion_id=reunion_id)

        transcripciones = self.listar_transcripciones_de_reunion(usuario, reunion_id)
        if not transcripciones:
            raise ErrorDeGraph(
                f"La reunión {reunion_id} no tiene transcripción. Puede que la "
                f"transcripción no estuviera activada, o que el administrador "
                f"la tenga desactivada."
            )
        primera = transcripciones[0]
        resultado.transcripcion_id = primera.get("id", "")
        vtt = self.descargar_transcripcion(
            usuario, reunion_id, resultado.transcripcion_id
        )
        resultado.segmentos = parsear_vtt(vtt)

        # El asunto y las fechas no vienen en la transcripción: hay que
        # preguntárselos a la reunión. Si no se puede, la captura sigue
        # siendo válida: lo que no se puede saber, no se inventa.
        try:
            metadatos = self.reunion(usuario, reunion_id)
        except ErrorDeTranscripcionDeshabilitada:
            # El 403 del tenant no se traga: es una decisión del
            # administrador, no un fallo de esta llamada. Si se
            # degrada a {}, la captura devolvería éxito con la
            # transcripción vacía y sin motivo.
            raise
        except ErrorDeGraph:
            metadatos = {}
        resultado.asunto = metadatos.get("subject", "") or ""
        resultado.inicio = metadatos.get("startDateTime", "") or ""
        resultado.fin = metadatos.get("endDateTime", "") or ""

        try:
            informes = self.participantes(usuario, reunion_id)
        except ErrorDeTranscripcionDeshabilitada:
            # Véase la nota de arriba: el 403 del tenant se propaga.
            raise
        except ErrorDeGraph:
            informes = []
        for informe in informes:
            resultado.informe_id = informe.get("id", resultado.informe_id)
            for participante in informe.get("participants", []):
                resultado.participantes.append(
                    {
                        "identidad": participante.get("identity", {}).get("user", {}).get("id", ""),
                        "nombre": participante.get("identity", {}).get("user", {}).get("displayName", ""),
                        "entrada": participante.get("attendanceRecords", [{}])[0].get("joinDateTime", ""),
                        "salida": participante.get("attendanceRecords", [{}])[0].get("leaveDateTime", ""),
                    }
                )

        if con_audio:
            try:
                grabaciones = self.listar_grabaciones(usuario, reunion_id)
            except ErrorDeTranscripcionDeshabilitada:
                # Véase la nota de arriba: el 403 del tenant se propaga.
                raise
            except ErrorDeGraph:
                grabaciones = []
            if grabaciones:
                resultado.audio = self.descargar_grabacion(
                    usuario, reunion_id, grabaciones[0].get("id", "")
                )
                resultado.formato_audio = "mp4"

        return resultado


def _cuerpo_json(respuesta: Any) -> dict[str, Any]:
    """Saca el JSON de una respuesta, tolerando un cuerpo no JSON."""
    try:
        cuerpo = respuesta.json()
    except Exception:  # noqa: BLE001 - el cuerpo puede no ser JSON
        return {}
    return cuerpo if isinstance(cuerpo, dict) else {}


def _codigo_error(cuerpo: dict[str, Any]) -> str:
    """Extrae el código interno del error de Graph."""
    error = cuerpo.get("error", {})
    codigo = error.get("code", "")
    inner = error.get("innerError", {}) or {}
    if not codigo and inner:
        codigo = inner.get("code", "")
    return str(codigo)
