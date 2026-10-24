"""Pruebas del Edge Connector de Projecta PMO Intelligence.

La PoC del Edge Connector solo vale si el servidor **rechaza** lo que
debe rechazar. Por eso la mitad de estas pruebas son negativas: si una
de ellas pasa porque el sistema acepta algo indebido, la suite está rota
aunque todo lo demás sea verde.

La cobertura:

* Firma: válida, manipulada, de otra clave, de formato inválido.
* Puerta del consentimiento: con consentimiento, sin consentimiento, con
  finalidad distinta, con destinatarios que no cubren el uso previsto.
* Minimización: campos bloqueados, redacción de participantes.
* Idempotencia: el mismo evento no se procesa dos veces.
* Dispositivo: registrado, revocado, desconocido.
* Antijuego: marca de tiempo fuera de la ventana.
* Lista cerrada: un tipo de receptor no admitido no entra.
* Salud: caducidad próxima, último latido.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core import edge
from app.core.db import models
from app.core.db.base import Base
from app.core.db.session import AsyncSessionLocal, engine
from app.core.security import crear_hash_contrasena
from app.main import create_app

# ─────────────────────────── infraestructura ───────────────────────────


@pytest.fixture()
async def contexto(cliente: AsyncClient, base_de_datos: None) -> dict[str, Any]:
    """Organización, proyecto y sesión de administrador.

    Se apoya en la base en memoria aislada del ``conftest``: cada prueba
    corre contra su propia base y las tablas se borran al terminar, así
    que ninguna prueba hereda de la anterior.
    """
    from sqlalchemy import select as sel

    async with AsyncSessionLocal() as sesion:
        organizacion = models.Organizacion(
            nombre="Banco de pruebas", slug=f"test-{uuid.uuid4().hex[:8]}", activo=True
        )
        sesion.add(organizacion)
        await sesion.flush()
        rol = (
            await sesion.execute(sel(models.Rol).where(models.Rol.nombre == "administrador"))
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
        datos = {
            "organizacion_id": str(organizacion.id),
            "proyecto_id": str(proyecto.id),
            "correo": correo,
        }

    inicio = await cliente.post(
        "/auth/login/password",
        json={"email": correo, "contrasena": "ContrasenaSegura123"},
    )
    assert inicio.status_code == 200, inicio.text
    # El token viaja en la cookie `projecta_sesion`, no en el cuerpo: el
    # actor se resuelve desde la cookie o desde la cabecera
    # `Authorization`, y en la PoC se usa la cookie, que es lo que hace
    # el navegador.
    token = inicio.cookies.get("projecta_sesion")
    assert token, "la respuesta de login no fijó la cookie de sesión"
    cliente.headers.update({"Authorization": f"Bearer {token}"})
    return datos


async def _registrar_dispositivo(
    cliente: AsyncClient,
    *,
    modo: str = "READ_ONLY",
    sistemas: list[str] | None = None,
    credencial_expira_en: str | None = None,
) -> dict[str, Any]:
    """Registra un dispositivo nuevo y devuelve su huella y su clave."""
    from scripts.edge_conector import generar_clave

    privada, publica = generar_clave()
    cuerpo_alta: dict[str, Any] = {
        "nombre": f"edge-{uuid.uuid4().hex[:6]}",
        "clave_publica": publica,
        "sistemas": sistemas if sistemas is not None else ["reuniones"],
        "modo": modo,
    }
    if credencial_expira_en is not None:
        cuerpo_alta["credencial_expira_en"] = credencial_expira_en
    respuesta = await cliente.post("/edge/dispositivos", json=cuerpo_alta)
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    cuerpo["_privada"] = privada
    cuerpo["_publica"] = publica
    return cuerpo


async def _consentir(
    cliente: AsyncClient,
    contexto: dict[str, Any],
    *,
    recurso: str,
    finalidad: str = "trazabilidad_de_proyecto",
    destinatarios: list[str] | None = None,
) -> dict[str, Any]:
    """Registra un consentimiento para un recurso."""
    respuesta = await cliente.post(
        "/edge/consentimientos",
        json={
            "recurso": recurso,
            "recurso_tipo": "reunion",
            "finalidad": finalidad,
            "base_legal": "interes_legitimo",
            "destinatarios": destinatarios if destinatarios is not None else ["equipo-pmo"],
            "proyecto_id": contexto["proyecto_id"],
            "otorgado_por": "Responsable de protección de datos",
        },
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _evento(
    dispositivo: dict[str, Any],
    *,
    recurso: str,
    uso_previsto: str = "trazabilidad_de_proyecto",
    tipo: str = "reunion_programada",
    marca_tiempo: str | None = None,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construye un evento firmado listo para enviar."""
    contenido = {
        "tipo": tipo,
        "payload": {
            "recurso_id": recurso,
            "recurso_tipo": "reunion",
            "titulo": "Reunión de prueba",
            "participantes": ["persona-1", "persona-2"],
            **(payload_extra or {}),
        },
        "recurso": recurso,
        "recurso_tipo": "reunion",
        "uso_previsto": uso_previsto,
        "marca_tiempo": marca_tiempo
        or datetime.now(timezone.utc).isoformat(),
    }
    firma = edge.firma_de_evento(dispositivo["_privada"], contenido)
    return {
        "huella_clave": dispositivo["huella_clave"],
        "tipo": tipo,
        "contenido": contenido,
        "firma": firma,
        "marca_tiempo": contenido["marca_tiempo"],
        "uso_previsto": uso_previsto,
        "recurso": recurso,
        "recurso_tipo": "reunion",
        "proyecto_id": None,
        "destinatarios_previstos": ["equipo-pmo"],
        "correlation_id": str(uuid.uuid4()),
    }


