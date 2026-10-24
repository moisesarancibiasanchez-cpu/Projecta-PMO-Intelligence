"""Servicio de Projecta Capture: el ciclo de vida de una sesión de captura.

Aquí vive la lógica que no es HTTP: crear la sesión, registrar la
declaración de autorización, transicionar el estado, recibir los
fragmentos de audio, pedir el procesamiento, revisar los resultados y
aplicar la política de retención.

Dos reglas que atraviesan todo el módulo y que conviene tener presentes
al leerlo:

**Una captura no empieza sin declaración de autorización.** La puerta
está en :func:`transicionar_captura`, que la comprueba y falla si no está.
No es un aviso: es una condición.

**Una captura no es completa si no se obtuvo todo lo que se pidió.** El
modo solicitado y el modo efectivo se guardan por separado, y
``captura_completa`` solo es ``True`` cuando coinciden. Cuando no
coinciden, la diferencia es la que hay que comunicar.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.captura.estados import (
    ESTADOS_DE_CAPTURA,
    MODOS_DE_CAPTURA,
    ErrorDeTransicion,
    transicion_captura_permitida,
)
from app.core import auditoria
from app.core.db.models_captura import (
    ACCIONES_DE_REVISION,
    TIPOS_DE_ITEM,
    AccionDeRevision,
    ArtefactoDeAudio,
    ConsentimientoDeCaptura,
    ElementoExtraido,
    EvidenciaDeItem,
    FragmentoDeSubida,
    MapeoHablante,
    NotaDeReunion,
    ParticipanteDeSesion,
    PoliticaDeRetencion,
    SegmentoDeTranscripcion,
    SesionDeCaptura,
    TrabajoDeProcesamiento,
    Transcripcion,
)

LOG = logging.getLogger("projecta.captura.servicio")

__all__ = [
    "ErrorDeCaptura",
    "ErrorDeEstado",
    "ErrorDePeticion",
    "ResultadoDeCarga",
    "ResultadoDeFinalizacion",
    "analizar_fragmentos",
    "anadir_nota",
    "aplicar_purga",
    "crear_sesion",
    "eliminar_sesion",
    "finalizar_carga",
    "iniciar_carga",
    "registrar_autorizacion",
    "recibir_fragmento",
    "revisar_elemento",
    "solicitar_procesamiento",
    "tiene_autorizacion",
    "transicionar",
    "transicionar_captura",
]


class ErrorDeCaptura(Exception):
    """La operación de captura no se puede completar."""


class ErrorDePeticion(Exception):
    """La petición no es válida: falta un dato o lleva uno incorrecto."""


class ErrorDeEstado(Exception):
    """La operación no es válida en el estado actual.

    Envuelve a :class:`ErrorDeTransicion` para poder añadirle contexto
    sin perder la máquina de estados.
    """


#: Tamaño máximo de un fragmento, en bytes. El límite está en el
#: servidor y no solo en el navegador: un cliente que no lo respete no
#: debe poder tumbar la subida.
TAMANO_MAXIMO_FRAGMENTO: Final[int] = 16 * 1024 * 1024

#: Tamaño máximo del artefacto completo, en bytes.
TAMANO_MAXIMO_ARTEFACTO: Final[int] = 2 * 1024 * 1024 * 1024

#: Duración máxima de una sesión, en milisegundos. Cuatro horas.
DURACION_MAXIMA_MS: Final[int] = 4 * 60 * 60 * 1000

#: Texto de la declaración de autorización. Es el que ve el operador
#: antes de aceptarlo, y es el que queda registrado.
TEXTO_DECLARACION_AUTORIZACION: Final[str] = (
    "Declaro que esta sesión de captura está autorizada conforme a la "
    "política de mi organización, que los participantes han sido informados "
    "de la grabación por el procedimiento autorizado, y que acepto la "
    "política de conservación que se indica en esta sesión. Esta "
    "declaración es del organizador de la sesión y no sustituye al "
    "consentimiento de cada participante."
)

#: Texto del aviso que se muestra antes de empezar.
TEXTO_AVISO_PRIVACIDAD: Final[str] = (
    "La grabación se realiza en el equipo del operador, mediante los "
    "permisos que concede el navegador, y se envía al servidor de la "
    "organización. No se comparte pantalla ni se almacena imagen: solo "
    "audio. La grabación fuera de la aplicación de reuniones no genera un "
    "aviso dentro de ella: el organizador debe seguir el procedimiento "
    "autorizado para informar a los participantes."
)


def _ahora() -> datetime:
    """La hora actual, con zona horaria."""
    return datetime.now(timezone.utc)


def _huella(datos: bytes) -> str:
    """SHA-256 de unos bytes, en hexadecimal."""
    return hashlib.sha256(datos).hexdigest()


# ─────────────────────────── sesión ───────────────────────────


async def _auditar(
    sesion: AsyncSession,
    *,
    tipo: str,
    entidad: str,
    organizacion_id: uuid.UUID | str,
    detalle: str,
    proyecto_id: uuid.UUID | str | None = None,
    correlation_id: str | None = None,
    actor: str = "captura:navegador",
    entidad_id: str | None = None,
) -> None:
    """Escribe el evento en la cadena de auditoría del producto.

    **No hace `commit`**: la decisión es del llamante, para que el evento
    y el cambio que lo origina compartan transacción. Si el evento no se
    puede escribir, la excepción sube y el cambio tampoco se confirma.

    Es la misma regla que sigue el resto de Projecta PMO Intelligence, y
    es la razón por la que el `correlation_id` de la sesión permite
    reconstruir qué pasó en una captura concreta.

    El identificador de la entidad va en el campo ``entidad``, con el
    formato ``tipo:identificador``, y el ``id`` del evento se deja vacío
    para que lo asigne la auditoría. El ``id`` del evento es **único por
    evento**: poner ahí el de la entidad haría que dos eventos sobre la
    misma sesión chocasen entre sí.
    """
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo=tipo,
            entidad=f"{entidad}:{entidad_id}" if entidad_id else entidad,
            id="",
            actor=actor,
            organizacion_id=str(organizacion_id),
            detalle=detalle,
            proyecto_id=str(proyecto_id) if proyecto_id else None,
            correlation_id=correlation_id,
        ),
    )


async def crear_sesion(
    sesion: AsyncSession,
    *,
    organizacion_id: uuid.UUID,
    proyecto_id: uuid.UUID,
    titulo: str,
    descripcion: str = "",
    finalidad: str = "",
    participantes: list[dict[str, Any]] | None = None,
    modo_solicitado: str = "MIC_ONLY",
    politica: dict[str, Any] | None = None,
    creado_por: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> SesionDeCaptura:
    """Crea una sesión de captura en estado ``DRAFT``.

    La sesión nace ``DRAFT`` y no ``READY``: para estar lista tiene que
    tener sus datos y la declaración de autorización registrada. Separar
    el «hecho» del «está lista» es lo que permite que la puerta exista.
    """
    if not titulo or not titulo.strip():
        raise ErrorDePeticion("La sesión necesita un título.")
    if modo_solicitado not in MODOS_DE_CAPTURA:
        raise ErrorDePeticion(
            f"El modo '{modo_solicitado}' no existe. Los modos son: "
            f"{', '.join(sorted(MODOS_DE_CAPTURA))}."
        )

    nueva = SesionDeCaptura(
        organizacion_id=organizacion_id,
        proyecto_id=proyecto_id,
        titulo=titulo.strip(),
        descripcion=descripcion or "",
        finalidad=finalidad or "",
        estado="DRAFT",
        modo_solicitado=modo_solicitado,
        creado_por=creado_por,
        correlation_id=correlation_id,
    )
    sesion.add(nueva)
    await sesion.flush()

    for persona in participantes or []:
        nombre = str(persona.get("nombre", "")).strip()
        if not nombre:
            raise ErrorDePeticion("Un participante necesita un nombre.")
        sesion.add(
            ParticipanteDeSesion(
                sesion_id=nueva.id,
                nombre=nombre,
                rol=persona.get("rol") or None,
                identidad=persona.get("identidad") or None,
                organizador=bool(persona.get("organizador", False)),
                informado=bool(persona.get("informado", False)),
            )
        )

    ajustes = politica or {}
    sesion.add(
        PoliticaDeRetencion(
            sesion_id=nueva.id,
            dias_transcripcion=int(ajustes.get("dias_transcripcion", 90)),
            dias_elementos=int(ajustes.get("dias_elementos", 365)),
            dias_audio=int(ajustes.get("dias_audio", 0)),
            dias_notas=int(ajustes.get("dias_notas", 365)),
            conservar_audio=bool(ajustes.get("conservar_audio", False)),
        )
    )
    await sesion.flush()
    await _auditar(
        sesion,
        tipo="CAPTURA_SESION_CREADA",
        entidad="sesion_de_captura",
        organizacion_id=nueva.organizacion_id,
        proyecto_id=nueva.proyecto_id,
        detalle=f"Sesión de captura creada: {nueva.titulo}",
        correlation_id=correlation_id,
        entidad_id=str(nueva.id),
    )
    return nueva


async def registrar_autorizacion(
    sesion: AsyncSession,
    *,
    sesion_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    actor_nombre: str,
    tipo: str = "declaracion_autorizacion",
    texto: str = TEXTO_DECLARACION_AUTORIZACION,
    correlation_id: str | None = None,
) -> ConsentimientoDeCaptura:
    """Registra la declaración de autorización del operador.

    No es un consentimiento de los participantes: es la declaración del
    organizador. El texto lo dice, y el campo se llama así a propósito.
    """
    if tipo not in ("declaracion_autorizacion", "informacion_participantes"):
        raise ErrorDePeticion(f"Tipo de registro desconocido: '{tipo}'.")

    registro = ConsentimientoDeCaptura(
        sesion_id=sesion_id,
        tipo=tipo,
        texto=texto,
        aceptado=True,
        declarado_por=actor_id,
        declarado_por_nombre=actor_nombre,
        correlation_id=correlation_id,
    )
    sesion.add(registro)
    await sesion.flush()
    sesión_cargada = await _cargar_sesion(sesion, sesion_id)
    await _auditar(
        sesion,
        tipo="CAPTURA_AUTORIZACION_REGISTRADA",
        entidad="sesion_de_captura",
        organizacion_id=sesión_cargada.organizacion_id,
        proyecto_id=sesión_cargada.proyecto_id,
        detalle=f"Declaración de autorización registrada por {actor_nombre}.",
        correlation_id=correlation_id,
        entidad_id=str(registro.id),
    )
    return registro


async def tiene_autorizacion(sesion: AsyncSession, sesion_id: uuid.UUID) -> bool:
    """¿Está registrada la declaración de autorización de la sesión?"""
    resultado = await sesion.execute(
        select(func.count())
        .select_from(ConsentimientoDeCaptura)
        .where(
            ConsentimientoDeCaptura.sesion_id == sesion_id,
            ConsentimientoDeCaptura.tipo == "declaracion_autorizacion",
            ConsentimientoDeCaptura.aceptado.is_(True),
        )
    )
    return (resultado.scalar() or 0) > 0


async def transicionar_captura(
    sesion: AsyncSession,
    *,
    sesion_id: uuid.UUID,
    destino: str,
    modo_efectivo: str | None = None,
    duracion_ms: int | None = None,
    detencion_inmediata: bool = False,
) -> SesionDeCaptura:
    """Cambia el estado de la captura, si la transición está permitida.

    La comprobación de la transición va **antes** que la escritura, y
    la puerta de autorización va antes que las dos: no tiene sentido
    validar una transición que además no se va a permitir empezar.
    """
    if destino not in ESTADOS_DE_CAPTURA:
        raise ErrorDePeticion(
            f"El estado '{destino}' no existe. Los estados son: "
            f"{', '.join(sorted(ESTADOS_DE_CAPTURA))}."
        )

    actual = await _cargar_sesion(sesion, sesion_id)

    if destino in ("READY", "RECORDING"):
        # La puerta está en ``READY``, no en ``RECORDING``. ``READY``
        # significa «todo listo para grabar», y no se está listo sin la
        # declaración. Además, ``RECORDING`` solo es alcanzable desde
        # ``READY``, así que la puerta queda antes de empezar de verdad.
        if not await tiene_autorizacion(sesion, sesion_id):
            raise ErrorDeEstado(
                "No se puede pasar a 'listo para grabar' sin la declaración "
                "de autorización registrada. Regístrala antes de iniciar la captura."
            )

    if destino == "RECORDING" and actual.estado == "RECORDING":
        raise ErrorDeEstado("La sesión ya está grabando.")

    # El estado de partida, para el rastro: de dónde se viene.
    estado_anterior = actual.estado

    if not transicion_captura_permitida(actual.estado, destino):
        raise ErrorDeTransicion(actual.estado, destino, "captura")

    actual.estado = destino
    if destino == "RECORDING" and actual.inicio_captura is None:
        actual.inicio_captura = _ahora()
    if destino in ("STOPPED", "CANCELLED", "FAILED"):
        actual.fin_captura = _ahora()
    if duracion_ms is not None:
        actual.duracion_ms = max(0, int(duracion_ms))
    if detencion_inmediata:
        actual.detencion_inmediata = True
    if modo_efectivo is not None:
        if modo_efectivo not in MODOS_DE_CAPTURA:
            raise ErrorDePeticion(f"El modo efectivo '{modo_efectivo}' no existe.")
        actual.modo_efectivo = modo_efectivo
        # La captura solo es completa si se obtuvo **todo** lo que se
        # pidió. Pedir las dos fuentes y quedarse con una no es una
        # captura completa, y decirlo es lo que evita que alguien
        # presente un resultado parcial como si fuera entero.
        actual.captura_completa = modo_efectivo == actual.modo_solicitado
    await sesion.flush()
    await _auditar(
        sesion,
        tipo="CAPTURA_ESTADO_CAMBIADO",
        entidad="sesion_de_captura",
        organizacion_id=actual.organizacion_id,
        proyecto_id=actual.proyecto_id,
        detalle=(
            f"La captura pasa de {estado_anterior} a {destino}."
        ),
        correlation_id=actual.correlation_id,
        entidad_id=str(actual.id),
    )
    return actual


async def _cargar_sesion(sesion: AsyncSession, sesion_id: uuid.UUID) -> SesionDeCaptura:
    """Carga la sesión o falla con un error claro."""
    resultado = await sesion.execute(
        select(SesionDeCaptura).where(SesionDeCaptura.id == sesion_id)
    )
    encontrada = resultado.scalars().first()
    if encontrada is None:
        raise ErrorDePeticion(f"No existe la sesión de captura {sesion_id}.")
    return encontrada


# ─────────────────────────── carga de audio ───────────────────────────


async def iniciar_carga(
    sesion: AsyncSession,
    *,
    sesion_id: uuid.UUID,
    formato: str,
    codec: str = "",
    correlation_id: str | None = None,
) -> ArtefactoDeAudio:
    """Abre un artefacto de audio para la sesión.

    Se llama **antes** de subir el primer fragmento. El artefacto nace
    en estado ``pendiente`` y no es utilizable hasta que se finaliza.
    """
    sesión_cargada = await _cargar_sesion(sesion, sesion_id)
    if formato not in ("webm", "mp4", "ogg", "wav", "m4a"):
        raise ErrorDePeticion(
            f"Formato de audio no admitido: '{formato}'. Los admitidos son: "
            f"webm, mp4, ogg, wav, m4a."
        )
    artefacto = ArtefactoDeAudio(
        sesion_id=sesion_id,
        formato=formato,
        codec=codec,
        estado="pendiente",
        correlation_id=correlation_id,
    )
    sesion.add(artefacto)
    await sesion.flush()
    await _auditar(
        sesion,
        tipo="CAPTURA_ARTIFACTO_CREADO",
        entidad="artefacto_de_audio",
        organizacion_id=sesión_cargada.organizacion_id,
        proyecto_id=sesión_cargada.proyecto_id,
        detalle=f"Se abre el artefacto de audio en formato {formato}.",
        correlation_id=correlation_id,
        entidad_id=str(artefacto.id),
    )
    return artefacto


@dataclass(frozen=True)
class ResultadoDeCarga:
    """El resultado de recibir un fragmento."""

    #: Índice del fragmento.
    indice: int
    #: `True` si es la primera vez que llega este fragmento.
    nuevo: bool
    #: `True` si el contenido coincide con la huella ya registrada.
    coincide: bool
    #: Tamaño del fragmento.
    tamano_bytes: int


async def recibir_fragmento(
    sesion: AsyncSession,
    *,
    artefacto_id: uuid.UUID,
    indice: int,
    datos: bytes,
    clave_objeto: str | None = None,
) -> ResultadoDeCarga:
    """Recibe un fragmento de la grabación.

    Es **idempotente** por índice: el mismo fragmento reenviado no se
    guarda dos veces. Y si llega con un contenido **distinto** del que
    se había registrado, se dice: el cliente y el servidor no están de
    acuerdo, y seguir como si nada ocultaría un problema real.
    """
    if indice < 0:
        raise ErrorDePeticion("El índice del fragmento no puede ser negativo.")
    if not datos:
        raise ErrorDePeticion("El fragmento está vacío.")
    if len(datos) > TAMANO_MAXIMO_FRAGMENTO:
        raise ErrorDePeticion(
            f"El fragmento supera el máximo de {TAMANO_MAXIMO_FRAGMENTO} bytes."
        )

    resultado = await sesion.execute(
        select(ArtefactoDeAudio).where(ArtefactoDeAudio.id == artefacto_id)
    )
    artefacto = resultado.scalars().first()
    if artefacto is None:
        raise ErrorDePeticion(f"No existe el artefacto de audio {artefacto_id}.")

    huella = _huella(datos)

    existente = await sesion.execute(
        select(FragmentoDeSubida).where(
            FragmentoDeSubida.artefacto_id == artefacto_id,
            FragmentoDeSubida.indice == indice,
        )
    )
    fragmento = existente.scalars().first()
    if fragmento is not None:
        coincide = fragmento.huella == huella
        if not coincide:
            raise ErrorDePeticion(
                f"El fragmento {indice} ya se había recibido con otro contenido. "
                f"El índice ya está ocupado por un fragmento distinto: no se "
                f"sobrescribe, porque eso rompería la secuencia."
            )
        return ResultadoDeCarga(
            indice=indice, nuevo=False, coincide=True, tamano_bytes=fragmento.tamano_bytes
        )

    nuevo = FragmentoDeSubida(
        artefacto_id=artefacto_id,
        indice=indice,
        tamano_bytes=len(datos),
        huella=huella,
        clave_objeto=clave_objeto,
        verificado=True,
    )
    sesion.add(nuevo)
    artefacto.tamano_bytes += len(datos)
    if artefacto.fragmentos_totales <= indice:
        artefacto.fragmentos_totales = indice + 1
    artefacto.estado = "subiendo"
    try:
        await sesion.flush()
    except IntegrityError:
        # Otra subida del mismo fragmento ganó la carrera. No es un
        # error: el resultado es el mismo.
        await sesion.rollback()
        return ResultadoDeCarga(indice=indice, nuevo=False, coincide=True, tamano_bytes=len(datos))
    return ResultadoDeCarga(
        indice=indice, nuevo=True, coincide=True, tamano_bytes=len(datos)
    )


@dataclass(frozen=True)
class ResultadoDeFinalizacion:
    """El resultado de finalizar una carga."""

    artefacto_id: uuid.UUID
    #: `True` si la secuencia de fragmentos está completa y sin huecos.
    completa: bool
    #: Índices que faltan.
    faltantes: list[int]
    #: Índices que están repetidos con contenido distinto.
    duplicados: list[int]
    #: Tamaño total en bytes.
    tamano_bytes: int
    #: SHA-256 del artefacto, calculado con los fragmentos en orden.
    huella: str


async def analizar_fragmentos(
    sesion: AsyncSession, artefacto_id: uuid.UUID
) -> tuple[list[int], list[int]]:
    """Devuelve los índices que faltan y los que están duplicados."""
    resultado = await sesion.execute(
        select(FragmentoDeSubida)
        .where(FragmentoDeSubida.artefacto_id == artefacto_id)
        .order_by(FragmentoDeSubida.indice)
    )
    fragmentos = list(resultado.scalars())
    if not fragmentos:
        return [], []

    indices = [f.indice for f in fragmentos]
    faltantes = [i for i in range(max(indices) + 1) if i not in set(indices)]
    duplicados = [
        i for i in set(indices) if indices.count(i) > 1
    ]
    return faltantes, sorted(duplicados)


async def finalizar_carga(
    sesion: AsyncSession,
    *,
    artefacto_id: uuid.UUID,
    duracion_ms: int = 0,
    fragmentos_esperados: int | None = None,
) -> ResultadoDeFinalizacion:
    """Finaliza la carga de forma explícita.

    Finalizar es un acto, no un efecto secundario: sin él, el artefacto
    queda «en curso» y no se puede procesar. Y finalizar con huecos
    **no** es un error silencioso: devuelve la lista de lo que falta
    para que el cliente pueda reenviarlo.
    """
    resultado = await sesion.execute(
        select(ArtefactoDeAudio).where(ArtefactoDeAudio.id == artefacto_id)
    )
    artefacto = resultado.scalars().first()
    if artefacto is None:
        raise ErrorDePeticion(f"No existe el artefacto de audio {artefacto_id}.")

    faltantes, duplicados = await analizar_fragmentos(sesion, artefacto_id)

    # El cliente sabe cuántos fragmentos grabó, y el servidor no: si el
    # último fragmento se perdió, el servidor no lo detectaría por sí
    # solo, porque lo recibido sería una secuencia completa de lo que llegó.
    # Por eso el cliente declara lo que esperaba, y la comparación es la
    # que revela el hueco.
    if fragmentos_esperados is not None:
        esperados = max(0, int(fragmentos_esperados))
        for indice in range(esperados):
            if indice not in faltantes and indice >= artefacto.fragmentos_totales:
                faltantes.append(indice)
        faltantes = sorted(set(faltantes))

    resultado = await sesion.execute(
        select(FragmentoDeSubida)
        .where(FragmentoDeSubida.artefacto_id == artefacto_id)
        .order_by(FragmentoDeSubida.indice)
    )
    fragmentos = list(resultado.scalars())

    # La huella del artefacto se calcula con el contenido concatenado en
    # orden. Es lo que permite comprobar después que lo que se procesó
    # es lo mismo que se subió.
    resumen = hashlib.sha256()
    for fragmento in fragmentos:
        resumen.update(fragmento.huella.encode("ascii"))

    artefacto.estado = "completo" if not faltantes else "incompleto"
    artefacto.finalizado = True
    artefacto.duracion_ms = max(0, int(duracion_ms))
    artefacto.huella = resumen.hexdigest()
    artefacto.fragmentos_totales = len(fragmentos)
    await sesion.flush()
    sesión_cargada = await _cargar_sesion(sesion, artefacto.sesion_id)
    await _auditar(
        sesion,
        tipo="CAPTURA_FINALIZADA",
        entidad="artefacto_de_audio",
        organizacion_id=sesión_cargada.organizacion_id,
        proyecto_id=sesión_cargada.proyecto_id,
        detalle=(
            f"Carga finalizada: {len(fragmentos)} fragmentos, "
            f"{len(faltantes)} faltantes, {artefacto.tamano_bytes} bytes."
        ),
        correlation_id=artefacto.correlation_id,
        entidad_id=str(artefacto_id),
    )

    return ResultadoDeFinalizacion(
        artefacto_id=artefacto_id,
        completa=not faltantes,
        faltantes=faltantes,
        duplicados=duplicados,
        tamano_bytes=artefacto.tamano_bytes,
        huella=artefacto.huella or "",
    )


# ─────────────────────────── procesamiento ───────────────────────────


async def solicitar_procesamiento(
    sesion: AsyncSession,
    *,
    artefacto_id: uuid.UUID,
    tipo: str = "transcripcion",
    proveedor: str = "simulado",
    correlation_id: str | None = None,
) -> TrabajoDeProcesamiento:
    """Crea o devuelve el trabajo de procesamiento.

    Es **idempotente** por ``(artefacto_id, tipo)``: pedir dos veces la
    transcripción del mismo artefacto no crea dos trabajos, devuelve el
    que ya hay. Es lo que evita que un doble clic genere dos
    transcripciones y dos costes.
    """
    if tipo not in ("transcripcion", "analisis"):
        raise ErrorDePeticion(f"Tipo de trabajo desconocido: '{tipo}'.")

    resultado = await sesion.execute(
        select(ArtefactoDeAudio).where(ArtefactoDeAudio.id == artefacto_id)
    )
    artefacto = resultado.scalars().first()
    if artefacto is None:
        raise ErrorDePeticion(f"No existe el artefacto de audio {artefacto_id}.")
    if not artefacto.finalizado:
        raise ErrorDePeticion(
            "El artefacto no está finalizado. No se puede procesar una carga "
            "que todavía puede recibir fragmentos."
        )

    existente = await sesion.execute(
        select(TrabajoDeProcesamiento).where(
            TrabajoDeProcesamiento.artefacto_id == artefacto_id,
            TrabajoDeProcesamiento.tipo == tipo,
        )
    )
    trabajo = existente.scalars().first()
    if trabajo is not None:
        return trabajo

    trabajo = TrabajoDeProcesamiento(
        sesion_id=artefacto.sesion_id,
        artefacto_id=artefacto_id,
        tipo=tipo,
        estado="NOT_STARTED",
        proveedor=proveedor,
        correlation_id=correlation_id,
    )
    sesión_cargada = await _cargar_sesion(sesion, trabajo.sesion_id)
    await _auditar(
        sesion,
        tipo="CAPTURA_PROCESAMIENTO_SOLICITADO",
        entidad="trabajo_de_procesamiento",
        organizacion_id=sesión_cargada.organizacion_id,
        proyecto_id=sesión_cargada.proyecto_id,
        detalle=f"Se pide el procesamiento «{tipo}» con el proveedor {proveedor}.",
        correlation_id=correlation_id,
        entidad_id=str(trabajo.id),
    )
    return trabajo


async def transicionar(
    sesion: AsyncSession,
    *,
    trabajo_id: uuid.UUID,
    destino: str,
    progreso: int | None = None,
    error: str | None = None,
    id_externo: str | None = None,
) -> TrabajoDeProcesamiento:
    """Cambia el estado de un trabajo de procesamiento."""
    from app.core.captura.estados import transicion_procesamiento_permitida

    resultado = await sesion.execute(
        select(TrabajoDeProcesamiento).where(TrabajoDeProcesamiento.id == trabajo_id)
    )
    trabajo = resultado.scalars().first()
    if trabajo is None:
        raise ErrorDePeticion(f"No existe el trabajo {trabajo_id}.")

    if not transicion_procesamiento_permitida(trabajo.estado, destino):
        raise ErrorDeTransicion(trabajo.estado, destino, "procesamiento")

    if trabajo.estado == "ANALYZING" and destino == "APPROVED":
        # La transición directa es el camino rápido de «no hay
        # nada que revisar». Solo vale cuando **no hay nada que
        # revisar**: el principio del producto es «la IA propone,
        # la persona valida», y aprobar con elementos pendientes
        # sería saltarse la revisión humana.
        pendientes = await sesion.execute(
            select(func.count())
            .select_from(ElementoExtraido)
            .where(
                ElementoExtraido.sesion_id == trabajo.sesion_id,
                ElementoExtraido.requiere_revision.is_(True),
                ElementoExtraido.aprobado.is_(False),
            )
        )
        if (pendientes.scalar() or 0) > 0:
            raise ErrorDeEstado(
                "No se puede aprobar el procesamiento: hay elementos "
                "que requieren revisión humana. Pasa por REVIEW_REQUIRED."
            )

    trabajo.estado = destino
    if progreso is not None:
        trabajo.progreso = max(0, min(100, int(progreso)))
    if error is not None:
        trabajo.error = error
    if id_externo is not None:
        trabajo.id_externo = id_externo
    await sesion.flush()
    return trabajo


# ─────────────────────────── notas y revisión ───────────────────────────


async def anadir_nota(
    sesion: AsyncSession,
    *,
    sesion_id: uuid.UUID,
    texto: str,
    marca_ms: int = 0,
    autor_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> NotaDeReunion:
    """Añade una nota manual con su marca de tiempo."""
    if not texto or not texto.strip():
        raise ErrorDePeticion("La nota está vacía.")
    nota = NotaDeReunion(
        sesion_id=sesion_id,
        texto=texto.strip(),
        marca_ms=max(0, int(marca_ms)),
        autor_id=autor_id,
        correlation_id=correlation_id,
    )
    sesion.add(nota)
    await sesion.flush()
    sesión_cargada = await _cargar_sesion(sesion, sesion_id)
    await _auditar(
        sesion,
        tipo="CAPTURA_NOTA_ANADIDA",
        entidad="nota_de_reunion",
        organizacion_id=sesión_cargada.organizacion_id,
        proyecto_id=sesión_cargada.proyecto_id,
        detalle=f"Se añade una nota en el minuto {marca_ms // 60000}:{marca_ms // 1000 % 60:02d}.",
        correlation_id=correlation_id,
        entidad_id=str(nota.id),
    )
    return nota


async def revisar_elemento(
    sesion: AsyncSession,
    *,
    elemento_id: uuid.UUID,
    accion: str,
    actor_id: uuid.UUID | None = None,
    texto_nuevo: str | None = None,
    motivo: str | None = None,
    correlation_id: str | None = None,
) -> ElementoExtraido:
    """Aplica una acción de revisión sobre un elemento extraído.

    Toda revisión queda en el historial. Una corrección que no deja
    rastro es indistinguible de un error.
    """
    if accion not in ACCIONES_DE_REVISION:
        raise ErrorDePeticion(
            f"Acción de revisión desconocida: '{accion}'. Las válidas son: "
            f"{', '.join(sorted(ACCIONES_DE_REVISION))}."
        )

    resultado = await sesion.execute(
        select(ElementoExtraido).where(ElementoExtraido.id == elemento_id)
    )
    elemento = resultado.scalars().first()
    if elemento is None:
        raise ErrorDePeticion(f"No existe el elemento extraído {elemento_id}.")
    if elemento.tipo not in TIPOS_DE_ITEM:
        raise ErrorDePeticion(f"Tipo de elemento desconocido: '{elemento.tipo}'.")

    anterior = elemento.texto_corregido or elemento.descripcion
    accion_registro = AccionDeRevision(
        sesion_id=elemento.sesion_id,
        entidad_tipo="elemento",
        entidad_id=elemento.id,
        accion=accion,
        texto_anterior=anterior,
        texto_nuevo=texto_nuevo,
        motivo=motivo,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )
    sesion.add(accion_registro)

    if accion == "aprobado":
        elemento.aprobado = True
        elemento.requiere_revision = False
        elemento.motivo_revision = None
        elemento.estado_revision = "aprobado"
    elif accion == "rechazado":
        elemento.aprobado = False
        elemento.requiere_revision = False
        elemento.estado_revision = "rechazado"
    elif accion == "corregido":
        if not texto_nuevo or not texto_nuevo.strip():
            raise ErrorDePeticion("Una corrección necesita el texto nuevo.")
        elemento.texto_corregido = texto_nuevo.strip()
        elemento.requiere_revision = False
        elemento.estado_revision = "corregido"
    else:
        elemento.estado_revision = "pendiente"

    await sesion.flush()
    sesión_cargada = await _cargar_sesion(sesion, elemento.sesion_id)
    await _auditar(
        sesion,
        tipo="CAPTURA_ELEMENTO_REVISADO",
        entidad="elemento_extraido",
        organizacion_id=sesión_cargada.organizacion_id,
        proyecto_id=sesión_cargada.proyecto_id,
        detalle=(
            f"Revisión «{accion}» del elemento {elemento.clave} "
            f"({elemento.tipo})."
        ),
        correlation_id=elemento.correlation_id,
        entidad_id=str(elemento.id),
    )
    return elemento


# ─────────────────────────── retención ───────────────────────────


async def aplicar_purga(
    sesion: AsyncSession, *, ahora: datetime | None = None
) -> dict[str, Any]:
    """Elimina lo que ha caducado según la política de cada sesión.

    Devuelve un recuento de lo eliminado, por tipo. Es una operación
    **verificable**: se puede llamar y mirar qué ha hecho, y la
    segunda llamada no borra nada más.
    """
    momento = ahora or _ahora()
    eliminados: dict[str, int] = {
        "audio": 0,
        "transcripciones": 0,
        "notas": 0,
        "elementos": 0,
        "sesiones": 0,
    }

    politicas = await sesion.execute(select(PoliticaDeRetencion))
    for politica in politicas.scalars().all():
        caducidad_transcripcion = momento - timedelta(days=politica.dias_transcripcion)
        caducidad_elementos = momento - timedelta(days=politica.dias_elementos)
        caducidad_notas = momento - timedelta(days=politica.dias_notas)
        caducidad_audio = momento - timedelta(days=politica.dias_audio)

        # El audio es lo primero que se borra, y es lo que más duele
        # perder: es el original.
        if not politica.conservar_audio or politica.dias_audio <= 0:
            resultado = await sesion.execute(
                select(ArtefactoDeAudio).where(
                    ArtefactoDeAudio.sesion_id == politica.sesion_id
                )
            )
            for artefacto in resultado.scalars().all():
                if artefacto.clave_objeto:
                    await _eliminar_objeto(sesion, artefacto.clave_objeto)
                await sesion.delete(artefacto)
                eliminados["audio"] += 1

        resultado = await sesion.execute(
            select(Transcripcion).where(
                Transcripcion.sesion_id == politica.sesion_id,
                Transcripcion.creado_en < caducidad_transcripcion,
            )
        )
        for transcripcion in resultado.scalars().all():
            await sesion.delete(transcripcion)
            eliminados["transcripciones"] += 1

        resultado = await sesion.execute(
            select(NotaDeReunion).where(
                NotaDeReunion.sesion_id == politica.sesion_id,
                NotaDeReunion.creado_en < caducidad_notas,
            )
        )
        for nota in resultado.scalars().all():
            await sesion.delete(nota)
            eliminados["notas"] += 1

        resultado = await sesion.execute(
            select(ElementoExtraido).where(
                ElementoExtraido.sesion_id == politica.sesion_id,
                ElementoExtraido.creado_en < caducidad_elementos,
            )
        )
        for elemento in resultado.scalars().all():
            await sesion.delete(elemento)
            eliminados["elementos"] += 1

        politica.ultima_purga_en = momento
        politica.purga_programada_para = momento + timedelta(days=1)

        # La purga recorre **todas** las políticas, así que el
        # evento va uno por sesión, dentro del bucle: es lo que
        # significa la operación.
        sesión_purgada = await _cargar_sesion(sesion, politica.sesion_id)
        await _auditar(
            sesion,
            tipo="CAPTURA_PURGA",
            entidad="sesion_de_captura",
            organizacion_id=sesión_purgada.organizacion_id,
            proyecto_id=sesión_purgada.proyecto_id,
            detalle=(
                f"Purga por retención de la sesión {politica.sesion_id}: "
                f"audio {eliminados['audio']}, transcripciones "
                f"{eliminados['transcripciones']}, notas "
                f"{eliminados['notas']}, elementos "
                f"{eliminados['elementos']}."
            ),
            correlation_id=sesión_purgada.correlation_id,
            entidad_id=str(politica.sesion_id),
        )

    await sesion.flush()
    return eliminados


async def _eliminar_objeto(sesion: AsyncSession, clave: str) -> None:
    """Elimina un objeto del almacenamiento, si se puede.

    Que falle no impide el borrado de la fila: el objeto huérfano es
    un problema de almacenamiento, no de la política de retención, y
    dejar la fila sin borrar sería peor.
    """
    try:
        from app.core.storage import almacenamiento

        almacen = almacenamiento()
        if almacen.disponible():
            almacen.eliminar(clave)
    except Exception as error:  # noqa: BLE001 - el borrado de la fila no depende de esto
        LOG.warning("no se pudo eliminar el objeto %s: %s", clave, error)


async def eliminar_sesion(
    sesion: AsyncSession, *, sesion_id: uuid.UUID
) -> dict[str, Any]:
    """Elimina una sesión y todo lo que le pertenece.

    Es el borrado **explícito**: el operador lo pide. La purga por
    política es otra cosa, y no se mezclan.
    """
    await _cargar_sesion(sesion, sesion_id)

    resultado = await sesion.execute(
        select(ArtefactoDeAudio).where(ArtefactoDeAudio.sesion_id == sesion_id)
    )
    for artefacto in resultado.scalars().all():
        if artefacto.clave_objeto:
            await _eliminar_objeto(sesion, artefacto.clave_objeto)

    await sesion.execute(
        select(SesionDeCaptura).where(SesionDeCaptura.id == sesion_id)
    )
    from sqlalchemy import delete

    await sesion.execute(delete(SesionDeCaptura).where(SesionDeCaptura.id == sesion_id))
    await sesion.flush()
    return {"eliminada": str(sesion_id)}
