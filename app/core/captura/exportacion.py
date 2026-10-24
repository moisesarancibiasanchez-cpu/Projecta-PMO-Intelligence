"""Exportación de los resultados de una sesión de captura.

Tres formatos, según para qué sirve cada uno:

* **Markdown** — el acta. Se lee en un editor de texto y en un visor.
* **CSV** — los compromisos. Se abre en una hoja de cálculo.
* **JSON** — el resultado estructurado, para alimentar otra
  herramienta.

El CSV lleva un tratamiento especial, y no por capricho. Un fichero CSV
abierto en una hoja de cálculo **evalúa** lo que parece texto: una
celda que empieza por ``=`` se interpreta como una fórmula, y una que
empieza por ``+``, ``-`` o ``@`` también. Un campo de texto que
contenga eso, venience de una transcripción, podría ejecutar algo en
la máquina de quien lo abre. La mitigación es **sanear** esos
campos: anteponerles un apóstrofo, que la hoja de cálculo lee como
«esto es texto» y no como «esto es una orden».

Y hay un límite más: una exportación **solo** incluye la sesión que se
pide. Nunca los datos de otra sesión ni de otro proyecto.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "FORMATEOS",
    "exportar_csv",
    "exportar_json",
    "exportar_markdown",
    "sanear_celda",
    "sanear_texto",
]


#: Los prefijos que una hoja de cálculo interpreta como fórmula.
PREFIXOS_DE_FORMULA: Final[tuple[str, ...]] = ("=", "+", "-", "@")

#: El carácter con el que se neutraliza una fórmula.
SANITIZADOR: Final[str] = "'"


def sanear_celda(valor: Any) -> str:
    """Convierte un valor en una celda de CSV segura.

    Si el valor empieza por un carácter que una hoja de cálculo
    interpretaría como fórmula, se le antepone un apóstrofo. El
    apóstrofo no se ve en la celda, pero le dice a la hoja de cálculo
    que el contenido es texto.

    Los tabuladores y los saltos de línea también se sustituyen: una
    celda con un salto de línea se parte en dos al abrirse, y el
    resultado es un CSV mal formado.
    """
    if valor is None:
        return ""
    texto = str(valor)
    if texto.startswith(PREFIXOS_DE_FORMULA):
        return SANITIZADOR + texto
    return texto.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def sanear_texto(texto: str) -> str:
    """Sanea un texto que va a un documento de texto.

    Para Markdown el riesgo es distinto: un texto con ``<script>`` en
    un visor que lo interprete como HTML. Se escapan los caracteres
    de HTML.
    """
    return (
        texto.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@dataclass(frozen=True)
class FilaDeExportacion:
    """Una fila de la exportación de compromisos."""

    id: str
    tipo: str
    descripcion: str
    responsable: str
    fecha: str
    estado: str
    requiere_revision: bool
    motivo_revision: str
    marca_ms: int
    evidencia: str


def exportar_markdown(
    *,
    sesion: dict[str, Any],
    participantes: list[dict[str, Any]],
    elementos: list[dict[str, Any]],
    notas: list[dict[str, Any]],
    transcripcion: list[dict[str, Any]] | None = None,
) -> str:
    """Genera el acta en Markdown.

    El acta distingue lo **aprobado** de lo que **está pendiente de
    revisión**. Presentarlos juntos sería presentar como acuerdo lo que
    nadie ha validado, y esa es exactamente la confusión que el
    proyecto lleva evitando desde el principio.
    """
    lineas: list[str] = []
    lineas.append(f"# Acta · {sanear_texto(sesion.get('titulo', 'Sesión de captura'))}")
    lineas.append("")
    lineas.append(f"- **Proyecto:** {sanear_texto(str(sesion.get('proyecto_id', '')))}")
    lineas.append(f"- **Sesión:** {sesion.get('id', '')}")
    lineas.append(f"- **Fecha:** {sesion.get('creado_en', '')}")
    lineas.append(f"- **Finalidad:** {sanear_texto(sesion.get('finalidad', ''))}")
    lineas.append(
        f"- **Modo de captura:** {sesion.get('modo_solicitado', '')} "
        f"(obtenido: {sesion.get('modo_efectivo') or 'sin determinar'})"
    )
    if not sesion.get("captura_completa", False):
        lineas.append(
            "- **Aviso:** la captura **no es completa**: no se obtuvo todo lo "
            "que se pidió. El acta refleja únicamente lo capturado."
        )
    lineas.append("")

    lineas.append("## Participantes")
    lineas.append("")
    if participantes:
        lineas.append("| Nombre | Rol | Identidad | Informado |")
        lineas.append("|---|---|---|---|")
        for persona in participantes:
            lineas.append(
                f"| {sanear_texto(persona.get('nombre', ''))} "
                f"| {sanear_texto(persona.get('rol') or '—')} "
                f"| {sanear_texto(persona.get('identidad') or '—')} "
                f"| {'sí' if persona.get('informado') else 'no'} |"
            )
    else:
        lineas.append("_Sin participantes registrados._")
    lineas.append("")

    aprobados = [e for e in elementos if e.get("aprobado")]
    pendientes = [e for e in elementos if not e.get("aprobado")]

    lineas.append("## Resultados aprobados")
    lineas.append("")
    if aprobados:
        for elemento in aprobados:
            lineas.append(_linea_de_elemento(elemento))
    else:
        lineas.append("_No hay resultados aprobados todavía._")
    lineas.append("")

    lineas.append("## Pendientes de revisión")
    lineas.append("")
    if pendientes:
        lineas.append(
            "Lo que sigue **no ha sido validado por una persona**. No debe "
            "presentarse como acuerdo."
        )
        lineas.append("")
        for elemento in pendientes:
            lineas.append(_linea_de_elemento(elemento))
    else:
        lineas.append("_No queda nada pendiente de revisión._")
    lineas.append("")

    if notas:
        lineas.append("## Notas manuales")
        lineas.append("")
        for nota in notas:
            lineas.append(f"- `{_mmss(nota.get('marca_ms', 0))}` {sanear_texto(nota.get('texto', ''))}")
        lineas.append("")

    if transcripcion:
        lineas.append("## Transcripción")
        lineas.append("")
        for segmento in transcripcion:
            hablante = sanear_texto(segmento.get("hablante") or "Hablante")
            lineas.append(
                f"- `{_mmss(segmento.get('inicio_ms', 0))}` **{hablante}**: "
                f"{sanear_texto(segmento.get('texto', ''))}"
            )
        lineas.append("")

    return "\n".join(lineas)


def _linea_de_elemento(elemento: dict[str, Any]) -> str:
    """Una línea de Markdown para un elemento extraído."""
    texto = elemento.get("texto_corregido") or elemento.get("descripcion", "")
    partes = [f"- **{elemento.get('tipo', '')}** — {sanear_texto(texto)}"]
    detalle: list[str] = []
    if elemento.get("responsable_nombre"):
        detalle.append(f"responsable: {sanear_texto(elemento['responsable_nombre'])}")
    if elemento.get("fecha_propuesta"):
        detalle.append(f"fecha: {elemento['fecha_propuesta']}")
    if elemento.get("motivo_revision"):
        detalle.append(f"motivo de revisión: {sanear_texto(elemento['motivo_revision'])}")
    if detalle:
        partes.append(f"  ({'; '.join(detalle)})")
    if elemento.get("segmentos_evidencia"):
        partes.append(
            f"  _evidencia: segmentos {elemento['segmentos_evidencia']}_"
        )
    return "\n".join(partes)


def _mmss(milisegundos: Any) -> str:
    """Milisegundos a ``mm:ss``."""
    try:
        total = int(milisegundos) // 1000
    except (TypeError, ValueError):
        total = 0
    return f"{total // 60:02d}:{total % 60:02d}"


def exportar_csv(elementos: list[dict[str, Any]]) -> str:
    """Genera el CSV de los compromisos y resultados.

    Todas las celdas de texto pasan por :func:`sanear_celda`, así que
    un CSV exportado no puede ejecutar nada al abrirse en una hoja de
    cálculo.
    """
    buffer = io.StringIO()
    escritor = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    escritor.writerow(
        [
            "id",
            "tipo",
            "descripcion",
            "responsable",
            "fecha",
            "estado",
            "requiere_revision",
            "motivo_revision",
            "marca",
            "evidencia",
        ]
    )
    for elemento in elementos:
        escritor.writerow(
            [
                sanear_celda(elemento.get("id", "")),
                sanear_celda(elemento.get("tipo", "")),
                sanear_celda(elemento.get("texto_corregido") or elemento.get("descripcion", "")),
                sanear_celda(elemento.get("responsable_nombre") or ""),
                sanear_celda(elemento.get("fecha_propuesta") or ""),
                sanear_celda(elemento.get("estado_revision", "")),
                sanear_celda("sí" if elemento.get("requiere_revision") else "no"),
                sanear_celda(elemento.get("motivo_revision") or ""),
                sanear_celda(_mmss(elemento.get("marca_ms", 0))),
                sanear_celda(
                    " ".join(str(i) for i in (elemento.get("segmentos_evidencia") or []))
                ),
            ]
        )
    return buffer.getvalue()


def exportar_json(
    *,
    sesion: dict[str, Any],
    participantes: list[dict[str, Any]],
    elementos: list[dict[str, Any]],
    notas: list[dict[str, Any]],
    transcripcion: list[dict[str, Any]] | None = None,
) -> str:
    """Genera el resultado estructurado en JSON."""
    return json.dumps(
        {
            "sesion": sesion,
            "participantes": participantes,
            "elementos": elementos,
            "notas": notas,
            "transcripcion": transcripcion or [],
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


#: Los formatos de exportación disponibles.
FORMATEOS: Final[tuple[str, ...]] = ("markdown", "csv", "json")