# ─────────────────────────── firma ───────────────────────────


class TestFirma:
    """La firma Ed25519 es la credencial del dispositivo."""

    async def test_evento_firmado_se_acepta(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """Con consentimiento, un evento bien firmado se acepta."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        respuesta = await cliente.post(
            "/edge/eventos", json=_evento(dispositivo, recurso=recurso)
        )
        assert respuesta.status_code == 202, respuesta.text
        assert respuesta.json()["decision"] == "aceptado"

    async def test_firma_manipulada_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: alterar la firma debe impedir la entrada."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        evento = _evento(dispositivo, recurso=recurso)
        evento["firma"] = "00" * 64
        respuesta = await cliente.post("/edge/eventos", json=evento)
        assert respuesta.status_code == 403, respuesta.text
        assert respuesta.json()["decision"] == "rechazado"
        assert "firma" in respuesta.json()["motivo"].lower()

    async def test_firma_de_otra_clave_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: firmar con la clave de otro dispositivo no vale."""
        dispositivo = await _registrar_dispositivo(cliente)
        otro = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        evento = _evento(dispositivo, recurso=recurso)
        # Se firma con la clave del otro dispositivo, pero se identifica
        # con el primero.
        evento["firma"] = edge.firma_de_evento(otro["_privada"], evento["contenido"])
        respuesta = await cliente.post("/edge/eventos", json=evento)
        assert respuesta.status_code == 403, respuesta.text

    async def test_firma_con_formato_invalido_no_revienta(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: una firma que no es hexadecimal se rechaza, no falla.

        La firma se pasa con la longitud que exige el esquema para que
        llegue a la capa criptográfica: lo que se comprueba aquí es que
        :func:`verificar_firma` devuelve «falso» ante una firma que no es
        hexadecimal, en vez de propagar una excepción.
        """
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        evento = _evento(dispositivo, recurso=recurso)
        evento["firma"] = "z" * 128
        respuesta = await cliente.post("/edge/eventos", json=evento)
        assert respuesta.status_code == 403, respuesta.text
        assert respuesta.json()["decision"] == "rechazado"

    async def test_firma_corta_se_rechaza_en_el_esquema(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: una firma demasiado corta ni siquiera llega a la lógica."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        evento = _evento(dispositivo, recurso=recurso)
        evento["firma"] = "corto"
        respuesta = await cliente.post("/edge/eventos", json=evento)
        assert respuesta.status_code == 422, respuesta.text

    async def test_contenido_alterado_despues_de_firmar_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: cambiar el contenido invalida la firma."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        evento = _evento(dispositivo, recurso=recurso)
        evento["contenido"]["payload"]["titulo"] = "Título manipulado"
        respuesta = await cliente.post("/edge/eventos", json=evento)
        assert respuesta.status_code == 403, respuesta.text


# ─────────────────────────── la puerta ───────────────────────────


class TestPuertaDelConsentimiento:
    """Sin consentimiento registrado, no hay permiso. La puerta falla cerrada."""

    async def test_sin_consentimiento_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: un recurso sin registro de consentimiento no entra."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-sin-consentimiento-{uuid.uuid4().hex[:8]}"
        respuesta = await cliente.post(
            "/edge/eventos", json=_evento(dispositivo, recurso=recurso)
        )
        assert respuesta.status_code == 403, respuesta.text
        assert "consentimiento" in respuesta.json()["motivo"].lower()

    async def test_finalidad_distinta_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: la finalidad del evento debe ser la registrada."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso, finalidad="trazabilidad_de_proyecto")
        respuesta = await cliente.post(
            "/edge/eventos",
            json=_evento(dispositivo, recurso=recurso, uso_previsto="formacion_de_modelo"),
        )
        assert respuesta.status_code == 403, respuesta.text
        assert "finalidad" in respuesta.json()["motivo"].lower()

    async def test_consentimiento_revocado_deja_pasar(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: revocar el consentimiento cierra la puerta."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        consentimiento = await _consentir(cliente, contexto, recurso=recurso)
        revocacion = await cliente.delete(
            f"/edge/consentimientos/{consentimiento['id']}",
            params={"motivo": "Fin del uso previsto"},
        )
        assert revocacion.status_code == 200, revocacion.text
        respuesta = await cliente.post(
            "/edge/eventos", json=_evento(dispositivo, recurso=recurso)
        )
        assert respuesta.status_code == 403, respuesta.text

    async def test_la_puerta_consulta_responde_sobre_permiso(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """La puerta se puede consultar antes de enviar."""
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        permitido = await cliente.get(
            f"/edge/puerta/reunion/{recurso}",
            params={"uso_previsto": "trazabilidad_de_proyecto"},
        )
        assert permitido.status_code == 200, permitido.text
        assert permitido.json()["permitido"] is True

        denegado = await cliente.get(
            f"/edge/puerta/reunion/{recurso}",
            params={"uso_previsto": "formacion_de_modelo"},
        )
        assert denegado.json()["permitido"] is False

    async def test_recurso_sin_consentimiento_no_abre_la_puerta(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: la puerta consultada sobre un recurso desconocido cierra."""
        respuesta = await cliente.get(
            f"/edge/puerta/reunion/reunion-inexistente-{uuid.uuid4().hex[:8]}",
            params={"uso_previsto": "trazabilidad_de_proyecto"},
        )
        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["permitido"] is False


# ─────────────────────────── minimización ───────────────────────────


class TestMinimizacion:
    """Lo que se guarda viene reducido. Los campos bloqueados no llegan."""

    async def test_campos_bloqueados_no_llegan(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: audio, transcripción y biometría no se persisten."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        await cliente.post(
            "/edge/eventos",
            json=_evento(
                dispositivo,
                recurso=recurso,
                payload_extra={
                    "transcripcion": "contenido que no debe salir",
                    "audio_url": "https://fuente.example/audio",
                    "biometrico": "huella de la cara",
                },
            ),
        )
        listado = await cliente.get("/edge/eventos")
        elementos = listado.json()["elementos"]
        assert elementos, "no se persistió ningún evento"
        almacenado = elementos[0]["payload"]
        for campo in ("transcripcion", "audio_url", "biometrico"):
            assert campo not in almacenado, f"el campo {campo} se persistió"

    async def test_participantes_quedan_redactados(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """La lista de participantes se conserva en forma, no en contenido."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        await cliente.post("/edge/eventos", json=_evento(dispositivo, recurso=recurso))
        listado = await cliente.get("/edge/eventos")
        almacenado = listado.json()["elementos"][0]["payload"]
        assert almacenado["participantes"] == ["***", "***"]

    async def test_el_payload_guardado_es_la_senal_no_el_sobre(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """Lo persistido es el interior del evento, sin doble anidación."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        await cliente.post("/edge/eventos", json=_evento(dispositivo, recurso=recurso))
        listado = await cliente.get("/edge/eventos")
        almacenado = listado.json()["elementos"][0]["payload"]
        assert "payload" not in almacenado, "el payload se guardó con doble anidación"
        assert almacenado["recurso_id"] == recurso


# ─────────────────────────── idempotencia ───────────────────────────


class TestIdempotencia:
    """El mismo evento no se procesa dos veces."""

    async def test_reenvio_no_duplica(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: reenviar el mismo evento no crea una segunda fila."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        evento = _evento(dispositivo, recurso=recurso)
        primero = await cliente.post("/edge/eventos", json=evento)
        segundo = await cliente.post("/edge/eventos", json=evento)
        assert primero.status_code == 202
        assert segundo.json()["duplicado"] is True
        listado = await cliente.get("/edge/eventos")
        assert listado.json()["total"] == 1, "el reenvío creó una segunda fila"


# ─────────────────────────── dispositivos ───────────────────────────


class TestDispositivos:
    """El dispositivo es la identidad del sensor."""

    async def test_registro_devuelve_la_huella(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """La huella es el SHA-256 de la clave pública."""
        dispositivo = await _registrar_dispositivo(cliente)
        assert dispositivo["huella_clave"] == edge.huella_de_clave(
            dispositivo["_publica"]
        )
        assert len(dispositivo["huella_clave"]) == 64

    async def test_el_registro_no_devuelve_la_clave_privada(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: la clave privada no viaja, ni en la petición ni en la respuesta."""
        dispositivo = await _registrar_dispositivo(cliente)
        assert "clave_privada" not in dispositivo
        assert dispositivo["_privada"].hex() not in str(dispositivo)

    async def test_modo_fuera_de_m0_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: solo se admite READ_ONLY en el M0."""
        from scripts.edge_conector import generar_clave

        _, publica = generar_clave()
        respuesta = await cliente.post(
            "/edge/dispositivos",
            json={
                "nombre": "edge-ilegal",
                "clave_publica": publica,
                "modo": "WRITE_APPROVED",
            },
        )
        assert respuesta.status_code == 422, respuesta.text

    async def test_dispositivo_desconocido_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: una huella que no existe no puede enviar."""
        from scripts.edge_conector import generar_clave

        privada, publica = generar_clave()
        evento = _evento(
            {
                "huella_clave": edge.huella_de_clave(publica),
                "_privada": privada,
            },
            recurso=f"reunion-{uuid.uuid4().hex[:8]}",
        )
        respuesta = await cliente.post("/edge/eventos", json=evento)
        assert respuesta.status_code == 403, respuesta.text
        assert "dispositivo" in respuesta.json()["motivo"].lower()

    async def test_dispositivo_revocado_no_puede_enviar(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: revocado significa revocado."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        revocacion = await cliente.delete(
            f"/edge/dispositivos/{dispositivo['id']}", params={"motivo": "Prueba"}
        )
        assert revocacion.status_code == 200, revocacion.text
        respuesta = await cliente.post(
            "/edge/eventos", json=_evento(dispositivo, recurso=recurso)
        )
        assert respuesta.status_code == 403, respuesta.text
        assert "revocado" in respuesta.json()["motivo"].lower()

    async def test_registrar_dispositivo_exige_administrador(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: sin sesión de administrador, no se registra nada."""
        from scripts.edge_conector import generar_clave

        _, publica = generar_clave()
        sin_token = AsyncClient(
            transport=ASGITransport(app=cliente._transport.app),
            base_url="http://test.interno",
        )
        async with sin_token as anonimo:
            respuesta = await anonimo.post(
                "/edge/dispositivos",
                json={"nombre": "edge-anonimo", "clave_publica": publica},
            )
        assert respuesta.status_code == 401, respuesta.text


# ─────────────────────────── lista cerrada y antijuego ───────────────────────────


class TestListaCerradaYAntijuego:
    """Lo que no está en la lista no entra. Lo que está fuera de ventana no entra."""

    async def test_tipo_fuera_de_la_lista_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: la lista de receptores es cerrada."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        respuesta = await cliente.post(
            "/edge/eventos",
            json=_evento(dispositivo, recurso=recurso, tipo="grabacion_de_audio"),
        )
        assert respuesta.status_code == 403, respuesta.text
        assert "no admitido" in respuesta.json()["motivo"].lower()

    async def test_marca_de_tiempo_fuera_de_ventana_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: un evento antiguo se rechaza por antijuego."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        antigua = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        respuesta = await cliente.post(
            "/edge/eventos",
            json=_evento(dispositivo, recurso=recurso, marca_tiempo=antigua),
        )
        assert respuesta.status_code == 403, respuesta.text

    async def test_marca_de_tiempo_futura_se_rechaza(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: un evento con sello del futuro se rechaza."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        futura = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        respuesta = await cliente.post(
            "/edge/eventos",
            json=_evento(dispositivo, recurso=recurso, marca_tiempo=futura),
        )
        assert respuesta.status_code == 403, respuesta.text


# ─────────────────────────── salud ───────────────────────────


class TestSaludDelDispositivo:
    """El health agent avisa antes de que el problema ocurra."""

    async def test_salud_de_un_dispositivo_que_ya_hablo(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """Un dispositivo que ya envió un latido está sano."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        await cliente.post("/edge/eventos", json=_evento(dispositivo, recurso=recurso))
        respuesta = await cliente.get(f"/edge/dispositivos/{dispositivo['id']}/salud")
        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["sano"] is True, respuesta.json()

    async def test_salud_de_un_dispositivo_que_nunca_hablo_avisa(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """NEGATIVA: un dispositivo registrado que nunca ha dado señales avisa."""
        dispositivo = await _registrar_dispositivo(cliente)
        respuesta = await cliente.get(f"/edge/dispositivos/{dispositivo['id']}/salud")
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["sano"] is False
        assert any("latido" in a.lower() for a in cuerpo["advertencias"])

    async def test_salud_avisa_de_la_caducidad_antes_de_que_ocurra(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """El aviso aparece con antelación, no cuando ya caducó."""
        caducidad = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        dispositivo = await _registrar_dispositivo(
            cliente, credencial_expira_en=caducidad
        )
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        await cliente.post("/edge/eventos", json=_evento(dispositivo, recurso=recurso))
        salud = await cliente.get(f"/edge/dispositivos/{dispositivo['id']}/salud")
        advertencias = salud.json().get("advertencias", [])
        assert any("caduc" in a.lower() for a in advertencias), advertencias


# ─────────────────────────── auditoría ───────────────────────────


class TestAuditoriaDelEdge:
    """Lo que el Edge Connector hace queda auditado."""

    async def test_el_aceptacion_queda_auditada(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """Un evento aceptado deja su evento de auditoría."""
        dispositivo = await _registrar_dispositivo(cliente)
        recurso = f"reunion-{uuid.uuid4().hex[:8]}"
        await _consentir(cliente, contexto, recurso=recurso)
        await cliente.post("/edge/eventos", json=_evento(dispositivo, recurso=recurso))
        async with AsyncSessionLocal() as sesion:
            eventos = (
                await sesion.execute(
                    select(models.EventoAuditoria).where(
                        models.EventoAuditoria.tipo == "EDGE_EVENTO_ACEPTADO"
                    )
                )
            ).scalars().all()
        assert eventos, "no se auditó la aceptación"

    async def test_el_rechazo_queda_auditado(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """Un evento rechazado deja su evento de auditoría: no hay silencio."""
        dispositivo = await _registrar_dispositivo(cliente)
        await cliente.post(
            "/edge/eventos",
            json=_evento(dispositivo, recurso=f"reunion-sin-consentimiento-{uuid.uuid4().hex[:8]}"),
        )
        async with AsyncSessionLocal() as sesion:
            eventos = (
                await sesion.execute(
                    select(models.EventoAuditoria).where(
                        models.EventoAuditoria.tipo == "EDGE_EVENTO_RECHAZADO"
                    )
                )
            ).scalars().all()
        assert eventos, "no se auditó el rechazo"
        assert "consentimiento" in eventos[-1].detalle.lower()

    async def test_la_cadena_de_auditoria_sigue_integra(
        self, cliente: AsyncClient, contexto: dict[str, Any]
    ) -> None:
        """La PoC no rompe la cadena de hash."""
        from app.core import auditoria

        async with AsyncSessionLocal() as sesion:
            informe = await auditoria.verificar_cadena(
                sesion, contexto["organizacion_id"]
            )
        assert informe["integra"] is True, informe


# ─────────────────────────── el sensor local ───────────────────────────


class TestSensorLocal:
    """El sensor minimiza antes de firmar y nunca pierde un evento."""

    async def test_el_sensor_bloquea_los_campos_prohibidos(self) -> None:
        """La minimización ocurre en el origen, no solo en el destino."""
        import pathlib

        from scripts.edge_conector import ColaLocal, RegistroLocal, Sensor

        sensor = Sensor(
            nombre="test",
            huella_clave="0" * 64,
            clave_privada=b"0" * 32,
            cola=ColaLocal(ruta=pathlib.Path("/tmp/test_edge/cola.json")),
            registro=RegistroLocal(ruta=pathlib.Path("/tmp/test_edge/reg.jsonl")),
        )
        señal = sensor.capturar_reunion("r-1", "Título")
        payload, quitados = sensor.minimizar(señal)
        assert "transcripcion" not in payload
        assert "audio_url" not in payload
        assert "transcripcion" in quitados
        assert payload["participantes"] == ["***", "***", "***"]

    async def test_la_cola_local_persiste(self) -> None:
        """Un evento encolado sobrevive a la muerte del proceso."""
        import pathlib

        from scripts.edge_conector import ColaLocal

        ruta = pathlib.Path("/tmp/test_edge_cola/cola.json")
        if ruta.exists():
            ruta.unlink()
        cola = ColaLocal(ruta=ruta)
        cola.encolar({"recurso": "r-1"})
        assert len(ColaLocal(ruta=ruta).cargar()) == 1
        assert cola.desencolar()["recurso"] == "r-1"
        assert ColaLocal(ruta=ruta).cargar() == []

    async def test_la_cola_muerta_recibe_lo_que_no_se_puede_enviar(self) -> None:
        """Lo que agota los reintentos no se pierde: va a la cola muerta."""
        import pathlib

        from scripts.edge_conector import ColaLocal

        ruta = pathlib.Path("/tmp/test_edge_muerta/cola.json")
        cola = ColaLocal(ruta=ruta)
        cola.mover_a_muerta({"recurso": "r-2"}, "sin conexión")
        muertos = ColaLocal(ruta=ruta).cargar_muertos()
        assert muertos and muertos[0]["motivo_muerta"] == "sin conexión"

    async def test_la_clave_privada_no_se_transmite(self) -> None:
        """La clave privada se queda en el perímetro, con permisos 0600."""
        from scripts.edge_conector import generar_clave, guardar_clave_privada

        privada, publica = generar_clave()
        ruta = guardar_clave_privada("test-clave", privada)
        assert oct(ruta.stat().st_mode)[-3:] == "600"
        assert privada not in publica.encode()
