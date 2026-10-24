"""Dependencias compartidas de la API de Projecta PMO Intelligence.

Cubre las tres capas de autorización:

* **Sesión** — la cookie o la cabecera ``Authorization`` llevan un JWT de
  sesión firmado. La cookie es ``HttpOnly`` y ``Secure`` en producción.
* **Estado del usuario** — un token vigente no basta: si el usuario está
  desactivado en base de datos, la petición se rechaza con 403. El token
  transporta identidad, no permisos.
* **RBAC** — las políticas del TO-BE se evalúan en runtime contra
  ``roles.nivel``, y la ausencia de membresía impide operar sobre un
  proyecto ajeno.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import rbac
from app.core.config import Settings, settings
from app.core.db.models import MembresiaProyecto, Usuario
from app.core.db.session import get_session
from app.core.security import ErrorDeToken, verificar_token_sesion

__all__ = [
    "COOKIE_REFRESH",
    "COOKIE_SESION",
    "DependenciaActor",
    "DependenciaRedis",
    "DependenciaSesion",
    "DependenciaToken",
    "DependenciaUsuario",
    "exigir_politica",
    "get_actor",
    "get_redis",
    "get_session",
    "get_settings",
    "get_usuario_actual",
    "obtener_token",
]

#: Nombre de la cookie de sesión.
COOKIE_SESION = "projecta_sesion"

#: Nombre de la cookie del refresh token.
COOKIE_REFRESH = "projecta_refresh"


def get_settings() -> Settings:
    """Entrega la configuración de la aplicación."""
    return settings


async def get_redis() -> AsyncGenerator[Redis, None]:
    """Entrega un cliente Redis asíncrono y lo cierra al terminar la petición."""
    cliente = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
        health_check_interval=30,
    )
    try:
        yield cliente
    finally:
        await cliente.aclose()


# ─────────────────────────── identidad ───────────────────────────


def obtener_token(
    cookie_sesion: Annotated[str | None, Cookie(alias=COOKIE_SESION)] = None,
    encabezado_authorization: Annotated[str | None, Header()] = None,
) -> str | None:
    """Extrae el token de sesión de la cookie o de la cabecera.

    La cookie tiene prioridad: es el mecanismo que usa el navegador, y la
    cabecera se reserva para clientes de API.
    """
    if cookie_sesion:
        return cookie_sesion
    if encabezado_authorization:
        partes = encabezado_authorization.split(None, 1)
        if len(partes) == 2 and partes[0].lower() == "bearer":
            return partes[1].strip()
    return None


DependenciaToken = Annotated[str | None, Depends(obtener_token)]


async def get_usuario_actual(
    sesion: Annotated[AsyncSession, Depends(get_session)],
    token: DependenciaToken,
) -> Usuario:
    """Resuelve el usuario a partir del token de sesión.

    Lanza 401 si el token falta, no es válido o ha expirado, y 403 si el
    usuario está desactivado. La comprobación del estado se hace **en base
    de datos**: un token vigente de un usuario desactivado no sirve.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Se requiere autenticación.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = verificar_token_sesion(token)
    except ErrorDeToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.motivo,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        usuario_id = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Identidad no válida."
        ) from exc

    resultado = await sesion.execute(
        select(Usuario).options(selectinload(Usuario.rol)).where(Usuario.id == usuario_id)
    )
    usuario = resultado.scalar_one_or_none()
    if usuario is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="La sesión ya no existe."
        )
    if not usuario.activo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El usuario está desactivado.",
        )
    return usuario


async def get_actor(
    usuario: Annotated[Usuario, Depends(get_usuario_actual)],
    sesion: Annotated[AsyncSession, Depends(get_session)],
) -> rbac.Actor:
    """Construye el actor con su nivel de rol y sus proyectos.

    Los proyectos se cargan en una sola consulta, no uno por usuario, para
    no caer en N+1 al listar.
    """
    proyectos: set[str] = set()
    if usuario.rol and usuario.rol.nivel < 4:
        resultado = await sesion.execute(
            select(MembresiaProyecto.proyecto_id).where(
                MembresiaProyecto.usuario_id == usuario.id
            )
        )
        proyectos = {str(fila[0]) for fila in resultado.all()}
    return rbac.actor_desde_registro(usuario, proyectos=proyectos)


#: Anotaciones de dependencia listas para usar en los routers.
DependenciaSesion = Annotated[AsyncSession, Depends(get_session)]
DependenciaRedis = Annotated[Redis, Depends(get_redis)]
DependenciaUsuario = Annotated[Usuario, Depends(get_usuario_actual)]
DependenciaActor = Annotated[rbac.Actor, Depends(get_actor)]


def exigir_politica(
    politica: rbac.Politica, *, parametro_proyecto: str = "proyecto_id"
) -> Any:
    """Dependencia que exige una política concreta sobre el actor.

    Se usa así::

        @router.post("/proyectos/{proyecto_id}/aprobar")
        async def aprobar(
            proyecto_id: uuid.UUID,
            _: None = Depends(exigir_politica(rbac.aprobar)),
        ): ...

    El identificador del proyecto se toma de los parámetros de la ruta, de
    modo que la política de ámbito de proyecto se evalúa con el proyecto
    real de la petición.
    """

    async def _comprobar(
        request: Request,
        actor: Annotated[rbac.Actor, Depends(get_actor)],
    ) -> None:
        proyecto_id = request.path_params.get(parametro_proyecto)
        decision = rbac.evaluar(actor, politica, proyecto_id=proyecto_id)
        if not decision.permitido:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=decision.motivo,
                headers={"X-Regla": decision.regla},
            )

    return _comprobar
