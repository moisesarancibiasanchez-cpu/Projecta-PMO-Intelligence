"""Autenticación de Projecta PMO Intelligence (HU_04).

Implementa el flujo de sesión con cookie HttpOnly y refresh rotativo:

* ``GET  /auth/login`` — inicia el flujo OIDC redirigiendo al proveedor.
* ``GET  /auth/callback`` — canjea el código, valida la identidad, abre la
  sesión y fija las cookies.
* ``POST /auth/refresh`` — rota el refresh token.
* ``POST /auth/logout`` — cierra la sesión y revoca la familia de tokens.
* ``GET  /auth/me`` — devuelve la identidad de la sesión en curso.
* ``POST /auth/login/password`` — autenticación local por contraseña.
  **Solo en desarrollo**: en producción se rechaza con 403, porque la
  identidad tiene que ser la corporativa.

El refresh token **rota en cada uso**. Si un token ya usado vuelve a
presentarse, se revoca **toda la familia**: es la señal de que el token se
ha robado, y lo correcto es invalidar la familia entera en lugar de
aceptar el uso.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    COOKIE_REFRESH,
    COOKIE_SESION,
    DependenciaSesion,
    DependenciaUsuario,
)
from app.core import auditoria
from app.core.config import settings
from app.core.db.models import Organizacion, Rol, Sesion, Usuario
from app.core.oidc import (
    ErrorDeOIDC,
    ProveedorOIDC,
    configuracion_oidc,
    nuevo_estado,
    nuevo_nonce,
)
from app.core.security import (
    a_utc,
    generar_refresh_token,
    generar_token_sesion,
    hash_de_refresh,
    verificar_contrasena,
)

LOG = logging.getLogger("projecta.auth")

router = APIRouter(prefix="/auth", tags=["autenticación"])


# ─────────────────────────── esquemas ───────────────────────────


class Identidad(BaseModel):
    """Identidad del usuario en sesión."""

    id: str
    email: str
    nombre: str
    rol: str
    nivel: int
    organizacion_id: str
    organizacion: str
    activo: bool = True


class RespuestaLogin(BaseModel):
    """Respuesta del inicio de sesión."""

    autenticado: bool
    identidad: Identidad
    expira_en: int


class PeticionPassword(BaseModel):
    """Credenciales para el inicio de sesión local de desarrollo."""

    email: str = Field(min_length=5, max_length=320)
    contrasena: str = Field(min_length=8, max_length=200)


# ─────────────────────────── utilidades ───────────────────────────


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _fijar_cookies(respuesta: Response, token: str, refresh: str, expira_en: datetime) -> None:
    """Fija las cookies de sesión y de refresh.

    La cookie de sesión es ``HttpOnly`` en todos los entornos y ``Secure``
    en producción: sin ``HttpOnly`` el token es accesible desde JavaScript
    y cualquier XSS lo roba.
    """
    max_age_sesion = max(1, int((expira_en - _ahora()).total_seconds()))
    respuesta.set_cookie(
        COOKIE_SESION,
        token,
        max_age=max_age_sesion,
        httponly=True,
        secure=settings.es_produccion,
        samesite="lax",
        path="/",
    )
    respuesta.set_cookie(
        COOKIE_REFRESH,
        refresh,
        max_age=7 * 24 * 3600,
        httponly=True,
        secure=settings.es_produccion,
        samesite="strict",
        path="/auth",
    )


def _limpiar_cookies(respuesta: Response) -> None:
    """Borra las cookies de sesión."""
    respuesta.delete_cookie(COOKIE_SESION, path="/")
    respuesta.delete_cookie(COOKIE_REFRESH, path="/auth")


async def _identidad_de(usuario: Usuario, sesion: Any) -> Identidad:
    """Construye la identidad pública de un usuario.

    El rol se resuelve con una consulta propia, por la misma razón que en
    ``_abrir_sesion``: la relación ``usuario.rol`` no siempre está
    cargada y acceder a ella de forma perezosa no es válido en código
    asíncrono.
    """
    organizacion = await sesion.get(Organizacion, usuario.organizacion_id)
    rol = await sesion.get(Rol, usuario.rol_id)
    return Identidad(
        id=str(usuario.id),
        email=usuario.email,
        nombre=usuario.nombre,
        rol=rol.nombre if rol else "desconocido",
        nivel=rol.nivel if rol else 0,
        organizacion_id=str(usuario.organizacion_id),
        organizacion=organizacion.nombre if organizacion else "desconocida",
        activo=usuario.activo,
    )


async def _abrir_sesion(
    sesion: Any, usuario: Usuario, *, familia_id: str | None = None
) -> tuple[str, datetime, str, str]:
    """Crea una sesión y devuelve ``(token, expira, refresh, familia)``.

    El rol se resuelve con una consulta propia y **no** a través de la
    relación ``usuario.rol``. Acceder a una relación no cargada dispara
    una carga perezosa, y SQLAlchemy no permite eso dentro de código
    asíncrono: el fallo aparecería como un error de concurrencia y no
    como un problema de permisos.
    """
    familia = familia_id or uuid.uuid4().hex
    identificador_sesion = uuid.uuid4().hex
    rol = await sesion.get(Rol, usuario.rol_id)
    token, expira = generar_token_sesion(
        usuario_id=str(usuario.id),
        organizacion_id=str(usuario.organizacion_id),
        rol=rol.nombre if rol else "observador",
        nivel_rol=rol.nivel if rol else 0,
        sesion_id=identificador_sesion,
    )
    refresh, huella = generar_refresh_token()
    fila = Sesion(
        id=identificador_sesion,
        usuario_id=usuario.id,
        organizacion_id=usuario.organizacion_id,
        familia_id=familia,
        hash_refresh=huella,
        expira_en=_ahora() + timedelta(days=14),
    )
    sesion.add(fila)
    await sesion.flush()
    return token, expira, refresh, familia


# ─────────────────────────── endpoints ───────────────────────────


@router.get("/login", summary="Inicia el flujo de autenticación OIDC")
async def login(respuesta: Response, request: Request) -> Any:
    """Devuelve la URL a la que redirigir al usuario para autenticarse."""
    configuracion = configuracion_oidc()
    if not configuracion.habilitado:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No hay proveedor OIDC configurado. Define OIDC_ISSUER, "
                "OIDC_CLIENT_ID, OIDC_CLIENT_SECRET y OIDC_REDIRECT_URI."
            ),
        )
    estado = request.cookies.get("projecta_oauth_state") or nuevo_estado()
    nonce = nuevo_nonce()
    proveedor = ProveedorOIDC(configuracion)
    try:
        url = await proveedor.url_autorizacion(estado=estado, nonce=nonce)
    finally:
        await proveedor.cliente.aclose()
    respuesta.set_cookie(
        "projecta_oauth_state", estado, max_age=600, httponly=True, samesite="lax", path="/"
    )
    respuesta.set_cookie(
        "projecta_oauth_nonce", nonce, max_age=600, httponly=True, samesite="lax", path="/"
    )
    return {"url": url, "estado": estado}


@router.get("/callback", summary=".Callback del proveedor OIDC")
async def callback(
    request: Request,
    respuesta: Response,
    sesion: DependenciaSesion,
    codigo: str | None = None,
    estado: str | None = None,
) -> Any:
    """Canjea el código, valida la identidad y abre la sesión."""
    if not codigo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Falta el código de autorización."
        )
    estado_esperado = request.cookies.get("projecta_oauth_state")
    if estado_esperado and estado and estado != estado_esperado:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El estado de la autenticación no coincide: posible CSRF.",
        )

    configuracion = configuracion_oidc()
    proveedor = ProveedorOIDC(configuracion)
    try:
        tokens = await proveedor.canjear_codigo(codigo)
        claims = await proveedor.validar_token_identidad(
            tokens["id_token"], nonce_esperado=request.cookies.get("projecta_oauth_nonce")
        )
    except ErrorDeOIDC as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    finally:
        await proveedor.cliente.aclose()

    email = str(claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El proveedor no devuelve un correo utilizable.",
        )
    nombre = str(claims.get("name") or email)
    usuario = await _buscar_o_crear_usuario(sesion, email, nombre, claims)
    if not usuario.activo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="El usuario está desactivado."
        )

    token, expira, refresh, _familia = await _abrir_sesion(sesion, usuario)
    await _registrar_sesion(sesion, usuario, "SESION_INICIADA")
    await sesion.commit()
    _fijar_cookies(respuesta, token, refresh, expira)
    return {"autenticado": True, "identidad": await _identidad_de(usuario, sesion)}


@router.post("/refresh", summary="Rota el refresh token")
async def refrescar(
    request: Request, respuesta: Response, sesion: DependenciaSesion
) -> Any:
    """Rota el refresh token y emite una sesión nueva.

    Si el token presentado ya se usó, se revoca **toda la familia** y se
    responde con 401: es la señal de que el token se ha robado.
    """
    refresh = request.cookies.get(COOKIE_REFRESH)
    if not refresh:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="No hay refresh token."
        )
    resultado = await sesion.execute(
        select(Sesion).where(Sesion.hash_refresh == hash_de_refresh(refresh))
    )
    fila = resultado.scalar_one_or_none()
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token no válido."
        )
    if fila.revocado_en is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="La sesión está revocada."
        )
    if a_utc(fila.expira_en) < _ahora():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="El refresh token ha expirado."
        )
    if fila.usado_en is not None:
        await _revocar_familia(sesion, fila.familia_id)
        await sesion.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "El refresh token ya se había usado: se ha revocado toda la familia "
                "de sesiones por posible robo."
            ),
        )

    usuario = await sesion.get(Usuario, fila.usuario_id)
    if usuario is None or not usuario.activo:
        await _revocar_familia(sesion, fila.familia_id)
        await sesion.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="El usuario ya no puede operar."
        )

    fila.usado_en = _ahora()
    token, expira, refresh_nuevo, familia = await _abrir_sesion(
        sesion, usuario, familia_id=fila.familia_id
    )
    await sesion.commit()
    _fijar_cookies(respuesta, token, refresh_nuevo, expira)
    return {"autenticado": True, "identidad": await _identidad_de(usuario, sesion)}


@router.post("/logout", summary="Cierra la sesión")
async def cerrar_sesion(
    request: Request, respuesta: Response, sesion: DependenciaSesion
) -> Any:
    """Revoca la familia de sesiones del refresh token presentado."""
    refresh = request.cookies.get(COOKIE_REFRESH)
    if refresh:
        resultado = await sesion.execute(
            select(Sesion).where(Sesion.hash_refresh == hash_de_refresh(refresh))
        )
        fila = resultado.scalar_one_or_none()
        if fila is not None:
            await _revocar_familia(sesion, fila.familia_id)
            await sesion.commit()
    _limpiar_cookies(respuesta)
    return {"autenticado": False}


@router.get("/me", summary="Identidad de la sesión en curso")
async def quien_soy(usuario: DependenciaUsuario, sesion: DependenciaSesion) -> Any:
    """Devuelve la identidad del usuario autenticado."""
    return await _identidad_de(usuario, sesion)


@router.post("/login/password", summary="Inicio de sesión local (solo desarrollo)")
async def login_password(
    peticion: PeticionPassword, respuesta: Response, sesion: DependenciaSesion
) -> Any:
    """Autentica con correo y contraseña.

    **Solo disponible fuera de producción.** En producción la identidad
    tiene que ser la corporativa (OIDC), y este camino se rechaza con 403.
    """
    if settings.es_produccion:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "El inicio de sesión por contraseña está deshabilitado en producción: "
                "la identidad debe ser la corporativa."
            ),
        )
    resultado = await sesion.execute(
        select(Usuario).options(selectinload(Usuario.rol)).where(
            Usuario.email == peticion.email.strip().lower()
        )
    )
    usuario = resultado.scalar_one_or_none()
    if usuario is None or not usuario.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales no válidas."
        )
    if not verificar_contrasena(peticion.contrasena, usuario.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales no válidas."
        )
    if not usuario.activo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="El usuario está desactivado."
        )
    token, expira, refresh, _familia = await _abrir_sesion(sesion, usuario)
    await _registrar_sesion(sesion, usuario, "SESION_INICIADA")
    await sesion.commit()
    _fijar_cookies(respuesta, token, refresh, expira)
    return {"autenticado": True, "identidad": await _identidad_de(usuario, sesion)}


# ─────────────────────────── apoyo ───────────────────────────


async def _buscar_o_crear_usuario(
    sesion: Any, email: str, nombre: str, claims: dict[str, Any]
) -> Usuario:
    """Busca al usuario por correo y lo crea si no existe.

    Un usuario nuevo entra con el rol más bajo del catálogo: los permisos se
    conceden, no se obtienen por el mero hecho de iniciar sesión.
    """
    resultado = await sesion.execute(
        select(Usuario).options(selectinload(Usuario.rol)).where(Usuario.email == email)
    )
    usuario = resultado.scalar_one_or_none()
    if usuario is not None:
        return usuario

    organizacion = await _organizacion_para(sesion, email, claims)
    rol_observador = await sesion.execute(
        select(Rol).where(Rol.nombre == "observador")
    )
    rol = rol_observador.scalar_one_or_none()
    if rol is None:
        rol = Rol(nombre="observador", nivel=0, ambito="organizacion")
        sesion.add(rol)
        await sesion.flush()
    usuario = Usuario(
        organizacion_id=organizacion.id,
        rol_id=rol.id,
        email=email,
        nombre=nombre,
        proveedor_identidad=str(claims.get("iss") or "oidc"),
        activo=True,
    )
    sesion.add(usuario)
    await sesion.flush()
    return usuario


async def _organizacion_para(
    sesion: Any, email: str, claims: dict[str, Any]
) -> Organizacion:
    """Determina (o crea) la organización a la que pertenece el usuario."""
    organizacion_claim = claims.get("organization") or claims.get("organizacion")
    if organizacion_claim:
        resultado = await sesion.execute(
            select(Organizacion).where(Organizacion.slug == str(organizacion_claim))
        )
        encontrada = resultado.scalar_one_or_none()
        if encontrada is not None:
            return encontrada
    dominio = email.rsplit("@", 1)[-1] if "@" in email else "desconocido"
    resultado = await sesion.execute(
        select(Organizacion).where(Organizacion.slug == dominio)
    )
    encontrada = resultado.scalar_one_or_none()
    if encontrada is not None:
        return encontrada
    nueva = Organizacion(nombre=dominio, slug=dominio, activo=True)
    sesion.add(nueva)
    await sesion.flush()
    return nueva


async def _registrar_sesion(sesion: Any, usuario: Usuario, tipo: str) -> None:
    """Registra en la auditoría el inicio o el cierre de una sesión."""
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo=tipo,
            entidad=f"usuario:{usuario.id}",
            actor=usuario.email,
            organizacion_id=str(usuario.organizacion_id),
            usuario_id=str(usuario.id),
            detalle="Inicio de sesión",
        ),
    )


async def _revocar_familia(sesion: Any, familia_id: str) -> None:
    """Revoca todos los refresh tokens de una familia."""
    resultado = await sesion.execute(
        select(Sesion).where(Sesion.familia_id == familia_id)
    )
    for fila in resultado.scalars():
        if fila.revocado_en is None:
            fila.revocado_en = _ahora()
