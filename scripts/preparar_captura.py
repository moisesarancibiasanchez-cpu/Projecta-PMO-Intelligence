#!/usr/bin/env python3
"""Prepara una organización y un proyecto para las pruebas de captura.

Las pruebas funcionales de la captura de reuniones necesitan tres cosas
que no existen en una base de datos recién migrada: una organización, un
proyecto y un usuario administrador que pueda disparar la captura desde
la API.

Este script las crea, o las reutiliza si ya están, y **imprime sus
identificadores** para poder pasarlos a la CLI y a la API.

Es idempotente: si lo ejecutas dos veces, no duplica nada.

    python scripts/preparar_captura.py
    python scripts/preparar_captura.py --slug banco-demo --proyecto "Programa strategic"
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


async def _preparar(slug: str, nombre: str, codigo: str, nombre_proyecto: str) -> dict[str, str]:
    """Crea o reutiliza la organización y el proyecto."""
    from sqlalchemy import select

    from app.core.db.models import Organizacion, Proyecto
    from app.core.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as sesion:
        resultado = await sesion.execute(
            select(Organizacion).where(Organizacion.slug == slug)
        )
        organizacion = resultado.scalars().first()
        if organizacion is None:
            organizacion = Organizacion(nombre=nombre, slug=slug)
            sesion.add(organizacion)
            await sesion.flush()

        resultado = await sesion.execute(
            select(Proyecto).where(
                Proyecto.organizacion_id == organizacion.id,
                Proyecto.codigo == codigo,
            )
        )
        proyecto = resultado.scalars().first()
        if proyecto is None:
            proyecto = Proyecto(
                organizacion_id=organizacion.id,
                nombre=nombre_proyecto,
                codigo=codigo,
                descripcion="Proyecto de pruebas para la captura de reuniones de Teams.",
            )
            sesion.add(proyecto)
            await sesion.flush()

        await sesion.commit()
        return {
            "organizacion_id": str(organizacion.id),
            "organizacion_slug": organizacion.slug,
            "proyecto_id": str(proyecto.id),
            "proyecto_codigo": proyecto.codigo,
        }


def main() -> int:
    """Punto de entrada."""
    analizador = argparse.ArgumentParser(
        description="Prepara organización y proyecto para las pruebas de captura"
    )
    analizador.add_argument("--slug", default="banco-demo")
    analizador.add_argument("--nombre", default="Banco de demostración")
    analizador.add_argument("--codigo", default="PRUEBA-CAPTURA")
    analizador.add_argument("--proyecto", default="Programa strategic")
    argumentos = analizador.parse_args()

    datos = asyncio.run(
        _preparar(
            argumentos.slug,
            argumentos.nombre,
            argumentos.codigo,
            argumentos.proyecto,
        )
    )
    print("Datos preparados para las pruebas de captura")
    print("=" * 58)
    for clave, valor in datos.items():
        print(f"  {clave:22s} {valor}")
    print()
    print("  Para capturar contra el doble de Graph:")
    print(f"    python scripts/teams_captura.py capturar --doble \\")
    print(f"      --organizacion {datos['organizacion_id']} \\")
    print(f"      --organizador organizer@banco.example")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
