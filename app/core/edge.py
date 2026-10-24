"""Edge Connector de Projecta PMO Intelligence — núcleo del lado servidor.

Implementa la prueba de concepto del conector descrita en
``/workspace/output/integraciones/edge-connector/Projecta-PMO-Intelligence-Edge-Connector-v2.md``.

El conector no es un agente que ejecuta: es un **sensor con contrato**.
El servidor de Projecta PMO Intelligence es la autoridad, y este módulo
es donde se aplica esa autoridad. Las cinco reglas que gobiernan todo lo
que hay aquí:

1. **La puerta del consentimiento se consulta antes de leer nada**
   (§5 del diseño). La ausencia de registro se trata como ausencia de
   consentimiento: la puerta **falla cerrada**, nunca por omisión.
2. **El uso previsto se compara con la finalidad registrada.** Un
   consentimiento que existe pero no cubre el uso cierra la puerta igual.
3. **La minimización ocurre antes de persistir.** Lo que se guarda es lo
   que se minimizó: los campos bloqueados no llegan a la base de datos,
   no se filtran a la vista.
4. **El rechazo se audita.** No modifica estado ni dato, pero el intento
   queda registrado con su motivo, como cualquier otro rechazo.
5. **La idempotencia es por contenido.** El mismo evento recibido dos
   veces se procesa una sola, y la huella permite recalcular que no ha
   cambiado.

La lista de receptores es **cerrada**: un tipo de evento fuera del
catálogo se rechaza. Y el modo de operación es ``READ_ONLY``: el conector
no escribe en ningún sistema externo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auditoria
from app.core.db.models import (
    ConsentimientoEdge,
    DispositivoEdge,
    EventoEdge,
    Organizacion,
)

__all__ = [
    "CAMPOS_BLOQUEADOS",
    "DECISION_ACEPTADO",
    "DECISION_RECHAZADO",
    "ErrorDeEdge",
    "MODOS_ADMITIDOS",
    "RECEPTORES_ADMITIDOS",
    "ResultadoIngesta",
    "consultar_consentimiento",
    "evaluar_consentimiento",
    "firma_de_evento",
    "huella_de_evento",
    "ingerir_evento",
    "minimizar",
    "registrar_dispositivo",
    "revoocar_dispositivo",
    "verificar_firma",
]

LOG = logging.getLogger("projecta.edge")

#: Modos de operación. M0 solo admite ``READ_ONLY``: la progresión
#: ``READ_ONLY → DRAFT_ONLY → WRITE_APPROVED`` es la regla operativa del
#: diseño (§7), y saltarse un escalón invalidaría la propia PoC.
MODOS_ADMITIDOS: Final[tuple[str, ...]] = ("READ_ONLY",)

#: Tipo de receptor. La lista es **cerrada**: un tipo fuera de ella se
#: rechaza en la puerta, antes de tocar nada.
RECEPTORES_ADMITIDOS: Final[dict[str, str]] = {
    "reunion_programada": "Reunión programada: metadatos y agenda.",
    "reunion_terminada": "Reunión concluida: hora de fin y participantes.",
    "ticket_actualizado": "Ticket de trabajo actualizado en el ITSM.",
    "documento_publicado": "Documento publicado en el repositorio oficial.",
    "compromiso_actualizado": "Estado de un compromiso del proyecto.",
    # Adaptador de Microsoft Teams (M1). Es un mensaje de un canal o
    # chat habilitado por la politica local, ya minimizado. La
    # INTERPRETACION del mensaje -que es un compromiso, un riesgo o una
    # dependencia- no ocurre aqui, sino en Projecta PMO Intelligence:
    # el sensor transporta y filtra, no decide.
    "mensaje_canal": "Mensaje de un canal o chat de Microsoft Teams.",
}

#: Campos que **nunca** salen del perímetro del banco. La lista cubre
#: contenido y material audiovisual: el PoC no los necesita, y su
#: presencia en el cable es exactamente lo que un análisis de protección
#: de datos marcaría.
CAMPOS_BLOQUEADOS: Final[frozenset[str]] = frozenset(
    {
        "audio",
        "audio_url",
        "grabacion",
        "grabacion_url",
        "transcripcion",
        "transcripcion_url",
        "vtt",
        "contenido",
        "texto",
        "cuerpo",
        "body",
        "mensaje",
        "adjuntos",
        "emocion",
        "emociones",
        "sentimiento",
        "estado_animo",
        "rostro",
        "voz",
        "biometrico",
    }
)

#: Regla de redacción básica: los campos que quedan no se transmiten tal
#: cual, sino enmascarados. Es la minimización de M0: reducción, no solo
#: bloqueo.
CAMPO_REDACTADO: Final[str] = "participantes"
MASCARA_REDACCION: Final[str] = "***"

#: Decisiones posibles de la ingesta.
DECISION_ACEPTADO: Final[str] = "aceptado"
DECISION_RECHAZADO: Final[str] = "rechazado"

#: Ventana de antijuego de la marca de tiempo de la firma.
ANTIJUEGO_SEGUNDOS: Final[int] = 300


class ErrorDeEdge(Exception):
    """El evento del Edge Connector no cumple el contrato."""


@dataclass(frozen=True)
class ResultadoIngesta:
    """Resultado de intentar ingerir un evento del conector."""

    decision: str
    motivo: str = ""
    evento_id: str | None = None
    huella: str = ""
    #: ``True`` si el evento ya existía y no se procesó de nuevo.
    duplicado: bool = False

    @property
    def aceptado(self) -> bool:
        return self.decision == DECISION_ACEPTADO


# ─────────────────────────── criptografía ───────────────────────────


def huella_de_evento(contenido: dict[str, Any]) -> str:
    """SHA-256 del contenido canónico de un evento.

    El contenido se serializa con las claves ordenadas y sin espacios:
    así la huella no depende del orden en que se construyó el diccionario,
    y dos sensores que envían lo mismo producen la misma huella.
    """
    canonico = json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def firma_de_evento(clave_privada: bytes, contenido: dict[str, Any]) -> str:
    """Firma el contenido canónico con Ed25519 y devuelve el hexadecimal."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    canonico = json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    firma = Ed25519PrivateKey.from_private_bytes(clave_privada).sign(
        canonico.encode("utf-8")
    )
    return firma.hex()


