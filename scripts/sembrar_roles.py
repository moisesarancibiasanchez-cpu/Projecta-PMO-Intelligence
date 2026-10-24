#!/usr/bin/env python
"""Siembra del catálogo de roles de Projecta PMO Intelligence (HU_02).

Inserta los seis roles normativos del TO-BE v1.1 §6.1 en la tabla
``roles``, con su **nivel jerárquico**. El nivel es lo que resuelve el
RBAC en runtime, así que la siembra tiene que ser **idempotente y
verificable**: si alguien cambia un nivel en la base de datos, el script
lo detecta y lo corrige, y avisa de la discrepancia.

Uso:

    python -m scripts.sembrar_roles
    python -m scripts.sembrar_roles --verificar   # solo comprueba
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

#: Permite ejecutar el script tanto como módulo como desde la raíz.
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import select  # noqa: E402

from app.core.db.models import Rol  # noqa: E402
from app.core.db.session import get_session  # noqa: E402
from app.core.rbac import CATALOGO_ROLES  # noqa: E402


async def sembrar() -> tuple[int, list[str]]:
    """Inserta o corrige los roles del catálogo. Devuelve el número de cambios."""
    cambios = 0
    discrepancias: list[str] = []
    async for sesion in get_session():
        for clave, rol in CATALOGO_ROLES.items():
            resultado = await sesion.execute(select(Rol).where(Rol.nombre == clave))
            fila = resultado.scalar_one_or_none()
            if fila is None:
                sesion.add(
                    Rol(
                        nombre=clave,
                        nivel=rol.nivel,
                        ambito=rol.ambito,
                        descripcion=f"{rol.puede}. No puede: {rol.no_puede}.",
                    )
                )
                cambios += 1
                continue
            if fila.nivel != rol.nivel:
                discrepancias.append(
                    f"rol '{clave}': nivel {fila.nivel} en la base de datos, "
                    f"{rol.nivel} en el catálogo normativo"
                )
                fila.nivel = rol.nivel
                cambios += 1
            if fila.ambito != rol.ambito:
                discrepancias.append(
                    f"rol '{clave}': ámbito '{fila.ambito}' en la base de datos, "
                    f"'{rol.ambito}' en el catálogo normativo"
                )
                fila.ambito = rol.ambito
                cambios += 1
            if not fila.descripcion:
                fila.descripcion = f"{rol.puede}. No puede: {rol.no_puede}."
                cambios += 1
        await sesion.commit()
        break
    return cambios, discrepancias


async def verificar() -> tuple[bool, list[str]]:
    """Comprueba que la base de datos coincide con el catálogo normativo."""
    problemas: list[str] = []
    async for sesion in get_session():
        resultado = await sesion.execute(select(Rol))
        filas = {fila.nombre: fila for fila in resultado.scalars()}
        for clave, rol in CATALOGO_ROLES.items():
            fila = filas.get(clave)
            if fila is None:
                problemas.append(f"falta el rol '{clave}'")
                continue
            if fila.nivel != rol.nivel:
                problemas.append(
                    f"rol '{clave}': nivel {fila.nivel} en la base de datos, "
                    f"{rol.nivel} en el catálogo"
                )
        for clave in filas:
            if clave not in CATALOGO_ROLES:
                problemas.append(
                    f"el rol '{clave}' existe en la base de datos y no está en el catálogo"
                )
        break
    return (not problemas), problemas


def main() -> int:
    """Punto de entrada."""
    analizador = argparse.ArgumentParser(description="Siembra del catálogo de roles.")
    analizador.add_argument(
        "--verificar",
        action="store_true",
        help="Solo comprueba la coherencia, sin escribir nada.",
    )
    argumentos = analizador.parse_args()

    if argumentos.verificar:
        correcto, problemas = asyncio.run(verificar())
        if correcto:
            print("OK: el catálogo de roles coincide con el catálogo normativo.")
            return 0
        print(f"FALLO: {len(problemas)} discrepancias:")
        for problema in problemas:
            print(f"  · {problema}")
        return 1

    cambios, discrepancias = asyncio.run(sembrar())
    for discrepancia in discrepancias:
        print(f"AVISO: {discrepancia}")
    print(f"Catálogo de roles sembrado: {cambios} cambio(s) aplicado(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
