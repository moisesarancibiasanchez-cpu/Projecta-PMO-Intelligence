#!/usr/bin/env python3
"""Doble de Microsoft Graph para las reuniones de Teams.

Sirve para dos cosas:

1. **Pruebas funcionales sin tenant.** Se levanta en un puerto local y
   el cliente de Graph pointed a él. Las pruebas de captura se pueden
   hacer hoy mismo, sin Microsoft 365, sin registro de aplicación y sin
   pedir nada a nadie.
2. **Pruebas manuales.** `python scripts/mock_graph.py --puerto 8099`
   levanta el doble con datos de ejemplo, y la CLI de captura se
   puede apuntar a él para ver el ciclo completo.

Los datos son **ficticios** y están marcados como tales. El doble
implementa los mismos verbos, las mismas rutas y los mismos códigos de
error que el Graph real, incluido el 403 con
``GraphAccessToTranscriptsDisabled``, que es el error que aparece
cuando el administrador del tenant desactiva las transcripciones.

Para apuntar el cliente al doble, se pasa la base por parámetro o
por la variable de entorno ``GRAPH_BASE_URL``.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Identificador de la reunión de ejemplo.
REUNION_1 = "MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmY3YS1hNjMxNzY5MTI4ZjQ"

#: Identificador de la segunda reunión de ejemplo.
REUNION_2 = "MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmY4ZS1hNjMxNzY5MTI4ZjQ"

#: Identificador de una reunión que existió pero sin transcripción: el
#: caso real de una reunión en la que no se activó la transcripción.
REUNION_3 = "MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmY5ZS1hNjMxNzY5MTI4ZjQ"

#: Transcripción de ejemplo, en WebVTT, con dos hablantes.
VTT_EJEMPLO = """WEBVTT

1
00:00:02.000 --> 00:00:06.500
<v Ana García>Buenos días, arrancamos con el comité de seguimiento del programa.

2
00:00:06.800 --> 00:00:12.100
<v Bruno Díaz>Perfecto. El primer punto es el estado de la integración con el core.

3
00:00:12.400 --> 00:00:19.900
<v Ana García>Nos falta cerrar la validación con el área de riesgos. Sin eso no podemos
firmar el hito.

4
00:00:20.200 --> 00:00:26.000
<v Bruno Díaz>Me lo quedo. Lo cierro antes del viernes y lo subo al tablero.

5
00:00:26.300 --> 00:00:33.800
<v Ana García>Perfecto. El segundo punto es la migración de datos históricos.

6
00:00:34.000 --> 00:00:41.200
<v Bruno Díaz>Ahí vamos con retraso. Necesitamos dos semanas más de lo previsto.