def verificar_firma(clave_publica_hex: str, contenido: dict[str, Any], firma_hex: str) -> bool:
    """Verifica una firma Ed25519. Devuelve ``False`` ante cualquier error.

    Una firma inválida, una clave que no es Ed25519 o un contenido que no
    se puede canonicalizar dan el mismo resultado: ``False``. Distinguir
    los casos aquí no aporta nada al que llama, y filtrar mensajes de
    error hacia fuera da información sobre por qué falló.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        clave = Ed25519PublicKey.from_public_bytes(bytes.fromhex(clave_publica_hex))
        canonico = json.dumps(
            contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        clave.verify(bytes.fromhex(firma_hex), canonico.encode("utf-8"))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def huella_de_clave(clave_publica_hex: str) -> str:
    """SHA-256 de la clave pública: la identidad estable del dispositivo."""
    return hashlib.sha256(bytes.fromhex(clave_publica_hex)).hexdigest()


# ─────────────────────────── minimización ───────────────────────────


def minimizar(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Aplica la minimización de datos y devuelve el resultado y lo quitado.

    Dos capas:

    · **Bloqueo.** Los campos de :data:`CAMPOS_BLOQUEADOS` se eliminan
      enteros, y quedan registrados sus nombres en el resultado. Saber
      qué se quitó es lo que permite auditar la minimización.
    · **Redacción.** Los campos de :data:`CAMPO_REDACTADO` se sustituyen
      por una máscara, conservando la forma pero no el contenido.

    La minimización ocurre **antes de persistir**, no al mostrar: lo que
    llega a la base de datos ya está reducido.
    """
    quitados: list[str] = []
    resultado: dict[str, Any] = {}

    for clave, valor in payload.items():
        normalizada = clave.strip().lower()
        if normalizada in CAMPOS_BLOQUEADOS:
            quitados.append(clave)
            continue
        if normalizada == CAMPO_REDACTADO and isinstance(valor, list):
            resultado[clave] = [MASCARA_REDACCION] * len(valor)
            continue
        if isinstance(valor, dict):
            interno, quitados_internos = minimizar(valor)
            resultado[clave] = interno
            quitados.extend(f"{clave}.{q}" for q in quitados_internos)
            continue
        resultado[clave] = valor

    return resultado, sorted(quitados)


