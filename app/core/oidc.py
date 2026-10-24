"""Cliente OIDC de Projecta PMO Intelligence.

Implementa la parte de HU_04 que corresponde a la identidad corporativa:
descubrimiento del proveedor, construcción de la URL de autorización,
canje del código por tokens y **validación criptográfica** del token de
identidad.

Dos decisiones que importan:

* **El token de identidad se valida de verdad.** Se comprueba la firma
  contra las claves JWKS del proveedor, y también ``iss``, ``aud``,
  ``exp`` y ``nonce``. Aceptar un ``id_token`` sin validar la firma sería
  aceptar una identidad que cualquiera pueda fabricate.
* **El estado y el nonce se comprueban.** Son la protección contra CSRF en
  el flujo de código de autorización y contra la reutilización de un
  código por otro cliente.

Cuando no hay proveedor configurado, el módulo entra en **modo
desarrollo**, que solo sirve para las pruebas locales. Ese modo se niega
explícitamente en producción.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import jwt

__all__ = [
    "ConfiguracionOIDC",
    "ErrorDeOIDC",
    "ProveedorOIDC",
    "configuracion_oidc",
    "descubrir",
]

LOG = logging.getLogger("projecta.oidc")

#: Ámbito de_scope_ solicitado por defecto: identidad básica y correo.
SCOPOS_POR_DEFECTO: Final[str] = "openid profile email"


class ErrorDeOIDC(Exception):
    """El proveedor de identidad ha rechazado la operación o ha fallado."""


@dataclass(frozen=True)
class ConfiguracionOIDC:
    """Parámetros del proveedor de identidad."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str = SCOPOS_POR_DEFECTO
    #: Si es ``False``, el proveedor no se usa y la autenticación local
    #: por contraseña queda habilitada. En producción tiene que ser
    #: ``True``: sin proveedor, la identidad no es corporativa.
    habilitado: bool = True
    #: Extrae la organización del correo: ``usuario@banco.com`` ->
    #: ``banco.com``. Es el criterio por defecto cuando el proveedor no
    #: entrega una organization claim.
    dominio_organizacion: str | None = None


