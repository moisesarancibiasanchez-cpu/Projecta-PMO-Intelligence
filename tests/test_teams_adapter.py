"""Pruebas del adaptador de Microsoft Teams del Edge Connector.

El adaptador de Teams es la pieza que conecta un sistema real con el
perímetro del banco, así que sus pruebas se centran en tres preguntas:

1. **¿Lee solo lo que la política permite?** Un canal no habilitado no
   se lee, y de un mensaje no sale ningún campo que no esté en la lista
   blanca.
2. **¿Sobrevive a los fallos?** Un 429 de Graph se respeta en vez de
   martillear la API, y un cursor corrupto no rompe la siguiente pasada.
3. **¿El cambio es incremental?** La segunda pasada no relee lo ya
   leído, porque el cursor delta se guarda.

Ninguna de estas pruebas necesita red: Graph se sustituye por un doble
que devuelve respuestas fijas. La prueba de que el adaptador funciona
contra el Graph real no se puede hacer aquí, y no se finge: en la guía
de instalación hay un procedimiento explícito para verificarla contra el
tenant del cliente.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from app.core.edge_teams import (
    CAMPOS_LOCALES_BLOQUEADOS,
    CAMPOS_PERMITIDOS,
    AdaptadorTeams,
    ErrorDeGraph,
)

# ─────────────────────────── dobles de prueba ───────────────────────────


class RespuestaFalsa:
    """Respuesta HTTP mínima, con lo que el adaptador necesita leer."""

    def __init__(self, codigo: int, cuerpo: dict[str, Any], cabeceras: dict[str, str] | None = None) -> None:
        self.status_code = codigo
        self._cuerpo = cuerpo
        self.headers = cabeceras or {}
        self.text = json.dumps(cuerpo, ensure_ascii=False)

    def json(self) -> dict[str, Any]:
        """Devuelve el cuerpo de la respuesta."""
        return self._cuerpo


class GraphFalso:
    """Sustituto del cliente HTTP de Microsoft Graph.

    Registra las URLs que se le piden, para poder comprobar que el
    adaptador **no** llama a lo que la política prohíbe, y reproduce el
    ``429`` con su ``Retry-After``.
    """

    def __init__(self, respuestas: dict[str, dict[str, Any]] | None = None) -> None:
        self.respuestas = respuestas or {}
        self.peticiones: list[str] = []
        self.estado_429 = 0
        self.peticiones_429 = 0

    def get(self, url: str, headers: dict[str, str] | None = None, timeout: float = 0.0) -> RespuestaFalsa:
        """Responde como lo haría Graph."""
        self.peticiones.append(url)
        if self.estado_429 > 0:
            self.estado_429 -= 1
            self.peticiones_429 += 1
            return RespuestaFalsa(
                429, {"error": {"code": "TooManyRequests"}}, {"Retry-After": "0"}
            )
        for prefijo, cuerpo in self.respuestas.items():
            if prefijo in url:
                return RespuestaFalsa(200, cuerpo)
        return RespuestaFalsa(404, {"error": {"code": "NotFound"}})


@pytest.fixture()
def sensor(tmp_path: pathlib.Path):
    """Sensor del Edge Connector sobre un estado temporal de la prueba.

    El estado es **por prueba**: el cursor delta es persistente a
    propósito en producción, y si se compartiera entre pruebas una
    dejaría el cursor puesto a la siguiente, que es exactamente el
    cruce de estado que estas pruebas no quieren.
    """
    from scripts.edge_conector import ColaLocal, RegistroLocal, Sensor

    estado = tmp_path / "edge"
    estado.mkdir(parents=True, exist_ok=True)
    return Sensor(
        nombre="edge-conector-teams",
        huella_clave="0" * 64,
        clave_privada=b"0" * 32,
        cola=ColaLocal(ruta=estado / "cola.json"),
        registro=RegistroLocal(ruta=estado / "registro.jsonl"),
    )


def _adaptador(sensor, grafo: GraphFalso, **kwargs: Any) -> AdaptadorTeams:
    """Construye un adaptador con el cliente HTTP sustituido.

    La clave del dispositivo se genera aquí: en la Variante A es la misma
    clave Ed25519 que firma los eventos, y sin ella el adaptador no
    puede construir un evento firmado.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from app.core.edge import huella_de_clave

    privada = Ed25519PrivateKey.generate().private_bytes_raw()
    publica = Ed25519PrivateKey.from_private_bytes(privada).public_key().public_bytes_raw()
    return AdaptadorTeams(
        token="token-de-prueba",
        estado=sensor.cola.ruta.parent,
        huella_clave=huella_de_clave(publica.hex()),
        clave_privada=privada,
        http=grafo,
        registrar=lambda accion, detalle: sensor.registro.anotar(accion, detalle),
        **kwargs,
    )