# ─────────────────────────── la puerta ───────────────────────────


@dataclass
class DecisionConsentimiento:
    """Resultado de consultar la puerta del consentimiento."""

    permitido: bool
    motivo: str
    #: Identificador del registro de consentimiento que sostiene la decisión.
    consentimiento_id: str | None = None
    #: Finalidad registrada, cuando la hay.
    finalidad: str | None = None
    #: Campos que la puerta ha bloqueado por estar fuera de la finalidad.
    extra: list[str] = field(default_factory=list)


async def consultar_consentimiento(
    sesion: AsyncSession,
    organizacion_id: str,
    recurso: str,
    recurso_tipo: str,
) -> ConsentimientoEdge | None:
    """Busca un consentimiento vigente para un recurso.

    Vigente significa: no revocado y no caducado. La búsqueda es por
    recurso **y** tipo, porque un consentimiento para un ticket no cubre
    una reunión.
    """
    ahora = datetime.now(timezone.utc)
    resultado = await sesion.execute(
        select(ConsentimientoEdge).where(
            ConsentimientoEdge.organizacion_id == uuid.UUID(str(organizacion_id)),
            ConsentimientoEdge.recurso == recurso,
            ConsentimientoEdge.recurso_tipo == recurso_tipo,
            ConsentimientoEdge.revocado_en.is_(None),
        )
    )
    candidatos = list(resultado.scalars())
    for candidato in candidatos:
        if candidato.expira_en is None:
            return candidato
        expira = candidato.expira_en
        if expira.tzinfo is None:
            expira = expira.replace(tzinfo=timezone.utc)
        if expira > ahora:
            return candidato
    return None


def evaluar_consentimiento(
    consentimiento: ConsentimientoEdge | None,
    *,
    uso_previsto: str,
    destinatarios_previstos: list[str] | None = None,
) -> DecisionConsentimiento:
    """La puerta: decide si el uso previsto está cubierto.

    Falla cerrada. Sin registro, no hay puerta abierta. Y con registro,
    la **finalidad** y los **destinatarios** tienen que cubrir el uso: un
    consentimiento que existe pero no cubre lo que se va a hacer también
    cierra la puerta.
    """
    if consentimiento is None:
        return DecisionConsentimiento(
            permitido=False,
            motivo=(
                "No hay consentimiento registrado para este recurso. "
                "La ausencia de registro se trata como ausencia de consentimiento."
            ),
        )

    if consentimiento.finalidad.strip().lower() != uso_previsto.strip().lower():
        return DecisionConsentimiento(
            permitido=False,
            motivo=(
                "El uso previsto no coincide con la finalidad registrada en el "
                "consentimiento. Informar de que se va a leer no es suficiente: "
                "cualquier uso distinto del consentido requiere una nueva base legal."
            ),
            consentimiento_id=str(consentimiento.id),
            finalidad=consentimiento.finalidad,
        )

    if destinatarios_previstos:
        registrados = {d.strip().lower() for d in (consentimiento.destinatarios or [])}
        if registrados:
            no_previstos = [
                d for d in destinatarios_previstos if d.strip().lower() not in registrados
            ]
            if no_previstos:
                return DecisionConsentimiento(
                    permitido=False,
                    motivo=(
                        "Hay destinatarios previstos que no constan en el "
                        "consentimiento registrado."
                    ),
                    consentimiento_id=str(consentimiento.id),
                    finalidad=consentimiento.finalidad,
                    extra=no_previstos,
                )

    return DecisionConsentimiento(
        permitido=True,
        motivo="El uso previsto está cubierto por el consentimiento registrado.",
        consentimiento_id=str(consentimiento.id),
        finalidad=consentimiento.finalidad,
    )


# ─────────────────────────── dispositivos ───────────────────────────


