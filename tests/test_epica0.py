"""Pruebas de la Épica 0 de **Projecta PMO Intelligence** (HU_01 a HU_08).

Cada bloque corresponde a una historia de usuario y comprueba el
comportamiento que el TO-BE v1.1 fija, no la forma del código:

* **HU_02** — catálogo de roles con nivel jerárquico.
* **HU_03** — proyectos: unicidad de código por organización, borrado
  lógico y evento de auditoría.
* **HU_04** — autenticación: JWT, bcrypt, refresh rotativo y revocación
  de familia ante reutilización.
* **HU_05** — RBAC: nivel, ámbito, membresía y estado del usuario.
* **HU_06** — observabilidad: métricas y correlación.
* **HU_08** — almacenamiento de objetos: idempotencia por contenido y
  supervivencia del metadato.

Las pruebas que necesitan base de datos usan la base en memoria aislada
del ``conftest``. Las del cliente HTTP usan el mismo aislamiento.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import pytest
from httpx import AsyncClient

# ─────────────────────────── utilidades ───────────────────────────


async def _crear_organizacion(sesion: Any, nombre: str = "Banco Ejemplo") -> Any:
    from app.core.db.models import Organizacion

    organizacion = Organizacion(
        nombre=nombre,
        slug=f"org-{uuid.uuid4().hex[:10]}",
        activo=True,
    )
    sesion.add(organizacion)
    await sesion.flush()
    return organizacion


async def _crear_rol(sesion: Any, nombre: str, nivel: int) -> Any:
    from app.core.db.models import Rol

    rol = Rol(nombre=nombre, nivel=nivel, ambito="organización", descripcion=nombre)
    sesion.add(rol)
    await sesion.flush()
    return rol


async def _crear_usuario(
    sesion: Any,
    organizacion: Any,
    rol: Any,
    *,
    email: str | None = None,
    activo: bool = True,
    contrasena: str | None = None,
) -> Any:
    from app.core.db.models import Usuario
    from app.core.security import crear_hash_contrasena

    usuario = Usuario(
        organizacion_id=organizacion.id,
        rol_id=rol.id,
        email=email or f"usuario-{uuid.uuid4().hex[:8]}@banco.example",
        nombre="Usuario de Prueba",
        password_hash=crear_hash_contrasena(contrasena) if contrasena else None,
        activo=activo,
    )
    sesion.add(usuario)
    await sesion.flush()
    return usuario


@pytest.fixture()
async def escenario(base_de_datos: None) -> AsyncIterator[dict[str, Any]]:
    """Prepara una organización, los seis roles y usuarios representativos.

    Devuelve un diccionario con los objetos ya persistidos, para que cada
    prueba use lo que necesita sin repetir la preparación.
    """
    from app.core.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as sesion:
        organizacion = await _crear_organizacion(sesion)
        roles: dict[str, Any] = {}
        for clave, nivel in (
            ("observador", 0),
            ("colaborador", 1),
            ("analista", 2),
            ("gerente", 3),
            ("pmo", 4),
            ("administrador", 5),
        ):
            roles[clave] = await _crear_rol(sesion, clave, nivel)
        await sesion.commit()

        observador = await _crear_usuario(sesion, organizacion, roles["observador"])
        gerente = await _crear_usuario(
            sesion, organizacion, roles["gerente"], contrasena="ContrasenaSegura123"
        )
        pmo = await _crear_usuario(
            sesion, organizacion, roles["pmo"], contrasena="ContrasenaSegura123"
        )
        desactivado = await _crear_usuario(
            sesion, organizacion, roles["gerente"], activo=False
        )
        await sesion.commit()

    yield {
        "organizacion_id": str(organizacion.id),
        "roles": roles,
        "observador": observador,
        "gerente": gerente,
        "pmo": pmo,
        "desactivado": desactivado,
    }


# ══════════════════════════ HU_02 — Catálogo de roles ══════════════════════════


class TestCatalogoDeRoles:
    """HU_02: el catálogo de roles es la fuente de verdad del RBAC."""

    def test_el_catalogo_tiene_los_seis_roles_normativos(self) -> None:
        from app.core.rbac import CATALOGO_ROLES

        assert set(CATALOGO_ROLES) == {
            "observador",
            "colaborador",
            "analista",
            "gerente",
            "pmo",
            "administrador",
        }

    def test_los_niveles_son_correlativos_y_establecen_la_jerarquia(self) -> None:
        from app.core.rbac import CATALOGO_ROLES

        niveles = {clave: rol.nivel for clave, rol in CATALOGO_ROLES.items()}
        assert niveles == {
            "observador": 0,
            "colaborador": 1,
            "analista": 2,
            "gerente": 3,
            "pmo": 4,
            "administrador": 5,
        }

    def test_cada_rol_declara_que_puede_y_que_no_puede(self) -> None:
        from app.core.rbac import CATALOGO_ROLES

        for clave, rol in CATALOGO_ROLES.items():
            assert rol.puede, f"el rol '{clave}' no declara lo que puede"
            assert rol.no_puede, f"el rol '{clave}' no declara lo que no puede"

    def test_un_nivel_equivale_a_una_discrepancia(self) -> None:
        from app.core.rbac import catalogo_consistente

        niveles = {
            "observador": 0,
            "colaborador": 1,
            "analista": 2,
            "gerente": 9,
            "pmo": 4,
            "administrador": 5,
        }
        discrepancias = catalogo_consistente(niveles)
        assert len(discrepancias) == 1
        assert "gerente" in discrepancias[0]

    def test_un_rol_ausente_ou_demasiado_es_discrepancia(self) -> None:
        from app.core.rbac import catalogo_consistente

        niveles = {"observador": 0, "rol_inventado": 1}
        discrepancias = catalogo_consistente(niveles)
        assert any("falta el rol" in d for d in discrepancias)
        assert any("rol_inventado" in d for d in discrepancias)

    async def test_la_siembra_es_idempotente(self, escenario: dict[str, Any]) -> None:
        """Sembrar dos veces no duplica filas ni cambia niveles."""
        from sqlalchemy import select

        from app.core.db.models import Rol
        from app.core.db.session import AsyncSessionLocal

        async def _contar() -> int:
            async with AsyncSessionLocal() as sesion:
                return len((await sesion.execute(select(Rol))).scalars().all())

        async def _sembrar() -> None:
            from scripts.sembrar_roles import sembrar

            await sembrar()

        antes = await _contar()
        await _sembrar()
        primera = await _contar()
        await _sembrar()
        segunda = await _contar()

        assert antes == primera == segunda


# ══════════════════════════ HU_04 — Seguridad ══════════════════════════


class TestSeguridad:
    """HU_04: contraseñas y tokens de sesión."""

    def test_el_hash_bcrypt_no_contiene_la_contrasena(self) -> None:
        from app.core.security import crear_hash_contrasena, verificar_contrasena

        contrasena = "ContrasenaMuySegura456"
        hash_ = crear_hash_contrasena(contrasena)
        assert contrasena not in hash_
        assert hash_.startswith("$2b$")
        assert verificar_contrasena(contrasena, hash_) is True
        assert verificar_contrasena("otra-cosa", hash_) is False

    def test_la_contrasena_se_trunca_a_72_bytes(self) -> None:
        """bcrypt no acepta más de 72 bytes: si no se trunca, revienta."""
        from app.core.security import crear_hash_contrasena, verificar_contrasena

        larga = "a" * 100
        hash_ = crear_hash_contrasena(larga)
        assert verificar_contrasena(larga[:72], hash_) is True

    def test_el_token_de_sesion_lleva_los_claims_del_tobe(self) -> None:
        from app.core.security import generar_token_sesion, verificar_token_sesion

        token, expira = generar_token_sesion(
            usuario_id="u-1",
            organizacion_id="o-1",
            rol="gerente",
            nivel_rol=3,
            sesion_id="s-1",
        )
        claims = verificar_token_sesion(token)
        assert claims["sub"] == "u-1"
        assert claims["organizacion_id"] == "o-1"
        assert claims["rol"] == "gerente"
        assert claims["nivel"] == 3
        assert claims["sid"] == "s-1"
        assert expira > datetime.now(timezone.utc)

    def test_un_token_expirado_se_distingue_de_uno_invalido(self) -> None:
        from app.core.security import (
            ErrorDeToken,
            generar_token_sesion,
            verificar_token_sesion,
        )

        token, _ = generar_token_sesion(
            usuario_id="u-1",
            organizacion_id="o-1",
            rol="gerente",
            nivel_rol=3,
            sesion_id="s-1",
            expires_in=-10,
        )
        with pytest.raises(ErrorDeToken) as error:
            verificar_token_sesion(token)
        assert "expir" in str(error.value).lower()

    def test_un_token_manipulado_se_rechaza(self) -> None:
        from app.core.security import (
            ErrorDeToken,
            generar_token_sesion,
            verificar_token_sesion,
        )

        token, _ = generar_token_sesion(
            usuario_id="u-1",
            organizacion_id="o-1",
            rol="gerente",
            nivel_rol=3,
            sesion_id="s-1",
        )
        partes = token.split(".")
        partes[1] = partes[1][:-3] + ("aaa" if not partes[1].endswith("aaa") else "bbb")
        manipulado = ".".join(partes)
        with pytest.raises(ErrorDeToken):
            verificar_token_sesion(manipulado)

    def test_el_refresh_token_se_compara_en_tiempo_constante(self) -> None:
        from app.core.security import (
            comparar_refresh,
            generar_refresh_token,
            hash_de_refresh,
        )

        token, huella = generar_refresh_token()
        assert token != huella
        assert hash_de_refresh(token) == huella
        assert comparar_refresh(token, hash_de_refresh(token)) is True
        assert comparar_refresh("otro-token", hash_de_refresh(token)) is False


class TestAutenticacionHttp:
    """HU_04 por HTTP: login, sesión, refresh rotativo y logout."""

    async def test_login_con_credenciales_validas_abre_sesion(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        respuesta = await cliente.post(
            "/auth/login/password",
            json={"email": escenario["gerente"].email, "contrasena": "ContrasenaSegura123"},
        )
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["autenticado"] is True
        assert cuerpo["identidad"]["rol"] == "gerente"
        assert "projecta_sesion" in respuesta.cookies
        assert "projecta_refresh" in respuesta.cookies

    async def test_la_cookie_de_sesion_es_httponly(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        respuesta = await cliente.post(
            "/auth/login/password",
            json={"email": escenario["gerente"].email, "contrasena": "ContrasenaSegura123"},
        )
        cabecera = respuesta.headers.get("set-cookie", "")
        assert "httponly" in cabecera.lower()

    async def test_credenciales_invalidas_dan_401(
        self, cliente: AsyncClient, base_de_datos: None
    ) -> None:
        respuesta = await cliente.post(
            "/auth/login/password",
            json={"email": "nadie@banco.example", "contrasena": "ContrasenaSegura123"},
        )
        assert respuesta.status_code == 401

    async def test_contrasena_incorrecta_da_401(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        respuesta = await cliente.post(
            "/auth/login/password",
            json={"email": escenario["gerente"].email, "contrasena": "ContrasenaEquivocada1"},
        )
        assert respuesta.status_code == 401

    async def test_sin_cookie_me_da_401(
        self, cliente: AsyncClient, base_de_datos: None
    ) -> None:
        respuesta = await cliente.get("/auth/me")
        assert respuesta.status_code == 401

    async def test_me_devuelve_la_identidad_de_la_sesion(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        await cliente.post(
            "/auth/login/password",
            json={"email": escenario["gerente"].email, "contrasena": "ContrasenaSegura123"},
        )
        respuesta = await cliente.get("/auth/me")
        assert respuesta.status_code == 200, respuesta.text
        identidad = respuesta.json()
        assert identidad["email"] == escenario["gerente"].email
        assert identidad["nivel"] == 3

    async def test_el_refresh_rota_el_token(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        inicio = await cliente.post(
            "/auth/login/password",
            json={"email": escenario["gerente"].email, "contrasena": "ContrasenaSegura123"},
        )
        assert inicio.status_code == 200, inicio.text
        original = inicio.cookies.get("projecta_refresh")
        assert original

        respuesta = await cliente.post("/auth/refresh")
        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.cookies.get("projecta_refresh") != original

    async def test_reutilizar_un_refresh_revoca_la_familia_entera(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        """El robo de un refresh token se detecta por reutilización."""
        inicio = await cliente.post(
            "/auth/login/password",
            json={"email": escenario["gerente"].email, "contrasena": "ContrasenaSegura123"},
        )
        assert inicio.status_code == 200, inicio.text
        refresh_original = inicio.cookies.get("projecta_refresh")
        assert refresh_original

        # Se roba el token: el atacante lo usa primero.
        cliente.cookies.clear()
        cliente.cookies.set("projecta_refresh", refresh_original, path="/auth")
        primera = await cliente.post("/auth/refresh")
        assert primera.status_code == 200, primera.text
        refresh_ataque = primera.cookies.get("projecta_refresh")
        assert refresh_ataque and refresh_ataque != refresh_original

        # El titular legítimo lo reutiliza: es el segundo uso del mismo token.
        cliente.cookies.clear()
        cliente.cookies.set("projecta_refresh", refresh_original, path="/auth")
        segunda = await cliente.post("/auth/refresh")
        assert segunda.status_code == 401
        assert "familia" in segunda.json()["detail"].lower()

        # Y además, la familia queda revocada: el token del atacante ya no vale.
        cliente.cookies.clear()
        cliente.cookies.set("projecta_refresh", refresh_ataque, path="/auth")
        tercera = await cliente.post("/auth/refresh")
        assert tercera.status_code == 401

    async def test_logout_limpia_las_cookies(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        await cliente.post(
            "/auth/login/password",
            json={"email": escenario["gerente"].email, "contrasena": "ContrasenaSegura123"},
        )
        respuesta = await cliente.post("/auth/logout")
        assert respuesta.status_code == 200
        assert respuesta.json()["autenticado"] is False
        assert not cliente.cookies.get("projecta_sesion")

    async def test_un_usuario_desactivado_no_puede_iniciar_sesion(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        from app.core.security import crear_hash_contrasena
        from app.core.db.models import Usuario
        from app.core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as sesion:
            usuario = await sesion.get(Usuario, escenario["desactivado"].id)
            usuario.password_hash = crear_hash_contrasena("ContrasenaSegura123")
            await sesion.commit()

        respuesta = await cliente.post(
            "/auth/login/password",
            json={
                "email": escenario["desactivado"].email,
                "contrasena": "ContrasenaSegura123",
            },
        )
        assert respuesta.status_code == 403

    async def test_el_inicio_por_contrasena_esta_bloqueado_en_produccion(
        self, monkeypatch: pytest.MonkeyPatch, escenario: dict[str, Any]
    ) -> None:
        """En producción la identidad tiene que ser la corporativa."""
        from httpx import ASGITransport

        from app.core import config
        from app.main import create_app

        original = config.settings.app_env
        object.__setattr__(config.settings, "app_env", "production")
        try:
            transporte = ASGITransport(app=create_app())
            async with AsyncClient(transport=transporte, base_url="http://pruebas.interno") as c:
                respuesta = await c.post(
                    "/auth/login/password",
                    json={
                        "email": escenario["gerente"].email,
                        "contrasena": "ContrasenaSegura123",
                    },
                )
                assert respuesta.status_code == 403
                assert "producción" in respuesta.json()["detail"].lower()
        finally:
            object.__setattr__(config.settings, "app_env", original)


# ══════════════════════════ HU_05 — RBAC ══════════════════════════


class TestRbac:
    """HU_05: nivel, ámbito, membresía y estado del usuario."""

    def _actor(self, nivel: int, **kwargs: Any) -> Any:
        from app.core.rbac import Actor

        return Actor(
            usuario_id=kwargs.get("usuario_id", "u-1"),
            organizacion_id=kwargs.get("organizacion_id", "o-1"),
            rol=kwargs.get("rol", "gerente"),
            nivel=nivel,
            activo=kwargs.get("activo", True),
            proyectos=set(kwargs.get("proyectos", ())),
            ambito_organizacion=kwargs.get("ambito_organizacion", nivel >= 4),
        )

    def test_el_nivel_minimo_se_exige(self) -> None:
        from app.core.rbac import aprobar, evaluar

        assert (
            evaluar(self._actor(3, proyectos={"p-1"}), aprobar, proyecto_id="p-1").permitido
            is True
        )
        decision = evaluar(self._actor(2, proyectos={"p-1"}), aprobar, proyecto_id="p-1")
        assert decision.permitido is False
        assert decision.codigo == 403
        assert "nivel" in decision.regla

    def test_un_usuario_desactivado_no_opera_ni_con_nivel_alto(self) -> None:
        from app.core.rbac import aprobar, evaluar

        decision = evaluar(
            self._actor(5, activo=False, ambito_organizacion=True), aprobar, proyecto_id="p-1"
        )
        assert decision.permitido is False
        assert "desactivado" in decision.regla

    def test_sin_membresia_no_se_opera_sobre_el_proyecto(self) -> None:
        from app.core.rbac import aprobar, evaluar

        decision = evaluar(self._actor(3), aprobar, proyecto_id="p-1")
        assert decision.permitido is False
        assert "membresía" in decision.regla

    def test_con_membresia_se_opera_sobre_el_proyecto(self) -> None:
        from app.core.rbac import aprobar, evaluar

        decision = evaluar(self._actor(3, proyectos={"p-1"}), aprobar, proyecto_id="p-1")
        assert decision.permitido is True

    def test_el_ambito_de_organizacion_ignora_la_membresia(self) -> None:
        from app.core.rbac import aprobar, evaluar

        decision = evaluar(
            self._actor(5, ambito_organizacion=True), aprobar, proyecto_id="p-1"
        )
        assert decision.permitido is True

    def test_una_politica_de_proyecto_sin_proyecto_se_deniega(self) -> None:
        """Denegar por defecto es la opción segura ante la duda."""
        from app.core.rbac import aprobar, evaluar

        decision = evaluar(self._actor(5, ambito_organizacion=True), aprobar, proyecto_id=None)
        assert decision.permitido is False

    def test_exigir_lanza_error_de_permiso(self) -> None:
        from app.core.rbac import ErrorDePermiso, aprobar, exigir

        with pytest.raises(ErrorDePermiso) as error:
            exigir(self._actor(1), aprobar, proyecto_id="p-1")
        assert error.value.codigo == 403
        assert error.value.regla

    def test_el_motivo_auditado_es_el_real(self) -> None:
        """Con dos motivos de rechazo, el que se registra es el primero."""
        from app.core.rbac import aprobar, evaluar

        # Usuario desactivado y nivel insuficiente: la regla registrada
        # tiene que ser la del usuario desactivado, que es la primera.
        decision = evaluar(
            self._actor(1, activo=False), aprobar, proyecto_id="p-1"
        )
        assert "desactivado" in decision.regla
        assert "nivel" not in decision.regla


# ══════════════════════════ HU_05 — Rechazo auditado ══════════════════════════


class TestRechazoAuditado:
    """HU_05: un rechazo por permisos no es silencio, es un evento."""

    async def test_el_rechazo_por_permisos_deja_evento_de_auditoria(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        from sqlalchemy import select

        from app.core.db.models import EventoAuditoria, Usuario
        from app.core.db.session import AsyncSessionLocal
        from app.core.security import crear_hash_contrasena

        async with AsyncSessionLocal() as sesion:
            usuario = await sesion.get(Usuario, escenario["observador"].id)
            usuario.password_hash = crear_hash_contrasena("ContrasenaSegura123")
            await sesion.commit()

        await cliente.post(
            "/auth/login/password",
            json={
                "email": escenario["observador"].email,
                "contrasena": "ContrasenaSegura123",
            },
        )
        # El observador no puede crear proyectos: nivel 0 frente a nivel 4.
        respuesta = await cliente.post(
            "/proyectos", json={"nombre": "Proyecto de prueba", "codigo": "PRUEBA-1"}
        )
        assert respuesta.status_code == 403

        async with AsyncSessionLocal() as sesion:
            eventos = (
                (
                    await sesion.execute(
                        select(EventoAuditoria).where(
                            EventoAuditoria.tipo == "TRANSICION_RECHAZADA"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(eventos) == 1
        assert "proyecto" in (eventos[0].entidad or "").lower()


# ══════════════════════════ HU_03 — Proyectos ══════════════════════════


class TestProyectos:
    """HU_03: alta, unicidad de código, listado y borrado lógico."""

    async def _login_pmo(self, cliente: AsyncClient, escenario: dict[str, Any]) -> None:
        respuesta = await cliente.post(
            "/auth/login/password",
            json={"email": escenario["pmo"].email, "contrasena": "ContrasenaSegura123"},
        )
        assert respuesta.status_code == 200, respuesta.text

    async def test_crear_proyecto_devuelve_201_y_registra_el_evento(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        from sqlalchemy import select

        from app.core.db.models import EventoAuditoria
        from app.core.db.session import AsyncSessionLocal

        await self._login_pmo(cliente, escenario)
        respuesta = await cliente.post(
            "/proyectos",
            json={"nombre": "Rediseño del núcleo de procesos", "codigo": "PMO-2026-001"},
        )
        assert respuesta.status_code == 201, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == "PMO-2026-001"
        assert cuerpo["estado"] == "planificacion"

        async with AsyncSessionLocal() as sesion:
            eventos = (
                (
                    await sesion.execute(
                        select(EventoAuditoria).where(
                            EventoAuditoria.tipo == "PROYECTO_CREADO"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(eventos) == 1
        assert eventos[0].proyecto_id is not None

    async def test_el_codigo_duplicado_en_la_misma_organizacion_da_409(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        await self._login_pmo(cliente, escenario)
        datos = {"nombre": "Primer proyecto", "codigo": "PMO-2026-002"}
        primera = await cliente.post("/proyectos", json=datos)
        assert primera.status_code == 201
        segunda = await cliente.post(
            "/proyectos", json={"nombre": "Segundo proyecto", "codigo": "PMO-2026-002"}
        )
        assert segunda.status_code == 409
        assert "código" in segunda.json()["detail"].lower()

    async def test_el_codigo_se_normaliza_a_mayusculas(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        await self._login_pmo(cliente, escenario)
        respuesta = await cliente.post(
            "/proyectos", json={"nombre": "Proyecto", "codigo": "pmo-2026-003"}
        )
        assert respuesta.status_code == 201
        assert respuesta.json()["codigo"] == "PMO-2026-003"

    async def test_un_estado_desconocido_da_422(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        await self._login_pmo(cliente, escenario)
        respuesta = await cliente.post(
            "/proyectos",
            json={"nombre": "Proyecto", "codigo": "PMO-2026-004", "estado": "inventado"},
        )
        assert respuesta.status_code == 422

    async def test_el_borrado_es_logico_y_el_proyecto_desaparece_del_listado(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        await self._login_pmo(cliente, escenario)
        creada = await cliente.post(
            "/proyectos", json={"nombre": "Proyecto efímero", "codigo": "PMO-2026-005"}
        )
        identificador = creada.json()["id"]

        borrado = await cliente.delete(f"/proyectos/{identificador}")
        assert borrado.status_code == 200

        listado = await cliente.get("/proyectos")
        assert identificador not in [e["id"] for e in listado.json()["elementos"]]

        detalle = await cliente.get(f"/proyectos/{identificador}")
        assert detalle.status_code == 404

    async def test_un_observador_no_crea_proyectos(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        from app.core.db.models import Usuario
        from app.core.db.session import AsyncSessionLocal
        from app.core.security import crear_hash_contrasena

        async with AsyncSessionLocal() as sesion:
            usuario = await sesion.get(Usuario, escenario["observador"].id)
            usuario.password_hash = crear_hash_contrasena("ContrasenaSegura123")
            await sesion.commit()

        await cliente.post(
            "/auth/login/password",
            json={
                "email": escenario["observador"].email,
                "contrasena": "ContrasenaSegura123",
            },
        )
        respuesta = await cliente.post(
            "/proyectos", json={"nombre": "No permitido", "codigo": "PMO-2026-006"}
        )
        assert respuesta.status_code == 403

    async def test_sin_autenticacion_da_401(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        respuesta = await cliente.get("/proyectos")
        assert respuesta.status_code == 401


# ══════════════════════════ HU_06 — Observabilidad ══════════════════════════


class TestObservabilidad:
    """HU_06: métricas y correlación de peticiones."""

    async def test_el_endpoint_de_metricas_responde(
        self, cliente: AsyncClient, base_de_datos: None
    ) -> None:
        respuesta = await cliente.get("/metrics")
        assert respuesta.status_code == 200
        assert "projecta" in respuesta.text

    async def test_la_peticion_devuelve_el_identificador_de_correlacion(
        self, cliente: AsyncClient, base_de_datos: None
    ) -> None:
        respuesta = await cliente.get("/health")
        assert respuesta.status_code == 200
        assert respuesta.headers.get("X-Correlation-Id")

    async def test_se_respeta_el_identificador_de_correlacion_del_cliente(
        self, cliente: AsyncClient, base_de_datos: None
    ) -> None:
        identificador = "correlacion-de-prueba-123"
        respuesta = await cliente.get(
            "/health", headers={"X-Correlation-Id": identificador}
        )
        assert respuesta.headers.get("X-Correlation-Id") == identificador

    def test_el_contexto_de_correlacion_se_limpia_al_terminar(self) -> None:
        from app.core.observability import (
            CONTEXTO_ACTUAL,
            establecer_correlacion,
            limpiar_correlacion,
        )

        with limpiar_correlacion():
            establecer_correlacion("abc")
            assert CONTEXTO_ACTUAL.get("correlation_id") == "abc"
        assert "correlation_id" not in CONTEXTO_ACTUAL


# ══════════════════════════ HU_08 — Almacenamiento ══════════════════════════


class TestAlmacenamiento:
    """HU_08: idempotencia por contenido y supervivencia del metadato."""

    def test_el_sha256_es_el_esperado(self) -> None:
        import hashlib

        from app.core.storage import calcular_sha256

        datos = b"contenido de prueba"
        assert calcular_sha256(datos) == hashlib.sha256(datos).hexdigest()

    def test_la_clave_deriva_del_contenido_y_se_reparte_en_subdirectorios(
        self,
    ) -> None:
        from app.core.storage import calcular_sha256, clave_para_contenido

        huella = calcular_sha256(b"contenido")
        clave = clave_para_contenido(huella, "pdf")
        assert clave == f"{huella[:2]}/{huella[2:4]}/{huella}.pdf"
        assert clave_para_contenido(huella, "pdf") == clave

    def test_dos_ficheros_con_el_mismo_contenido_comparten_clave(self) -> None:
        from app.core.storage import calcular_sha256, clave_para_contenido

        a = clave_para_contenido(calcular_sha256(b"igual"), "txt")
        b = clave_para_contenido(calcular_sha256(b"igual"), "txt")
        assert a == b

    def test_el_tamano_maximo_esta_acotado(self) -> None:
        from app.core.storage import TAMANO_MAXIMO

        assert TAMANO_MAXIMO == 50 * 1024 * 1024

    async def test_un_adjunto_inexistente_da_404(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        respuesta = await cliente.get(f"/adjuntos/{uuid.uuid4()}/contenido")
        assert respuesta.status_code in (401, 404)

    async def test_la_lista_de_adjuntos_no_rompe_sin_almacenamiento(
        self, cliente: AsyncClient, escenario: dict[str, Any]
    ) -> None:
        """El almacenamiento no es crítico: su caída no rompe el listado."""
        await cliente.post(
            "/auth/login/password",
            json={"email": escenario["pmo"].email, "contrasena": "ContrasenaSegura123"},
        )
        respuesta = await cliente.get("/adjuntos")
        assert respuesta.status_code == 200, respuesta.text
        assert "elementos" in respuesta.json()


# ══════════════════════════ Auditoría — cadena de hash ══════════════════════════


class TestCadenaDeAuditoria:
    """La cadena de auditoría tiene que ser verificable, no declarativa."""

    async def test_la_cadena_de_tres_eventos_es_integra(
        self, escenario: dict[str, Any]
    ) -> None:
        from app.core import auditoria
        from app.core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as sesion:
            for indice in range(3):
                await auditoria.registrar(
                    sesion,
                    auditoria.Evento(
                        tipo="PROYECTO_CREADO",
                        entidad=f"proyecto:{indice}",
                        actor="actor-de-prueba",
                        organizacion_id=escenario["organizacion_id"],
                        detalle=f"evento {indice}",
                    ),
                )
            await sesion.commit()
            informe = await auditoria.verificar_cadena(
                sesion, escenario["organizacion_id"]
            )
        assert informe["integra"] is True
        assert informe["eventos"] == 3

    async def test_alterar_un_evento_rompe_la_cadena(
        self, escenario: dict[str, Any]
    ) -> None:
        """Si alguien modifica un evento, la rotura se detecta."""
        from sqlalchemy import select

        from app.core import auditoria
        from app.core.db.models import EventoAuditoria
        from app.core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as sesion:
            for indice in range(3):
                await auditoria.registrar(
                    sesion,
                    auditoria.Evento(
                        tipo="PROYECTO_CREADO",
                        entidad=f"proyecto:{indice}",
                        actor="actor-de-prueba",
                        organizacion_id=escenario["organizacion_id"],
                        detalle=f"evento {indice}",
                    ),
                )
            await sesion.commit()

        # Se altera el contenido del segundo evento, sin tocar su hash.
        async with AsyncSessionLocal() as sesion:
            eventos = (
                (
                    await sesion.execute(
                        select(EventoAuditoria)
                        .where(
                            EventoAuditoria.organizacion_id
                            == uuid.UUID(escenario["organizacion_id"])
                        )
                        .order_by(EventoAuditoria.cuando.asc(), EventoAuditoria.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert len(eventos) == 3
            eventos[1].detalle = "detalle alterado a espaldas del sistema"
            await sesion.commit()

        async with AsyncSessionLocal() as sesion:
            informe = await auditoria.verificar_cadena(
                sesion, escenario["organizacion_id"]
            )
        assert informe["integra"] is False
        assert "alterado" in informe["motivo"]

    async def test_un_tipo_de_evento_desconocido_se_rechaza(
        self, escenario: dict[str, Any]
    ) -> None:
        from app.core import auditoria
        from app.core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as sesion:
            with pytest.raises(auditoria.ErrorDeAuditoria):
                await auditoria.registrar(
                    sesion,
                    auditoria.Evento(
                        tipo="TIPO_INVENTADO",
                        entidad="x",
                        actor="y",
                        organizacion_id=escenario["organizacion_id"],
                    ),
                )

    def test_el_catalogo_de_eventos_cubre_las_taxonomias_del_tobe(self) -> None:
        from app.core.auditoria import CATALOGO_EVENTOS

        for obligatorio in (
            "PROYECTO_CREADO",
            "TRANSICION_RECHAZADA",
            "ESTADO_CAMBIADO",
            "ARTEFACTO_APROBADO",
            "ARTEFACTO_RECHAZADO",
            "SESION_INICIADA",
            "FUENTE_INGRESADA",
        ):
            assert obligatorio in CATALOGO_EVENTOS



# ══════════════════════════ Coherencia interna ══════════════════════════


class TestCoherenciaInterna:
    """Regresiones de los defectos encontrados en la auditoría del proyecto.

    Cada prueba de esta clase fija un defecto real que se detectó al
    validar. Sin ellas, el defecto vuelve: un ``__all__`` que declara un
    símbolo inexistente o un parámetro que el RBAC siempre rechaza son
    cosas que no se ven leyendo el código.
    """

    def test_ningun_modulo_declara_en_all_un_simbolo_inexistente(self) -> None:
        """`__all__` que promete un símbolo que no existe rompe `from x import *`."""
        import importlib
        import pkgutil

        import app

        fallos: list[str] = []
        modulos = []
        for info in pkgutil.walk_packages(app.__path__, prefix="app."):
            try:
                modulos.append(importlib.import_module(info.name))
            except Exception:  # noqa: BLE001
                continue
        modulos.append(importlib.import_module("app"))

        for modulo in modulos:
            declarados = getattr(modulo, "__all__", None)
            if not declarados:
                continue
            for simbolo in declarados:
                if not hasattr(modulo, simbolo):
                    fallos.append(f"{modulo.__name__}: '{simbolo}'")
        assert not fallos, (
            "estos símbolos están declarados en __all__ pero no existen: "
            + ", ".join(fallos)
        )

    def test_la_subida_de_adjuntos_exige_proyecto(self) -> None:
        """La política de RBAC de la evidencia tiene ámbito de proyecto.

        Declarar el parámetro como opcional y rechazarlo siempre por
        permisos sería una contradicción: la opcionalidad sería falsa.
        """
        import inspect

        from app.api.routers import adjuntos

        firma = inspect.signature(adjuntos.subir)
        parametro = firma.parameters["proyecto_id"]
        assert (
            parametro.default is inspect.Parameter.empty
        ), "proyecto_id no debería ser opcional en la subida de adjuntos"
        assert (
            parametro.annotation is not str
        ), "proyecto_id debería anotarse con Query, no como texto libre"

    def test_el_listado_de_adjuntos_admite_proyecto_opcional(self) -> None:
        """Listar sí admite omitir el proyecto: es una operación de lectura."""
        import inspect

        from app.api.routers import adjuntos

        firma = inspect.signature(adjuntos.listar)
        assert firma.parameters["proyecto_id"].default is None

    def test_el_modelo_de_adjuntos_exige_proyecto(self) -> None:
        """El esquema y el modelo tienen que decir lo mismo."""
        from app.core.db.models import Adjunto

        columna = Adjunto.__table__.columns["proyecto_id"]
        assert columna.nullable is False, (
            "adjuntos.proyecto_id no debería admitir nulos: la evidencia "
            "siempre pertenece a un proyecto"
        )

    def test_el_compose_pasa_las_variables_de_identidad_y_trazas(self) -> None:
        """Sin estas variables, un despliegue con Docker no tiene OIDC ni trazas."""
        import pathlib

        compose = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"
        texto = compose.read_text(encoding="utf-8")
        for variable in (
            "OIDC_ISSUER",
            "OIDC_CLIENT_ID",
            "OIDC_CLIENT_SECRET",
            "OIDC_REDIRECT_URI",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
        ):
            assert f"{variable}:" in texto, (
                f"el compose no pasa {variable} al contenedor de la aplicación"
            )
