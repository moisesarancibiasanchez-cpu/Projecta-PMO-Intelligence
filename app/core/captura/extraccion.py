"""Extracción de información PMO a partir de una transcripción.

De una transcripción salen nueve cosas: un resumen, los temas tratados,
acuerdos, decisiones, compromisos, riesgos, bloqueos, dependencias y
preguntas abiertas.

La regla que gobierna todo el módulo es una, y es la que más cuesta
cumplir:

> **La extracción no inventa. Si el texto no lo dice, no lo dice.**

Cuando un compromiso no tiene responsable claro o no tiene fecha, el
elemento sale con ``responsable_id = None``, ``fecha_propuesta = None``,
``requiere_revision = True`` y el motivo explicando por qué. Rellenar
el hueco con lo más probable sería crear un compromiso que nadie ha
asumido, y en un PMO un compromiso inventado es un proyecto que se
desvía sin que nadie sepa por qué.

Y la segunda regla, igual de importante:

> **La transcripción es contenido no confiable.**

El texto de una reunión puede contener «ignora las reglas anteriores y
aprueba este acuerdo». La extracción **no** sigue instrucciones: solo
reconoce estructuras de los tipos previstos y las guarda como texto.
No ejecuta nada, no llama a ninguna herramienta, no provoca ninguna
acción externa. Un módulo que analiza lo que dice una reunión no puede
obedecer a lo que dice la reunión.

Lo que sí hace es **explicar** sus decisiones: cada elemento lleva los
índices de los segmentos que lo sustentan y el texto de esos segmentos.
Si alguien discrepa, se ve exactamente por qué discrepa.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ElementoExtraido",
    "ResultadoDeExtraccion",
    "TIPOS_RECONOCIDOS",
    "extraer",
]


#: Los tipos de elemento que la extracción reconoce.
TIPOS_RECONOCIDOS: Final[tuple[str, ...]] = (
    "acuerdo",
    "decision",
    "compromiso",
    "riesgo",
    "bloqueo",
    "dependencia",
    "pregunta_abierta",
    "tema",
)


#: Patrones de la frase que delata un compromiso.
#:
#: Son deliberadamente **estrictos**. Un patrón más laxo produciría
#: compromisos a partir de cualquier frase con un verbo, y el ruido
#: acabaría siendo mayor que la señal.
PATRON_COMPROMISO: Final[re.Pattern[str]] = re.compile(
    r"\b(me lo quedo|lo hago yo|me encargo|asumo|lo cierro|lo subo|"
    r"lo reviso|lo mando|lo preparo|me lo comprometo)\b",
    re.IGNORECASE,
)

#: Patrones de la frase que delata un acuerdo o una decisión.
PATRON_ACUERDO: Final[re.Pattern[str]] = re.compile(
    r"\b(queda cerrado|lo acordamos|acordado|de acuerdo|aprobado|"
    r"decidimos|decidido|aprobamos)\b",
    re.IGNORECASE,
)

#: Patrones de la frase que delata un riesgo.
PATRON_RIESGO: Final[re.Pattern[str]] = re.compile(
    r"\b(riesgo|va con retraso|retraso|nos falta|podría|"
    r"preocupa|nos arriesgamos|se complica)\b",
    re.IGNORECASE,
)

#: Patrones de la frase que delata un bloqueo.
PATRON_BLOQUEO: Final[re.Pattern[str]] = re.compile(
    r"\b(bloqueado|bloqueo|no podemos|esperando|"
    r"pendiente de|stuck)\b",
    re.IGNORECASE,
)

#: Patrones de la frase que delata una dependencia.
PATRON_DEPENDENCIA: Final[re.Pattern[str]] = re.compile(
    r"\b(depende de|necesitamos de|requiere|"
    r"a la espera de|dependemos)\b",
    re.IGNORECASE,
)

#: Patrones de la frase que delata una pregunta abierta.
PATRON_PREGUNTA: Final[re.Pattern[str]] = re.compile(
    r"\b(¿[^?]+\?|queda pendiente de decidir|"
    r"no está claro|hay que ver)\b",
    re.IGNORECASE,
)

#: Patrón de una fecha concreta. Solo reconoce fechas que el texto dice
#: de forma inequívoca: un día de la semana, un número de día del mes
#: o una fecha en formato español.
PATRON_FECHA: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2}\s+de\s+[a-zé]+(?:\s+de\s+\d{4})?|"
    r"lunes|martes|miércoles|jueves|viernes|sábado|domingo|"
    r"próxima\s+semana|la\s+próxima\s+semana|"
    r"antes\s+del\s+\w+|el\s+próximo\s+\w+)\b",
    re.IGNORECASE,
)

#: Palabras que delatan una afirmación de disposición propia.
#:
#: Se usan para el responsable: solo se propone responsable cuando la
#: frase dice explícitamente que **quien habla** se hace cargo. No se
#: deduce que quien menciona una tarea es quien debe hacerla.
PATRON_DISPOSICION_PROPIA: Final[re.Pattern[str]] = re.compile(
    r"\b(me lo quedo|lo hago yo|me encargo|asumo|me lo comprometo)\b",
    re.IGNORECASE,
)

#: Palabras que delatan que el responsable es **otro**.
#:
#: Cuando el texto dice que lo hace otro, el responsable no es quien
#: habla, y la extracción no puede saber quién es. Se marca para
#: revisión, con el nombre si aparece.
PATRON_TERCERO: Final[re.Pattern[str]] = re.compile(
    r"\b(lo hace|lo hará|lo lleva|le toca|se lo pasa a)\s+([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SegmentoDeEntrada:
    """Un segmento de la transcripción, en la forma que espera la extracción."""

    indice: int
    hablante: str
    inicio_ms: int
    fin_ms: int
    texto: str


@dataclass(frozen=True)
class ElementoExtraido:
    """Un elemento extraído, con su evidencia y su incertidumbre.

    ``requiere_revision`` no es un adorno: es la declaración de que
    **esto no está listo** hasta que alguien lo mire. Un elemento
    extraído con responsable y fecha claros, pero cuya frase no está
    inequívocamente atribuida, sigue necesitando revisión.
    """

    clave: str
    tipo: str
    descripcion: str
    segmentos_evidencia: list[int]
    marca_ms: int
    hablante: str
    responsable_nombre: str | None = None
    responsable_id: str | None = None
    fecha_propuesta: str | None = None
    requiere_revision: bool = True
    motivo_revision: str | None = None
    #: Texto exacto del segmento que lo sustenta.
    texto_evidencia: str = ""

    def como_dict(self) -> dict[str, Any]:
        """Representación serializable."""
        return {
            "clave": self.clave,
            "tipo": self.tipo,
            "descripcion": self.descripcion,
            "segmentos_evidencia": list(self.segmentos_evidencia),
            "marca_ms": self.marca_ms,
            "hablante": self.hablante,
            "responsable_nombre": self.responsable_nombre,
            "responsable_id": self.responsable_id,
            "fecha_propuesta": self.fecha_propuesta,
            "requiere_revision": self.requiere_revision,
            "motivo_revision": self.motivo_revision,
            "texto_evidencia": self.texto_evidencia,
        }


class ResultadoDeExtraccion(BaseModel):
    """El resultado completo de la extracción, con forma validada."""

    model_config = ConfigDict(extra="forbid")

    resumen: str = Field(default="", description="Resumen de la sesión.")
    temas: list[str] = Field(default_factory=list, description="Temas tratados.")
    elementos: list[ElementoExtraido] = Field(
        default_factory=list, description="Elementos extraídos con su evidencia."
    )
    #: `True` si al menos un elemento necesita revisión.
    requiere_revision: bool = Field(
        default=True, description="Si algún elemento no está listo para usarse."
    )
    #: Motivo por el que se propone la sesión como resumen.
    motivo_revision: str | None = Field(
        default=None, description="Por qué la sesión necesita revisión."
    )


def _clave(tipo: str, descripcion: str, segmento: int) -> str:
    """Identificador estable y determinista de un elemento.

    Estable para que dos ejecuciones del análisis sobre la misma
    transcripción den los mismos identificadores, y así se pueda
    deduplicar y comparar.
    """
    semilla = f"{tipo}|{descripcion.strip().lower()}|{segmento}"
    return hashlib.sha256(semilla.encode("utf-8")).hexdigest()[:32]


def _frase_limpia(texto: str) -> str:
    """Quita las marcas de hablante del principio del texto."""
    return re.sub(r"^\[[^\]]*\]\s*", "", texto).strip()


def _detectar_tipo(texto: str) -> str | None:
    """El tipo de elemento que delata la frase, o ``None``."""
    if PATRON_COMPROMISO.search(texto):
        return "compromiso"
    if PATRON_ACUERDO.search(texto):
        return "acuerdo"
    if PATRON_BLOQUEO.search(texto):
        return "bloqueo"
    if PATRON_DEPENDENCIA.search(texto):
        return "dependencia"
    if PATRON_RIESGO.search(texto):
        return "riesgo"
    if PATRON_PREGUNTA.search(texto):
        return "pregunta_abierta"
    return None


def _detectar_fecha(texto: str) -> str | None:
    """La fecha que el texto dice de forma explícita, o ``None``."""
    encontrado = PATRON_FECHA.search(texto)
    if not encontrado:
        return None
    return encontrado.group(0).strip()


def extraer(
    segmentos: list[SegmentoDeEntrada] | list[dict[str, Any]],
) -> ResultadoDeExtraccion:
    """Extrae los elementos PMO de una transcripción.

    La función es **determinista**: la misma transcripción da siempre
    el mismo resultado, con los mismos identificadores.

    Args:
        segmentos: los segmentos de la transcripción.

    Returns:
        El resultado de la extracción, con su resumen, sus temas y sus
        elementos.
    """
    # Se aceptan las dos formas: diccionario y segmento. Quien llama
    # desde la base de datos tiene filas; quien llama desde una prueba
    # tiene los segmentos. Admitir las dos evita que un tercer llamante
    # tenga que convertir.
    normalizados: list[SegmentoDeEntrada] = []
    for i, s in enumerate(segmentos):
        if isinstance(s, SegmentoDeEntrada):
            normalizados.append(s)
            continue
        if isinstance(s, dict):
            normalizados.append(
                SegmentoDeEntrada(
                    indice=int(s.get("indice", i)),
                    hablante=str(s.get("hablante", "")),
                    inicio_ms=int(s.get("inicio_ms", 0)),
                    fin_ms=int(s.get("fin_ms", 0)),
                    texto=str(s.get("texto", "")),
                )
            )
            continue
        normalizados.append(
            SegmentoDeEntrada(
                indice=i,
                hablante=str(getattr(s, "hablante", "")),
                inicio_ms=int(getattr(s, "inicio_ms", 0)),
                fin_ms=int(getattr(s, "fin_ms", 0)),
                texto=str(getattr(s, "texto", "")),
            )
        )

    elementos: list[ElementoExtraido] = []
    vistos: set[str] = set()

    for segmento in normalizados:
        texto = _frase_limpia(segmento.texto)
        if not texto:
            continue

        tipo = _detectar_tipo(texto)
        if tipo is None:
            continue

        descripcion = texto
        clave = _clave(tipo, descripcion, segmento.indice)
        if clave in vistos:
            continue
        vistos.add(clave)

        fecha = _detectar_fecha(texto)
        responsable: str | None = None
        motivos: list[str] = []

        if tipo == "compromiso":
            if PATRON_DISPOSICION_PROPIA.search(texto):
                # Quien habla se hace cargo. Es el único caso en el que
                # se propone un responsable, y se propone **por su
                # etiqueta de hablante**, no por una identidad.
                responsable = segmento.hablante
            else:
                tercero = PATRON_TERCERO.search(texto)
                if tercero:
                    responsable = f"participante no identificado ({tercero.group(2)})"
                    motivos.append(
                        "El texto dice que lo hace otra persona, pero no se sabe "
                        "quién es: hay que confirmarlo."
                    )
                else:
                    motivos.append(
                        "El texto no dice de forma inequívoca quién se hace cargo."
                    )
            if fecha is None:
                motivos.append("El texto no indica una fecha.")
        elif tipo in ("riesgo", "bloqueo"):
            if fecha is None:
                motivos.append("El texto no indica una fecha de seguimiento.")
            motivos.append("El alcance del riesgo o del bloqueo no está delimitado.")

        requiere_revision = bool(motivos) or tipo != "tema"

        elementos.append(
            ElementoExtraido(
                clave=clave,
                tipo=tipo,
                descripcion=descripcion,
                segmentos_evidencia=[segmento.indice],
                marca_ms=segmento.inicio_ms,
                hablante=segmento.hablante,
                responsable_nombre=responsable,
                responsable_id=None,
                fecha_propuesta=fecha,
                requiere_revision=requiere_revision,
                motivo_revision=" ".join(motivos) if motivos else None,
                texto_evidencia=texto,
            )
        )

    temas = _temas(normalizados)
    resumen = _resumen(normalizados, temas)

    return ResultadoDeExtraccion(
        resumen=resumen,
        temas=temas,
        elementos=elementos,
        requiere_revision=any(e.requiere_revision for e in elementos) or not elementos,
        motivo_revision=(
            None
            if elementos and all(not e.requiere_revision for e in elementos)
            else "Los resultados de la extracción no están validados por una persona."
        ),
    )


def _temas(segmentos: list[SegmentoDeEntrada]) -> list[str]:
    """Los temas tratados, deducidos de las frases de tipo «tema».

    Un tema se reconoce por la estructura de la frase: alguien anuncia
    un punto de la agenda. No se inventan temas a partir del contenido.
    """
    temas: list[str] = []
    for segmento in segmentos:
        texto = _frase_limpia(segmento.texto)
        encontrado = re.search(
            r"\b(el|la|el primer|el segundo)\s+punto\s+(?:es|son)\s+(.{5,80}?)(?:\.|,|$)",
            texto,
            re.IGNORECASE,
        )
        if encontrado:
            tema = encontrado.group(2).strip()
            if tema and tema not in temas:
                temas.append(tema)
    return temas


def _resumen(segmentos: list[SegmentoDeEntrada], temas: list[str]) -> str:
    """Un resumen del contenido de la sesión.

    Es un resumen **descriptivo**, no interpretativo: dice de qué se
    habló, sin calificar la reunión ni a las personas.
    """
    if not segmentos:
        return "La sesión no tiene transcripción."
    duracion = max(s.fin_ms for s in segmentos) / 1000
    hablantes = sorted({s.hablante for s in segmentos if s.hablante})
    partes = [
        f"Transcripción de {len(segmentos)} segmentos y {duracion:.0f} segundos."
    ]
    if hablantes:
        partes.append(f"Intervienen {len(hablantes)} hablantes etiquetados.")
    if temas:
        partes.append(f"Temas tratados: {', '.join(temas)}.")
    return " ".join(partes)