async def registrar_dispositivo(
    sesion: AsyncSession,
    *,
    organizacion_id: str,
    nombre: str,
    clave_publica_hex: str,
    sistemas: list[str] | None = None,
    modo: str = "READ_ONLY",
    version: str = "M0",
    credencial_expira_en: datetime | None = None,
) -> DispositivoEdge:
    """Registra un dispositivo del Edge Connector.

    La clave privada **nunca** llega aquí: el servidor solo guarda la
    pública. Es la diferencia entre un registro de identidad y un
    depósito de credenciales.
    """
    if modo not in MODOS_ADMITIDOS:
        raise ErrorDeEdge(
            f"modo de operación no admitido: '{modo}'. "
            f"La prueba de concepto solo admite {', '.join(MODOS_ADMITIDOS)}."
        )
    try:
        bytes.fromhex(clave_publica_hex)
    except ValueError as exc:
        raise ErrorDeEdge("la clave pública no es hexadecimal válido") from exc

    huella = huella_de_clave(clave_publica_hex)
    resultado = await sesion.execute(
        select(DispositivoEdge).where(DispositivoEdge.huella_clave == huella)
    )
    existente = resultado.scalar_one_or_none()
    if existente is not None:
        raise ErrorDeEdge(
            f"ya existe un dispositivo registrado con esa clave: '{existente.nombre}'"
        )

    dispositivo = DispositivoEdge(
        organizacion_id=uuid.UUID(str(organizacion_id)),
        nombre=nombre,
        huella_clave=huella,
        clave_publica=clave_publica_hex,
        version=version,
        estado="activo",
        modo=modo,
        sistemas=list(sistemas or []),
        credencial_expira_en=credencial_expira_en,
    )
    sesion.add(dispositivo)
    await sesion.flush()

    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="EDGE_DISPOSITIVO_REGISTRADO",
            entidad=f"dispositivo:{dispositivo.id}",
            actor=nombre,
            organizacion_id=str(organizacion_id),
            detalle=(
                f"Dispositivo '{nombre}' registrado en modo {modo}, "
                f"huella de clave {huella[:12]}…"
            ),
        ),
    )
    return dispositivo


async def revoocar_dispositivo(
    sesion: AsyncSession, dispositivo: DispositivoEdge, *, motivo: str
) -> None:
    """Revoca un dispositivo. A partir de aquí, no puede enviar nada."""
    dispositivo.estado = "revocado"
    dispositivo.revocado_en = datetime.now(timezone.utc)
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="EDGE_DISPOSITIVO_REVOCADO",
            entidad=f"dispositivo:{dispositivo.id}",
            actor=dispositivo.nombre,
            organizacion_id=str(dispositivo.organizacion_id),
            detalle=f"Dispositivo '{dispositivo.nombre}' revocado. Motivo: {motivo}",
        ),
    )


# ─────────────────────────── ingesta ───────────────────────────


