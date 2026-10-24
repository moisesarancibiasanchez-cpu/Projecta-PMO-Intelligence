"""Puente entre el mundo sincrónico de Celery y el asíncrono de la aplicación.

El problema que resuelve
------------------------

Una tarea de Celery es **síncrona**: la ejecuta el worker, en un hilo.
Pero el cuerpo de la tarea necesita hablar con la base de datos, que
es asíncrona. La forma habitual de resolverlo es `asyncio.run(...)`, y
funciona... **hasta que no funciona**.

`asyncio.run()` no se puede llamar desde dentro de un bucle de eventos
que ya esté corriendo. Y hay tres situaciones en las que la tarea se
ejecuta con un bucle activo:

* El modo **eager** de Celery, que ejecuta la tarea en el proceso que
  la encola. Si la encola una ruta de FastAPI, ese proceso **tiene** un
  bucle activo, y la tarea revienta con
  ``asyncio.run() cannot be called from a running event loop``.
* Un worker con un *pool* de hilos asíncronos.
* Cualquier llamada directa a la función de la tarea desde código
  asíncrono.

La solución
-----------

:func:`ejecutar` detecta si hay un bucle activo. Si no lo hay, usa
`asyncio.run`, que es lo rápido. Si lo hay, lanza la corrutina en un
hilo aparte, con su propio bucle, y espera el resultado.

El coste es el de un hilo más, y solo se paga en el caso que lo
necesita. La alternativa —no supporting el caso async— es dejar una
tierra minada en el código que se descubre en producción.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Coroutine, TypeVar

__all__ = ["ejecutar"]

T = TypeVar("T")


def ejecutar(corrutina: Coroutine[Any, Any, T]) -> T:
    """Ejecuta una corrutina desde código sincrónico y devuelve su resultado.

    Funciona tanto si el llamante está dentro de un bucle de eventos
    como si no lo está.

    Args:
        corrutina: la corrutina a ejecutar.

    Returns:
        El valor que devuelve la corrutina.

    Raises:
        Exception: lo que lance la corrutina, sin envolver.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No hay bucle activo: se puede usar asyncio.run directamente.
        return asyncio.run(corrutina)

    # Hay un bucle activo: la corrutina necesita el suyo. Se ejecuta en
    # un hilo aparte, que sí puede tener su propio bucle.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ejecutor:
        futuro = ejecutor.submit(asyncio.run, corrutina)
        return futuro.result()
