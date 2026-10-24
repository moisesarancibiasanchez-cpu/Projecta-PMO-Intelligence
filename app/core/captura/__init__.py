"""Projecta PMO Intelligence — Projecta Capture: captura de reuniones en el navegador.

Este paquete tiene tres piezas, y cada una tiene una responsabilidad
que no se comparte con las otras:

* :mod:`app.core.captura.estados` — las dos máquinas de estados (captura
  y procesamiento) y las transiciones válidas entre ellas.
* :mod:`app.core.captura.servicio` — el ciclo de vida de una sesión:
  la puerta de autorización, la carga de fragmentos, el procesamiento y
  la retención.
* :mod:`app.core.captura.exportacion` — los tres formatos de salida, con
  el saneamiento del CSV.

La captura en el navegador no es un caso aparte de la plataforma: es
una fuente más, y sus resultados entran por el mismo camino de revisión
humana que el resto. Un compromiso extraído de una transcripción no es
un compromiso hasta que alguien lo aprueba.
"""

from __future__ import annotations

# Los tres módulos se importan aquí para que los nombres de `__all__`
# existan de verdad: `from app.core.captura import *` sin ellos falla
# con un `AttributeError` que no aparece hasta que alguien lo usa.
from app.core.captura import estados, exportacion, servicio

__all__ = ["estados", "exportacion", "servicio"]
