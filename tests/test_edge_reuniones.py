"""Pruebas de la capa de decisión de escucha de reuniones.

Esta es la pieza que decide si se captura audio, y la que va a
responder ante un inspector o ante un empleado que pregunta qué se ha
grabado. Por eso las pruebas se centran en **tres** cosas:

1. **Que se escuche solo cuando toca.** La positively es la parte fácil;
   lo que hay que demostrar es que se **niega** en todos los demás
   casos, y que dice por qué.
2. **Que el motivo sea siempre concreto.** Una decisión sin motivo no
   es auditable, y una decisión positiva sin motivo tampoco.
3. **Que el fallo sea cerrado.** Sin registro de consentimientos, no se
   escucha nada, y no hay forma de «desbloquearlo» por accidente.

La captura en sí no se prueba aquí: eso es código de sistema operativo
y vive en los adaptadores. Aquí se prueba **la decisión**, que es la
que tiene consecuencias.
"""

from __future__ import annotations

from app.core.edge_reuniones import (
    MOTIVOS,
    DetectorDeReunion,
    ReunionConsentida,
    SesionDeAudio,
)

#: Una reunión consentida de ejemplo.
REUNION = ReunionConsentida(
    identificador="reunion-2026-10-23-001",
    proyecto_id="proyecto-1",
    participantes_autorizados=("ana", "bruno"),
    procesos_permitidos=("Microsoft Teams",),
)

#: Sesión de salida de Teams: la aplicación reproduciendo audio.
TEAMS_SALIDA = SesionDeAudio(
    proceso="Microsoft Teams",
    identificador_proceso="1234",
    reproduccion=True,
)

#: Sesión de entrada de Teams: el micrófono abierto.
TEAMS_ENTRADA = SesionDeAudio(
    proceso="Microsoft Teams",
    identificador_proceso="1234",
    captura=True,
)

#: Una sesión de un proceso no habilitado.
SPOTIFY = SesionDeAudio(
    proceso="Spotify",
    identificador_proceso="9999",
    reproduccion=True,
)


# ─────────────────────────── se escucha ───────────────────────────


class TestSeEscucha:
    """El camino positivo, y solo con consentimiento de por medio."""

    def test_reunion_consentida_se_escucha(self) -> None:
        """Con registro, coincidencia y señal, se escucha."""
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion="reunion-2026-10-23-001",
        )
        decision = detector.decidir((TEAMS_SALIDA, TEAMS_ENTRADA))
        assert decision.escuchar is True
        assert decision.motivo is None
        assert decision.reunion is not None
        assert decision.reunion.identificador == REUNION.identificador

    def test_la_decision_positiva_lleva_motivo(self) -> None:
        """Una decisión afirmativa también se explica."""
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=REUNION.identificador,
        )
        decision = detector.decidir((TEAMS_SALIDA,))
        assert decision.explicacion
        assert "reunion-2026-10-23-001" in decision.explicacion
        assert decision.resumen().startswith("ESCUCHA autorizada")

    def test_el_proceso_de_salida_es_el_origen(self) -> None:
        """El audio se toma de la aplicación que reproduce, no de la que captura."""
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=REUNION.identificador,
        )
        decision = detector.decidir((TEAMS_SALIDA, TEAMS_ENTRADA))
        assert decision.proceso_origen == "Microsoft Teams"


# ─────────────────────────── no se escucha ───────────────────────────


