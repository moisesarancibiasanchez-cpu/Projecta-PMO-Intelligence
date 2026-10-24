"""Auditoría de Projecta PMO Intelligence: registro append-only con hash chain.

Implementa la base de HU_25 y HU_26 (taxonomía de eventos y audit log
inmutable con cadena SHA-256) y la parte de HU_27 que corresponde a la
trazabilidad del lado servidor.

El principio es que **un evento de auditoría no se modifica ni se borra**.
Para que eso sea comprobable y no una promesa, cada evento guarda la
huella SHA-256 de su contenido **y** la huella del evento anterior. Alterar
un evento rompe la cadena a partir de ahí, y la rotura es detectable con
una simple recorrida.

La cadena se calcula **dentro de la misma transacción** que el cambio de
estado que la origina. Si el cambio se deshace, el evento se deshace con
él: nunca queda una acción registrada que no ocurrió, ni al revés.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import EventoAuditoria

__all__ = [
    "CATALOGO_EVENTOS",
    "ErrorDeAuditoria",
    "Evento",
    "calcular_hash",
    "registrar",
    "verificar_cadena",
]

#: Huella con la que arranca la cadena, cuando no hay evento anterior.
HASH_INICIAL: Final[str] = "0" * 64

#: Catálogo de tipos de evento del TO-BE v1.1 §6.5. Ampliarlo es añadir una
#: entrada, no desplegar código.
CATALOGO_EVENTOS: Final[dict[str, str]] = {
    "PROYECTO_CREADO": "Se crea un proyecto dentro de una organización.",
    "PROYECTO_ESTADO_CAMBIADO": "Cambia el estado de un proyecto.",
    "USUARIO_CREADO": "Se da de alta un usuario en la organización.",
    "ROL_ASIGNADO": "Se asigna o cambia el rol de un usuario.",
    "MEMBRESIA_CREADA": "Un usuario entra en un proyecto con un rol concreto.",
    "SESION_INICIADA": "Un usuario inicia sesión.",
    "SESION_CERRADA": "Una sesión se cierra.",
    "SESION_REVOCADA": "Se revoca una familia de sesiones.",
    "TRANSICION_RECHAZADA": (
        "Una operación se rechaza por permisos. No se modifica ningún dato "
        "ni estado, pero el intento queda registrado."
    ),
    "ESTADO_CAMBIADO": "Cambia el estado de un compromiso.",
    "ARTEFACTO_APROBADO": "Se aprueba un artefacto.",
    "ARTEFACTO_RECHAZADO": "Se rechaza un artefacto.",
    "ARTEFACTO_VERSIONADO": "Nueva versión de un artefacto tras corrección humana.",
    "CAMPO_CORREGIDO": "Se corrige un campo de un artefacto con motivo.",
    "FUENTE_INGRESADA": "Entra una fuente: documento, acta, ticket o correo.",
    "ESCRITURA_EXTERNA": "Se escribe en un sistema externo.",
    "ADJUNTO_CARGADO": "Se carga un fichero al almacenamiento de objetos.",
    "ADJUNTO_NO_DISPONIBLE": "El objeto de un adjunto ya no está en el almacenamiento.",
    "CONFIGURACION_CAMBIADA": "Cambia la configuración de la organización.",
    "INTEGRACION_HABILITADA": "Se habilita una integración.",
    # --- Projecta Capture (captura en navegador) --------------------
    "CAPTURA_SESION_CREADA": "Se crea una sesión de captura.",
    "CAPTURA_AUTORIZACION_REGISTRADA": (
        "Se registra la declaración de autorización de una sesión de "
        "captura. Sin ella no se puede pasar a READY."
    ),
    "CAPTURA_ESTADO_CAMBIADO": (
        "Cambia el estado de una sesión de captura o de su "
        "procesamiento."
    ),
    "CAPTURA_ARTIFACTO_CREADO": (
        "Se crea el artefacto de audio de una sesión de captura."
    ),
    "CAPTURA_FINALIZADA": (
        "Se cierra la carga de audio de una sesión de captura, con el "
        "número de fragmentos declarados y los recibidos."
    ),
    "CAPTURA_PROCESAMIENTO_SOLICITADO": (
        "Se pide el procesamiento de un artefacto de audio: "
        "transcripción y extracción de elementos de PMO."
    ),
    "CAPTURA_NOTA_ANADIDA": (
        "Se añade una nota humana a una sesión de captura, con su "
        "marca de tiempo."
    ),
    "CAPTURA_ELEMENTO_REVISADO": (
        "Una persona revisa un elemento extraído: lo aprueba, lo "
        "corrige o lo rechaza, y queda el motivo."
    ),
    "CAPTURA_PURGA": (
        "Se aplica la política de retención y se purga lo que ha "
        "cumplido su plazo."
    ),
    # --- Edge Connector (prueba de concepto) --------------------------
    "EDGE_DISPOSITIVO_REGISTRADO": (
        "Se registra un dispositivo del Edge Connector con su clave pública."
    ),
    "EDGE_DISPOSITIVO_REVOCADO": (
        "Se revoca un dispositivo del Edge Connector. A partir de ese momento "
        "no puede enviar eventos."
    ),
    "EDGE_CONSENTIMIENTO_OTORGADO": (
        "Se registra un consentimiento para un recurso y una finalidad. "
        "Es lo que la puerta consulta antes de dejar leer."
    ),
    "EDGE_CONSENTIMIENTO_REVOCADO": "Se revoca un consentimiento registrado.",
    "EDGE_EVENTO_ACEPTADO": (
        "Se acepta un evento firmado del Edge Connector, después de pasar "
        "la puerta del consentimiento y la minimización de datos."
    ),
    "EDGE_EVENTO_RECHAZADO": (
        "Se rechaza un evento del Edge Connector. No se modifica ningún dato, "
        "pero el intento queda registrado con su motivo."
    ),
    # --- Captura de reuniones de Teams --------------------------------
    "REUNION_CAPTURADA": (
        "Se captura una reunión de Teams: su transcripción, sus participantes "
        "y, si está configurado, su audio."
    ),
    "REUNION_YA_CAPTURADA": (
        "La reunión ya estaba capturada y no se vuelve a guardar. La captura "
        "es idempotente."
    ),
    "REUNION_NO_CAPTURABLE": (
        "Una reunión no se ha podido capturar. Se registra el motivo: sin "
        "transcripción no hay nada que capturar."
    ),
}


class ErrorDeAuditoria(RuntimeError):
    """La cadena de auditoría está rota o el evento no se puede registrar."""


@dataclass(frozen=True)
class Evento:
    """Evento de auditoría antes de persistirse."""

    tipo: str
    entidad: str
    actor: str
    organizacion_id: str
    detalle: str | None = None
    proyecto_id: str | None = None
    usuario_id: str | None = None
    correlation_id: str | None = None
    id: str = ""
    cuando: datetime | None = None

    def contenido_a_hashear(self, hash_anterior: str, momento: datetime) -> str:
        """Devuelve la cadena cuyo hash es la huella del evento.

        El instante se recibe como parámetro y no se calcula dentro: si se
        calculase aquí, la huella se calcularía sobre un microsegundo
        distinto del que se guarda en la fila, y al releer el evento la
        cadena aparecería rota sin que nadie la hubiera tocado.
        """
        return contenido_a_hashear(
            hash_anterior=hash_anterior,
            tipo=self.tipo,
            entidad=self.entidad,
            actor=self.actor,
            organizacion_id=self.organizacion_id,
            proyecto_id=self.proyecto_id,
            detalle=self.detalle,
            correlation_id=self.correlation_id,
            momento=momento,
        )


def contenido_a_hashear(
    *,
    hash_anterior: str,
    tipo: str,
    entidad: str,
    actor: str,
    organizacion_id: str,
    proyecto_id: str | None,
    detalle: str | None,
    correlation_id: str | None,
    momento: datetime,
) -> str:
    """Construye el texto cuyo SHA-256 es la huella de un evento.

    Vive como función de módulo y no como método porque la necesitan dos
    cosas distintas: el evento que se va a insertar y la fila ya
    persistida que se relee para verificar la cadena. Si el cálculo
    estuviera duplicado, una de las dos copias podría divergir y la
    verificación daría un falso negativo.
    """
    return "|".join(
        [
            hash_anterior,
            tipo,
            entidad,
            actor,
            organizacion_id,
            proyecto_id or "",
            detalle or "",
            correlation_id or "",
            momento_canonico(momento),
        ]
    )


def momento_canonico(momento: datetime) -> str:
    """Normaliza un instante a UTC y lo devuelve en formato ISO.

    SQLite no guarda la zona horaria: un instante almacenado como UTC
    vuelve sin etiqueta al releerlo. Sin esta normalización, la huella
    calculada en el alta y la recalculada en la verificación serían
    textos distintos y la cadena parecería manipulada.
    """
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    else:
        momento = momento.astimezone(timezone.utc)
    return momento.isoformat()


def calcular_hash(hash_anterior: str, contenido: str) -> str:
    """Calcula la huella SHA-256 de un evento encadenado."""
    return hashlib.sha256(contenido.encode("utf-8")).hexdigest()


async def _ultimo_hash(sesion: AsyncSession, organizacion_id: str) -> str:
    """Devuelve la huella del último evento de la organización."""
    consulta = (
        select(EventoAuditoria.hash)
        .where(EventoAuditoria.organizacion_id == uuid.UUID(str(organizacion_id)))
        .order_by(EventoAuditoria.cuando.desc(), EventoAuditoria.id.desc())
        .limit(1)
    )
    resultado = await sesion.execute(consulta)
    fila = resultado.first()
    return fila[0] if fila else HASH_INICIAL


async def registrar(sesion: AsyncSession, evento: Evento) -> EventoAuditoria:
    """Registra un evento de auditoría y devuelve la fila persistida.

    No hace ``commit``: la decisión es del llamante, para que el evento y
    el cambio de estado que lo origina compartan transacción.
    """
    if evento.tipo not in CATALOGO_EVENTOS:
        raise ErrorDeAuditoria(
            f"tipo de evento desconocido: '{evento.tipo}'. "
            f"Tipos admitidos: {', '.join(sorted(CATALOGO_EVENTOS))}"
        )
    # Los eventos pendientes se escriben antes de buscar el último hash.
    # Sin este vaciado, la consulta no vería el evento anterior —que en
    # ese momento solo está en la sesión— y el encadenado volvería al hash
    # inicial en cada evento. La cadena parecería válida, pero cada
    # evento arrancaría desde cero.
    await sesion.flush()
    hash_anterior = await _ultimo_hash(sesion, evento.organizacion_id)
    momento = evento.cuando or datetime.now(timezone.utc)
    identificador = evento.id or str(uuid.uuid4())
    contenido = evento.contenido_a_hashear(hash_anterior, momento)
    huella = calcular_hash(hash_anterior, contenido)

    fila = EventoAuditoria(
        id=uuid.UUID(identificador),
        organizacion_id=uuid.UUID(str(evento.organizacion_id)),
        proyecto_id=uuid.UUID(evento.proyecto_id) if evento.proyecto_id else None,
        tipo=evento.tipo,
        entidad=evento.entidad,
        actor=evento.actor,
        usuario_id=uuid.UUID(evento.usuario_id) if evento.usuario_id else None,
        detalle=evento.detalle,
        hash=huella,
        hash_anterior=hash_anterior,
        correlation_id=evento.correlation_id,
        cuando=momento,
    )
    sesion.add(fila)
    return fila


async def verificar_cadena(sesion: AsyncSession, organizacion_id: str) -> dict[str, Any]:
    """Recorre la cadena de auditoría de una organización y la verifica.

    Devuelve el número de eventos comprobados, si la cadena está íntegra y
    el primer punto de rotura si la hay. Es la comprobación que un auditor
    ejecutaría: **no se fía de que el registro sea correcto, lo recalcula**.
    """
    consulta = (
        select(EventoAuditoria)
        .where(EventoAuditoria.organizacion_id == uuid.UUID(str(organizacion_id)))
        .order_by(EventoAuditoria.cuando.asc(), EventoAuditoria.id.asc())
    )
    resultado = await sesion.execute(consulta)
    eventos = list(resultado.scalars())

    anterior = HASH_INICIAL
    for indice, evento in enumerate(eventos):
        if evento.hash_anterior != anterior:
            return {
                "integra": False,
                "eventos": indice,
                "motivo": (
                    f"el evento {evento.id} declara un hash anterior distinto del "
                    f"calculado: la cadena está rota en este punto"
                ),
            }
        calculado = calcular_hash(
            anterior,
            contenido_a_hashear(
                hash_anterior=anterior,
                tipo=evento.tipo,
                entidad=evento.entidad,
                actor=evento.actor,
                organizacion_id=str(evento.organizacion_id),
                proyecto_id=str(evento.proyecto_id) if evento.proyecto_id else None,
                detalle=evento.detalle,
                correlation_id=evento.correlation_id,
                momento=evento.cuando,
            ),
        )
        if calculado != evento.hash:
            return {
                "integra": False,
                "eventos": indice,
                "motivo": (
                    f"el evento {evento.id} tiene una huella que no corresponde a su "
                    f"contenido: el contenido fue alterado"
                ),
            }
        anterior = evento.hash

    return {
        "integra": True,
        "eventos": len(eventos),
        "ultimo_hash": anterior,
        "motivo": "la cadena de auditoría está íntegra",
    }
