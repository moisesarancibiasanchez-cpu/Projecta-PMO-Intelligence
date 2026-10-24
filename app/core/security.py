"""Seguridad de Projecta PMO Intelligence: contraseñas, JWT y sesiones.

Implementa HU_04 — autenticación con sesión JWT en cookie HttpOnly y
refresh rotativo.

Decisiones que importan:

* **El hash de contraseña nunca se almacena en claro.** Se usa bcrypt con
  12 rondas, como fija el TO-BE v1.1 (§6.1).
* **El token de sesión viaja en cookie HttpOnly.** Con ``Secure`` en
  producción y ``SameSite=Lax``, para que el navegador no lo adjunte a
  peticiones de terceros.
* **El refresh token rota en cada uso y pertenece a una familia.** Si un
  token ya usado vuelve a presentarse, se revoca **toda** la familia: es
  la señal de que el token se ha robado, y lo correcto es invalidar la
  familia entera en lugar de acceptar el uso.
* **Un usuario desactivado pierde la sesión aunque el token no haya
  expirado.** La comprobación del estado del usuario es en base de datos, no
  en el token: el token solo transporta identidad, no permisos.
* **Los secretos se derivan, no se transportan.** El token lleva un
  identificador de sesión y un identificador de familia; el material
  criptográfico se guarda hasheado en base de datos.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import bcrypt
import jwt

from app.core.config import settings

__all__ = [
    "a_utc",
    "ConfiguracionToken",
    "ErrorDeToken",
    "configuracion_token",
    "crear_hash_contrasena",
    "generar_refresh_token",
    "generar_token_sesion",
    "hash_de_refresh",
    "verificar_contrasena",
    "verificar_token_sesion",
]

#: Rondas de bcrypt. El TO-BE v1.1 fija 12.
RONDAS_BCRYPT: Final[int] = 12

#: Longitud en bytes del material aleatorio de un refresh token.
BYTES_REFRESH: Final[int] = 48

#: Duración por defecto de la sesión, en minutos.
MINUTOS_SESION: Final[int] = 480

#: Duración por defecto del refresh token, en días.
DIAS_REFRESH: Final[int] = 14


class ErrorDeToken(Exception):
    """El token presentado no es válido, ha expirado o ha sido revocado."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


@dataclass(frozen=True)
class ConfiguracionToken:
    """Parámetros de emisión y verificación de tokens."""

    algoritmo: str
    clave: str
    minutos_sesion: int
    dias_refresh: int
    issuer: str


def configuracion_token() -> ConfiguracionToken:
    """Devuelve la configuración de tokens leída de la aplicación."""
    return ConfiguracionToken(
        algoritmo=settings.jwt_algorithm,
        clave=settings.secret_key,
        minutos_sesion=settings.jwt_expiration_minutes,
        dias_refresh=DIAS_REFRESH,
        issuer="projecta-pmo-intelligence",
    )


# ─────────────────────────── contraseñas ───────────────────────────


def crear_hash_contrasena(contrasena: str) -> str:
    """Devuelve el hash bcrypt de una contraseña.

    La contraseña se limita a 72 bytes porque bcrypt trunca la entrada a ese
    tamaño: aceptarla más larga daría la falsa sensación de que los
    caracteres sobrantes protegen.
    """
    if not contrasena:
        raise ValueError("la contraseña no puede estar vacía")
    datos = contrasena.encode("utf-8")[:72]
    return bcrypt.hashpw(datos, bcrypt.gensalt(rounds=RONDAS_BCRYPT)).decode("ascii")


def verificar_contrasena(contrasena: str, hash_almacenado: str) -> bool:
    """Comprueba una contraseña contra su hash bcrypt.

    Devuelve ``False`` en lugar de propagar errores: un hash corrupto en
    base de datos no debe tumbar la autenticación, solo rechazarla.
    """
    if not contrasena or not hash_almacenado:
        return False
    try:
        return bcrypt.checkpw(
            contrasena.encode("utf-8")[:72], hash_almacenado.encode("ascii")
        )
    except (ValueError, TypeError):
        return False


# ─────────────────────────── token de sesión ───────────────────────────


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def a_utc(momento: datetime) -> datetime:
    """Normaliza un instante a UTC con zona horaria explícita.

    No todas las bases de datos devuelven la zona horaria: SQLite, por
    ejemplo, la pierde. Comparar un instante sin zona con otro que la
    tiene lanza ``TypeError``, así que toda comparación pasa por aquí.
    """
    if momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc)


def generar_token_sesion(
    *,
    usuario_id: str,
    organizacion_id: str,
    rol: str,
    nivel_rol: int,
    sesion_id: str,
    expires_in: int | None = None,
) -> tuple[str, datetime]:
    """Emite un token de sesión JWT.

    Devuelve el token y su fecha de expiración. Los *claims* son los que
    fija el TO-BE: ``sub``, ``iat``, ``exp``, ``rol`` y ``organizacion_id``,
    más ``nivel`` y ``sid`` (identificador de sesión) que son necesarios
    para revocar del lado del servidor.
    """
    cfg = configuracion_token()
    expira = _ahora() + timedelta(minutes=expires_in or cfg.minutos_sesion)
    payload: dict[str, Any] = {
        "sub": usuario_id,
        "iat": int(_ahora().timestamp()),
        "exp": int(expira.timestamp()),
        "rol": rol,
        "nivel": nivel_rol,
        "organizacion_id": organizacion_id,
        "sid": sesion_id,
        "iss": cfg.issuer,
    }
    token = jwt.encode(payload, cfg.clave, algorithm=cfg.algoritmo)
    return token, expira


def verificar_token_sesion(token: str) -> dict[str, Any]:
    """Verifica la firma y la expiración de un token de sesión.

    Lanza :class:`ErrorDeToken` si el token no es válido. El motivo
    distingue entre los casos porque el registro de auditoría los separa:
    un token expirado es normal, un token con firma inválida no.
    """
    cfg = configuracion_token()
    try:
        return jwt.decode(
            token,
            cfg.clave,
            algorithms=[cfg.algoritmo],
            issuer=cfg.issuer,
            options={"require": ["sub", "iat", "exp", "rol", "organizacion_id", "sid"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ErrorDeToken("el token de sesión ha expirado") from exc
    except jwt.InvalidTokenError as exc:
        raise ErrorDeToken("el token de sesión no es válido") from exc


# ─────────────────────────── refresh rotativo ───────────────────────────


def generar_refresh_token() -> tuple[str, str]:
    """Emite un refresh token y su hash.

    Devuelve ``(token_en_claro, hash)``. El token en claro se entrega al
    cliente una única vez y solo se guarda su hash, de modo que una copia
    de la base de datos no permite suplantar a nadie.
    """
    material = secrets.token_urlsafe(BYTES_REFRESH)
    return material, hash_de_refresh(material)


def hash_de_refresh(token: str) -> str:
    """Deriva el hash SHA-256 de un refresh token.

    El refresh token es material aleatorio de alta entropía, así que un
    SHA-256 sin sal es suficiente y permite indexar la columna.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def nuevo_identificador() -> str:
    """Genera un identificador de sesión o de familia de refresh."""
    return uuid.uuid4().hex


def comparar_refresh(token: str, hash_almacenado: str) -> bool:
    """Compara un refresh token con su hash almacenado, en tiempo constante."""
    if not token or not hash_almacenado:
        return False
    return hmac.compare_digest(hash_de_refresh(token), hash_almacenado)