def configuracion_oidc() -> ConfiguracionOIDC:
    """Lee la configuración OIDC del entorno."""
    import os

    issuer = os.environ.get("OIDC_ISSUER", "").strip()
    client_id = os.environ.get("OIDC_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("OIDC_REDIRECT_URI", "").strip()
    return ConfiguracionOIDC(
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=os.environ.get("OIDC_SCOPES", SCOPOS_POR_DEFECTO),
        habilitado=bool(issuer and client_id and redirect_uri),
        dominio_organizacion=os.environ.get("OIDC_DOMINIO_ORGANIZACION") or None,
    )


@dataclass
class ProveedorOIDC:
    """Cliente de un proveedor OpenID Connect."""

    configuracion: ConfiguracionOIDC
    cliente: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)
    _documento: dict[str, Any] | None = None
    _jwks: Any = None
    _jwks_cargado_en: float = 0.0

    #: Las claves del proveedor se refrescan cada hora, salvo que el propio
    #: proveedor indique otra cosa.
    CADUCIDAD_JWKS: Final[float] = 3600.0

    async def documento(self) -> dict[str, Any]:
        """Devuelve el documento de descubrimiento, con caché en memoria."""
        if self._documento is not None:
            return self._documento
        url = self.configuracion.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            respuesta = await self.cliente.get(url, timeout=10.0)
            respuesta.raise_for_status()
            self._documento = respuesta.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ErrorDeOIDC(
                f"no se puede descubrir el proveedor OIDC en {url}: {exc}"
            ) from exc
        return self._documento

    async def url_autorizacion(self, *, estado: str, nonce: str) -> str:
        """Construye la URL a la que se redirige al usuario para autenticarse."""
        documento = await self.documento()
        endpoint = documento.get("authorization_endpoint")
        if not endpoint:
            raise ErrorDeOIDC("el proveedor no declara authorization_endpoint")
        from urllib.parse import urlencode

        parametros = {
            "client_id": self.configuracion.client_id,
            "redirect_uri": self.configuracion.redirect_uri,
            "response_type": "code",
            "scope": self.configuracion.scopes,
            "state": estado,
            "nonce": nonce,
        }
        return endpoint + "?" + urlencode(parametros)

    async def canjear_codigo(self, codigo: str) -> dict[str, Any]:
        """Canjea el código de autorización por los tokens del proveedor."""
        documento = await self.documento()
        endpoint = documento.get("token_endpoint")
        if not endpoint:
            raise ErrorDeOIDC("el proveedor no declara token_endpoint")
        datos = {
            "grant_type": "authorization_code",
            "code": codigo,
            "redirect_uri": self.configuracion.redirect_uri,
            "client_id": self.configuracion.client_id,
            "client_secret": self.configuracion.client_secret,
        }
        try:
            respuesta = await self.cliente.post(endpoint, data=datos, timeout=15.0)
            respuesta.raise_for_status()
            return respuesta.json()
        except httpx.HTTPStatusError as exc:
            raise ErrorDeOIDC(
                f"el proveedor rechazó el canje del código: {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ErrorDeOIDC(f"fallo de red al canjear el código: {exc}") from exc

    async def _claves(self) -> Any:
        """Devuelve el conjunto de claves del proveedor, con caché."""
        ahora = time.monotonic()
        if self._jwks is not None and ahora - self._jwks_cargado_en < self.CADUCIDAD_JWKS:
            return self._jwks
        documento = await self.documento()
        endpoint = documento.get("jwks_uri")
        if not endpoint:
            raise ErrorDeOIDC("el proveedor no declara jwks_uri")
        try:
            respuesta = await self.cliente.get(endpoint, timeout=15.0)
            respuesta.raise_for_status()
            self._jwks = jwt.PyJWKClient(endpoint)
            self._jwks_cargado_en = ahora
        except (httpx.HTTPError, ValueError) as exc:
            raise ErrorDeOIDC(f"no se pueden obtener las claves del proveedor: {exc}") from exc
        return self._jwks

    async def validar_token_identidad(
        self, token_identidad: str, *, nonce_esperado: str | None = None
    ) -> dict[str, Any]:
        """Valida el token de identidad y devuelve sus *claims*.

        Comprueba la firma contra las claves del proveedor, y además
        ``iss``, ``aud``, ``exp`` y —si se conoce— ``nonce``.
        """
        documento = await self.documento()
        algoritmo = documento.get("id_token_signing_alg_values_supported", ["RS256"])
        algoritmo_principal = algoritmo[0] if algoritmo else "RS256"
        claves = await self._claves()
        try:
            clave = claves.get_signing_key_from_jwt(token_identidad)
            claims = jwt.decode(
                token_identidad,
                clave.key,
                algorithms=[algoritmo_principal],
                audience=self.configuracion.client_id,
                issuer=documento.get("issuer", self.configuracion.issuer),
                options={"require": ["sub", "iss", "aud", "exp"]},
            )
        except jwt.PyJWTError as exc:
            raise ErrorDeOIDC(f"el token de identidad no es válido: {exc}") from exc
        if nonce_esperado is not None and claims.get("nonce") != nonce_esperado:
            raise ErrorDeOIDC("el nonce del token de identidad no coincide")
        return claims


async def descubrir(configuracion: ConfiguracionOIDC) -> dict[str, Any]:
    """Descubre el documento del proveedor con una configuración dada."""
    if not configuracion.habilitado:
        raise ErrorDeOIDC("no hay proveedor OIDC configurado")
    proveedor = ProveedorOIDC(configuracion)
    try:
        return await proveedor.documento()
    finally:
        await proveedor.cliente.aclose()


def nuevo_estado() -> str:
    """Genera un valor de ``state`` para proteger el flujo contra CSRF."""
    return secrets.token_urlsafe(32)


def nuevo_nonce() -> str:
    """Genera un ``nonce`` para vincular la respuesta con la petición."""
    return secrets.token_urlsafe(32)
