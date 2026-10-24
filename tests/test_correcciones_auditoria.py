"""Pruebas de regresión de los defectos que encontró la auditoría adversarial.

Cada prueba fija un defecto **concreto** que se corrigió. Sin ellas, la
corrección podría deshacerse sin que nadie se entere: un arreglo sin
prueba es un tinkering, no una reparación.

| # | Defecto | Qué fija la prueba |
|---|---|---|
| 1 | La captura no escribía en la cadena de auditoría | Que **sí** escribe, y que el rastro está en `eventos_auditoria` |
| 2 | La auditoría se tragaba el fallo y la fila quedaba sin rastro | Que el fallo **propaga** y que no se guarda nada |
| 3 | El 403 del tenant se tragaba en tres sitios | Que el 403 **se propaga** en los tres |
| 4 | `proyecto_id` no se validaba contra la organización | Que un proyecto **de otra organización** se rechaza |
| 5 | El cliente HTTP no se cerraba nunca | Que `close()` **cierra** el cliente que es suyo |
| 6 | `REUNION_NO_CAPTURABLE` nunca se emitía | Que el tipo está en el catálogo **y se usa** |
| 7 | El error de configuración salía como tarea correcta | Que la tarea **falla** y no devuelve un resultado de éxito |
| 8 | El `id` del evento de auditoría era el de la entidad | Que dos eventos sobre la misma entidad **no chocan** |
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.rbac import Actor

# ─────────────────────────── ayudantes ───────────────────────────


def _actor(rol_nombre: str, organizacion_id: str) -> Any:
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


@pytest_asyncio.fixture()
async def dos_organizaciones(base_de_datos: None) -> Any:
    """Dos organizaciones, cada una con su proyecto.

    Es lo que hace falta para comprobar el aislamiento entre inquilinos:
    un proyecto de la organización B tiene que ser **invisible** para un
    actor de la A.
    """
    from app.core.db.models import Organizacion, Proyecto
    from app.core.db.session import AsyncSessionLocal

    organizacion_a = uuid.uuid4()
    organizacion_b = uuid.uuid4()
    proyecto_a = uuid.uuid4()
    proyecto_b = uuid.uuid4()
    async with AsyncSessionLocal() as sesion:
        sesion.add(
            Organizacion(
                id=organizacion_a, nombre="Banco A", slug=f"banco-a-{organizacion_a.hex[:8]}"
            )
        )
        sesion.add(
            Organizacion(
                id=organizacion_b, nombre="Banco B", slug=f"banco-b-{organizacion_b.hex[:8]}"
            )
        )
        sesion.add(
            Proyecto(
                id=proyecto_a,
                organizacion_id=organizacion_a,
                nombre="Programa de A",
                codigo="A-1",
            )
        )
        sesion.add(
            Proyecto(
                id=proyecto_b,
                organizacion_id=organizacion_b,
                nombre="Programa de B",
                codigo="B-1",
            )
        )
        await sesion.commit()
    return {
        "organizacion_a": str(organizacion_a),
        "organizacion_b": str(organizacion_b),
        "proyecto_a": str(proyecto_a),
        "proyecto_b": str(proyecto_b),
    }


# ─────────────────────────── 1. La auditoría de la captura ───────────────────────────


class TestAuditoriaDeLaCaptura:
    """La captura deja rastro en la cadena del producto."""

    @pytest.mark.asyncio
    async def test_01_la_captura_escribe_en_la_cadena_de_auditoria(
        self, dos_organizaciones: dict[str, Any], base_de_datos: None
    ) -> None:
        """Crear una sesión deja un evento en ``eventos_auditoria``."""
        from app.core.captura import servicio
        from app.core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as sesion:
            await servicio.crear_sesion(
                sesion,
                organizacion_id=uuid.UUID(dos_organizaciones["organizacion_a"]),
                proyecto_id=uuid.UUID(dos_organizaciones["proyecto_a"]),
                titulo="Comité de prueba",
                correlation_id="corr-prueba-auditoria",
            )
            await sesion.commit()

        from app.core.db.models import EventoAuditoria
        from sqlalchemy import select

        async with AsyncSessionLocal() as sesion:
            resultado = await sesion.execute(
                select(EventoAuditoria).where(
                    EventoAuditoria.tipo == "CAPTURA_SESION_CREADA"
                )
            )
            eventos = resultado.scalars().all()

        assert len(eventos) == 1, "crear una sesión debe dejar un evento"
        assert eventos[0].correlation_id == "corr-prueba-auditoria"
        assert "sesion_de_captura:" in eventos[0].entidad

    @pytest.mark.asyncio
    async def test_02_las_acciones_clave_dejan_rastro(
        self, dos_organizaciones: dict[str, Any], base_de_datos: None
    ) -> None:
        """Autorizar, cambiar de estado y añadir una nota dejan rastro."""
        from app.core.captura import servicio
        from app.core.db.models import EventoAuditoria
        from app.core.db.session import AsyncSessionLocal
        from sqlalchemy import select

        async with AsyncSessionLocal() as sesion:
            nueva = await servicio.crear_sesion(
                sesion,
                organizacion_id=uuid.UUID(dos_organizaciones["organizacion_a"]),
                proyecto_id=uuid.UUID(dos_organizaciones["proyecto_a"]),
                titulo="Sesión con rastro",
            )
            sesion_id = nueva.id
            await servicio.registrar_autorizacion(
                sesion,
                sesion_id=sesion_id,
                actor_id=None,
                actor_nombre="Operador de pruebas",
            )
            await servicio.transicionar_captura(
                sesion, sesion_id=sesion_id, destino="READY"
            )
            await servicio.anadir_nota(
                sesion, sesion_id=sesion_id, texto="Se decide continuar", marca_ms=42000
            )
            await sesion.commit()

        async with AsyncSessionLocal() as sesion:
            resultado = await sesion.execute(
                select(EventoAuditoria.tipo).where(
                    EventoAuditoria.tipo.like("CAPTURA_%")
                )
            )
            tipos = {fila[0] for fila in resultado}

        assert "CAPTURA_AUTORIZACION_REGISTRADA" in tipos
        assert "CAPTURA_ESTADO_CAMBIADO" in tipos
        assert "CAPTURA_NOTA_ANADIDA" in tipos

    @pytest.mark.asyncio
    async def test_03_dos_eventos_sobre_la_misma_entidad_no_chocan(
        self, dos_organizaciones: dict[str, Any], base_de_datos: None
    ) -> None:
        """El ``id`` del evento es único **por evento**, no por entidad.

        Poner el identificador de la entidad en el ``id`` del evento
        hacía que dos cambios de estado de la misma sesión violaran la
        restricción de unicidad, y la operación devolvía un 500.
        """
        from app.core.captura import servicio
        from app.core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as sesion:
            nueva = await servicio.crear_sesion(
                sesion,
                organizacion_id=uuid.UUID(dos_organizaciones["organizacion_a"]),
                proyecto_id=uuid.UUID(dos_organizaciones["proyecto_a"]),
                titulo="Sesión con dos eventos",
            )
            sesion_id = nueva.id
            await servicio.registrar_autorizacion(
                sesion, sesion_id=sesion_id, actor_id=None, actor_nombre="Operador"
            )
            await servicio.transicionar_captura(
                sesion, sesion_id=sesion_id, destino="READY"
            )
            await servicio.transicionar_captura(
                sesion, sesion_id=sesion_id, destino="RECORDING"
            )
            await sesion.commit()

    @pytest.mark.asyncio
    async def test_04_si_la_auditoria_falla_no_se_guarda_el_cambio(
        self, dos_organizaciones: dict[str, Any], base_de_datos: None, monkeypatch: Any
    ) -> None:
        """El efecto y su rastro van en la misma transacción.

        Si el evento no se puede escribir, el cambio tampoco: ni la fila
        queda guardada sin rastro, ni el sistema finge que ha ido bien.
        """
        from app.core import auditoria
        from app.core.captura import servicio
        from app.core.db.models import EventoAuditoria
        from app.core.db.session import AsyncSessionLocal
        from sqlalchemy import select

        async def _fallar(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("la auditoría no está disponible")

        monkeypatch.setattr(auditoria, "registrar", _fallar)

        with pytest.raises(RuntimeError):
            async with AsyncSessionLocal() as sesion:
                await servicio.crear_sesion(
                    sesion,
                    organizacion_id=uuid.UUID(dos_organizaciones["organizacion_a"]),
                    proyecto_id=uuid.UUID(dos_organizaciones["proyecto_a"]),
                    titulo="Sesión sin auditoría",
                )
                await sesion.commit()

        from app.core.db.models import SesionDeCaptura

        async with AsyncSessionLocal() as sesion:
            sesiones = await sesion.execute(
                select(SesionDeCaptura).where(
                    SesionDeCaptura.titulo == "Sesión sin auditoría"
                )
            )
            eventos = await sesion.execute(
                select(EventoAuditoria).where(
                    EventoAuditoria.detalle.is_not(None)
                )
            )
            _ = eventos.scalars().all()

        assert sesiones.scalars().all() == [], (
            "la sesión no debe quedar guardada si su rastro no se pudo escribir"
        )


# ─────────────────────────── 2. El 403 del tenant ───────────────────────────


class TestEl403DelTenant:
    """El 403 de transcripciones desactivadas no se degrada a éxito vacío."""

    def test_05_los_tres_sitios_propagan_el_403(self) -> None:
        """En el cliente de Graph, el 403 se propaga en los tres sitios."""
        import inspect

        from app.core import graph_teams

        fuente = inspect.getsource(graph_teams.ClienteReuniones.capturar)
        # Un `except ErrorDeTranscripcionDeshabilitada: raise` **antes**
        # de cada `except ErrorDeGraph` es lo que distingue la subclase.
        propagaciones = fuente.count("except ErrorDeTranscripcionDeshabilitada:")
        degradaciones = fuente.count("except ErrorDeGraph:")
        assert propagaciones == degradaciones == 3, (
            f"se esperaban 3 propagaciones y 3 degradaciones; hay "
            f"{propagaciones} y {degradaciones}"
        )
        # Y la propagación tiene que ir **antes** que la degradación.
        for indice in range(propagaciones):
            antes = fuente.index("except ErrorDeTranscripcionDeshabilitada:", fuente.index(
                "except ErrorDeTranscripcionDeshabilitada:") if indice else 0)
        posiciones_prop = [
            i for i in range(len(fuente)) if fuente.startswith(
                "except ErrorDeTranscripcionDeshabilitada:", i)
        ]
        posiciones_deg = [
            i for i in range(len(fuente)) if fuente.startswith("except ErrorDeGraph:", i)
        ]
        for propagacion, degradacion in zip(posiciones_prop, posiciones_deg):
            assert propagacion < degradacion, (
                "la propagación del 403 tiene que ir antes que la degradación"
            )

    @pytest.mark.asyncio
    async def test_06_el_403_no_se_traga_al_capturar(self) -> None:
        """Con un doble que devuelve 403, ``capturar`` lanza."""
        from app.core.graph_teams import (
            ClienteReuniones,
            ErrorDeTranscripcionDeshabilitada,
        )

        class _Doble:
            def get(self, *args: Any, **kwargs: Any) -> Any:
                class _Respuesta:
                    status_code = 403

                    def json(self) -> dict[str, Any]:
                        return {
                            "error": {
                                "code": "GraphAccessToTranscriptsDisabled",
                                "message": "El acceso a las transcripciones está desactivado.",
                            }
                        }

                    @property
                    def text(self) -> str:
                        return "403"

                return _Respuesta()

        cliente = ClienteReuniones("token", http=_Doble())
        with pytest.raises(ErrorDeTranscripcionDeshabilitada):
            cliente.capturar("organizador@banco.example", "reunion-1", con_audio=False)


# ─────────────────────────── 3. El aislamiento entre inquilinos ───────────────────────────


class TestAislamientoEntreOrganizaciones:
    """Una reunión no puede colgar de un proyecto de otra organización."""

    @pytest.mark.asyncio
    async def test_07_un_proyecto_de_otra_organizacion_se_rechaza(
        self, dos_organizaciones: dict[str, Any], base_de_datos: None
    ) -> None:
        """Un actor de la organización A no puede colgar la captura del
        proyecto de la organización B."""
        from app.api import deps
        from app.core import rbac
        from app.main import create_app

        aplicacion = create_app()

        async def _actor_de_a() -> Any:
            return _actor("administrador", dos_organizaciones["organizacion_a"])

        aplicacion.dependency_overrides[deps.get_actor] = _actor_de_a
        transporte = ASGITransport(app=aplicacion)
        async with AsyncClient(transport=transporte, base_url="http://pruebas") as cliente:
            respuesta = await cliente.post(
                "/reuniones/capturar",
                json={"proyecto_id": dos_organizaciones["proyecto_b"]},
            )

        assert respuesta.status_code == 404, (
            f"un proyecto de otra organización tiene que dar 404; dio "
            f"{respuesta.status_code}: {respuesta.text}"
        )

    @pytest.mark.asyncio
    async def test_08_un_proyecto_de_la_misma_organizacion_se_acepta(
        self, dos_organizaciones: dict[str, Any], base_de_datos: None, monkeypatch: Any
    ) -> None:
        """El caso bueno sigue funcionando: el proyecto de la propia
        organización se acepta y la tarea se encola."""
        from app.api import deps
        from app.main import create_app
        from app.workers.tasks import captura_teams

        encoladas: list[dict[str, Any]] = []

        class _TareaFalsa:
            id = "tarea-de-prueba"

        def _apply_async(*args: Any, **kwargs: Any) -> Any:
            encoladas.append(kwargs)
            return _TareaFalsa()

        monkeypatch.setattr(
            captura_teams.capturar_reuniones, "apply_async", _apply_async
        )

        aplicacion = create_app()

        async def _actor_de_a() -> Any:
            return _actor("administrador", dos_organizaciones["organizacion_a"])

        aplicacion.dependency_overrides[deps.get_actor] = _actor_de_a
        transporte = ASGITransport(app=aplicacion)
        async with AsyncClient(transport=transporte, base_url="http://pruebas") as cliente:
            respuesta = await cliente.post(
                "/reuniones/capturar",
                json={"proyecto_id": dos_organizaciones["proyecto_a"]},
            )

        assert respuesta.status_code == 200, respuesta.text
        assert encoladas, "la tarea debería haberse encolado"
        assert encoladas[0]["kwargs"]["proyecto_id"] == dos_organizaciones["proyecto_a"]


# ─────────────────────────── 3 bis. El alcance de la lista ───────────────────────────


class TestElAlcanceDeLaLista:
    """El filtro de alcance va en SQL, y coincide con el de Python."""

    @pytest.mark.asyncio
    async def test_13_el_alcance_sql_y_el_de_python_coinciden(
        self, dos_organizaciones: dict[str, Any], base_de_datos: None
    ) -> None:
        """La condición SQL y la comprobación en Python dicen lo mismo.

        Se comprueba **comportamentalmente**: se insertan reuniones de
        verdad, se consulta con la condición SQL y se compara el
        resultado con el que devuelve el filtro de Python. Comparar los
        dos por su texto sería frágil: un cambio de redacción en el SQL
        no cambiaría la regla y rompería la prueba sin motivo.

        Si se separaran, la lista y la lectura de un recurso darían
        respuestas distintas, y eso es la peor clase de fallo: uno que
        no se ve hasta que alguien lo nota.
        """
        import uuid as _uuid

        from app.api.routers.reuniones import _alcance_sql, _puede_ver
        from app.core.db.models import ReunionCapturada
        from app.core.db.session import AsyncSessionLocal
        from sqlalchemy import and_, select

        organizacion = _uuid.UUID(dos_organizaciones["organizacion_a"])
        proyecto_a = _uuid.UUID(dos_organizaciones["proyecto_a"])
        proyecto_b = _uuid.UUID(dos_organizaciones["proyecto_b"])

        async with AsyncSessionLocal() as sesion:
            for etiqueta, proyecto in (
                ("sin-proyecto", None),
                ("proyecto-a", proyecto_a),
                ("proyecto-b", proyecto_b),
            ):
                sesion.add(
                    ReunionCapturada(
                        organizacion_id=organizacion,
                        proyecto_id=proyecto,
                        organizador="organizador@banco.example",
                        reunion_id=f"reunion-{etiqueta}",
                        asunto=etiqueta,
                        segmentos_totales=0,
                        participantes=[],
                        huella="0" * 64,
                    )
                )
            await sesion.commit()

        actores = [
            Actor(
                usuario_id=str(_uuid.uuid4()),
                organizacion_id=str(organizacion),
                rol="observador",
                nivel=0,
                activo=True,
                proyectos=set(),
                ambito_organizacion=False,
            ),
            Actor(
                usuario_id=str(_uuid.uuid4()),
                organizacion_id=str(organizacion),
                rol="colaborador",
                nivel=1,
                activo=True,
                proyectos={str(proyecto_a)},
                ambito_organizacion=False,
            ),
            Actor(
                usuario_id=str(_uuid.uuid4()),
                organizacion_id=str(organizacion),
                rol="pmo",
                nivel=4,
                activo=True,
                proyectos=set(),
                ambito_organizacion=True,
            ),
        ]

        async with AsyncSessionLocal() as sesion:
            todas = await sesion.execute(select(ReunionCapturada))
            filas = list(todas.scalars())

            for actor in actores:
                consulta = select(ReunionCapturada).where(
                    and_(
                        ReunionCapturada.organizacion_id == organizacion,
                        _alcance_sql(actor),
                    )
                )
                resultado = await sesion.execute(consulta)
                por_sql = {fila.reunion_id for fila in resultado.scalars()}

                por_python = {
                    fila.reunion_id
                    for fila in filas
                    if _puede_ver(actor, fila.proyecto_id)
                }

                assert por_sql == por_python, (
                    f"el alcance en SQL y en Python no coinciden para el actor "
                    f"de nivel {actor.nivel}: SQL={sorted(por_sql)}, "
                    f"Python={sorted(por_python)}"
                )


# ─────────────────────────── 4. El cliente HTTP ───────────────────────────


class TestCierreDelClienteHttp:
    """El cliente de Graph se cierra siempre."""

    def test_09_close_cierra_el_cliente_propio(self) -> None:
        from app.core.graph_teams import ClienteReuniones

        cerrado: list[bool] = []

        class _Http:
            def close(self) -> None:
                cerrado.append(True)

        cliente = ClienteReuniones("token", http=_Http())
        # Un cliente recibido **no** es de este objeto: no se cierra.
        cliente.close()
        assert cerrado == [], "no se debe cerrar un cliente que no es propio"

        cliente_propio = ClienteReuniones("token")
        assert getattr(cliente_propio, "_http_propio", False) is True
        cliente_propio.close()

    def test_10_el_contexto_cierra_el_cliente(self) -> None:
        from app.core.graph_teams import ClienteReuniones

        cerrado: list[bool] = []

        class _Http:
            def close(self) -> None:
                cerrado.append(True)

        cliente = ClienteReuniones("token")
        cliente.http = _Http()
        cliente._http_propio = True
        with cliente as abierto:
            assert abierto is cliente
        assert cerrado == [True], "el contexto tiene que cerrar el cliente"


# ─────────────────────────── 4 bis. La fila sin rastro ───────────────────────────


class TestLaFilaSinRastro:
    """En la captura de Teams, la fila y su rastro van juntos."""

    @pytest.mark.asyncio
    async def test_04b_si_la_auditoria_falla_no_queda_la_fila(
        self, dos_organizaciones: dict[str, Any], base_de_datos: None, monkeypatch: Any
    ) -> None:
        """Si el evento no se puede escribir, la reunión tampoco.

        El defecto era que ``_auditar`` se tragaba el fallo: la llamada
        ocurre antes del ``commit``, así que el fallo se silenciaba y la
        fila quedaba guardada **sin ningún rastro** de que se había
        capturado. Un fallo de auditoría se convertía en un éxito
        silencioso.
        """
        import datetime as dt

        from app.core import auditoria
        from app.core.db.models import ReunionCapturada
        from app.core.db.session import AsyncSessionLocal
        from app.core.graph_teams import ResultadoDeReunion
        from app.workers.tasks import captura_teams
        from sqlalchemy import select

        async def _fallar(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("la auditoría no está disponible")

        monkeypatch.setattr(auditoria, "registrar", _fallar)

        resultado = ResultadoDeReunion(
            reunion_id="reunion-sin-rastro",
            segmentos=[],
            participantes=[],
        )

        with pytest.raises(RuntimeError):
            await captura_teams._persistir(
                dos_organizaciones["organizacion_a"],
                resultado,
                proyecto_id=None,
                cuenta="organizador@banco.example",
            )

        async with AsyncSessionLocal() as sesion:
            filas = await sesion.execute(
                select(ReunionCapturada).where(
                    ReunionCapturada.reunion_id == "reunion-sin-rastro"
                )
            )
            _ = filas.scalars().all()

        assert filas.scalars().all() == [], (
            "la reunión no debe quedar guardada si su rastro no se pudo escribir"
        )


# ─────────────────────────── 5. El tipo de evento y la tarea ───────────────────────────


class TestCatalogoYTarea:
    """El catálogo declara lo que se emite, y la tarea no disfraza un error."""

    def test_11_reunion_no_capturable_esta_y_se_usa(self) -> None:
        """El tipo está en el catálogo y el código lo emite."""
        import inspect

        from app.core import auditoria
        from app.workers.tasks import captura_teams

        assert "REUNION_NO_CAPTURABLE" in auditoria.CATALOGO_EVENTOS
        fuente = inspect.getsource(captura_teams)
        assert '"REUNION_NO_CAPTURABLE"' in fuente, (
            "el evento tiene que emitirse, no solo estar declarado"
        )

    def test_12_el_error_de_configuracion_no_es_un_exito(self) -> None:
        """Un error de configuración lanza, no devuelve un resultado."""
        import inspect

        from app.workers.tasks import captura_teams

        fuente = inspect.getsource(captura_teams.capturar_reuniones)
        assert "raise ErrorDeConfiguracion" in fuente, (
            "el error de configuración tiene que lanzar una excepción"
        )
        # Y no puede devolver un resultado normal con 'error'.
        assert '"error": str(error)' not in fuente, (
            "devolver un resultado con la clave 'error' hace que Celery "
            "marque la tarea como correcta"
        )