async def ingerir_evento(
    sesion: AsyncSession,
    *,
    organizacion_id: str,
    huella_clave: str,
    tipo: str,
    contenido: dict[str, Any],
    firma: str,
    marca_tiempo: str,
    uso_previsto: str,
    recurso: str,
    recurso_tipo: str,
    proyecto_id: str | None = None,
    destinatarios_previstos: list[str] | None = None,
    correlation_id: str | None = None,
) -> ResultadoIngesta:
    """Ingesta un evento firmado del Edge Connector.

    El orden de las comprobaciones **no es indiferente**. La firma se
    verifica primero, porque comprobar el consentimiento de un evento
    cuya firma no es válida sería validar datos que nadie ha acredita.
    Después, la puerta del consentimiento. Después, la minimización. Y
    solo entonces se persiste.
    """
    ahora = datetime.now(timezone.utc)

    # 1. ¿El tipo está en la lista cerrada de receptores?
    if tipo not in RECEPTORES_ADMITIDOS:
        return await _rechazar(
            sesion,
            organizacion_id=organizacion_id,
            tipo=tipo,
            contenido=contenido,
            motivo=(
                f"Tipo de receptor no admitido: '{tipo}'. "
                f"La lista de receptores es cerrada."
            ),
            correlation_id=correlation_id,
        )

    # 2. ¿Existe el dispositivo y está activo?
    resultado = await sesion.execute(
        select(DispositivoEdge).where(
            DispositivoEdge.huella_clave == huella_clave,
            DispositivoEdge.organizacion_id == uuid.UUID(str(organizacion_id)),
        )
    )
    dispositivo = resultado.scalar_one_or_none()
    if dispositivo is None:
        return await _rechazar(
            sesion,
            organizacion_id=organizacion_id,
            tipo=tipo,
            contenido=contenido,
            motivo="No hay ningún dispositivo registrado con esa huella de clave.",
            correlation_id=correlation_id,
        )
    if dispositivo.estado != "activo":
        return await _rechazar(
            sesion,
            organizacion_id=organizacion_id,
            tipo=tipo,
            contenido=contenido,
            motivo=f"El dispositivo '{dispositivo.nombre}' está revocado.",
            correlation_id=correlation_id,
        )

    # 3. ¿La firma es válida?
    if not verificar_firma(dispositivo.clave_publica, contenido, firma):
        return await _rechazar(
            sesion,
            organizacion_id=organizacion_id,
            tipo=tipo,
            contenido=contenido,
            motivo="La firma del evento no es válida para la clave del dispositivo.",
            correlation_id=correlation_id,
        )

    # 4. ¿La marca de tiempo está dentro del antijuego?
    try:
        momento = datetime.fromisoformat(marca_tiempo)
    except ValueError:
        return await _rechazar(
            sesion,
            organizacion_id=organizacion_id,
            tipo=tipo,
            contenido=contenido,
            motivo="La marca de tiempo del evento no es una fecha ISO válida.",
            correlation_id=correlation_id,
        )
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    if abs((ahora - momento).total_seconds()) > ANTIJUEGO_SEGUNDOS:
        return await _rechazar(
            sesion,
            organizacion_id=organizacion_id,
            tipo=tipo,
            contenido=contenido,
            motivo=(
                "La marca de tiempo del evento está fuera de la ventana de "
                f"antijuego de {ANTIJUEGO_SEGUNDOS} segundos."
            ),
            correlation_id=correlation_id,
        )

    # 5. **La puerta del consentimiento**, antes de leer nada.
    consentimiento = await consultar_consentimiento(
        sesion, organizacion_id, recurso, recurso_tipo
    )
    decision = evaluar_consentimiento(
        consentimiento,
        uso_previsto=uso_previsto,
        destinatarios_previstos=destinatarios_previstos,
    )
    if not decision.permitido:
        return await _rechazar(
            sesion,
            organizacion_id=organizacion_id,
            tipo=tipo,
            contenido=contenido,
            motivo=decision.motivo,
            correlation_id=correlation_id,
            proyecto_id=proyecto_id,
        )

    # 6. Minimización, antes de persistir.
    #
    # Se minimiza el **payload interior**, no el sobre completo. El sobre
    # lleva los metadatos del evento (tipo, recurso, marca de tiempo) que
    # ya viven en columnas propias de `eventos_edge`: si se guardara el
    # sobre entero, lo persistido tendría un `payload.payload` que
    # duplicaría lo que ya está en la fila y confunde a quien lo lea.
    # Minimizar el interior es además lo que hace el inventario real: lo
    # que se guarda es **la señal**, no su envoltorio.
    payload_crudo = contenido.get("payload")
    if not isinstance(payload_crudo, dict):
        payload_crudo = {}
    payload, quitados = minimizar(payload_crudo)
    if quitados:
        LOG.info(
            "minimización aplicada al evento %s: campos bloqueados %s", tipo, quitados
        )

    # 7. Idempotencia por contenido.
    huella = huella_de_evento(
        {"tipo": tipo, "payload": payload, "recurso": recurso, "proyecto_id": proyecto_id}
    )
    existente = await sesion.execute(
        select(EventoEdge).where(
            EventoEdge.dispositivo_id == dispositivo.id,
            EventoEdge.huella == huella,
        )
    )
    duplicado = existente.scalar_one_or_none()
    if duplicado is not None:
        return ResultadoIngesta(
            decision=duplicado.decision,
            motivo="El evento ya se había recibido: no se procesa de nuevo.",
            evento_id=str(duplicado.id),
            huella=huella,
            duplicado=True,
        )

    # 8. Persistencia y auditoría.
    evento = EventoEdge(
        organizacion_id=uuid.UUID(str(organizacion_id)),
        dispositivo_id=dispositivo.id,
        proyecto_id=uuid.UUID(proyecto_id) if proyecto_id else None,
        tipo=tipo,
        payload=payload,
        huella=huella,
        decision=DECISION_ACEPTADO,
        motivo="",
        correlation_id=correlation_id,
    )
    sesion.add(evento)
    dispositivo.ultimo_latido_en = ahora
    await sesion.flush()

    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="EDGE_EVENTO_ACEPTADO",
            entidad=f"evento_edge:{evento.id}",
            actor=dispositivo.nombre,
            organizacion_id=str(organizacion_id),
            proyecto_id=proyecto_id,
            correlation_id=correlation_id,
            detalle=(
                f"Evento '{tipo}' del dispositivo '{dispositivo.nombre}' aceptado. "
                f"Recurso '{recurso}'. Finalidad: {decision.finalidad}. "
                f"Campos minimizados: {len(quitados)}."
            ),
        ),
    )
    return ResultadoIngesta(
        decision=DECISION_ACEPTADO,
        motivo=decision.motivo,
        evento_id=str(evento.id),
        huella=huella,
    )