class TestNoSeEscucha:
    """La parte negativa, que es la que tiene consecuencias."""

    def test_sin_registro_no_se_escucha(self) -> None:
        """NEGATIVA: sin registro de consentimientos, no se escucha nada."""
        detector = DetectorDeReunion(
            registro=(),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion="reunion-2026-10-23-001",
        )
        decision = detector.decidir((TEAMS_SALIDA, TEAMS_ENTRADA))
        assert decision.escuchar is False
        assert decision.motivo == "sin_consentimiento_de_reunion"

    def test_reunion_no_consentida_no_se_escucha(self) -> None:
        """NEGATIVA: una reunión que no está en el registro no se escucha.

        Este es el caso que importa: el detector ve Teams activa, ve el
        micrófono abierto, y aun así se niega, porque la reunión en
        curso no está consentida.
        """
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion="reunion-que-no-existe",
        )
        decision = detector.decidir((TEAMS_SALIDA, TEAMS_ENTRADA))
        assert decision.escuchar is False
        assert decision.motivo == "reunion_no_consentida"

    def test_proceso_no_habilitado_no_se_escucha(self) -> None:
        """NEGATIVA: una aplicación que no está en la política no se escucha."""
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=REUNION.identificador,
        )
        decision = detector.decidir((SPOTIFY,))
        assert decision.escuchar is False
        assert decision.motivo == "sin_senal_de_reunion"

    def test_sin_senal_de_reunion_no_se_escucha(self) -> None:
        """NEGATIVA: la aplicación habilitada pero inactiva no se escucha."""
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=REUNION.identificador,
        )
        teams_inactivo = SesionDeAudio(
            proceso="Microsoft Teams", identificador_proceso="1234"
        )
        decision = detector.decidir((teams_inactivo,))
        assert decision.escuchar is False
        assert decision.motivo == "sin_senal_de_reunion"

    def test_identidad_no_verificada_no_se_escucha(self) -> None:
        """NEGATIVA: en Linux, un proceso que se hace pasar por Teams no cuela.

        Las propiedades `application.name` de PipeWire las declara el
        propio cliente: se pueden falsear. Por defecto, un proceso con
        identidad no verificada no habilita la escucha.
        """
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=REUNION.identificador,
        )
        falso = SesionDeAudio(
            proceso="Microsoft Teams",
            identificador_proceso="6666",
            reproduccion=True,
            identidad_verificada=False,
        )
        decision = detector.decidir((falso,))
        assert decision.escuchar is False
        assert decision.motivo == "sin_senal_de_reunion"

    def test_identidad_no_verificada_se_puede_admitir_explicitamente(self) -> None:
        """La excepción existe, pero hay que pedirla explícitamente."""
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=REUNION.identificador,
            permitir_audio_no_verificado=True,
        )
        falso = SesionDeAudio(
            proceso="Microsoft Teams",
            identificador_proceso="6666",
            reproduccion=True,
            identidad_verificada=False,
        )
        decision = detector.decidir((falso,))
        assert decision.escuchar is True

    def test_sin_identificador_y_varias_reuniones_no_se_escucha(self) -> None:
        """NEGATIVA: si hay varias reuniones consentidas y no se sabe cuál
        es, no se escucha. Adivinar sería el error que el módulo evita."""
        otra = ReunionConsentida(
            identificador="reunion-2026-10-24-002",
            proyecto_id="proyecto-2",
            procesos_permitidos=("Microsoft Teams",),
        )
        detector = DetectorDeReunion(
            registro=(REUNION, otra),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=None,
        )
        decision = detector.decidir((TEAMS_SALIDA, TEAMS_ENTRADA))
        assert decision.escuchar is False
        assert decision.motivo == "reunion_no_consentida"

    def test_sin_identificador_y_una_sola_reunion_se_escucha(self) -> None:
        """Si solo hay una reunión en el registro, no hay duda que resolver."""
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=None,
        )
        decision = detector.decidir((TEAMS_SALIDA, TEAMS_ENTRADA))
        assert decision.escuchar is True


# ─────────────────────────── la trazabilidad ───────────────────────────


class TestTrazabilidad:
    """Toda decisión deja un motivo legible."""

    def test_todo_motivo_esta_catalogado(self) -> None:
        """Los motivos posibles son una lista cerrada y conocida."""
        assert set(MOTIVOS) == {
            "reunion_no_consentida",
            "proceso_no_habilitado",
            "sin_senal_de_reunion",
            "sin_consentimiento_de_reunion",
        }
        for texto in MOTIVOS.values():
            assert texto
            assert "No se ha capturado audio." in texto

    def test_la_decision_negativa_explica_y_resume(self) -> None:
        """La decisión negativa lleva explicación y resumen."""
        detector = DetectorDeReunion(registro=(), procesos_habilitados=("Microsoft Teams",))
        decision = detector.decidir((TEAMS_SALIDA,))
        assert decision.explicacion == MOTIVOS["sin_consentimiento_de_reunion"]
        assert decision.resumen().startswith("NO ESCUCHA")

    def test_la_decision_conserva_las_sesiones(self) -> None:
        """La decisión conserva las sesiones consideradas, para la traza."""
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=REUNION.identificador,
        )
        sesiones = (TEAMS_SALIDA, TEAMS_ENTRADA, SPOTIFY)
        decision = detector.decidir(sesiones)
        assert decision.sesiones == sesiones

    def test_la_decision_es_determinista(self) -> None:
        """El mismo estado da siempre la misma decisión.

        No es purismo: una decisión que depende del azar no se puede
        auditar, y auditar es el propósito de todo esto.
        """
        detector = DetectorDeReunion(
            registro=(REUNION,),
            procesos_habilitados=("Microsoft Teams",),
            identificador_reunion=REUNION.identificador,
        )
        primera = detector.decidir((TEAMS_SALIDA, TEAMS_ENTRADA))
        segunda = detector.decidir((TEAMS_SALIDA, TEAMS_ENTRADA))
        assert primera == segunda
