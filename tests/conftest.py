"""Aislamiento de la suite de pruebas del esqueleto de **Projecta PMO Intelligence**.

Este módulo se importa **antes** que cualquier módulo de ``app.*``. Por eso fija las
variables de entorno aquí, en el nivel superior: la clase ``Settings`` valida las
variables obligatorias (``SECRET_KEY``, ``DATABASE_URL``, ``MINIO_ACCESS_KEY``,
``MINIO_SECRET_KEY``) en el momento de importarse, y el motor asíncrono se crea
en el momento de importarse ``app.core.db.session``.

Dos aislamientos, en concreto:

1. **Base de datos**: SQLite en memoria (``sqlite+aiosqlite:///:memory:``). Cada
   prueba corre contra una base aislada y sin tocar el PostgreSQL real del
   entorno.
2. **Broker de Celery**: se desactivan las conexiones y se fuerzan las tareas en
   modo *eager*, de modo que ninguna prueba intenta hablar con el Redis real ni
   dejar mensajes encolados.

Ninguna de las dos cosas se hace con un parcheo posterior: se resuelve en el
momento de importar, que es cuando el motor y los ajustes se construyen.
"""

from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import AsyncIterator, Iterator

import pytest

# ---------------------------------------------------------------------------
# 0. Purga de la caché de bytecode.
# ---------------------------------------------------------------------------
#
# El sistema de ficheros del entorno de validación no actualiza la marca
# de tiempo de un fichero cuando su contenido cambia sin que cambie su
# tamaño. Python valida su caché de bytecode comparando la pareja
# ``(mtime, tamaño)``, de modo que después de editar un módulo el
# intérprete puede seguir ejecutando **la versión anterior** del código.
#
# Para una suite de pruebas eso es peor que un fallo: es un falso
# positivo. La prueba pasa porque se está midiendo código que ya no
# existe. Por eso la caché se purga aquí, en el punto más temprano
# posible —antes de importar nada de `app.*`—, y no en un script
# externo que alguien podría no ejecutar.
#
# El detalle completo del problema y su reproducción está en
# `scripts/purgar_cache.py`.

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from scripts.purgar_cache import purgar  # noqa: E402  (necesario antes de los imports)

purgar(_RAIZ)

# ---------------------------------------------------------------------------
# 1. Variables de entorno: deben fijarse ANTES de importar `app.*`.
# ---------------------------------------------------------------------------

#: URL de la base de datos en memoria que usan las pruebas.
BASE_DE_PRUEBAS = "sqlite+aiosqlite:///:memory:"

#: Base de datos que el entorno exterior tenía configurada, si la había.
#: Solo se usa para avisar por pantalla.
_BASE_DE_DATOS_EXTERIOR = os.environ.get("DATABASE_URL", "")

#: La base de datos de las pruebas se fija **siempre**, sin respetar el
#: valor que venga del entorno.
#:
#: Esto no es un detalle de estilo: la fixture `base_de_datos` crea y
#: borra las tablas del modelo, y si apuntara a la base de datos real
#: —la que exporta `scripts/verificar_stack.sh`, por ejemplo— la suite
#: dejaría el esquema de la base de datos de trabajo vacío. Con
#: `setdefault` bastaba con tener `DATABASE_URL` exportada para que eso
#: ocurriera sin previo aviso.
os.environ["DATABASE_URL"] = BASE_DE_PRUEBAS
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas-no-usar-en-produccion")
os.environ.setdefault("MINIO_ACCESS_KEY", "pruebas")
os.environ.setdefault("MINIO_SECRET_KEY", "pruebas")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# El broker y el backend se apuntan a URLs locales inaccesibles: si alguna prueba
# intentara encolar de verdad, fallaría de forma ruidosa en vez de colarse con
# el Redis real del sandbox.
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")
os.environ.setdefault("CELERY_BROKER_URL", "redis://127.0.0.1:1/1")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://127.0.0.1:1/2")

if _BASE_DE_DATOS_EXTERIOR and not _BASE_DE_DATOS_EXTERIOR.startswith("sqlite"):
    import warnings

    warnings.warn(
        "Las pruebas se ejecutan contra la base de datos en memoria "
        f"({BASE_DE_PRUEBAS}), no contra '{_BASE_DE_DATOS_EXTERIOR}'. "
        "La base de datos real no se toca.",
        RuntimeWarning,
        stacklevel=1,
    )

# ---------------------------------------------------------------------------
# 2. Aislamiento de Celery: sin broker, sin side effects.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _celery_sin_broker() -> Iterator[None]:
    """Deja Celery en modo *eager* y sin conexiones salientes.

    ``task_always_eager`` ejecuta la tarea en el propio proceso, sin broker ni
    backend. ``task_eager_propagates`` hace que los errores se propagen en vez de
    quedar encapsulados en el resultado, que es justo lo que se quiere en una
    prueba.
    """
    from app.core.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    celery_app.conf.task_store_eager_result = False
    celery_app.conf.worker_prefetch_multiplier = 1
    yield
    celery_app.conf.task_always_eager = False


# ---------------------------------------------------------------------------
# 3. Base de datos aislada por prueba.
# ---------------------------------------------------------------------------


@pytest.fixture()
async def base_de_datos() -> AsyncIterator[None]:
    """Crea las tablas del modelo en la base en memoria y las borra al final.

    Los modelos se importan **antes** de ``create_all``. No es un detalle:
    ``Base.metadata`` solo conoce las tablas cuyos módulos se han
    importado, y si el import es diferido la creación de tablas se hace
    sobre un metadato vacío: las pruebas pasan sin tocar la base de
    datos y fallan más tarde con «no such table».
    """
    from app.core.db import models  # noqa: F401  (importa los modelos)
    from app.core.db.base import Base
    from app.core.db.session import engine

    # Segunda barrera: esta fixture borra las tablas al terminar. Si la
    # base de datos no fuese la de pruebas, ese borrado sería
    # destructivo. Se comprueba aquí, en el punto donde el daño se
    # produciría, y no solo en la configuración del módulo.
    if not str(engine.url).startswith("sqlite"):
        raise RuntimeError(
            "la fixture 'base_de_datos' solo puede usarse contra una base de "
            f"datos de pruebas; la actual es '{engine.url}'. Borra las tablas "
            "al terminar y no debe tocar una base de datos real."
        )

    assert models.Organizacion.__tablename__ in Base.metadata.tables, (
        "los modelos no se han registrado en Base.metadata"
    )

    async with engine.begin() as conexion:
        await conexion.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        async with engine.begin() as conexion:
            await conexion.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture()
def cliente() -> Iterator[object]:
    """Cliente HTTP de prueba sobre la aplicación, con lifespan desactivado.

    Se usa ``ASGITransport`` en vez de ``TestClient`` para no arrastrar el
    ciclo de vida de la aplicación (que abriría el pool de PostgreSQL real).
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    transporte = ASGITransport(app=app)
    cliente = AsyncClient(transport=transporte, base_url="http://pruebas.interno")
    yield cliente
