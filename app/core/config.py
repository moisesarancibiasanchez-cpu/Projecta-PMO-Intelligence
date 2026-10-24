"""Configuración de Projecta PMO Intelligence.

Fuente única de verdad para los parámetros de la aplicación. Los valores se
leen del entorno y, si existe, del archivo ``.env`` de la raíz del proyecto.

Reglas del contrato técnico:

* ``SECRET_KEY``, ``DATABASE_URL``, ``MINIO_ACCESS_KEY`` y ``MINIO_SECRET_KEY``
  son obligatorias. Si falta alguna (o llega vacía) el proceso **falla al
  arrancar** con un mensaje que **nombra la variable** que falta.
* ``SECRET_KEY`` nunca tiene valor por defecto.
* ``BOOTSTRAP_ADMIN_PASSWORD`` no existe en este proyecto.
* ``DATABASE_URL`` usa siempre un driver asíncrono
  (``postgresql+asyncpg`` o ``sqlite+aiosqlite``). La propiedad
  :attr:`Settings.database_url_sync` traduce el esquema a uno síncrono
  (``postgresql+psycopg``) para Alembic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from dotenv import load_dotenv
from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError
from pydantic_core import ValidationError

#: Versión de la aplicación que se muestra en la interfaz y en el OpenAPI.
APP_VERSION: Final[str] = "0.1.0"

#: Raíz del repositorio (tres niveles arriba: app/core/config.py).
RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parents[2]

#: Traducción entre el nombre del campo de pydantic y el nombre de la variable
#: de entorno. Se usa para que los errores de arranque nombren la variable
#: de entorno exacta y no el nombre interno del campo.
VARIABLES_OBLIGATORIAS: Final[dict[str, str]] = {
    "secret_key": "SECRET_KEY",
    "database_url": "DATABASE_URL",
    "minio_access_key": "MINIO_ACCESS_KEY",
    "minio_secret_key": "MINIO_SECRET_KEY",
}

#: Ayuda en texto claro para cada variable obligatoria.
AYUDA_VARIABLES: Final[dict[str, str]] = {
    "SECRET_KEY": "clave de firma de la aplicación (genérala con `openssl rand -hex 32`)",
    "DATABASE_URL": (
        "URL de la base de datos con driver asíncrono "
        "(postgresql+asyncpg://usuario:clave@servidor/bd o sqlite+aiosqlite:///ruta.db)"
    ),
    "MINIO_ACCESS_KEY": "clave de acceso al almacenamiento de objetos MinIO",
    "MINIO_SECRET_KEY": "clave secreta del almacenamiento de objetos MinIO",
}

#: Drivers asíncronos admitidos en ``DATABASE_URL``.
DRIVERS_ASINCRONOS_ACEPTADOS: Final[tuple[str, ...]] = (
    "postgresql+asyncpg",
    "sqlite+aiosqlite",
)

#: Traducción de esquema asíncrono -> síncrono (para Alembic).
TRADUCCION_ESQUEMAS: Final[dict[str, str]] = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "postgres+asyncpg": "postgresql+psycopg",
    "postgresql": "postgresql+psycopg",
    "postgres": "postgresql+psycopg",
    "sqlite+aiosqlite": "sqlite",
}

#: Traducción **inversa**: de un driver síncrono al asíncrono equivalente.
#: Railway entrega la URL de PostgreSQL con el driver síncrono por
#: defecto, y la aplicación necesita el asíncrono.
TRADUCCION_ESQUEMAS_INVERSAS: Final[dict[str, str]] = {
    "postgresql": "postgresql+asyncpg",
    "postgres": "postgresql+asyncpg",
    "postgresql+psycopg": "postgresql+asyncpg",
    "postgresql+psycopg2": "postgresql+asyncpg",
    "postgresql+pg8000": "postgresql+asyncpg",
}


def _redis_con_base(url: str, base: int) -> str:
    """Devuelve la URL de Redis con otra base de datos.

    >>> _redis_con_base("redis://h:6379/0", 1)
    'redis://h:6379/1'
    >>> _redis_con_base("redis://h:6379", 2)
    'redis://h:6379/2'
    """
    esquema, separador, resto = url.partition("://")
    if not separador:
        return url
    ruta, _, _consulta = resto.partition("?")
    base_actual, slash, _ = ruta.rpartition("/")
    if not slash:
        # Sin base de datos explícita: se añade.
        return f"{esquema}://{ruta}/{base}"
    return f"{esquema}://{base_actual}/{base}"


class ConfiguracionInvalidaError(RuntimeError):
    """Error de arranque: falta una variable de entorno obligatoria o es inválida."""


def nombre_variable(campo: str) -> str:
    """Devuelve el nombre de la variable de entorno que corresponde a un campo."""
    return VARIABLES_OBLIGATORIAS.get(campo, campo.upper())


def enmascarar_url(url: str) -> str:
    """Oculta las credenciales de una URL para poder registrarla en el log."""
    if not url:
        return ""
    esquema, _, resto = url.partition("://")
    if not resto or "@" not in resto:
        return url
    credenciales, _, servidor = resto.rpartition("@")
    if ":" not in credenciales:
        return url
    usuario, _, _ = credenciales.partition(":")
    return f"{esquema}://{usuario}:***@{servidor}"


def _cargar_env_local() -> None:
    """Carga el ``.env`` de la raíz del proyecto si existe.

    No sobrescribe variables ya presentes en el entorno: las variables reales
    del proceso mandan siempre sobre el archivo. Es una comodidad para poder
    arrancar la aplicación desde cualquier directorio de trabajo.
    """
    archivo_env = RAIZ_PROYECTO / ".env"
    if not archivo_env.is_file():
        return
    try:
        load_dotenv(dotenv_path=archivo_env, override=False, encoding="utf-8")
    except OSError:  # pragma: no cover - el archivo .env es opcional
        return


def _normalizar_cors_origins() -> None:
    """Normaliza ``CORS_ORIGINS`` a una lista JSON.

    Acepta tres formatos habituales: lista JSON (``["http://a"]``), lista
    separada por comas (``http://a,http://b``) y valor vacío.
    """
    valor = os.environ.get("CORS_ORIGINS")
    if valor is None:
        return
    valor = valor.strip()
    if not valor:
        os.environ["CORS_ORIGINS"] = "[]"
        return
    if valor.startswith("["):
        return
    origenes = [origen.strip() for origen in valor.split(",") if origen.strip()]
    os.environ["CORS_ORIGINS"] = json.dumps(origenes)


def _mensaje_configuracion_invalida(error: ValidationError) -> str:
    """Construye el mensaje de arranque con las variables que faltan."""
    lineas: list[str] = []
    for detalle in error.errors():
        campo = str(detalle.get("loc", ("",))[0]) if detalle.get("loc") else ""
        variable = nombre_variable(campo)
        tipo = str(detalle.get("type", ""))
        if tipo == "missing":
            estado = "no está definida"
        elif tipo == "value_error":
            estado = "está vacía"
        else:
            estado = "tiene un valor inválido"
        if variable not in {linea.split(":", 1)[0] for linea in lineas}:
            lineas.append(f"  - {variable}: {estado}.")

    ayuda = [
        f"    {variable}: {AYUDA_VARIABLES[variable]}"
        for variable in VARIABLES_OBLIGATORIAS.values()
        if any(linea.strip().startswith(f"- {variable}:") for linea in lineas)
    ]

    mensaje = [
        "Configuración inválida de Projecta PMO Intelligence.",
        "La aplicación no puede arrancar porque faltan variables de entorno obligatorias "
        "o tienen un valor vacío:",
        "",
        *lineas,
    ]
    if ayuda:
        mensaje += ["", "Detalle de cada variable:"] + ayuda
    mensaje += [
        "",
        "Definí esas variables en el entorno o en el archivo `.env` de la raíz del proyecto "
        f"({RAIZ_PROYECTO / '.env'}).",
    ]
    return "\n".join(mensaje)


class Settings(BaseSettings):
    """Parámetros de configuración de la aplicación."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Identidad de la aplicación -------------------------------------
    app_name: str = "Projecta PMO Intelligence"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    # --- Obligatorias (sin valor por defecto) ---------------------------
    secret_key: str
    database_url: str

    # --- Almacenamiento de objetos (MinIO) -------------------------------
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str = "projecta-evidencia"

    # --- Redis y Celery ---------------------------------------------------
    # `redis_url` es el Redis **de la aplicación**. El broker y el
    # backend de resultados se pueden fijar explícitamente (despliegue
    # nativo, con dos Redis) o se **derivan** de este (Railway, que solo
    # ofrece uno). Vacío significa «derivar».
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    # --- Autenticación ----------------------------------------------------
    jwt_algorithm: str = "HS256"
    jwt_expiration_minutes: int = 480

    # --- HTTP -------------------------------------------------------------
    cors_origins: list[str] = []

    # --- Edge Connector: integración con Microsoft Graph -------------------
    #
    # En la Variante A (todo dentro del perímetro) estos parámetros son
    # los de la **propia** aplicación de Projecta PMO Intelligence: no hay
    # una cuenta de servicio nueva que pedir, sino la que la aplicación
    # ya tiene aprobada. Por eso la lectura de Teams no abre ningún
    # expediente de seguridad nuevo.
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    #: Ruta del certificado X.509 en PEM, para la aserción de cliente.
    graph_certificado: str = ""
    #: Ruta de la clave privada en PEM. La lee el vault al arrancar.
    graph_clave_privada: str = ""
    #: Canal de Teams habilitado por la política local. Vacío = ninguno.
    teams_canal_permitido: str = ""
    #: Identificador del proyecto al que se asocia lo leído de Teams.
    teams_proyecto_id: str = ""
    #: Directorio de estado local: cursores delta y clave del dispositivo.
    edge_estado: str = "/var/lib/projecta-edge"
    #: Huella de la clave pública del dispositivo del Edge Connector.
    edge_huella_clave: str = ""
    #: Ruta de la clave Ed25519 del dispositivo.
    edge_clave_dispositivo: str = ""

    # --- Captura de reuniones de Teams ---------------------------------------
    #
    # Base de la API de Graph. Configurable para poder apuntar a un doble
    # de prueba durante las pruebas funcionales, sin tocar el código.
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    #: UPN o id del organizador cuyas reuniones se capturan. En la práctica
    #: es la cuenta de servicio de la aplicación.
    teams_organizador: str = ""
    #: Cuántas horas hacia atrás mira la captura en cada ejecución. El
    #: valor tiene que ser **mayor** que el intervalo entre ejecuciones,
    #: o quedarían reuniones en el hueco.
    teams_captura_ventana_horas: int = 24
    #: Si se descarga el audio de la reunión además de la transcripción.
    teams_captura_con_audio: bool = True
    #: Dónde se deja el audio cuando el almacenamiento de objetos no está
    #: disponible. Es el respaldo, no el camino normal.
    teams_directorio_capturas: str = "/var/lib/projecta-capturas"

    # --- Projecta Capture: transcripción -----------------------------------
    #
    #: URL del motor de transcripción. **Vacía = proveedor simulado**, que
    #: es lo que permite que el MVP funcione entero sin credenciales y sin
    #: enviar audio a ningún sitio. No hay un valor por defecto que apunte
    #: a un servicio externo: eso lo decide la configuración, no el
    #: código.
    captura_motor_transcripcion_url: str = ""
    #: Token del motor de transcripción, si lo pide.
    captura_motor_transcripcion_token: str = ""

    def __init__(self, **datos: Any) -> None:
        """Construye la configuración lanzando un error claro si algo falta."""
        try:
            super().__init__(**datos)
        except ValidationError as error:
            raise ConfiguracionInvalidaError(_mensaje_configuracion_invalida(error)) from error
        except SettingsError as error:
            raise ConfiguracionInvalidaError(
                "Configuración inválida de Projecta PMO Intelligence.\n"
                "Alguna variable de entorno tiene un valor que no se puede interpretar. "
                f"Detalle: {error}"
            ) from error

    # --- Validaciones -----------------------------------------------------

    @field_validator("secret_key", "database_url", "minio_access_key", "minio_secret_key")
    @classmethod
    def _valor_obligatorio(cls, valor: str, info: ValidationInfo) -> str:
        """Rechaza valores obligatorios vacíos o solo con espacios."""
        if not valor or not valor.strip():
            raise ValueError(
                f"la variable de entorno {nombre_variable(info.field_name)} está vacía"
            )
        return valor

    @field_validator("database_url")
    @classmethod
    def _driver_asincrono(cls, valor: str) -> str:
        """Normaliza la URL de la base de datos a un driver asíncrono.

        En el despliegue nativo la URL viene ya con driver asíncrono. En
        **Railway** viene con el driver síncrono por defecto
        (`postgresql://`), porque es lo que genera su plugin de
        PostgreSQL. Rechazarla dejaba la aplicación sin arrancar; lo
        correcto es **traducirla**, porque el esquema y el resto de la
        URL son los mismos.

        Un esquema **desconocido** sigue siendo un error: no se intenta
        adivinar.
        """
        esquema, separador, resto = valor.partition("://")
        esquema_normalizado = esquema.lower()
        if not separador:
            raise ValueError(
                "la variable de entorno DATABASE_URL no tiene el formato "
                "«esquema://...»"
            )
        if esquema_normalizado in DRIVERS_ASINCRONOS_ACEPTADOS:
            return valor
        destino = TRADUCCION_ESQUEMAS_INVERSAS.get(esquema_normalizado)
        if destino is None:
            admitidos = ", ".join(sorted(DRIVERS_ASINCRONOS_ACEPTADOS))
            raise ValueError(
                "la variable de entorno DATABASE_URL debe usar un driver "
                f"asíncrono ({admitidos}) o uno síncrono traducible "
                f"({', '.join(sorted(TRADUCCION_ESQUEMAS_INVERSAS))}); "
                f"se recibió el esquema '{esquema_normalizado}'"
            )
        return f"{destino}://{resto}"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _cors_origins_como_lista(cls, valor: Any) -> Any:
        """Acepta lista JSON, texto separado por comas o valor vacío."""
        if valor is None or isinstance(valor, list):
            return valor or []
        if isinstance(valor, str):
            texto = valor.strip()
            if not texto:
                return []
            if texto.startswith("["):
                return json.loads(texto)
            return [origen.strip() for origen in texto.split(",") if origen.strip()]
        return valor

    @model_validator(mode="after")
    def _deriva_celery_de_redis(self) -> "Settings":
        """Deriva el broker y el backend de Celery si no vienen fijados.

        Railway ofrece un único Redis con la variable ``REDIS_URL``. Sin
        esta derivación, el broker y el backend apuntarían a su valor
        por defecto —``localhost``— y en un contenedor de Railway no hay
        nadie escuchando: la tarea se encolaba y se perdía.

        Si vienen fijados, se respetan. El despliegue nativo los fija, y
        eso no se toca.
        """
        if not self.celery_broker_url and self.redis_url:
            self.celery_broker_url = _redis_con_base(self.redis_url, 1)
        if not self.celery_result_backend and self.redis_url:
            self.celery_result_backend = _redis_con_base(self.redis_url, 2)
        return self

    # --- Propiedades derivadas --------------------------------------------

    @property
    def database_url_sync(self) -> str:
        """URL de la base de datos con driver síncrono, lista para Alembic.

        Traduce ``postgresql+asyncpg://`` a ``postgresql+psycopg://`` y deja
        intactos los esquemas SQLite.
        """
        esquema, separador, resto = self.database_url.partition("://")
        esquema_normalizado = esquema.lower()
        destino = TRADUCCION_ESQUEMAS.get(esquema_normalizado)
        if destino is None:
            return self.database_url
        if not separador:
            return self.database_url
        return f"{destino}://{resto}"

    @property
    def version(self) -> str:
        """Versión de la aplicación."""
        return APP_VERSION

    @property
    def database_url_enmascarada(self) -> str:
        """URL de la base de datos sin credenciales, apta para logs."""
        return enmascarar_url(self.database_url)

    @property
    def redis_url_enmascarada(self) -> str:
        """URL de Redis sin credenciales, apta para logs."""
        return enmascarar_url(self.redis_url)

    @property
    def es_produccion(self) -> bool:
        """``True`` si la aplicación corre en el entorno de producción.

        En producción se activan los comportamientos que no son seguros en
        desarrollo: cookies ``Secure``, rechazo del inicio de sesión por
        contraseña y exigencia de proveedor OIDC configurado.
        """
        return self.app_env.strip().lower() in {
            "prod",
            "production",
            "produccion",
        }


# La configuración se resuelve al importar el módulo: si falta una variable
# obligatoria el proceso muere aquí, antes de levantar la aplicación.
_cargar_env_local()
_normalizar_cors_origins()

settings = Settings()
