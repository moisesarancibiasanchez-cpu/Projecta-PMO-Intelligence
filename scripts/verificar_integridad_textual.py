#!/usr/bin/env python
"""Detector de corrupción textual en el código fuente de Projecta PMO Intelligence.

El transporte de comandos de este entorno ya ha corrompido literales varias
veces (``elCelery``, ``const cyclos``, ``Riesgos porAbove umbral``). Este
script comprueba, **byte a byte**, que los literales críticos están
intactos y que no hay texto pegado.

No es un linter de estilo: es un control de integridad. Si el transporte
vuelve a mutilar una cadena, el script aborta con código 2.

Discriminación de falsos positivos:

* Una preposición española pegada a una palabra en inglés **sin espacio**
  (``elCelery``, ``porAbove``) es corrupción. Con espacio es prosa
  normal y no se marca.
* Una palabra de la marca abreviada (``Projecta`` sola) solo es defecto
  cuando aparece **entrecomillada** como literal, no cuando la prosa
  explica la propia prohibición.
* El propio script se excluye del análisis: contiene los patrones.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RAIZ = Path("/workspace/repo")
PROPIO = Path(__file__).resolve()

#: Preposiciones y artículos que, pegados a una mayúscula, delatan una
#: corrupción. Deben ir **sin espacio** delante de la mayúscula y empezar
#: en frontera de palabra, para no casar con el interior de un
#: identificador en ``camelCase`` (la ``y`` de ``PrimaryKeyConstraint``).
PEGADOS_ES = (
    "los|las|uno|una|unos|unas|este|esta|esto|ese|esa|eso|aquello|"
    "del|al|lo|el|la|un|y|en|con|por|para|sin|sobre|entre|hasta|desde|"
    "durante|tras|se|su|sus|de|a|o|u"
)
PATRON_PEGADO_ES = re.compile(rf"\b(?:{PEGADOS_ES})(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ])")

#: Palabras clave de JavaScript que, pegadas a un identificador, delatan
#: una corrupción. Solo se aplican a ficheros ``.js``.
CLAVES_JS = (
    "const|let|var|return|function|class|new|await|async|throw|typeof|"
    "instanceof|delete|switch|case|break|continue|default|export|import"
)

#: Sufijos de **propiedades estándar del DOM** que se leen como una palabra
#: clave de JavaScript pegada a un identificador, y que no son una
#: corrupción: `classList` y `returnValue` forman parte de la API del
#: navegador y no se pueden renombrar.
#:
#: La excepción es **acotada**: solo estos dos sufijos, y solo cuando la
#: palabra clave es la que corresponde. `classList` es `class` + `List`;
#: `returnValue` es `return` + `Value`. Cualquier otra cosa sigue
#: sospechosamente pegada y se sigue avisando.
SUFIJOS_DOM_JS = (
    ("class", "List"),
    ("class", "Name"),
    ("return", "Value"),
)
PATRON_PEGADO_JS = re.compile(rf"\b(?:{CLAVES_JS})(?=[a-zA-Z_$][a-zA-Z0-9_$])")


def _es_api_del_dom(texto: str, posicion: int) -> bool:
    """¿La coincidencia de la posición es una propiedad estándar del DOM?

    ``classList``, ``className`` y ``returnValue`` son API del navegador: no
    se pueden renombrar, y el verificador no debe avisar de ellas. La
    excepción es **acotada**: el sufijo tiene que ser exactamente el de la
    API, y después no puede venir otro carácter de identificador.
    ``classListExtra`` sigue siendo sospechoso.

    El identificador se acepta **se llame como se llame**: un detector de
    corrupción no puede saber si ``returnValue`` es la propiedad del DOM o
    una variable local con ese nombre, y en los dos casos el identificador
    es el mismo.
    """
    palabra = texto[posicion : posicion + 16]
    for clave, sufijo in SUFIJOS_DOM_JS:
        if not palabra.startswith(clave + sufijo):
            continue
        siguiente = posicion + len(clave + sufijo)
        if siguiente >= len(texto) or not (texto[siguiente].isalnum() or texto[siguiente] in "_$"):
            return True
    return False

#: Literales que deben aparecer exactamente así en el código.
LITERALES_OBLIGATORIOS: list[tuple[str, str]] = [
    ("marca completa", "Projecta PMO Intelligence"),
    ("cabecera de correlación", "X-Correlation-Id"),
    ("cookie de sesión", "projecta_sesion"),
    ("cookie de refresh", "projecta_refresh"),
    ("algoritmo de firma", "HS256"),
    ("bucket por defecto", "projecta-evidencia"),
    ("métrica de latencia", "projecta_api_peticiones_latencia_segundos"),
    ("métrica de tareas", "projecta_tareas_ejecutadas_total"),
    ("versión de la aplicación", "0.1.0"),
    ("nombre de la organización", "organizaciones"),
    ("nombre de la tabla de sesiones", "sesiones"),
    ("nombre de la tabla de auditoría", "eventos_auditoria"),
    ("nombre de la tabla de membresías", "membresias_proyecto"),
]

#: Formas prohibidas de la marca: la marca nunca aparece sola entrecomillada.
PATRONES_MARCA_PROHIBIDA = [
    re.compile(r"['\"]Projecta['\"]"),
    re.compile(r"['\"]Projecta\s+(?!PMO Intelligence)\w"),
]

#: Caracteres que nunca deben aparecer en el código.
PATRONES_CARACTER = [
    (re.compile(r"[\u0080-\u009f]"), "carácter de control no imprimible"),
    (re.compile(r"\ufffd"), "carácter de reemplazo Unicode"),
    # El proyecto está íntegramente en español: un ideograma es siempre una
    # corrupción del transporte, nunca una elección.
    (re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]"), "ideograma: el texto debería estar en español"),
    (re.compile(r"[\u0400-\u04ff]"), "cará-cirílico: el texto debería estar en español"),
]


def _ficheros() -> list[Path]:
    extensiones = (".py", ".js", ".html", ".yml", ".yaml", ".sh", ".md", ".css")
    return sorted(r for r in RAIZ.rglob("*") if r.suffix in extensiones and r.is_file())


def _excluir(ruta: Path) -> bool:
    partes = ruta.parts
    if ".git" in partes or "__pycache__" in partes or "node_modules" in partes:
        return True
    if ruta.resolve() == PROPIO:
        return True
    return False


def _excluir_de_patrones(ruta: Path) -> bool:
    """Excluye del escaneo de patrones lo que cita las corrupciones.

    ``docs/skeleton/VALIDACION.md`` contiene, **a propósito**, el texto
    corrupto de cada defecto que se corrigió: es la forma de dejar
    constancia de lo que pasó. Escanearlo produciría falsos positivos
    sobre un documento que es correcto.

    El escaneo de patrones protege el **código**. La documentación se
    comprueba con el auditor de entregables, que sí valida su coherencia.
    """
    partes = ruta.parts
    if "docs" in partes:
        return True
    return _excluir(ruta)


def _autoprueba() -> int:
    """La excepción de la API del DOM tiene que poder fallar.

    Un verificador que avisa de todo no sirve de nada, y uno que no avisa
    de nada tampoco. Aquí se comprueba que:

    * la API estándar del DOM **no** se avisa;
    * un identificador parecido **sí** se avisa;
    * una corrupción real **sí** se avisa.

    Es la misma exigencia que se le pide a todos los verificadores del
    proyecto: tienen que ser capaces de fallar.
    """
    casos = [
        ("elemento.classList.toggle('activo')", False, "classList: API del DOM"),
        ("linea.className = 'x';", False, "className: API del DOM"),
        ("evento.returnValue = 'aviso';", False, "returnValue: API del DOM"),
        ("x.classList;", False, "classList al final de la linea"),
        ("x.classListExtra = 1;", True, "classListExtra: no es la API del DOM"),
        ("x.classNameExtra = 1;", True, "classNameExtra: no es la API del DOM"),
        ("y.returnValueExtra = 1;", True, "returnValueExtra: no es la API del DOM"),
        ("function newValue() {}", True, "newValue: corrupcion real"),
        ("const awaitCosa = 1;", True, "awaitCosa: corrupcion real"),
        ("elemento.classlist.toggle('x')", True, "classlist en minuscula: no es la API"),
    ]
    fallos: list[str] = []
    for texto, esperado, nota in casos:
        detectado = any(
            not _es_api_del_dom(texto, c.start()) for c in PATRON_PEGADO_JS.finditer(texto)
        )
        if detectado != esperado:
            fallos.append(
                f"{nota}: detectado={detectado}, esperado={esperado} (texto: {texto!r})"
            )
    if fallos:
        print(f"FALLO: la autoprueba del verificador ha encontrado {len(fallos)} problemas:")
        for fallo in fallos:
            print(f"  · {fallo}")
        return 2
    print(f"Autoprueba: {len(casos)} casos, 0 problemas. La excepcion del DOM se comporta.")
    return 0


def main() -> int:
    fallos: list[str] = []
    comprobados = 0

    for ruta in _ficheros():
        if _excluir_de_patrones(ruta):
            continue
        try:
            texto = ruta.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            fallos.append(f"{ruta.relative_to(RAIZ)}: no decodificable como UTF-8: {exc}")
            continue
        relative = ruta.relative_to(RAIZ)

        for coincidencia in PATRON_PEGADO_ES.finditer(texto):
            contexto = texto[max(0, coincidencia.start() - 40) : coincidencia.end() + 40]
            fallos.append(
                f"{relative}: palabra española pegada a mayúscula: …{contexto}…"
            )

        if ruta.suffix == ".js":
            for coincidencia in PATRON_PEGADO_JS.finditer(texto):
                if _es_api_del_dom(texto, coincidencia.start()):
                    continue
                contexto = texto[max(0, coincidencia.start() - 40) : coincidencia.end() + 40]
                fallos.append(
                    f"{relative}: palabra clave pegada a un identificador: …{contexto}…"
                )

        for patron in PATRONES_MARCA_PROHIBIDA:
            for coincidencia in patron.finditer(texto):
                contexto = texto[max(0, coincidencia.start() - 40) : coincidencia.end() + 40]
                fallos.append(
                    f"{relative}: la marca aparece abreviada: …{contexto}…"
                )

        for patron, descripcion in PATRONES_CARACTER:
            for coincidencia in patron.finditer(texto):
                fallos.append(f"{relative}: {descripcion}")

        comprobados += 1

    # Los literales obligatorios se comprueban sobre el conjunto del código,
    # no fichero a fichero: cada literal vive en un sitio distinto.
    conjunto = "\n".join(
        ruta.read_text(encoding="utf-8", errors="replace")
        for ruta in _ficheros()
        if not _excluir(ruta)
    )
    for etiqueta, literal in LITERALES_OBLIGATORIOS:
        if literal not in conjunto:
            fallos.append(f"literal obligatorio ausente ({etiqueta}): '{literal}'")

    if fallos:
        print(f"FALLO: {len(fallos)} problemas de integridad textual en {comprobados} ficheros")
        for fallo in fallos:
            print(f"  · {fallo}")
        return 2

    print(
        f"OK: {comprobados} ficheros analizados, {len(LITERALES_OBLIGATORIOS)} literales "
        f"verificados, 0 problemas de integridad textual."
    )
    return 0


if __name__ == "__main__":
    if "--autoprueba" in sys.argv:
        sys.exit(_autoprueba())
    sys.exit(main())
