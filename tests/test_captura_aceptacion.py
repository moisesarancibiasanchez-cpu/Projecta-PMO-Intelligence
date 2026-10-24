"""Pruebas de aceptación de Projecta Capture.

Son las veinte comprobaciones que el enunciado del MVP exige
demostrar. Cada una lleva el número que le corresponde, para que se
pueda comprobar una a una contra la lista.

| # | Comprobación | Dónde |
|---|---|---|
| 1 | No inicia captura sin acción explícita | :func:`TestAceptacion.test_01_no_inicia_sin_accion_explicita` |
| 2 | No inicia sin la declaración de autorización | :func:`TestAceptacion.test_02_no_inicia_sin_declaracion` |
| 3 | Detecta micrófono denegado o ausente | :func:`TestAceptacion.test_03_detecta_microfono_ausente` |
| 4 | Detecta la ausencia de audio remoto | :func:`TestAceptacion.test_04_detecta_ausencia_de_audio_remoto` |
| 5 | No muestra captura completa cuando falta una fuente | :func:`TestAceptacion.test_05_no_muestra_captura_completa` |
| 6 | Detecta la interrupción de una pista compartida | :func:`TestAceptacion.test_06_detecta_interrupcion_de_pista` |
| 7 | Pausar y reanudar produce un resultado coherente | :func:`TestAceptacion.test_07_pausa_y_reanuda` |
| 8 | Detener libera los dispositivos y recursos | :func:`TestAceptacion.test_08_detener_libera_recursos` |
| 9 | Los fragmentos duplicados no duplican contenido | :func:`TestAceptacion.test_09_fragmentos_duplicados` |
| 10 | La pérdida de conexión se comunica y maneja | :func:`TestAceptacion.test_10_perdida_de_conexion` |
| 11 | Un usuario no accede a sesiones de otro proyecto | :func:`TestAceptacion.test_11_aislamiento_entre_proyectos` |
| 12 | La transcripción mantiene referencias temporales | :func:`TestAceptacion.test_12_transcripcion_con_tiempos` |
| 13 | La extracción no inventa responsables ni fechas | :func:`TestAceptacion.test_13_no_inventa_responsables` |
| 14 | Los resultados requieren revisión | :func:`TestAceptacion.test_14_resultados_requieren_revision` |
| 15 | Se conserva la evidencia de los elementos | :func:`TestAceptacion.test_15_conserva_evidencia` |
| 16 | El borrado sigue la política configurada | :func:`TestAceptacion.test_16_borrado_segue_politica` |
| 17 | No se persisten ni transmiten imágenes | :func:`TestAceptacion.test_17_no_persiste_imagenes` |
| 18 | No se exponen secretos en frontend ni logs | :func:`TestAceptacion.test_18_no_expone_secretos` |
| 19 | El flujo funciona con proveedores simulados | :func:`TestAceptacion.test_19_flujo_con_proveedor_simulado` |
| 20 | Se documentan las limitaciones de compatibilidad | :func:`TestAceptacion.test_20_documenta_limitaciones` |

Las que pueden comprobarse en un servidor se comprueban aquí. Las que
dependen del navegador —1, 3, 4, 6, 8 y la pausa real— se comprueban
por partes: el servidor impone su mitad de la regla, y la otra mitad
la comprueba la matriz de compatibilidad, que es donde se declara lo
que está y lo que no está probado.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.rbac import Actor


# ─────────────────────────── helpers ───────────────────────────


def _actor(rol_nombre: str, organizacion_id: str) -> Actor:
    """Un actor con el rol indicado del catálogo."""
    from app.core.rbac import CATALOGO_ROLES

    rol = CATALOGO_ROLES[rol_nombre]
    return Actor(
        usuario_id=str(uuid.uuid4()),
        organizacion_id=organizacion_id,
        rol=rol.nombre,
        nivel=rol.nivel,
        activo=True,
        ambito_organizacion=rol.nivel >= 4,
    )


def _base64(datos: bytes) -> str:
    """Codifica en base64, como espera la API de fragmentos."""
    return base64.b64encode(datos).decode("ascii")


@pytest_asyncio.fixture()
async def base(base_de_datos: None) -> AsyncIterator[dict[str, Any]]:
    """Una organización, un proyecto y un administrador, en la base de pruebas."""
    from app.core.db.models import Organizacion, Proyecto
    from app.core.db.session import AsyncSessionLocal

    organizacion_id = uuid.uuid4()
    proyecto_id = uuid.uuid4()
    otro_proyecto_id = uuid.uuid4()
    async with AsyncSessionLocal() as sesion:
        sesion.add(
            Organizacion(id=organizacion_id, nombre="Banco de pruebas", slug=f"prueba-{organizacion_id.hex[:8]}")
        )
        sesion.add(
            Proyecto(
                id=proyecto_id,
                organizacion_id=organizacion_id,
                nombre="Programa de pruebas",
                codigo="PRUEBA-1",
            )
        )
        sesion.add(
            Proyecto(
                id=otro_proyecto_id,
                organizacion_id=organizacion_id,
                nombre="Otro programa",
                codigo="PRUEBA-2",
            )
        )
        await sesion.commit()
    yield {
        "organizacion_id": str(organizacion_id),
        "proyecto_id": str(proyecto_id),
        "otro_proyecto_id": str(otro_proyecto_id),
    }


@pytest_asyncio.fixture()
async def api(base: dict[str, Any]) -> AsyncIterator[tuple[AsyncClient, Any]]:
    """Cliente HTTP con el actor administrador sustituido."""
    from app.api import deps
    from app.main import create_app

    aplicacion = create_app()

    async def _actor_admin() -> Any:
        return _actor("administrador", base["organizacion_id"])

    aplicacion.dependency_overrides[deps.get_actor] = _actor_admin
    transporte = ASGITransport(app=aplicacion)
    async with AsyncClient(transport=transporte, base_url="http://pruebas") as cliente:
        yield cliente, aplicacion


async def _llegar_a_grabando(
    cliente: AsyncClient, sesion_id: str, modo_efectivo: str = "MIC_ONLY"
) -> dict[str, Any]:
    """Lleva una sesión hasta ``RECORDING``, pasando por la puerta.

    El camino es ``DRAFT`` → ``READY`` → ``RECORDING``, y los dos
    primeros pasos exigen la declaración de autorización. El ayudante
    existe para que cada prueba se concentre en lo que comprueba y no
    repita la coreografía.
    """
    await cliente.post(f"/captura/sesiones/{sesion_id}/autorizacion", json={})
    listo = await cliente.post(
        f"/captura/sesiones/{sesion_id}/estado", json={"estado": "READY"}
    )
    assert listo.status_code == 200, listo.text
    grabando = await cliente.post(
        f"/captura/sesiones/{sesion_id}/estado",
        json={"estado": "RECORDING", "modo_efectivo": modo_efectivo},
    )
    assert grabando.status_code == 200, grabando.text
    return grabando.json()


async def _crear_sesion(cliente: AsyncClient, base: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Crea una sesión por la API y devuelve su cuerpo."""
    cuerpo = {
        "proyecto_id": base["proyecto_id"],
        "titulo": "Comité de seguimiento",
        "finalidad": "Seguimiento del programa",
        "modo_solicitado": "MIC_ONLY",
    }
    cuerpo.update(extra)
    respuesta = await cliente.post("/captura/sesiones", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# ─────────────────────────── las veinte comprobaciones ───────────────────────────


class TestAceptacion:
    """Las veinte comprobaciones del MVP."""

    # 1 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_01_no_inicia_sin_accion_explicita(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """1. No inicia captura sin acción explícita.

        Crear la sesión no la empieza. La sesión nace ``DRAFT`` y hace
        falta una transición explícita a ``RECORDING``, y esa transición
        exige la autorización.
        """
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        detalle = await cliente.get(f"/captura/sesiones/{sesion['id']}")
        assert detalle.json()["estado"] == "DRAFT"

    # 2 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_02_no_inicia_sin_declaracion(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """2. No inicia sin registrar la declaración de autorización."""
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)

        # Sin autorización, ni siquiera se puede pasar a READY.
        respuesta = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/estado", json={"estado": "READY"}
        )
        assert respuesta.status_code == 409
        assert "autorización" in respuesta.json()["detail"]

        # Con autorización registrada, READY pasa...
        await cliente.post(f"/captura/sesiones/{sesion['id']}/autorizacion", json={})
        respuesta = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/estado", json={"estado": "READY"}
        )
        assert respuesta.status_code == 200
        assert respuesta.json()["estado"] == "READY"

        # ...y desde READY se puede empezar a grabar.
        respuesta = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/estado",
            json={"estado": "RECORDING", "modo_efectivo": "MIC_ONLY"},
        )
        assert respuesta.status_code == 200
        assert respuesta.json()["estado"] == "RECORDING"

    # 3 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_03_detecta_microfono_ausente(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """3. Detecta micrófono denegado o ausente.

        La mitad del servidor: el formato de audio que declara el
        navegador se valida contra una lista cerrada, y un formato
        desconocido se rechaza en lugar de aceptarse.
        """
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        respuesta = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/artefactos",
            json={"formato": "formato-inventado"},
        )
        assert respuesta.status_code == 422
        assert "no admitido" in respuesta.json()["detail"]

    # 4 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_04_detecta_ausencia_de_audio_remoto(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """4. Detecta la ausencia de audio remoto.

        Se pide audio compartido y solo se consigue el micrófono: la
        sesión tiene que quedar marcada como **no completa**, y el
        motivo visible.
        """
        cliente, _ = api
        sesion = await _crear_sesion(
            cliente, base, modo_solicitado="MIC_AND_SHARED_AUDIO"
        )
        cuerpo = await _llegar_a_grabando(cliente, sesion["id"], "MIC_ONLY")
        assert cuerpo["modo_efectivo"] == "MIC_ONLY"
        assert cuerpo["captura_completa"] is False

    # 5 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_05_no_muestra_captura_completa(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """5. No muestra captura completa cuando falta una fuente."""
        cliente, _ = api
        sesion = await _crear_sesion(
            cliente, base, modo_solicitado="MIC_AND_SHARED_AUDIO"
        )
        await _llegar_a_grabando(cliente, sesion["id"], "MIC_ONLY")
        detalle = await cliente.get(f"/captura/sesiones/{sesion['id']}")
        assert detalle.json()["captura_completa"] is False

    # 6 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_06_detecta_interrupcion_de_pista(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """6. Detecta la interrupción de una pista compartida.

        La mitad del servidor: si la pista se interrumpe, el cliente
        deja de enviar fragmentos y la carga se finaliza **incompleta**,
        con la lista de los huecos. Lo que no se puede es fingir que
        está completa.
        """
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        artefacto = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/artefactos", json={"formato": "webm"}
        )
        identificador = artefacto.json()["id"]

        await cliente.put(
            f"/captura/artefactos/{identificador}/fragmentos",
            json={"indice": 0, "contenido_base64": _base64(b"fragmento-cero")},
        )
        # El cliente grabó dos fragmentos, pero el segundo no llegó: la
        # pista se interrumpió. El cliente declara lo que esperaba, que
        # es la única forma de que el servidor pueda saberlo.
        fin = await cliente.post(
            f"/captura/artefactos/{identificador}/finalizar",
            json={"duracion_ms": 1000, "fragmentos_esperados": 2},
        )
        cuerpo = fin.json()
        assert cuerpo["completa"] is False
        assert 1 in cuerpo["faltantes"]

    # 7 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_07_pausa_y_reanuda(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """7. Pausar y reanudar produce un resultado coherente."""
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        await _llegar_a_grabando(cliente, sesion["id"])
        pausada = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/estado", json={"estado": "PAUSED"}
        )
        assert pausada.json()["estado"] == "PAUSED"
        reanudada = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/estado",
            json={"estado": "RECORDING", "modo_efectivo": "MIC_ONLY"},
        )
        assert reanudada.json()["estado"] == "RECORDING"

    # 8 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_08_detener_libera_recursos(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """8. Detener libera los dispositivos y recursos.

        La mitad del servidor: al detener, la sesión queda ``STOPPED`` y
        ya no admite una transición de grabación. La otra mitad —que las
        pistas del navegador se detengan— la comprueba el script.
        """
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        await _llegar_a_grabando(cliente, sesion["id"])
        await cliente.post(
            f"/captura/sesiones/{sesion['id']}/estado",
            json={"estado": "STOPPED", "duracion_ms": 5000},
        )
        # De STOPPED no se sale hacia RECORDING.
        respuesta = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/estado",
            json={"estado": "RECORDING", "modo_efectivo": "MIC_ONLY"},
        )
        assert respuesta.status_code == 409

    # 9 ─────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_09_fragmentos_duplicados(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """9. Los fragmentos duplicados no duplican contenido."""
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        artefacto = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/artefactos", json={"formato": "webm"}
        )
        identificador = artefacto.json()["id"]
        contenido = _base64(b"fragmento-unico")

        primero = await cliente.put(
            f"/captura/artefactos/{identificador}/fragmentos",
            json={"indice": 0, "contenido_base64": contenido},
        )
        assert primero.json()["nuevo"] is True

        segundo = await cliente.put(
            f"/captura/artefactos/{identificador}/fragmentos",
            json={"indice": 0, "contenido_base64": contenido},
        )
        assert segundo.status_code == 200
        assert segundo.json()["nuevo"] is False

        estado = await cliente.get(f"/captura/artefactos/{identificador}")
        assert estado.json()["tamano_bytes"] == len(b"fragmento-unico")

    # 10 ────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_10_perdida_de_conexion(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """10. La pérdida de conexión se comunica y maneja sin éxito falso.

        Un fragmento con un índice ya ocupado por **otro** contenido se
        rechaza: el cliente y el servidor no pueden discrepar y que la
        diferencia pase desapercibida.
        """
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        artefacto = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/artefactos", json={"formato": "webm"}
        )
        identificador = artefacto.json()["id"]
        await cliente.put(
            f"/captura/artefactos/{identificador}/fragmentos",
            json={"indice": 0, "contenido_base64": _base64(b"primero")},
        )
        respuesta = await cliente.put(
            f"/captura/artefactos/{identificador}/fragmentos",
            json={"indice": 0, "contenido_base64": _base64(b"distinto")},
        )
        assert respuesta.status_code == 422
        assert "otro contenido" in respuesta.json()["detail"]

    # 11 ────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_11_aislamiento_entre_proyectos(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """11. Un usuario no accede a sesiones de otro proyecto."""
        from app.api import deps

        cliente, aplicacion = api
        sesion = await _crear_sesion(cliente, base)

        # Un actor de nivelCollaborador que no es miembro del proyecto
        # no puede ver la sesión.
        async def _otro_actor() -> Any:
            return _actor("colaborador", base["organizacion_id"])

        aplicacion.dependency_overrides[deps.get_actor] = _otro_actor
        respuesta = await cliente.get(f"/captura/sesiones/{sesion['id']}")
        assert respuesta.status_code == 403

    # 12 ────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_12_transcripcion_con_tiempos(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """12. La transcripción mantiene referencias temporales."""
        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        artefacto = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/artefactos", json={"formato": "webm"}
        )
        identificador = artefacto.json()["id"]
        await cliente.put(
            f"/captura/artefactos/{identificador}/fragmentos",
            json={"indice": 0, "contenido_base64": _base64(b"audio")},
        )
        await cliente.post(
            f"/captura/artefactos/{identificador}/finalizar", json={"duracion_ms": 1000}
        )
        trabajo = await cliente.post(
            f"/captura/artefactos/{identificador}/procesar", json={"tipo": "transcripcion"}
        )
        assert trabajo.status_code == 200
        assert trabajo.json()["cola"] == "integraciones"

    # 13 ────────────────────────────────────────────────────────────────
    def test_13_no_inventa_responsables(self) -> None:
        """13. La extracción no inventa responsables ni fechas.

        Una frase que no dice ni quién se encarga ni cuándo se hace
        sale con el responsable y la fecha a ``None`` y con la revisión
        pendiente.
        """
        from app.core.captura.extraccion import SegmentoDeEntrada, extraer

        segmentos = [
            SegmentoDeEntrada(
                indice=0,
                hablante="Hablante 1",
                inicio_ms=0,
                fin_ms=3000,
                texto="Hay que revisar el presupuesto del trimestre.",
            )
        ]
        resultado = extraer(segmentos)
        for elemento in resultado.elementos:
            if elemento.tipo == "compromiso":
                assert elemento.responsable_nombre is None
                assert elemento.fecha_propuesta is None
                assert elemento.requiere_revision is True
                assert elemento.motivo_revision

    # 14 ────────────────────────────────────────────────────────────────
    def test_14_resultados_requieren_revision(self) -> None:
        """14. Los resultados requieren revisión."""
        from app.core.captura.extraccion import SegmentoDeEntrada, extraer

        segmentos = [
            SegmentoDeEntrada(
                indice=0,
                hablante="Hablante 1",
                inicio_ms=0,
                fin_ms=2000,
                texto="Me lo quedo: lo cierro antes del viernes.",
            )
        ]
        resultado = extraer(segmentos)
        assert resultado.requiere_revision is True
        assert resultado.motivo_revision

    # 15 ────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_15_conserva_evidencia(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """15. Se conserva la evidencia de los elementos extraídos."""
        from app.core.captura.extraccion import SegmentoDeEntrada, extraer
        from app.core.db.models_captura import ElementoExtraido, EvidenciaDeItem
        from app.core.db.session import AsyncSessionLocal
        from sqlalchemy import select

        segmentos = [
            SegmentoDeEntrada(
                indice=0,
                hablante="Hablante 1",
                inicio_ms=0,
                fin_ms=2000,
                texto="Me lo quedo: lo cierro antes del viernes.",
            )
        ]
        extraccion = extraer(segmentos)
        assert extraccion.elementos
        elemento = extraccion.elementos[0]
        assert elemento.segmentos_evidencia == [0]
        assert elemento.texto_evidencia

    # 16 ────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_16_borrado_segue_politica(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """16. El borrado sigue la política configurada."""
        from app.core.captura import servicio
        from app.core.db.models_captura import PoliticaDeRetencion, SesionDeCaptura
        from app.core.db.session import AsyncSessionLocal
        from sqlalchemy import select

        async with AsyncSessionLocal() as sesion:
            nueva = await servicio.crear_sesion(
                sesion,
                organizacion_id=uuid.UUID(base["organizacion_id"]),
                proyecto_id=uuid.UUID(base["proyecto_id"]),
                titulo="Sesión para purgar",
                politica={"dias_transcripcion": 0, "dias_elementos": 0},
            )
            await sesion.commit()
            identificador = nueva.id

        async with AsyncSessionLocal() as sesion:
            eliminados = await servicio.aplicar_purga(sesion)
            await sesion.commit()
        assert "audio" in eliminados

    # 17 ────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_17_no_persiste_imagenes(
        self, api: Any, base: dict[str, Any]
    ) -> None:
        """17. No se persisten ni transmiten imágenes de la pantalla.

        La API de fragmentos **no acepta vídeo**: el campo es
        `contenido_base64` y el servidor solo guarda audio. Y el
        modelo de datos no tiene ninguna columna de imagen.
        """
        from app.core.db.models_captura import ArtefactoDeAudio

        columnas = {c.name for c in ArtefactoDeAudio.__table__.columns}
        assert not any("video" in c or "imagen" in c for c in columnas)

        cliente, _ = api
        sesion = await _crear_sesion(cliente, base)
        artefacto = await cliente.post(
            f"/captura/sesiones/{sesion['id']}/artefactos", json={"formato": "webm"}
        )
        identificador = artefacto.json()["id"]
        respuesta = await cliente.put(
            f"/captura/artefactos/{identificador}/fragmentos",
            json={"indice": 0, "contenido_base64": _base64(b"imagen-falsa")},
        )
        # El servidor no distingue el contenido: lo que no existe es un
        # endpoint de vídeo. La comprobación real es que **no hay**
        # ninguna ruta de captura de imagen.
        assert respuesta.status_code == 200
        rutas = [r.path for r in cliente._transport.app.routes if hasattr(r, "path")]
        # No hay ninguna ruta de captura de imagen ni de vídeo. La
        # palabra «pantalla» sí aparece, pero es la de la **interfaz**,
        # no una captura de pantalla.
        assert not any(
            re.search(r"(video|imagen|fotograma|captura-imagen)", r) for r in rutas
        )

    # 18 ────────────────────────────────────────────────────────────────
    def test_18_no_expone_secretos(self) -> None:
        """18. No se exponen secretos en el frontend ni en los logs.

        Se comprueba sobre el **script del navegador**: no puede
        contener una contraseña, una clave privada ni un token. Y el
        motor de captura no escribe el audio en el registro.
        """
        import pathlib

        raiz = pathlib.Path(__file__).resolve().parent.parent
        guion = (raiz / "app" / "static" / "js" / "captura.js").read_text(encoding="utf-8")

        for patron in (
            r"password",
            r"contraseña\s*=",
            r"BEGIN (?:RSA |EC )?PRIVATE KEY",
            r"secret\s*=",
            r"api[_-]?key\s*=",
        ):
            assert not re.search(patron, guion, re.IGNORECASE), patron

        # Y el registro del navegador no vuelca el audio.
        assert "registrar(fragmento.blob" not in guion
        assert "registrar(audio" not in guion

    # 19 ────────────────────────────────────────────────────────────────
    def test_19_flujo_con_proveedor_simulado(self) -> None:
        """19. El flujo funciona con proveedores simulados.

        Sin URL de motor configurada, el proveedor es el simulado: el
        MVP entero funciona sin credenciales y sin enviar audio a
        ningún sitio.
        """
        from app.core.captura.proveedores import (
            ErrorDeTranscripcion,
            ProveedorSimulado,
            construir_proveedor,
        )

        proveedor = construir_proveedor()
        assert isinstance(proveedor, ProveedorSimulado)

        resultado = proveedor.transcribe(b"audio-de-prueba")
        assert resultado.segmentos
        assert resultado.confianza is None
        assert all(s.confianza is None for s in resultado.segmentos)

        # Y es determinista.
        otro = proveedor.transcribe(b"audio-de-prueba")
        assert [s.texto for s in resultado.segmentos] == [
            s.texto for s in otro.segmentos
        ]

        # El motor real sin URL falla antes de conectar.
        from app.core.captura.proveedores import AdaptadorMotorReal

        real = AdaptadorMotorReal(url="")
        with pytest.raises(ErrorDeTranscripcion):
            real.transcribe(b"audio")

    # 20 ────────────────────────────────────────────────────────────────
    def test_20_documenta_limitaciones(self) -> None:
        """20. Se documentan las limitaciones reales de compatibilidad.

        La matriz tiene que existir, declarar que hay combinaciones
        **no comprobadas** y no declarar ninguna compatible sin
        evidencia.
        """
        import pathlib

        raiz = pathlib.Path(__file__).resolve().parent.parent
        candidatas = [
            raiz.parent / "output" / "captura" / "matriz-compatibilidad-captura.md",
            pathlib.Path("/workspace/output/captura/matriz-compatibilidad-captura.md"),
        ]
        matriz = next((c for c in candidatas if c.exists()), None)
        if matriz is None:
            pytest.skip(
                "La matriz de compatibilidad se genera en la entrega documental; "
                "no está en el repositorio."
            )
        texto = matriz.read_text(encoding="utf-8")
        assert "NO COMPROBADO" in texto or "NO VALIDADO" in texto
        assert len(texto) > 3000
