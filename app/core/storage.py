"""Almacenamiento de objetos de Projecta PMO Intelligence (HU_08).

Encapsula el bucket compatible con S3 (MinIO) con cuatro garantías:

* **Versionado activo.** El bucket se versiona, de modo que una carga
  sobrescrita no destruye la versión anterior.
* **Idempotencia por contenido.** La clave del objeto se deriva del
  SHA-256 del fichero. Cargar dos veces el mismo contenido **no crea dos
  objetos**: el segundo registro apunta al mismo checksum. Es la misma idea
  que usa la ingesta documental del TO-BE, y por eso se comparte la
  función de hash.
* **El metadato sobrevive al objeto.** Si el objeto se elimina del bucket,
  el adjunto se marca como no disponible en lugar de desaparecer: el
  listado no se rompe y la auditoría conserva la traza.
* **Unavailable no es un error de la aplicación.** Si el almacenamiento no
  responde, la API sigue viva: el almacenamiento no es una dependencia
  crítica del servicio, como fija el contrato técnico (la comprobación
  ``storage`` de ``/ready`` no es crítica).
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from typing import Any, BinaryIO, Final

from app.core.config import settings

__all__ = [
    "ErrorDeAlmacenamiento",
    "ObjetoNoDisponible",
    "ResultadoCarga",
    "almacenamiento",
    "calcular_sha256",
    "clave_para_contenido",
]

#: Tamaño máximo de un adjunto, en bytes (50 MiB).
TAMANO_MAXIMO: Final[int] = 50 * 1024 * 1024


class ErrorDeAlmacenamiento(RuntimeError):
    """El almacenamiento de objetos no está disponible o rechaza la operación."""


class ObjetoNoDisponible(Exception):
    """El objeto no existe en el bucket, pero su metadato se conserva."""


@dataclass(frozen=True)
class ResultadoCarga:
    """Resultado de cargar un fichero en el bucket."""

    sha256: str
    clave_objeto: str
    version: str | None
    tamano_bytes: int
    #: ``True`` si el objeto ya existía y no se ha vuelto a subir.
    ya_existia: bool
    nombre: str
    content_type: str | None = None


def calcular_sha256(datos: bytes) -> str:
    """Devuelve el SHA-256 en hexadecimal de un contenido."""
    return hashlib.sha256(datos).hexdigest()


def clave_para_contenido(sha256: str, extension: str = "") -> str:
    """Deriva la clave del objeto a partir de su contenido.

    La clave **no** incluye el nombre del fichero: el contenido es lo que
    identifica al objeto, y así dos ficheros con el mismo contenido y
    nombres distintos comparten objeto.
    """
    sufijo = f".{extension.lstrip('.')}" if extension else ""
    # Se reparte en subdirectorios para no crear un directorio con un
    # millón de entradas, que es lo que degrada el listado del bucket.
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}{sufijo}"


class Almacenamiento:
    """Cliente del bucket de objetos, con las garantías de HU_08."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
    ) -> None:
        self.endpoint = (endpoint or settings.minio_endpoint).rstrip("/")
        self.access_key = access_key or settings.minio_access_key
        self.secret_key = secret_key or settings.minio_secret_key
        self.bucket = bucket or settings.minio_bucket
        self._cliente: Any = None

    # ── cliente ──────────────────────────────────────────────────────
    @property
    def cliente(self) -> Any:
        """Devuelve el cliente MinIO, creándolo la primera vez."""
        if self._cliente is None:
            try:
                from minio import Minio
            except ImportError as exc:  # pragma: no cover - dependencia
                raise ErrorDeAlmacenamiento(
                    "la biblioteca 'minio' no está instalada: el almacenamiento de "
                    "objetos no está disponible"
                ) from exc
            self._cliente = Minio(
                self.endpoint.replace("http://", "").replace("https://", ""),
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.endpoint.startswith("https://"),
            )
        return self._cliente

    def disponible(self) -> bool:
        """Indica si el bucket responde. No lanza excepciones."""
        try:
            return bool(self.cliente.bucket_exists(self.bucket))
        except Exception:
            return False

    def asegurar_bucket(self) -> None:
        """Crea el bucket si no existe y activa su versionado."""
        try:
            cliente = self.cliente
            if not cliente.bucket_exists(self.bucket):
                cliente.make_bucket(self.bucket)
            cliente.set_bucket_versioning(self.bucket, {"Status": "Enabled"})
        except Exception as exc:
            raise ErrorDeAlmacenamiento(
                f"no se puede preparar el bucket '{self.bucket}': {exc}"
            ) from exc

    # ── operaciones ──────────────────────────────────────────────────
    def subir(
        self,
        datos: bytes,
        *,
        nombre: str,
        content_type: str | None = None,
        extension: str = "",
    ) -> ResultadoCarga:
        """Sube un fichero, o lo reutiliza si su contenido ya está.

        La idempotencia la da la clave derivada del SHA-256: si el objeto
        ya existe, no se vuelve a subir y se devuelve la marca de
        ``ya_existia``.
        """
        if len(datos) > TAMANO_MAXIMO:
            raise ErrorDeAlmacenamiento(
                f"el fichero supera el máximo de {TAMANO_MAXIMO} bytes"
            )
        huella = calcular_sha256(datos)
        clave = clave_para_contenido(huella, extension)
        try:
            cliente = self.cliente
            if cliente.stat_object(self.bucket, clave):
                return ResultadoCarga(
                    sha256=huella,
                    clave_objeto=clave,
                    version=None,
                    tamano_bytes=len(datos),
                    ya_existia=True,
                    nombre=nombre,
                    content_type=content_type,
                )
            resultado = cliente.put_object(
                self.bucket,
                clave,
                io.BytesIO(datos),
                length=len(datos),
                content_type=content_type or "application/octet-stream",
            )
            return ResultadoCarga(
                sha256=huella,
                clave_objeto=clave,
                version=getattr(resultado, "version_id", None),
                tamano_bytes=len(datos),
                ya_existia=False,
                nombre=nombre,
                content_type=content_type,
            )
        except ErrorDeAlmacenamiento:
            raise
        except Exception as exc:
            raise ErrorDeAlmacenamiento(
                f"no se puede guardar el fichero en el bucket '{self.bucket}': {exc}"
            ) from exc

    def leer(self, clave_objeto: str) -> bytes:
        """Lee el contenido de un objeto. Lanza si no está."""
        try:
            respuesta = self.cliente.get_object(self.bucket, clave_objeto)
            try:
                return respuesta.read()
            finally:
                respuesta.close()
                respuesta.release_conn()
        except Exception as exc:
            raise ObjetoNoDisponible(
                f"el objeto '{clave_objeto}' no está disponible en el bucket"
            ) from exc

    def existe(self, clave_objeto: str) -> bool:
        """Indica si un objeto está en el bucket."""
        try:
            self.cliente.stat_object(self.bucket, clave_objeto)
            return True
        except Exception:
            return False

    def eliminar(self, clave_objeto: str) -> None:
        """Elimina un objeto. No lanza si ya no estaba."""
        try:
            self.cliente.remove_object(self.bucket, clave_objeto)
        except Exception:
            return

    def url_presignada(self, clave_objeto: str, *, segundos: int = 900) -> str | None:
        """Devuelve una URL temporal de descarga, o ``None`` si no se puede."""
        try:
            return self.cliente.presigned_get_object(
                self.bucket, clave_objeto, expires=timedelta_segundos(segundos)
            )
        except Exception:
            return None


def timedelta_segundos(segundos: int) -> Any:
    """Construye un ``timedelta`` sin importar el módulo en la cabecera."""
    from datetime import timedelta

    return timedelta(seconds=segundos)


#: Cliente único del proceso. Se construye de forma perezosa para que la
#: aplicación arranque aunque el almacenamiento no esté disponible.
_almacenamiento: Almacenamiento | None = None


def almacenamiento() -> Almacenamiento:
    """Devuelve el cliente de almacenamiento compartido."""
    global _almacenamiento
    if _almacenamiento is None:
        _almacenamiento = Almacenamiento()
    return _almacenamiento
