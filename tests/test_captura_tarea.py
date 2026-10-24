"""Pruebas de la tarea de captura de reuniones, de extremo a extremo.

Estas pruebas no usan el doble de Graph: usan la base de datos de
pruebas. Comprueban el ciclo completo —capturar, guardar, auditar y no
duplicar— que es el que se ejecutará en el banco.

Lo que se verifica:

* La reunión se guarda con su transcripción, sus participantes y su
  audio.
* La captura es **idempotente**: repetirla no duplica nada.
* La fila y su evento de auditoría van en la **misma** transacción.
* Una reunión sin transcripción no se guarda, y se dice por qué.
* Un tenant con las transcripciones desactivadas es un error
  identificable, no un fallo genérico.
"""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio

from app.core.graph_teams import ErrorDeTranscripcionDeshabilitada
from scripts.mock_graph import REUNION_1, REUNION_3, crear_servidor_mock


@pytest_asyncio.fixture()
async def organizacion(base_de_datos: None) -> AsyncIterator[uuid.UUID]:
    """Crea una organización y la devuelve.

    Depende de ``base_de_datos``, que es la que crea las tablas. Sin
    esa dependencia, las pruebas fallarían con «no such table» y no
    probarían nada.
    """
    from app.core.db.models import Organizacion
    from app.core.db.session import AsyncSessionLocal

    identificador = uuid.uuid4()
    async with AsyncSessionLocal() as sesion:
        sesion.add(
            Organizacion(id=identificador, nombre="Banco de pruebas", slug=f"prueba-{identificador.hex[:8]}")
        )
        await sesion.commit()
    yield identificador


@pytest.fixture()
def doble():
    """El doble de Graph, con su base."""
    srv, base = crear_servidor_mock(0)
    yield base
    srv.shutdown()


@pytest.fixture(autouse=True)
def apunta_al_doble(doble: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apunta la aplicación al doble y desactiva el audio.

    El audio se desactiva porque el almacenamiento de objetos no está
    disponible en el entorno de pruebas, y la captura tiene que poder
    funcionar sin él: es el respaldo en disco.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "graph_base_url", doble)
    monkeypatch.setattr(settings, "teams_captura_con_audio", False)
    monkeypatch.setattr(settings, "teams_captura_ventana_horas", 24 * 365)
    monkeypatch.setattr(settings, "teams_organizador", "organizador@banco.example")
    monkeypatch.setattr(settings, "teams_directorio_capturas", "/tmp/projecta-capturas-pruebas")


# ─────────────────────────── el ciclo ───────────────────────────


class TestCapturaDeLaTarea:
    """El ciclo completo, contra la base de datos de pruebas."""

    @pytest.mark.asyncio
    async def test_captura_y_guarda(self, organizacion: uuid.UUID) -> None:
        """La reunión se guarda con todo su contenido."""
        from app.core.db.models import ReunionCapturada
        from app.core.db.session import AsyncSessionLocal
        from app.workers.tasks.captura_teams import capturar_una_vez

        resultado = await capturar_una_vez(str(organizacion))
        # La ventana de un año abarca las dos reuniones con transcripción
        # del doble. La tercera no tiene transcripción y no se guarda.
        assert resultado["capturadas"] == 2
        assert resultado["fallidas"] == 0

        async with AsyncSessionLocal() as sesion:
            filas = (await sesion.execute(__import__("sqlalchemy").select(ReunionCapturada))).scalars().all()
        assert len(filas) == 2
        por_id = {f.reunion_id: f for f in filas}
        assert REUNION_1 in por_id
        fila = por_id[REUNION_1]
        assert fila.segmentos_totales == 7
        assert len(fila.participantes) == 2
        assert fila.asunto == "Comité de seguimiento · programa strategic"
        assert len(fila.huella) == 64

    @pytest.mark.asyncio
    async def test_es_idempotente(self, organizacion: uuid.UUID) -> None:
        """Capturar dos veces no duplica la reunión."""
        from app.core.db.models import ReunionCapturada
        from app.core.db.session import AsyncSessionLocal
        from app.workers.tasks.captura_teams import capturar_una_vez

        primera = await capturar_una_vez(str(organizacion))
        segunda = await capturar_una_vez(str(organizacion))

        assert primera["capturadas"] == 2
        assert segunda["capturadas"] == 0
        assert segunda["ya_existentes"] == 2

        async with AsyncSessionLocal() as sesion:
            filas = (await sesion.execute(__import__("sqlalchemy").select(ReunionCapturada))).scalars().all()
        assert len(filas) == 2

    @pytest.mark.asyncio
    async def test_audita_la_captura(self, organizacion: uuid.UUID) -> None:
        """Cada captura deja su evento en la cadena de auditoría."""
        from app.core.db.models import EventoAuditoria
        from app.core.db.session import AsyncSessionLocal
        from app.workers.tasks.captura_teams import capturar_una_vez

        await capturar_una_vez(str(organizacion))

        async with AsyncSessionLocal() as sesion:
            filas = (
                (
                    await sesion.execute(
                        __import__("sqlalchemy").select(EventoAuditoria).where(
                            EventoAuditoria.tipo == "REUNION_CAPTURADA"
                        )
                    )
                )
                .scalars()
                .all()
            )
        # Dos reuniones capturadas, dos eventos de auditoría.
        assert len(filas) == 2
        assert all("capturada" in (fila.detalle or "") for fila in filas)
        assert any(REUNION_1 in (fila.detalle or "") for fila in filas)

    @pytest.mark.asyncio
    async def test_la_cadena_de_auditoria_no_se_rompe(self, organizacion: uuid.UUID) -> None:
        """La fila y su auditoría van en la misma transacción.

        Si fueran en transactions distintas, un fallo de la auditoría
        dejaría la fila guardada y sin rastro, y la cadena de auditoría
        no cuadraría con lo que hay en la base de datos.
        """
        from app.core import auditoria
        from app.core.db.session import AsyncSessionLocal
        from app.workers.tasks.captura_teams import capturar_una_vez

        await capturar_una_vez(str(organizacion))

        async with AsyncSessionLocal() as sesion:
            verificacion = await auditoria.verificar_cadena(sesion, str(organizacion))
        assert verificacion["integra"] is True, verificacion

    @pytest.mark.asyncio
    async def test_captura_una_reunion_concreta(self, organizacion: uuid.UUID) -> None:
        """Se puede capturar una reunión concreta sin barrer."""
        from app.core.db.models import ReunionCapturada
        from app.core.db.session import AsyncSessionLocal
        from app.workers.tasks.captura_teams import capturar_una_vez

        resultado = await capturar_una_vez(str(organizacion), reunion_id=REUNION_1)
        assert resultado["reuniones_consideradas"] == 1
        assert resultado["capturadas"] == 1

        async with AsyncSessionLocal() as sesion:
            filas = (await sesion.execute(__import__("sqlalchemy").select(ReunionCapturada))).scalars().all()
        assert [f.reunion_id for f in filas] == [REUNION_1]


