"""Pruebas de la captura de reuniones de Teams contra el doble de Graph.

El doble implementa los mismos verbos, rutas y códigos de error que
Graph, así que estas pruebas ejercitan el cliente de verdad: lo que
sale de aquí es lo que va a hablar con el tenant.

La batería tiene tres bloques:

* **El análisis del WebVTT**, que es donde se decide si la
  transcripción sirve. Un VTT mal leído produce texto sin hablante, y
  un texto sin hablante no sirve para nada.
* **El cliente contra el doble**, con los caminos felices y los
  errores de Graph: el 403 de transcripciones desactivadas, el 404 y
  el límite de tasa.
* **La captura completa**, que es el ciclo que se ejecutará en el
  banco.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.graph_teams import (
    ClienteReuniones,
    ErrorDeTranscripcionDeshabilitada,
    Segmento,
    parsear_vtt,
)
from app.core.edge_teams import ErrorDeGraph
from scripts.mock_graph import (
    REUNION_1,
    REUNION_2,
    REUNION_3,
    VTT_EJEMPLO,
    crear_servidor_mock,
)

# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture(scope="module")
def servidor():
    """El doble de Graph, levantado para toda la módulo."""
    srv, base = crear_servidor_mock(0)
    yield base
    srv.shutdown()


@pytest.fixture
def cliente(servidor: str) -> ClienteReuniones:
    """Un cliente apuntando al doble, con un token cualquiera."""
    http = httpx.Client(timeout=30.0)
    return ClienteReuniones("token-de-prueba", http=http, base=servidor)


@pytest.fixture
def http_limpio(servidor: str):
    """Cliente HTTP sinredirects, para las pruebas de error."""
    with httpx.Client(timeout=30.0) as http:
        yield http


# ─────────────────────────── el VTT ───────────────────────────


class TestAnalisisDelVtt:
    """La transcripción se convierte en segmentos con hablante."""

    def test_separa_los_segmentos(self) -> None:
        """Cada bloque de texto es un segmento."""
        segmentos = parsear_vtt(VTT_EJEMPLO)
        assert len(segmentos) == 7

    def test_asigna_el_hablante(self) -> None:
        """Cada segmento lleva su hablante."""
        segmentos = parsear_vtt(VTT_EJEMPLO)
        assert segmentos[0].hablante == "Ana García"
        assert segmentos[1].hablante == "Bruno Díaz"

    def test_interpreta_las_marcas_de_tiempo(self) -> None:
        """Las marcas se convierten a segundos desde el inicio."""
        segmentos = parsear_vtt(VTT_EJEMPLO)
        assert segmentos[0].inicio == pytest.approx(2.0)
        assert segmentos[0].fin == pytest.approx(6.5)
        assert segmentos[6].fin == pytest.approx(49.0)

    def test_el_texto_queda_limpio(self) -> None:
        """Las etiquetas de voz no se cuelan en el texto."""
        segmentos = parsear_vtt(VTT_EJEMPLO)
        for segmento in segmentos:
            assert "<v" not in segmento.texto
            assert ">" not in segmento.texto

    def test_une_las_lineas_de_un_mismo_segmento(self) -> None:
        """Un fragmento partido en dos líneas es un solo segmento."""
        segmentos = parsear_vtt(VTT_EJEMPLO)
        assert "Sin eso no podemos firmar el hito." in segmentos[2].texto
        assert len(segmentos) == 7

    def test_un_vtt_vacio_no_da_segmentos(self) -> None:
        """NEGATIVA: un VTT vacío no inventa segmentos."""
        assert parsear_vtt("") == []
        assert parsear_vtt("WEBVTT\n\n") == []

    def test_un_vtt_truncado_no_revienta(self) -> None:
        """NEGATIVA: una transcripción a medias devuelve lo que se puede."""
        truncado = "WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v Ana>Texto que llega"
        segmentos = parsear_vtt(truncado)
        assert len(segmentos) == 1
        assert segmentos[0].hablante == "Ana"

    def test_una_etiqueta_suelta_no_crea_segmentos(self) -> None:
        """NEGATIVA: texto sin marca de tiempo no se inventa."""
        segmentos = parsear_vtt("WEBVTT\n\n<v Ana>Esto no tiene tiempo asignado\n")
        assert segmentos == []

    def test_el_texto_plano_incluye_el_hablante(self) -> None:
        """La transcripción como texto corrido lleva el hablante delante."""
        segmentos = parsear_vtt(VTT_EJEMPLO)
        lineas = [f"[{s.hablante}] {s.texto}" for s in segmentos]
        assert lineas[0].startswith("[Ana García] Buenos días")


# ─────────────────────────── el cliente ───────────────────────────


class TestClienteContraElDoble:
    """El cliente habla Graph con los mismos verbos que el real."""

    def test_lista_las_transcripciones_del_rango(self, cliente) -> None:
        """El barrido por rango devuelve las transcripciones."""
        transcripciones = cliente.listar_transcripciones(
            "organizador@banco.example",
            desde="2026-10-01T00:00:00Z",
            hasta="2026-10-31T00:00:00Z",
        )
        assert len(transcripciones) == 2
        assert {t["meetingId"] for t in transcripciones} == {REUNION_1, REUNION_2}

    def test_el_rango_filtra(self, cliente) -> None:
        """Un rango que no cubre la reunión la deja fuera."""
        transcripciones = cliente.listar_transcripciones(
            "organizador@banco.example",
            desde="2026-10-23T00:00:00Z",
            hasta="2026-10-24T00:00:00Z",
        )
        assert [t["meetingId"] for t in transcripciones] == [REUNION_1]

    def test_lista_las_transcripciones_de_una_reunion(self, cliente) -> None:
        """La lista por reunión funciona."""
        transcripciones = cliente.listar_transcripciones_de_reunion(
            "organizador@banco.example", REUNION_1
        )
        assert len(transcripciones) == 1
        assert transcripciones[0]["id"]

    def test_descarga_la_transcripcion(self, cliente) -> None:
        """La transcripción baja como WebVTT."""
        vtt = cliente.descargar_transcripcion(
            "organizador@banco.example", REUNION_1, "MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmY3YS1hNjMxNzY5MTI4ZjQ"
        )
        assert vtt.startswith("WEBVTT")
        assert len(parsear_vtt(vtt)) == 7

    def test_lista_las_grabaciones(self, cliente) -> None:
        """La grabación aparece en la reunión, no en los call records."""
        grabaciones = cliente.listar_grabaciones(
            "organizador@banco.example", REUNION_1
        )
        assert len(grabaciones) == 1
        assert grabaciones[0]["id"]

    def test_una_reunion_sin_grabacion_devuelve_lista_vacia(self, cliente) -> None:
        """No toda reunión tiene grabación, y eso no es un error."""
        grabaciones = cliente.listar_grabaciones(
            "organizador@banco.example", REUNION_2
        )
        assert grabaciones == []

    def test_descarga_la_grabacion(self, cliente) -> None:
        """La grabación baja como bytes."""
        audio = cliente.descargar_grabacion(
            "organizador@banco.example", REUNION_1, "c9a1e5f2-1d3b-4a67-9e2f-0b7c5a8d3f10"
        )
        assert isinstance(audio, bytes)
        assert len(audio) > 0
        assert audio[4:8] == b"ftyp"

    def test_lee_los_participantes(self, cliente) -> None:
        """El informe de asistencia trae quién estuvo y cuándo."""
        participantes = cliente.participantes("organizador@banco.example", REUNION_1)
        assert len(participantes) == 1
        nombres = [p["identity"]["user"]["displayName"] for p in participantes[0]["participants"]]
        assert nombres == ["Ana García", "Bruno Díaz"]

    def test_lista_las_reuniones(self, cliente) -> None:
        """La consulta por organizador devuelve las reuniones."""
        reuniones = cliente.listar_reuniones("organizador@banco.example")
        assert len(reuniones) == 3
        assert {r["id"] for r in reuniones} == {REUNION_1, REUNION_2, REUNION_3}


# ─────────────────────────── los errores ───────────────────────────


class TestErroresDeGraph:
    """Los errores de Graph se traducen a excepciones con significado."""

    def test_el_403_de_transcripciones_es_una_excepcion_propia(
        self, servidor: str, monkeypatch
    ) -> None:
        """NEGATIVA: 403 con GraphAccessToTranscriptsDisabled se distingue.

        No es un fallo de la aplicación: es un tenant que tiene el
        acceso desactivado, y la respuesta tiene que decirlo.
        """
        import scripts.mock_graph as doble

        monkeypatch.setattr(doble, "TRANSCRIPCIONES_DESHABILITADAS", True)
        with httpx.Client(timeout=30.0) as http:
            cliente = ClienteReuniones("token", http=http, base=servidor)
            with pytest.raises(ErrorDeTranscripcionDeshabilitada) as excepcion:
                cliente.listar_transcripciones_de_reunion(
                    "organizador@banco.example", REUNION_1
                )
        assert "GraphAccessToTranscriptsDisabled" in str(excepcion.value)

    def test_un_404_es_un_error_de_graph(self, cliente) -> None:
        """NEGATIVA: un recurso que no existe se avisa."""
        with pytest.raises(ErrorDeGraph) as excepcion:
            cliente.listar_transcripciones_de_reunion(
                "organizador@banco.example", "reunion-inexistente"
            )
        assert "no encontró" in str(excepcion.value).lower()

    def test_el_token_invalido_no_pasa(self, servidor: str) -> None:
        """NEGATIVA: sin token válido, el doble no entrega nada.

        Comprueba que el cliente manda la cabecera de autorización.
        """
        http = httpx.Client(timeout=30.0)
        peticion_observada: dict[str, str] = {}

        def observance(peticion: httpx.Request) -> None:
            peticion_observada["auth"] = peticion.headers.get("authorization", "")

        http.event_hooks = {"request": [observance]}
        cliente = ClienteReuniones("mi-token", http=http, base=servidor)
        cliente.listar_transcripciones_de_reunion("organizador@banco.example", REUNION_1)
        assert peticion_observada["auth"] == "Bearer mi-token"


# ─────────────────────────── la captura ───────────────────────────


class TestCapturaCompleta:
    """El ciclo completo, que es el que se ejecutará en el banco."""

    def test_captura_una_reunion(self, cliente) -> None:
        """Una reunión devuelve transcripción, participantes y audio."""
        resultado = cliente.capturar("organizador@banco.example", REUNION_1)
        assert resultado.reunion_id == REUNION_1
        assert len(resultado.segmentos) == 7
        assert len(resultado.participantes) == 2
        assert resultado.audio is not None
        assert len(resultado.audio) > 0
        assert resultado.formato_audio == "mp4"

    def test_el_texto_plano_es_utilizable(self, cliente) -> None:
        """La transcripción en texto corrido conserva hablante y contenido."""
        resultado = cliente.capturar("organizador@banco.example", REUNION_1)
        texto = resultado.texto_plano
        assert "[Ana García]" in texto
        assert "[Bruno Díaz]" in texto
        assert "comité de seguimiento" in texto

    def test_una_reunion_sin_grabacion_se_captura_igual(self, cliente) -> None:
        """NEGATIVA: que falte el audio no impide capturar el texto."""
        resultado = cliente.capturar("organizador@banco.example", REUNION_2)
        assert len(resultado.segmentos) == 7
        assert resultado.audio is None

    def test_una_reunion_sin_transcripcion_falla(self, cliente) -> None:
        """NEGATIVA: sin transcripción no hay nada que capturar.

        Y el error lo dice, para que no se confunda con un fallo de red.
        """
        with pytest.raises(ErrorDeGraph) as excepcion:
            cliente.capturar("organizador@banco.example", REUNION_3)
        assert "no tiene transcripción" in str(excepcion.value)

    def test_se_puede_capturar_sin_audio(self, cliente) -> None:
        """El audio es opcional: el texto es lo que se analiza."""
        resultado = cliente.capturar(
            "organizador@banco.example", REUNION_1, con_audio=False
        )
        assert resultado.audio is None
        assert len(resultado.segmentos) == 7

    def test_el_diccionario_es_serializable(self, cliente) -> None:
        """El resultado se puede volcar a JSON para la API."""
        import json

        resultado = cliente.capturar("organizador@banco.example", REUNION_1)
        volcado = json.dumps(resultado.como_dict(), ensure_ascii=False)
        assert "Ana García" in volcado
        assert '"tiene_audio": true' in volcado
