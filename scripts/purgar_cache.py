#!/usr/bin/env python3
"""Purga la caché de bytecode del repositorio de Projecta PMO Intelligence.

**Por qué existe esto.** El sistema de ficheros del entorno de
validación **no actualiza la marca de tiempo** de un fichero cuando su
contenido cambia y su tamaño permanece igual. Python valida su caché de
bytecode comparando la pareja ``(mtime, tamaño)`` del fuente, así que
después de editar un módulo Python, el intérprete puede seguir
ejecutando la **versión anterior** del código sin ningún aviso.

El síntoma es desconcertante porque el código es correcto: se edita un
fichero, se ejecuta, y el comportamiento es el de antes. Peor todavía,
un cambio que arregla un defecto «no funciona», y se «corrige» un
defecto que ya no existe, y la entrega se presenta como un éxito.

La comprobación es de una línea::

    >>> import m           # m.py contiene VALOR = "VERSION-1"
    >>> m.VALOR
    'VERSION-1'
    >>> # se reescribe m.py con VALOR = "VERSION-2", sin cambiar el tamaño
    >>> import m
    >>> m.VALOR
    'VERSION-1'           # <- obsoleto: la caché no se invalidó

Este módulo es la mitigación: **purga la caché antes de medir**.

Uso:
    python3 scripts/purgar_cache.py [raíz]
"""

from __future__ import annotations

import pathlib
import shutil
import sys

#: Nombre del directorio de caché de bytecode.
NOMBRE_CACHE = "__pycache__"


def purgar(raiz: pathlib.Path | str = ".") -> int:
    """Elimina todos los directorios de caché de bytecode bajo la raíz.

    Devuelve el número de directorios eliminados. La operación es
    idempotente y no falla si la raíz no existe: purgar una caché
    inexistente no es un error, es el estado normal.
    """
    base = pathlib.Path(raiz).resolve()
    if not base.exists():
        return 0
    eliminados = 0
    for directorio in base.rglob(NOMBRE_CACHE):
        if directorio.is_dir():
            shutil.rmtree(directorio, ignore_errors=True)
            eliminados += 1
    return eliminados


def main() -> int:
    """Punto de entrada: purga la caché e informa."""
    raiz = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    eliminados = purgar(raiz)
    print(f"Cachés de bytecode purgadas: {eliminados} (raíz: {raiz})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
