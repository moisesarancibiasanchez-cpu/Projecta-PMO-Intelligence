"""Pruebas de la ingesta de Teams en la Variante A.

La Variante A es la que se aplica cuando la política de seguridad del
banco **no admite una aplicación cliente desplegada en su red**: el
adaptador deja de ser un agente aparte y pasa a ser una tarea del worker
de la propia aplicación.

Lo que estas pruebas comprueban es que la reconversión **no ha roto
nada de lo que ya estaba validado**:

1. La tarea se registra con el nombre exacto del contrato y va a la
   cola `integraciones`, que el contrato ya definía.
2. La ingesta se hace **en proceso**, por la misma función
   `edge.ingerir_evento` que valida la Variante B: el camino de
   validación del servidor no ha cambiado ni una línea.
3. La firma Ed25519 **se mantiene**: en esta Variante no acredita
   distancia, pero sí acredita procedencia auditada.
4. Un error de configuración **no se reintenta**: reintentar no lo
   arregla, y un reintento infinito esconde el problema.
5. La puerta del consentimiento sigue cerrando: un recurso sin
   consentimiento no entra, y el rechazo queda auditado.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core import edge
from app.core.db import models
from app.core.db.session import AsyncSessionLocal
from app.core.security import crear_hash_contrasena
from app.core.edge_teams import AdaptadorTeams, ErrorDeGraph
from app.workers.tasks.ingesta_teams import (
    TAREA_INGESTA_TEAMS,
    ErrorDeIngesta,
    _ingestar_una_vez,
)


# ─────────────────────────── infraestructura ───────────────────────────


@pytest.fixture()
async def contexto(cliente: AsyncClient, base_de_datos: None) -> dict[str, Any]:
    """Organización, proyecto, dispositivo y sesión de administrador."""
    async with AsyncSessionLocal() as sesion:
        organizacion = models.Organizacion(
            nombre="Banco de pruebas", slug=f"test-{uuid.uuid4().hex[:8]}", activo=True
        )
        sesion.add(organizacion)
        await sesion.flush()
        rol = (
            await sesion.execute(
                select(models.Rol).where(models.Rol.nombre == "administrador")
            )
        ).scalar_one_or_none()
        if rol is None:
            rol = models.Rol(
                nombre="administrador", nivel=5, ambito="organización", descripcion="Test"
            )
            sesion.add(rol)
            await sesion.flush()
        proyecto = models.Proyecto(
            organizacion_id=organizacion.id,
            nombre="Proyecto de pruebas",
            codigo=f"TST-{uuid.uuid4().hex[:8].upper()}",
            estado="en_curso",
        )
        sesion.add(proyecto)
        await sesion.flush()
        correo = f"admin-{uuid.uuid4().hex[:6]}@test.example"
        usuario = models.Usuario(
            organizacion_id=organizacion.id,
            rol_id=rol.id,
            email=correo,
            nombre="Administrador de pruebas",
            password_hash=crear_hash_contrasena("ContrasenaSegura123"),
            activo=True,
        )
        sesion.add(usuario)
        await sesion.commit()
        organizacion_id = str(organizacion.id)
        proyecto_id = str(proyecto.id)

    # El dispositivo del Edge Connector: en la Variante A es la propia
    # aplicación, y su clave pública está registrada en la tabla.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from app.core.edge import huella_de_clave

    privada = Ed25519PrivateKey.generate().private_bytes_raw()
    publica = Ed25519PrivateKey.from_private_bytes(privada).public_key().public_bytes_raw()
    huella = huella_de_clave(publica.hex())

    async with AsyncSessionLocal() as sesion:
        sesion.add(
            models.DispositivoEdge(
                organizacion_id=uuid.UUID(organizacion_id),
                nombre="edge-conector-teams",
                clave_publica=publica.hex(),
                huella_clave=huella,
                modo="READ_ONLY",
                sistemas=["teams"],
                estado="activo",
            )
        )
        await sesion.commit()

    inicio = await cliente.post(
        "/auth/login/password",
        json={"email": correo, "contrasena": "ContrasenaSegura123"},
    )
    assert inicio.status_code == 200, inicio.text
    token = inicio.cookies.get("projecta_sesion")
    cliente.headers.update({"Authorization": f"Bearer {token}"})

    return {
        "organizacion_id": organizacion_id,
        "proyecto_id": proyecto_id,
        "huella": huella,
        "privada": privada,
    }


# ─────────────────────────── el contrato ───────────────────────────


class TestContratoDeLaTarea:
    """La tarea entra en la cola que el contrato ya definía."""

    def test_el_nombre_es_el_del_contrato(self) -> None:
        """El nombre exacto de la tarea, sin variantes."""
        assert TAREA_INGESTA_TEAMS == "projecta.integraciones.teams.ingesta"

    def test_la_cola_integraciones_existe_en_el_contrato(self) -> None:
        """La cola existe; no se crea ninguna nueva."""
        from app.core.celery_app import COLAS, COLA_INTEGRACIONES

        assert COLA_INTEGRACIONES in COLAS
        assert len(COLAS) == 6, "el número de colas del contrato no ha cambiado"

    def test_la_tarea_esta_registrada(self) -> None:
        """Celery conoce la tarea al importar la aplicación."""
        from app.core.celery_app import celery_app

        assert TAREA_INGESTA_TEAMS in celery_app.tasks


# ─────────────────────────── la ingesta en proceso ───────────────────────────


class TestIngestaEnProceso:
    """La ingesta usa la misma validación que la Variante B."""

    async def test_el_evento_se_acepta_por_el_camino_de_servidor(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """Un evento consentido se acepta por `edge.ingerir_evento`."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        async with AsyncSessionLocal() as sesion:
            sesion.add(
                models.ConsentimientoEdge(
                    organizacion_id=uuid.UUID(contexto["organizacion_id"]),
                    recurso="canal-1",
                    recurso_tipo="canal",
                    finalidad="trazabilidad_de_proyecto",
                    base_legal="interes_legitimo",
                    destinatarios=["equipo-pmo"],
                    proyecto_id=uuid.UUID(contexto["proyecto_id"]),
                    otorgado_por="Responsable de protección de datos",
                )
            )
            await sesion.commit()

        adaptador = AdaptadorTeams(
            token="token-de-prueba",
            estado=_estado(),
            huella_clave=contexto["huella"],
            clave_privada=contexto["privada"],
        )
        señal = {
            "recurso_id": "canal-1",
            "recurso_tipo": "canal",
            "mensaje_id": "m-1",
            "titulo": "Avance",
            "inicio": "2026-10-23T09:00:00Z",
            "cuerpo": "Comprometido a entregar el 30",
            "participantes": ["Ana"],
        }
        eventos = adaptador.construir_eventos(
            [señal], proyecto_id=contexto["proyecto_id"]
        )
        assert eventos and len(eventos[0]["firma"]) == 128

        async with AsyncSessionLocal() as sesion:
            resultado = await edge.ingerir_evento(
                sesion,
                organizacion_id=contexto["organizacion_id"],
                huella_clave=eventos[0]["huella_clave"],
                tipo=eventos[0]["tipo"],
                contenido=eventos[0]["contenido"],
                firma=eventos[0]["firma"],
                marca_tiempo=eventos[0]["marca_tiempo"],
                uso_previsto=eventos[0]["uso_previsto"],
                recurso=eventos[0]["recurso"],
                recurso_tipo=eventos[0]["recurso_tipo"],
                proyecto_id=eventos[0]["proyecto_id"],
                destinatarios_previstos=eventos[0]["destinatarios_previstos"],
                correlation_id=eventos[0]["correlation_id"],
            )
            assert resultado.aceptado is True
            await sesion.commit()

    async def test_sin_consentimiento_no_entra(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: sin consentimiento, la puerta cierra en la Variante A."""
        adaptador = AdaptadorTeams(
            token="token-de-prueba",
            estado=_estado(),
            huella_clave=contexto["huella"],
            clave_privada=contexto["privada"],
        )
        señal = {
            "recurso_id": "canal-sin-consentimiento",
            "recurso_tipo": "canal",
            "mensaje_id": "m-9",
            "titulo": "Avance",
            "inicio": "2026-10-23T09:00:00Z",
            "cuerpo": "texto",
            "participantes": ["Ana"],
        }
        eventos = adaptador.construir_eventos([señal])
        async with AsyncSessionLocal() as sesion:
            resultado = await edge.ingerir_evento(
                sesion,
                organizacion_id=contexto["organizacion_id"],
                huella_clave=eventos[0]["huella_clave"],
                tipo=eventos[0]["tipo"],
                contenido=eventos[0]["contenido"],
                firma=eventos[0]["firma"],
                marca_tiempo=eventos[0]["marca_tiempo"],
                uso_previsto=eventos[0]["uso_previsto"],
                recurso=eventos[0]["recurso"],
                recurso_tipo=eventos[0]["recurso_tipo"],
                proyecto_id=None,
                destinatarios_previstos=eventos[0]["destinatarios_previstos"],
                correlation_id=eventos[0]["correlation_id"],
            )
            await sesion.commit()
        assert resultado.aceptado is False
        assert "consentimiento" in resultado.motivo.lower()

    async def test_sin_clave_de_dispositivo_no_se_firma(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: sin clave, no hay evento firmado."""
        adaptador = AdaptadorTeams(
            token="token-de-prueba",
            estado=_estado(),
            huella_clave=contexto["huella"],
            clave_privada=b"",
        )
        señal = {
            "recurso_id": "canal-1",
            "recurso_tipo": "canal",
            "mensaje_id": "m-1",
            "titulo": "Avance",
            "inicio": "2026-10-23T09:00:00Z",
            "cuerpo": "texto",
            "participantes": ["Ana"],
        }
        with pytest.raises(ErrorDeGraph):
            adaptador.construir_eventos([señal])


# ─────────────────────────── errores ───────────────────────────


class TestErroresDeConfiguracion:
    """Un error de configuración no se reintenta."""

    async def test_sin_graph_configurado_no_se_ingesta(
        self, cliente: AsyncClient, contexto: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVA: sin credenciales de Graph, la ingesta no corre."""
        from app.core import config

        original = config.settings

        class SinGraph:
            graph_tenant_id = ""
            graph_client_id = ""
            graph_certificado = ""
            graph_clave_privada = ""
            edge_clave_dispositivo = ""
            edge_huella_clave = ""
            edge_estado = str(_estado())
            teams_canal_permitido = ""

        monkeypatch.setattr("app.workers.tasks.ingesta_teams._configuracion", lambda: SinGraph())
        with pytest.raises(ErrorDeIngesta):
            await _ingestar_una_vez(
                contexto["organizacion_id"], chat_id="chat-1"
            )
        assert original is not None

    async def test_sin_recurso_que_leer_no_se_ingesta(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: hay que decir qué se lee."""
        with pytest.raises(ErrorDeIngesta):
            await _ingestar_una_vez(contexto["organizacion_id"])


# ─────────────────────────── el disparador ───────────────────────────


class TestDisparador:
    """Quién lanza la ingesta, y con qué autoridad."""

    async def test_la_ruta_exige_administrador(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: sin sesión de administrador, no se encola nada."""
        from httpx import ASGITransport, AsyncClient as Cliente

        async with Cliente(
            transport=ASGITransport(app=cliente._transport.app),
            base_url="http://test.interno",
        ) as anonimo:
            respuesta = await anonimo.post(
                "/edge/integraciones/teams/ingesta", json={"chat_id": "c-1"}
            )
        assert respuesta.status_code == 401, respuesta.text

    async def test_la_ruta_encola_en_la_cola_integraciones(
        self, cliente: AsyncClient, contexto: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La ruta encola en `integraciones`, que el contrato ya define."""
        capturadas: dict[str, Any] = {}

        class TareaFalsa:
            id = "tarea-1"

            @staticmethod
            def apply_async(kwargs: Any = None, queue: str = "") -> Any:
                capturadas["kwargs"] = kwargs
                capturadas["queue"] = queue
                return TareaFalsa()

        import app.workers.tasks.ingesta_teams as modulo

        monkeypatch.setattr(modulo, "ingesta_teams", TareaFalsa)
        respuesta = await cliente.post(
            "/edge/integraciones/teams/ingesta",
            json={"chat_id": "c-1", "proyecto_id": contexto["proyecto_id"]},
        )
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["cola"] == "integraciones"
        assert cuerpo["tarea"] == TAREA_INGESTA_TEAMS
        assert capturadas["queue"] == "integraciones"
        assert capturadas["kwargs"]["chat_id"] == "c-1"


def _estado():
    """Directorio de estado temporal de las pruebas."""
    import pathlib

    return pathlib.Path("/tmp/test_variante_a")