def _mensaje(identificador: str = "m-1") -> dict[str, Any]:
    """Un mensaje de Graph de ejemplo, con ruido incluido."""
    return {
        "id": identificador,
        "createdDateTime": "2026-10-23T09:00:00Z",
        "webUrl": "https://teams.microsoft.com/l/message/1",
        "body": {"contentType": "text", "content": "Comprometido a entregar el 30"},
        "from": {"user": {"id": "u-1", "displayName": "Ana"}},
        "subject": "Avance del proyecto",
        "reactions": [{"reactionType": "like"}],
        "mentions": [{"id": 0}],
        "attachments": [{"id": "a-1"}],
        "importance": "high",
        "policyViolation": {"severity": "none"},
        "channelIdentity": {"teamId": "t-1", "channelId": "c-1"},
    }


# ─────────────────────────── política ───────────────────────────


class TestPoliticaLocal:
    """Lo que la política no habilita, no se lee."""

    def test_un_canal_no_habilitado_no_se_lee(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVA: la política local acota los recursos."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-permitido")
        señales = adaptador.leer_canal("t-1", "c-1")
        assert señales == [], "se leyó un canal que la política no habilita"

    def test_un_canal_habilitado_se_lee(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El canal habilitado sí se lee."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        señales = adaptador.leer_canal("t-1", "c-1")
        assert len(señales) == 1
        assert señales[0]["mensaje_id"] == "m-1"


# ─────────────────────────── minimización ───────────────────────────


class TestMinimizacionLocal:
    """La política local se aplica antes de que el dato entre en la cola."""

    def test_los_campos_bloqueados_no_salen(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVA: reacciones, menciones, adjuntos e importancia no salen."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        señal = adaptador.leer_canal("t-1", "c-1")[0]
        for campo in CAMPOS_LOCALES_BLOQUEADOS:
            assert campo not in señal, f"el campo {campo} se copió a la señal"

    def test_la_lista_blanca_se_respeta(self, sensor) -> None:
        """NEGATIVA: ningún campo fuera de la lista blanca sale."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        señal = adaptador.leer_canal("t-1", "c-1")[0]
        for clave in señal:
            assert clave in CAMPOS_PERMITIDOS, f"el campo {clave} no está en la lista blanca"

    def test_el_cuerpo_del_mensaje_si_se_traslada(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El texto del mensaje es el dato mínimo imprescindible."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        señal = adaptador.leer_canal("t-1", "c-1")[0]
        assert señal["cuerpo"] == "Comprometido a entregar el 30"


# ─────────────────────────── incrementalidad ───────────────────────────


class TestCursorDelta:
    """La segunda pasada no relee lo ya leído."""

    def test_el_cursor_se_guarda(self, sensor, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tras la primera pasada, el cursor queda en disco."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?token=abc",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        adaptador.leer_canal("t-1", "c-1")
        cursor = adaptador.leer_cursor("canal_t-1_c-1")
        assert cursor == "https://graph.microsoft.com/delta?token=abc"

    def test_la_segunda_pasada_usa_el_cursor(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La segunda pasada pide el delta, no la lista completa."""
        grafo = GraphFalso(
            {
                "https://graph.microsoft.com/delta?token=abc": {
                    "value": [_mensaje("m-2")],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?token=def",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        # Cursor de una vuelta anterior.
        adaptador.guardar_cursor("canal_t-1_c-1", "https://graph.microsoft.com/delta?token=abc")
        adaptador.leer_canal("t-1", "c-1")
        assert grafo.peticiones[0].startswith("https://graph.microsoft.com/delta")
        assert "/teams/" not in grafo.peticiones[0]
        # Y el cursor avanza al deltaLink nuevo.
        assert (
            adaptador.leer_cursor("canal_t-1_c-1")
            == "https://graph.microsoft.com/delta?token=def"
        )

    def test_un_cursor_corrupto_no_rompe_la_pasada(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVA: un cursor ilegible se descarta, no aborta la lectura."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?token=abc",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        ruta = adaptador.ruta_cursor("canal_t-1_c-1")
        ruta.write_text("{ esto no es json", encoding="utf-8")
        señales = adaptador.leer_canal("t-1", "c-1")
        assert len(señales) == 1


# ─────────────────────────── robustez ───────────────────────────


class TestRobustez:
    """El conector es un cliente que comparte cuota con todo el tenant."""

    def test_un_429_se_respeta_y_se_reintenta(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVA:Graph limita; el adaptador espera, no martillea."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        grafo.estado_429 = 2
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        señales = adaptador.leer_canal("t-1", "c-1")
        assert len(señales) == 1
        assert grafo.peticiones_429 == 2, "no se respetó el límite de Graph"

    def test_un_error_de_graph_no_pasa_desapercibido(self, sensor) -> None:
        """NEGATIVA: un 404 tiene que ser un error, no una lista vacía."""
        grafo = GraphFalso({})
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        with pytest.raises(ErrorDeGraph):
            adaptador.leer_canal("t-1", "c-inexistente")

    def test_una_ronda_infinita_se_corta(self, sensor) -> None:
        """NEGATIVA: un `nextLink` que no termina se aborta, no se cuelga."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.nextLink": "https://graph.microsoft.com/otra",
                },
                "https://graph.microsoft.com/otra": {
                    "value": [_mensaje("m-2")],
                    "@odata.nextLink": "https://graph.microsoft.com/otra",
                },
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        with pytest.raises(ErrorDeGraph):
            adaptador.leer_canal("t-1", "c-1")


# ─────────────────────────── entrega ───────────────────────────


class TestEntrega:
    """Lo leído sale firmado y con el tipo de receptor correcto."""

    def test_la_entrega_usa_el_tipo_de_receptor_admitido(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El tipo emitido está en la lista cerrada de receptores."""
        from app.core.edge import RECEPTORES_ADMITIDOS

        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        señales = adaptador.leer_canal("t-1", "c-1")
        eventos = adaptador.construir_eventos(señales)
        assert eventos
        assert eventos[0]["tipo"] in RECEPTORES_ADMITIDOS
        assert eventos[0]["tipo"] == "mensaje_canal"

    def test_el_evento_lleva_firma_y_correlacion(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cada evento va firmado y con su `correlation_id`."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        señales = adaptador.leer_canal("t-1", "c-1")
        evento = adaptador.construir_eventos(señales)[0]
        assert len(evento["firma"]) == 128
        assert evento["correlation_id"]
        assert evento["uso_previsto"]

    def test_la_lectura_queda_registrada_en_local(
        self, sensor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El banco conserva su copia de lo que se leyó."""
        grafo = GraphFalso(
            {
                "/teams/t-1/channels/c-1/messages": {
                    "value": [_mensaje()],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta?c=1",
                }
            }
        )
        adaptador = _adaptador(sensor, grafo, canal_permitido="c-1")
        señales = adaptador.leer_canal("t-1", "c-1")
        adaptador.construir_eventos(señales)
        acciones = [e["accion"] for e in sensor.registro.leer()]
        assert "lectura_teams" in acciones