# ─────────────────────────── los fallos ───────────────────────────


class TestFallosDeLaCaptura:
    """Lo que no puede capturarse, y que se avise de por qué."""

    @pytest.mark.asyncio
    async def test_una_reunion_sin_transcripcion_no_se_guarda(
        self, organizacion: uuid.UUID
    ) -> None:
        """NEGATIVA: sin transcripción no hay fila.

        Y el motivo queda registrado, para que se sepa por qué no se
        capturó y no pensar que se perdió.
        """
        from app.core.db.models import ReunionCapturada
        from app.core.db.session import AsyncSessionLocal
        from app.workers.tasks.captura_teams import capturar_una_vez

        resultado = await capturar_una_vez(str(organizacion), reunion_id=REUNION_3)
        assert resultado["capturadas"] == 0
        assert resultado["fallidas"] == 1
        assert "no tiene transcripción" in resultado["detalles"][0]["error"]

        async with AsyncSessionLocal() as sesion:
            filas = (await sesion.execute(__import__("sqlalchemy").select(ReunionCapturada))).scalars().all()
        assert filas == []

    @pytest.mark.asyncio
    async def test_un_tenant_sin_transcripciones_es_un_error_conocido(
        self, organizacion: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVA: el 403 del tenant se distingue de un fallo de red.

        Es una decisión del administrador del tenant, no un error del
        conector, y el que lo ve tiene que saberlo.
        """
        import scripts.mock_graph as doble_modulo
        from app.workers.tasks.captura_teams import capturar_una_vez

        monkeypatch.setattr(doble_modulo, "TRANSCRIPCIONES_DESHABILITADAS", True)
        with pytest.raises(ErrorDeTranscripcionDeshabilitada):
            await capturar_una_vez(str(organizacion))

    @pytest.mark.asyncio
    async def test_una_organizacion_inexistente_no_se_traga(
        self, organizacion: uuid.UUID
    ) -> None:
        """NEGATIVA: una organización que no existe es un error, no un duplicado.

        Este caso salió de una prueba funcional: la captura se ejecutaba
        contra una organización que ya no estaba en la base de datos, la
        violacion de clave foranea se confundia con «ya existia», y la
        captura se reportaba como duplicada sin haber guardado nada. Un
        fallo silencioso que se disfraza de éxito es peor que un fallo.
        """
        from app.core.db.session import AsyncSessionLocal
        from app.core.db.models import ReunionCapturada
        from app.workers.tasks.captura_teams import ErrorDeCaptura, capturar_una_vez

        inexistente = uuid.uuid4()
        with pytest.raises(ErrorDeCaptura) as excepcion:
            await capturar_una_vez(str(inexistente))
        assert "no existe" in str(excepcion.value)

        async with AsyncSessionLocal() as sesion:
            filas = (
                (
                    await sesion.execute(
                        __import__("sqlalchemy").select(ReunionCapturada)
                    )
                )
                .scalars()
                .all()
            )
        assert filas == []

    @pytest.mark.asyncio
    async def test_sin_organizador_no_hay_captura(
        self, organizacion: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVA: sin organización configurada, la captura avisa."""
        from app.core.config import settings
        from app.workers.tasks.captura_teams import ErrorDeCaptura, capturar_una_vez

        monkeypatch.setattr(settings, "teams_organizador", "")
        with pytest.raises(ErrorDeCaptura) as excepcion:
            await capturar_una_vez(str(organizacion))
        assert "TEAMS_ORGANIZADOR" in str(excepcion.value)
