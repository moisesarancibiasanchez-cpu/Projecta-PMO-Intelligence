#!/usr/bin/env python
"""Preparación del despliegue en **Railway**: lo que se ejecuta ANTES de arrancar.

Es el `preDeployCommand` de los tres servicios de aplicación (API, worker y
beat). Hace, en este orden:

1. **Bloquea** la preparación con un cerrojo consultivo de PostgreSQL. Los
   tres servicios ejecutan su `preDeployCommand` **a la vez**, y las
   migraciones no son idempotentes entre sí: sin el cerrojo, dos servicios
   podrían aplicar la misma migración simultáneamente.
2. **Activa la extensión `vector`** de pgvector, si la base de datos es
   PostgreSQL. En el despliegue nativo ya está; en Railway, la extensión
   hay que habilitarla una vez por base de datos.
3. **Aplica las migraciones**: `alembic upgrade head`.
4. **Siembra el catálogo de roles**, que sin él no se puede autorizar
   nada: sin catálogo, un despliegue recién migrado devuelve 403 en todas
   partes.
5. **Prepara el bucket** de almacenamiento de objetos, con su versionado.
6. **Comprueba** el contrato de disponibilidad: base de datos y Redis.

Todo es **idempotente**: se puede ejecutar tantas veces como haga falta, y
volver a ejecutarlo no cambia nada.

Uso:

    python -m scripts.railway_preparar
    python -m scripts.railway_preparar --solo-migracion   # sin el resto
    python -m scripts.railway_preparar --verificar         # solo comprueba
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

#: Permite ejecutar el script tanto como módulo como desde la raíz.
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import text  # noqa: E402

#: Cerrojo consultivo. Es un número arbitrario pero fijo: todas las
#: instalaciones del proyecto usan el mismo, de modo que dos despliegues
#: simultáneos del mismo banco se serializan también.
CERROJO_PREPARACION = 728_314_915

LOG = "railway_preparar"


def _mensaje(texto: str) -> None:
    print(f"[{LOG}] {texto}", flush=True)


# ─────────────────────────── migras ───────────────────────────


def aplicar_migraciones() -> None:
    """Aplica las migraciones con Alembic.

    Se usa el **subproceso** y no la API de Alembic en línea, porque
    `alembic/env.py` resuelve su propia conexión y su propia URL: es el
    camino que ya está probado en el despliegue nativo.
    """
    _mensaje("se aplican las migraciones (alembic upgrade head)")
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(RAIZ),
        capture_output=True,
        text=True,
    )
    if resultado.returncode != 0:
        _mensaje("FALLO en las migraciones:")
        print(resultado.stdout)
        print(resultado.stderr, file=sys.stderr)
        raise SystemExit(2)
    _mensaje("migraciones aplicadas")


async def activar_pgvector() -> None:
    """Habilita la extensión `vector`, si la base de datos es PostgreSQL.

    En Railway, la extensión viene **instalada** en la imagen del plugin
    pero hay que habilitarla en la base de datos: `CREATE EXTENSION IF NOT
    EXISTS vector`. Es idempotente.
    """
    from app.core.db.session import get_session

    async for sesion in get_session():
        dialecto = sesion.bind.dialect.name if sesion.bind is not None else ""
        if dialecto != "postgresql":
            _mensaje(f"la base de datos no es PostgreSQL ({dialecto}): no hay pgvector")
            return
        await sesion.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await sesion.commit()
        version = await sesion.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        _mensaje(f"pgvector habilitado (versión {version.scalar() or 'desconocida'})")
        return


# ─────────────────────────── roles y bucket ───────────────────────────


async def sembrar_roles() -> None:
    """Siembra el catálogo de roles y avisa de cualquier discrepancia.

    Se llama a la **función** de siembra y no a su `main()`, porque ese
    `main()` lee `sys.argv` y se comería los argumentos de este script.
    """
    from scripts.sembrar_roles import sembrar

    cambios, discrepancias = await sembrar()
    for discrepancia in discrepancias:
        _mensaje(f"discrepancia corregida: {discrepancia}")
    _mensaje(f"catálogo de roles sembrado ({cambios} cambios)")


def preparar_bucket() -> None:
    """Crea el bucket de almacenamiento de objetos si no existe.

    Es **idempotente**, y el versionado se activa siempre, que es lo que
    garantiza que una evidencia no se pueda sobrescribir en silencio.
    """
    from app.core.storage import almacenamiento

    cliente = almacenamiento()
    if not cliente.disponible():
        _mensaje(
            "AVISO: el almacenamiento de objetos no responde. La aplicación "
            "arranca, pero las evidencias no se podrán guardar."
        )
        return
    cliente.asegurar_bucket()
    _mensaje("bucket de almacenamiento preparado")


async def comprobar_disponibilidad() -> None:
    """Comprueba el contrato de disponibilidad: base de datos y Redis."""
    from app.core.db.session import get_session

    async for sesion in get_session():
        await sesion.execute(text("SELECT 1"))
        await sesion.commit()
        _mensaje("base de datos: responde")
        break

    import redis

    from app.core.config import settings

    cliente = redis.Redis.from_url(
        settings.redis_url, socket_connect_timeout=5, socket_timeout=5
    )
    if not cliente.ping():
        raise SystemExit(f"[{LOG}] FALLO: Redis no responde en {settings.redis_url}")
    _mensaje("Redis: responde")


async def _con_bloqueo(funcion) -> None:
    """Ejecuta la función con el cerrojo consultivo tomado.

    El cerrojo es **de sesión**: se toma con `pg_advisory_lock` y se
    suelta al terminar la transacción o la sesión. Es lo que hace que dos
    servicios que ejecuten el `preDeployCommand` a la vez no apliquen las
    migraciones a la vez.
    """
    from app.core.db.session import get_session

    async for sesion in get_session():
        await sesion.execute(
            text("SELECT pg_advisory_lock(:clave)"), {"clave": CERROJO_PREPARACION}
        )
        _mensaje("cerrojo de preparación tomado")
        try:
            await funcion()
        finally:
            try:
                await sesion.execute(
                    text("SELECT pg_advisory_unlock(:clave)"),
                    {"clave": CERROJO_PREPARACION},
                )
                await sesion.commit()
            except Exception:  # noqa: BLE001 - el cerrojo se suelta al cerrar la sesión
                pass
        return


# ─────────────────────────── orquestación ───────────────────────────


async def preparar(solo_migracion: bool = False) -> None:
    """Ejecuta la preparación completa, con el cerrojo tomado."""

    async def _cuerpo() -> None:
        await activar_pgvector()
        aplicar_migraciones()
        if solo_migracion:
            _mensaje("solo migración: se termina aquí")
            return
        await sembrar_roles()
        preparar_bucket()
        await comprobar_disponibilidad()
        _mensaje("preparación completada")

    await _con_bloqueo(_cuerpo)


async def verificar() -> int:
    """Comprueba el estado del despliegue sin cambiar nada."""
    problemas: list[str] = []

    from app.core.db.session import get_session

    async for sesion in get_session():
        try:
            await sesion.execute(text("SELECT 1"))
        except Exception as error:  # noqa: BLE001
            problemas.append(f"la base de datos no responde: {error}")
        dialecto = sesion.bind.dialect.name if sesion.bind is not None else ""
        if dialecto == "postgresql":
            try:
                await sesion.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            except Exception:  # noqa: BLE001
                problemas.append("pgvector no está habilitado en la base de datos")
        break

    import redis

    from app.core.config import settings

    try:
        cliente = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=5, socket_timeout=5
        )
        if not cliente.ping():
            problemas.append("Redis no responde")
    except Exception as error:  # noqa: BLE001
        problemas.append(f"Redis no responde: {error}")

    from app.core.storage import almacenamiento

    if not almacenamiento().disponible():
        problemas.append("el almacenamiento de objetos no responde")

    if problemas:
        for problema in problemas:
            _mensaje(f"FALLO: {problema}")
        return 2
    _mensaje("el despliegue está listo")
    return 0


def main() -> int:
    analizador = argparse.ArgumentParser(
        description="Preparación del despliegue en Railway."
    )
    analizador.add_argument(
        "--solo-migracion",
        action="store_true",
        help="aplica solo las migraciones, sin roles, bucket ni comprobaciones",
    )
    analizador.add_argument(
        "--verificar",
        action="store_true",
        help="solo comprueba el estado del despliegue, sin cambiar nada",
    )
    argumentos = analizador.parse_args()

    if argumentos.verificar:
        return asyncio.run(verificar())

    asyncio.run(preparar(solo_migracion=argumentos.solo_migracion))
    return 0


if __name__ == "__main__":
    sys.exit(main())