7
00:00:41.500 --> 00:00:49.000
<v Ana García>Lo declaramos como riesgo en abierto y lo revisamos en el próximo comité.
"""

#: Grabación de ejemplo. No es un MP4 real: son bytes con la firma de
#: archivo, suficientes para verificar que el flujo descarga y guarda.
MP4_EJEMPLO = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8

#: Participantes de la reunión de ejemplo.
PARTICIPANTES_1 = [
    {
        "id": "455fa3e2-14ab-4575-9f9a-7cbd4a2a4e01",
        "identity": {
            "user": {
                "id": "455fa3e2-14ab-4575-9f9a-7cbd4a2a4e01",
                "displayName": "Ana García",
            }
        },
        "attendanceRecords": [
            {
                "joinDateTime": "2026-10-23T09:00:00Z",
                "leaveDateTime": "2026-10-23T09:48:12Z",
            }
        ],
    },
    {
        "id": "7d9e2c11-3b2a-4f8e-9a11-2f6b8c1d4e02",
        "identity": {
            "user": {
                "id": "7d9e2c11-3b2a-4f8e-9a11-2f6b8c1d4e02",
                "displayName": "Bruno Díaz",
            }
        },
        "attendanceRecords": [
            {
                "joinDateTime": "2026-10-23T09:02:11Z",
                "leaveDateTime": "2026-10-23T09:47:55Z",
            }
        ],
    },
]

#: Transcripciones indexadas por identificador de reunión.
TRANSCRIPCIONES: dict[str, list[dict[str, Any]]] = {
    REUNION_1: [
        {
            "id": "MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmY3YS1hNjMxNzY5MTI4ZjQ",
            "meetingId": REUNION_1,
            "callId": "af630fe0-04d3-4559-8cf9-91fe45e36296",
            "transcriptContentUrl": (
                f"users/organizador@banco.example/onlineMeetings/{REUNION_1}"
                "/transcripts/MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmY3YS1hNjMxNzY5MTI4ZjQ/content"
            ),
            "createdDateTime": "2026-10-23T09:52:03Z",
        }
    ],
    REUNION_2: [
        {
            "id": "MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmY4ZS1hNjMxNzY5MTI4ZjQ",
            "meetingId": REUNION_2,
            "callId": "bf730ff1-15c4-4660-bdfa-a2f56f2e37a7",
            "transcriptContentUrl": (
                f"users/organizador@banco.example/onlineMeetings/{REUNION_2}"
                "/transcripts/MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmY4ZS1hNjMxNzY5MTI4ZjQ/content"
            ),
            "createdDateTime": "2026-10-22T16:05:41Z",
        }
    ],
}

#: Grabaciones indexadas por identificador de reunión.
GRABACIONES: dict[str, list[dict[str, Any]]] = {
    REUNION_1: [
        {
            "id": "c9a1e5f2-1d3b-4a67-9e2f-0b7c5a8d3f10",
            "createdDateTime": "2026-10-23T09:52:00Z",
        }
    ],
    REUNION_2: [],
}

#: Informes de asistencia indexados por identificador de reunión.
INFORMES: dict[str, list[dict[str, Any]]] = {
    REUNION_1: [
        {
            "id": "b7e2c9a4-5f1d-4a83-b6c2-9d4e8f3a1b22",
            "participants": PARTICIPANTES_1,
        }
    ],
    REUNION_2: [],
}

#: Reuniones en línea, para la consulta por organizador.
REUNIONES = [
    {
        "id": REUNION_1,
        "subject": "Comité de seguimiento · programa strategic",
        "startDateTime": "2026-10-23T09:00:00Z",
        "endDateTime": "2026-10-23T09:48:00Z",
        "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/mock-1",
        "chatInfo": {"threadId": "19:meeting_mock_1@thread.v2"},
    },
    {
        "id": REUNION_2,
        "subject": "Revisión de riesgos · programa strategic",
        "startDateTime": "2026-10-22T15:30:00Z",
        "endDateTime": "2026-10-22T16:00:00Z",
        "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/mock-2",
        "chatInfo": {"threadId": "19:meeting_mock_2@thread.v2"},
    },
    {
        "id": REUNION_3,
        "subject": "Punto diario · sin transcripción",
        "startDateTime": "2026-10-21T09:00:00Z",
        "endDateTime": "2026-10-21T09:20:00Z",
        "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/mock-3",
        "chatInfo": {"threadId": "19:meeting_mock_3@thread.v2"},
    },
]

#: Reuniones que el doble conoce. Una reunión que no está aquí devuelve
#: 404, como en Graph. Una que está aquí pero no tiene transcripción
#: devuelve una lista vacía, que es el caso de una reunión en la que no
#: se activó la transcripción.
REUNIONES_CONOCIDAS = {REUNION_1, REUNION_2, REUNION_3}

#: Si se pone a True, el doble responde como un tenant que tiene las
#: transcripciones desactivadas.
TRANSCRIPCIONES_DESHABILITADAS = False

#: Si se pone a True, el doble responde 429 a las primeras peticiones.
LIMITAR_TASA = 0


def _desescapar(valor: str) -> str:
    """Desescapa un valor de la cadena de consulta.

    Las fechas van URL-encoded (`2026-10-23T00%3A00%3A00Z`) y, sin
    descodificar, la comparación de cadenas del filtro de fechas no
    ordena bien.
    """
    from urllib.parse import unquote

    return unquote(valor)


class ManejadorMock(BaseHTTPRequestHandler):
    """Atiende las rutas de Graph que usa la captura de reuniones."""

    #: Contador de peticiones, para el límite de tasa.
    peticiones = 0

    def log_message(self, formato: str, *args: Any) -> None:
        """Silencia el log por stderr: el doble no debe ensuciar la salida."""

    # -- utilidades --------------------------------------------------
    def _responder(self, codigo: int, cuerpo: Any, *, binario: bool = False) -> None:
        """Escribe una respuesta."""
        if binario:
            datos = cuerpo if isinstance(cuerpo, bytes) else str(cuerpo).encode("utf-8")
            tipo = "video/mp4"
        else:
            datos = json.dumps(cuerpo).encode("utf-8")
            tipo = "application/json"
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _error(self, codigo: int, mensaje: str, subcodigo: str = "") -> None:
        """Escribe un error con el formato de Graph."""
        error: dict[str, Any] = {"code": subcodigo or "BadRequest", "message": mensaje}
        if subcodigo:
            error["innerError"] = {"code": subcodigo}
        self._responder(codigo, {"error": error})

    # -- verbo -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - el nombre lo impone la librería
        """Atiende las lecturas."""
        type(self).peticiones += 1
        if LIMITAR_TASA and type(self).peticiones <= LIMITAR_TASA:
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if TRANSCRIPCIONES_DESHABILITADAS and "transcripts" in self.path:
            self._error(
                403,
                "El acceso a las transcripciones está desactivado en este tenant.",
                "GraphAccessToTranscriptsDisabled",
            )
            return

        ruta = self.path.split("?")[0]
        consulta = self.path.split("?")[1] if "?" in self.path else ""

        # GET /users/{u}/onlineMeetings/getAllTranscripts(...)
        if "getAllTranscripts" in ruta:
            self._responder(200, {"value": self._transcripciones_del_rango(self.path)})
            return

        m = re.search(
            r"/users/[^/]+/onlineMeetings/([^/]+)/transcripts/([^/]+)/content", ruta
        )
        if m:
            reunion, _transcripcion = m.group(1), m.group(2)
            if not TRANSCRIPCIONES.get(reunion):
                self._error(404, "Transcripción no encontrada.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt")
            cuerpo = VTT_EJEMPLO.encode("utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
            return

        m = re.search(r"/users/[^/]+/onlineMeetings/([^/]+)/transcripts", ruta)
        if m:
            reunion = m.group(1)
            if reunion not in REUNIONES_CONOCIDAS:
                self._error(404, f"No existe la reunión {reunion} en el doble.")
                return
            self._responder(200, {"value": TRANSCRIPCIONES.get(reunion, [])})
            return

        m = re.search(
            r"/users/[^/]+/onlineMeetings/([^/]+)/recordings/([^/]+)/content", ruta
        )
        if m:
            self._responder(200, MP4_EJEMPLO, binario=True)
            return

        m = re.search(r"/users/[^/]+/onlineMeetings/([^/]+)/recordings", ruta)
        if m:
            self._responder(200, {"value": GRABACIONES.get(m.group(1), [])})
            return

        m = re.search(r"/users/[^/]+/onlineMeetings/([^/]+)/attendanceReports", ruta)
        if m:
            self._responder(200, {"value": INFORMES.get(m.group(1), [])})
            return

        m = re.search(r"/users/[^/]+/onlineMeetings/([^/]+)$", ruta)
        if m:
            reunion = m.group(1)
            encontrada = next(
                (r for r in REUNIONES if r["id"] == reunion), None
            )
            if encontrada is None:
                self._error(404, f"No existe la reunión {reunion} en el doble.")
                return
            self._responder(200, encontrada)
            return

        if re.search(r"/users/[^/]+/onlineMeetings$", ruta):
            self._responder(200, {"value": REUNIONES})
            return

        self._error(404, f"Ruta no encontrada en el doble: {ruta}")

    def _transcripciones_del_rango(self, ruta_completa: str) -> list[dict[str, Any]]:
        """Devuelve las transcripciones cuyo rango de fechas encaja.

        Las fechas viajan en la **ruta**, no en la cadena de consulta:
        Graph las toma como parámetros de la función
        ``getAllTranscripts(...)``. Es lo que hace el cliente real, y
        el doble tiene que leerlas donde están.
        """
        if "startDateTime" not in ruta_completa:
            return [t for lista in TRANSCRIPCIONES.values() for t in lista]
        m = re.search(r"startDateTime=([^&,)]+)", ruta_completa)
        desde = _desescapar(m.group(1)) if m else ""
        m = re.search(r"endDateTime=([^&,)]+)", ruta_completa)
        hasta = _desescapar(m.group(1)) if m else ""
        resultado = []
        for lista in TRANSCRIPCIONES.values():
            for transcripcion in lista:
                creada = transcripcion.get("createdDateTime", "")
                if desde and creada < desde:
                    continue
                if hasta and creada >= hasta:
                    continue
                resultado.append(transcripcion)
        return resultado


def crear_servidor_mock(puerto: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Levanta el doble en un hilo aparte y devuelve el servidor y su base.

    Args:
        puerto: puerto en el que escuchar. 0 = el sistema elige uno libre.

    Returns:
        La tupla ``(servidor, url_base)``.
    """
    servidor = ThreadingHTTPServer(("127.0.0.1", puerto), ManejadorMock)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    host, puerto_real = servidor.server_address[0], servidor.server_address[1]
    return servidor, f"http://{host}:{puerto_real}/v1.0"


def main() -> None:
    """Levanta el doble y lo deja escuchando hasta que se corte."""
    analizador = argparse.ArgumentParser(
        description="Doble de Microsoft Graph para reuniones de Teams"
    )
    analizador.add_argument("--puerto", type=int, default=8099)
    analizador.add_argument(
        "--sin-transcripciones",
        action="store_true",
        help="Responde como un tenant con las transcripciones desactivadas.",
    )
    argumentos = analizador.parse_args()

    global TRANSCRIPCIONES_DESHABILITADAS
    TRANSCRIPCIONES_DESHABILITADAS = argumentos.sin_transcripciones

    servidor, base = crear_servidor_mock(argumentos.puerto)
    print(f"Doble de Microsoft Graph escuchando en {base}")
    print("  CTRL+C para detenerlo")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nDetenido.")
    finally:
        servidor.shutdown()


if __name__ == "__main__":
    main()
