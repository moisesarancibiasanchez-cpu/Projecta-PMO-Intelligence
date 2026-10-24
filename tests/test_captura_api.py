"""Pruebas funcionales de la API de captura de reuniones.

Comprueban el contrato HTTP: qué rutas existen, qué exigen y qué
devuelven. Son las pruebas que hay que hacer **antes** de las pruebas
funcionales contra el tenant real, porque fijan *qué se espera* de cada
ruta.

Los casos negativos importan más que los positivos aquí: lo que hay que
verificar es que **no** se pueda capturar sin autoridad, y que un actor
sin acceso al proyecto no vea las reuniones de ese proyecto.

Una nota sobre cómo se sustituye al actor: FastAPI captura la
dependencia en tiempo de import, así que cambiar la función en el
módulo no altera nada de lo ya registrado. La vía correcta es
``dependency_overrides``, que es la que usan estas pruebas.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.rbac import Actor


# ─────────────────────────── actores ───────────────────────────


def _actor_administrador(organizacion_id: str) -> Actor:
    """Un actor con nivel de administrador de organización."""
    from app.core.rbac import CATALOGO_ROLES

    rol = CATALOGO_ROLES["administrador"]
    return Actor(
        usuario_id="11111111-1111-1111-1111-111111111111",
        organizacion_id=organizacion_id,
        rol=rol.nombre,
        nivel=rol.nivel,
        activo=True,
        ambito_organizacion=True,
    )


def _actor_observador(organizacion_id: str) -> Actor:
    """Un actor con el nivel más bajo: el que no puede capturar nada."""
    from app.core.rbac import CATALOGO_ROLES

    rol = CATALOGO_ROLES["observador"]
    return Actor(
        usuario_id="22222222-2222-2222-2222-222222222222",
        organizacion_id=organizacion_id,
        rol=rol.nombre,
        nivel=rol.nivel,
        activo=True,
    )


# ─────────────────────────── fixtures ───────────────────────────


@pytest_asyncio.fixture()
async def api(base_de_datos: None) -> AsyncIterator[tuple[AsyncClient, Any]]:
    """Cliente HTTP contra la aplicación, con la base de pruebas.

    Devuelve también la aplicación, porque hay que poder **sobrescribir
    dependencias**.
    """
    from app.main import create_app

    aplicacion = create_app()
    transporte = ASGITransport(app=aplicacion)
    async with AsyncClient(transport=transporte, base_url="http://pruebas") as cliente:
        yield cliente, aplicacion


@pytest.fixture()
def como_administrador() -> Any:
    """Sustituye la dependencia de actor por un administrador.

    La aplicación resuelve el actor a partir de la sesión. En esta
    familia de pruebas lo que se prueba es el contrato de las rutas, no
    el inicio de sesión, que ya tiene sus propias pruebas.
    """
    organizacion = "00000000-0000-0000-0000-000000000000"

    def _aplicar(aplicacion: Any) -> None:
        from app.api import deps

        async def _resolver() -> Any:
            return _actor_administrador(organizacion)

        aplicacion.dependency_overrides[deps.get_actor] = _resolver

    return _aplicar


# ─────────────────────────── el contrato ───────────────────────────


class TestContratoDeLaApi:
    """Las rutas existen y responden lo que deben."""

    @pytest.mark.asyncio
    async def test_la_salud_de_la_integracion_responde(
        self, api: Any, como_administrador: Any
    ) -> None:
        """GET /reuniones/salud/integracion dice qué falta."""
        cliente, aplicacion = api
        como_administrador(aplicacion)
        respuesta = await cliente.get("/reuniones/salud/integracion")
        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        assert "configurada" in cuerpo
        assert "faltan" in cuerpo
        assert cuerpo["cola"] == "integraciones"

    @pytest.mark.asyncio
    async def test_listar_reuniones_devuelve_una_lista(
        self, api: Any, como_administrador: Any
    ) -> None:
        """GET /reuniones devuelve la lista, aunque esté vacía."""
        cliente, aplicacion = api
        como_administrador(aplicacion)
        respuesta = await cliente.get("/reuniones")
        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        assert "reuniones" in cuerpo
        assert "total" in cuerpo
        assert isinstance(cuerpo["reuniones"], list)

    @pytest.mark.asyncio
    async def test_una_reunion_inexistente_es_404(
        self, api: Any, como_administrador: Any
    ) -> None:
        """NEGATIVA: leer una reunión que no existe es 404."""
        cliente, aplicacion = api
        como_administrador(aplicacion)
        respuesta = await cliente.get("/reuniones/no-existe-esta")
        assert respuesta.status_code == 404
        assert "no-existe-esta" in respuesta.json()["detail"]

    @pytest.mark.asyncio
    async def test_capturar_devuelve_el_identificador_de_la_tarea(
        self, api: Any, como_administrador: Any, monkeypatch: Any
    ) -> None:
        """POST /reuniones/capturar encola el trabajo y lo dice.

        El encolado se sustituye, porque lo que se prueba aquí es el
        **contrato de la ruta**: que devuelve el identificador de la
        tarea, la cola y la organización. Lo que pase dentro de la tarea
        es otra cosa, y tiene sus propias pruebas.

        Antes de la corrección del BUG-9, esta prueba pasaba sin
        sustituir nada, porque la tarea devolvía un resultado normal
        incluso cuando no había nada que capturar. Ahora, en modo
        *eager*, un error de configuración sobrevive a la petición, y
        la respuesta sería un 500 con un mensaje de configuración. Que
        la ruta se pruebe con el encolado sustituido es lo correcto: la
        ruta no tiene por qué saber qué hace la tarea.
        """
        from app.workers.tasks import captura_teams

        cliente, aplicacion = api
        como_administrador(aplicacion)

        class _TareaFalsa:
            id = "tarea-de-prueba"

        def _encolar(*_args: Any, **kwargs: Any) -> Any:
            return _TareaFalsa()

        monkeypatch.setattr(
            captura_teams.capturar_reuniones, "apply_async", _encolar
        )

        respuesta = await cliente.post("/reuniones/capturar", json={})
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["tarea"] == "projecta.integraciones.teams.captura"
        assert cuerpo["cola"] == "integraciones"
        assert cuerpo["id"]

    @pytest.mark.asyncio
    async def test_un_error_de_configuracion_no_es_un_exito(
        self, api: Any, como_administrador: Any
    ) -> None:
        """NEGATIVA: un error de configuración no sale como tarea correcta.

        Sin nada configurado —ni organizador, ni credenciales de Graph—
        la captura no puede hacerse. La tarea **falla**, y el fallo
        llega a quien llama. Antes devolvía un resultado normal, Celery
        lo marcaba como SUCCESS, y quien vigilase el resultado veía un
        éxito donde no hubo captura.
        """
        cliente, aplicacion = api
        como_administrador(aplicacion)
        respuesta = await cliente.post("/reuniones/capturar", json={})
        # En modo *eager* la tarea corre dentro de la petición, así que
        # el error llega al cliente. Lo que no puede pasar es que la
        # respuesta parezca un éxito.
        assert respuesta.status_code != 200, (
            "un error de configuración no puede devolverse como un encolado "
            "correcto"
        )

    @pytest.mark.asyncio
    async def test_capturar_valida_la_ventana(
        self, api: Any, como_administrador: Any
    ) -> None:
        """NEGATIVA: una ventana absurda se rechaza antes de encolar."""
        cliente, aplicacion = api
        como_administrador(aplicacion)
        respuesta = await cliente.post("/reuniones/capturar", json={"horas": 0})
        assert respuesta.status_code == 422

    @pytest.mark.asyncio
    async def test_sin_sesion_no_se_puede_nada(self, api: Any) -> None:
        """NEGATIVA: sin actor, las rutas que exigen autoridad se cierran."""
        from fastapi import HTTPException, status

        from app.api import deps

        cliente, aplicacion = api

        async def _fallar() -> Any:
            # La dependencia real lanza HTTPException, no una excepción
            # propia: la prueba tiene que simular lo que ocurre de
            # verdad, o no está probando nada.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="no hay sesión"
            )

        aplicacion.dependency_overrides[deps.get_actor] = _fallar
        for ruta, metodo in (
            ("/reuniones", "GET"),
            ("/reuniones/capturar", "POST"),
            ("/reuniones/salud/integracion", "GET"),
        ):
            respuesta = (
                await cliente.get(ruta) if metodo == "GET" else await cliente.post(ruta, json={})
            )
            assert respuesta.status_code in (401, 403), (ruta, respuesta.status_code)

    @pytest.mark.asyncio
    async def test_un_actor_sin_autoridad_no_captura(self, api: Any) -> None:
        """NEGATIVA: un observador no puede lanzar una captura.

        Capturar reuniones es leer el tenant de Microsoft 365, y eso lo
        autoriza la figura más alta, no cualquiera.
        """
        from app.api import deps

        cliente, aplicacion = api
        observador = _actor_observador("00000000-0000-0000-0000-000000000000")

        async def _devolver() -> Any:
            return observador

        aplicacion.dependency_overrides[deps.get_actor] = _devolver
        respuesta = await cliente.post("/reuniones/capturar", json={})
        assert respuesta.status_code == 403

    @pytest.mark.asyncio
    async def test_el_esquema_abierto_describe_las_rutas(self, api: Any) -> None:
        """Las rutas nuevas aparecen en el esquema de la API."""
        cliente, _ = api
        respuesta = await cliente.get("/openapi.json")
        assert respuesta.status_code == 200
        rutas = respuesta.json()["paths"]
        assert "/reuniones" in rutas
        assert "/reuniones/capturar" in rutas
        assert "/reuniones/salud/integracion" in rutas