async def _rechazar(
    sesion: AsyncSession,
    *,
    organizacion_id: str,
    tipo: str,
    contenido: dict[str, Any],
    motivo: str,
    correlation_id: str | None = None,
    proyecto_id: str | None = None,
) -> ResultadoIngesta:
    """Registra un rechazo: no modifica datos, pero el intento queda."""
    await auditoria.registrar(
        sesion,
        auditoria.Evento(
            tipo="EDGE_EVENTO_RECHAZADO",
            entidad=f"evento_edge:{tipo}",
            actor="edge-connector",
            organizacion_id=str(organizacion_id),
            proyecto_id=proyecto_id,
            correlation_id=correlation_id,
            detalle=f"Evento '{tipo}' rechazado. Motivo: {motivo}",
        ),
    )
    return ResultadoIngesta(decision=DECISION_RECHAZADO, motivo=motivo)


# ─────────────────────────── salud ───────────────────────────


def estado_salud(dispositivo: DispositivoEdge, *, ahora: datetime | None = None) -> dict[str, Any]:
    """Estado de salud de un dispositivo, con las caducidades anticipadas.

    El health agent no solo informa de si el dispositivo está vivo: avisa
    **antes** de que caduque la credencial. Un aviso de caducidad que
    llega el día del caducidad no sirve de nada.
    """
    momento = ahora or datetime.now(timezone.utc)
    advertencias: list[str] = []

    if dispositivo.estado != "activo":
        advertencias.append(f"El dispositivo está {dispositivo.estado}.")

    if dispositivo.credencial_expira_en is not None:
        caducidad = dispositivo.credencial_expira_en
        if caducidad.tzinfo is None:
            caducidad = caducidad.replace(tzinfo=timezone.utc)
        dias = (caducidad - momento).days
        if dias < 0:
            advertencias.append(f"La credencial caducó hace {-dias} día(s).")
        elif dias <= 7:
            advertencias.append(
                f"La credencial caduca en {dias} día(s): hay que renovarla."
            )

    if dispositivo.ultimo_latido_en is None:
        advertencias.append("El dispositivo no ha enviado ningún latido.")
    else:
        latido = dispositivo.ultimo_latido_en
        if latido.tzinfo is None:
            latido = latido.replace(tzinfo=timezone.utc)
        minutos = (momento - latido).total_seconds() / 60
        if minutos > 60:
            advertencias.append(
                f"El último latido es de hace {minutos:.0f} minutos."
            )

    return {
        "dispositivo": dispositivo.nombre,
        "estado": dispositivo.estado,
        "modo": dispositivo.modo,
        "sano": not advertencias,
        "advertencias": advertencias,
    }
