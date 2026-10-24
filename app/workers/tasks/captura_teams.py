"""Captura de reuniones de Teams en Projecta PMO Intelligence.

Trae de Microsoft 365 el **audio y el texto** de las reuniones de Teams
y los deja en Projecta PMO Intelligence: la transcripción con su
hablante, los participantes y, si está configurado, la grabación.

Por qué Graph y no el micrófono del equipo
--------------------------------------------

Porque en Teams la grabación **ya está hecha**. Cuando alguien activa la
grabación, Microsoft la guarda, la transcribe y la atribuye a cada
hablante. Lo que hace este módulo es **traerla**, no capturarla.

Eso cambia tres cosas, y las tres importan:

* No hay que capturar el micrófono ni el audio del sistema. El equipo
  del usuario no abre el micrófono a una aplicación unbekannt.
* La transcripción **sabe quién habló**. Un bucle de audio del sistema
  mezcla las voces y no lo sabe; reconstruirlo exige separación de
  hablantes sobre audio mezclado, que es caro y poco fiable.
* La autorización la da el **administrador del tenant**, que es quien
  activa la transcripción en la reunión. Si está desactivada, Graph
  responde 403 y no hay forma de sortearlo desde aquí.

El barrido
----------

La tarea no espera a que le avise nadie: cada ejecución lista las
transcripciones del organizador en una **ventana temporal** y se lleva
todas las que no se había llevado. La ventana tiene que ser más ancha
que el intervalo entre ejecuciones, o quedarían reuniones en el hueco.

La captura es **idempotente**: la restricción de unicidad sobre
``(organizador, reunion_id)`` hace que reejecutarla no duplique nada.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import pathlib
import uuid
from typing import Any

from celery import Task

from app.core import auditoria
from app.core.celery_app import celery_app
from app.core.db.session import AsyncSessionLocal
from app.core.edge_teams import token_graph
from app.core.graph_teams import (
    ClienteReuniones,
    ErrorDeGraph,
    ErrorDeTranscripcionDeshabilitada,
    ResultadoDeReunion,
)

LOG = logging.getLogger("projecta.captura.teams")

#: Nombre exacto de la tarea. Entra en la cola `integraciones`, que el
#: contrato del proyecto ya define y ya enruta.
class ErrorDeConfiguracion(Exception):
    """Error de configuración de la captura.

    No se reintenta: si falta el certificado o la organización no
    existe, reintentar no lo va a arreglar. Se distingue del error
    genérico para que la tarea se marque como **fallida** y no como
    correcta.
    """


TAREA_CAPTURA_TEAMS = "projecta.integraciones.teams.captura"


class ErrorDeCaptura(Exception):
    """La captura de reuniones no se pudo completar."""


def _configuracion() -> Any:
    """Devuelve la configuración de la aplicación."""
    from app.core.config import settings

    return settings


def _token() -> str:
    """Consigue un token de Graph con la credencial de la aplicación."""
    config = _configuracion()
    if not config.graph_tenant_id or not config.graph_client_id:
        raise ErrorDeCaptura(
            "Microsoft Graph no está configurado: faltan GRAPH_TENANT_ID y "
            "GRAPH_CLIENT_ID."
        )
    if not config.graph_certificado or not config.graph_clave_privada:
        raise ErrorDeCaptura(
            "Microsoft Graph no está configurado: faltan GRAPH_CERTIFICADO y "
            "GRAPH_CLAVE_PRIVADA. La clave se lee del vault, nunca del código."
        )
    return token_graph(
        tenant_id=config.graph_tenant_id,
        client_id=config.graph_client_id,
        certificado=pathlib.Path(config.graph_certificado).read_text(encoding="utf-8"),
        clave_privada_pem=pathlib.Path(config.graph_clave_privada).read_text(encoding="utf-8"),
    )


def cliente_teams() -> ClienteReuniones:
    """Construye el cliente de reuniones con el token de la aplicación.

    Cuando la base de Graph no es la de Microsoft, no se pide token: es
    un doble de prueba, y contra un doble el token no se valida. Es lo
    que permite hacer las pruebas funcionales del ciclo completo sin
    tenant, sin certificado y sin pedir nada a nadie.
    """
    config = _configuracion()
    base = config.graph_base_url
    if "graph.microsoft.com" not in base:
        LOG.info("se usa un doble de Graph en %s: no se pide token", base)
        return ClienteReuniones("token-de-doble", base=base)
    return ClienteReuniones(_token(), base=base)


def huella_de_transcripcion(resultado: ResultadoDeReunion) -> str:
    """SHA-256 del contenido canónico de la transcripción.

    Es la misma idea que la huella del Edge Connector: sirve para saber
    si el contenido cambió y para no reprocesar lo mismo.
    """
    canonico = json.dumps(
        [
            {
                "hablante": segmento.hablante,
                "inicio": round(segmento.inicio, 3),
                "fin": round(segmento.fin, 3),
                "texto": segmento.texto,
            }
            for segmento in resultado.segmentos
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


async def _guardar_audio(
    sesion: Any, resultado: ResultadoDeReunion, organizacion_id: uuid.UUID
) -> tuple[str | None, int]:
    """Guarda el audio y devuelve su clave y su tamaño.

    El camino normal es el almacenamiento de objetos. Si no está
    disponible, el audio se deja en el directorio local de capturas: es
    preferible tener el archivo en el disco a no tenerlo, y la captura
    queda registrada como incompleta en lugar de fallar entera.
    """
    if resultado.audio is None:
        return None, 0

    config = _configuracion()
    tamano = len(resultado.audio)
    try:
        from app.core.storage import almacenamiento

        almacen = almacenamiento()
        carga = almacen.subir(
            resultado.audio,
            nombre=f"{resultado.reunion_id}.{resultado.formato_audio or 'mp4'}",
            content_type="video/mp4",
            extension=resultado.formato_audio or "mp4",
        )
        return carga.clave_objeto, carga.tamano_bytes
    except Exception as error:  # noqa: BLE001 - el respaldo es lo importante
        LOG.warning("el almacenamiento de objetos no está disponible: %s", error)
        directorio = pathlib.Path(config.teams_directorio_capturas)
        try:
            directorio.mkdir(parents=True, exist_ok=True)
            destino = directorio / f"{resultado.reunion_id}.{resultado.formato_audio or 'mp4'}"
            destino.write_bytes(resultado.audio)
        except OSError as error_local:
            LOG.error("no se pudo guardar el audio en disco: %s", error_local)
            return None, 0
        return f"local://{destino}", tamano


async def capturar_una_vez(
    organizacion_id: str,
    *,
    proyecto_id: str | None = None,
    horas: int | None = None,
    organizador: str | None = None,
    reunion_id: str | None = None,
) -> dict[str, Any]:
    """Captura las reuniones del organizador. Una pasada.

    Args:
        organizacion_id: organización a la que se atribuyen las capturas.
        proyecto_id: proyecto al que se asocian.
        horas: ventana hacia atrás, en horas. Por defecto, la de la
            configuración.
        organizador: UPN del organizador. Por defecto, el de la
            configuración.
        reunion_id: captura una reunión concreta en vez de barrer.
    """
    config = _configuracion()
    ventana = horas if horas is not None else config.teams_captura_ventana_horas
    cuenta = organizador or config.teams_organizador
    if not cuenta:
        raise ErrorDeCaptura(
            "no hay ningún organizador configurado: falta TEAMS_ORGANIZADOR. "
            "Es el UPN de la cuenta cuyas reuniones se capturan."
        )

    cliente = cliente_teams()
    ahora = dt.datetime.now(dt.timezone.utc)
    try:

        # Las reuniones concretas van directas, sin barrido.
        if reunion_id:
            objetivos = [reunion_id]
        else:
            desde = (ahora - dt.timedelta(hours=ventana)).strftime("%Y-%m-%dT%H:%M:%SZ")
            hasta = ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                transcripciones = cliente.listar_transcripciones(
                    cuenta, desde=desde, hasta=hasta
                )
            except ErrorDeTranscripcionDeshabilitada:
                raise
            objetivos = []
            vistos: set[str] = set()
            for transcripcion in transcripciones:
                identificador = transcripcion.get("meetingId", "")
                if identificador and identificador not in vistos:
                    vistos.add(identificador)
                    objetivos.append(identificador)

        capturadas = 0
        ya_existentes = 0
        fallidas = 0
        detalles: list[dict[str, Any]] = []

        for identificador in objetivos:
            try:
                resultado = cliente.capturar(
                    cuenta, identificador, con_audio=config.teams_captura_con_audio
                )
            except ErrorDeTranscripcionDeshabilitada:
                # Este error no es de esta reunión: es de **todas**. Si el
                # administrador del tenant ha desactivado las transcripciones,
                # seguir con la siguiente reunión solo produce más errores
                # idénticos. Se propaga para que se entere quien tiene que
                # enterarse.
                raise
            except ErrorDeGraph as error:
                fallidas += 1
                detalles.append({"reunion_id": identificador, "error": str(error)[:200]})
                LOG.warning("no se pudo capturar la reunión %s: %s", identificador, error)
                # Una reunión que no se puede capturar es exactamente el caso
                # que un auditor necesita ver. El tipo de evento estaba
                # declarado en el catálogo y no se emitía nunca.
                #
                # Aquí no hay sesión abierta: la que usa `_persistir` es
                # suya. Se abre una propia, corta, solo para el evento.
                try:
                    async with AsyncSessionLocal() as sesion_auditoria:
                        await _auditar(
                            sesion_auditoria,
                            uuid.UUID(str(organizacion_id)),
                            "REUNION_NO_CAPTURABLE",
                            identificador,
                            f"La reunión {identificador} no se pudo capturar: {error}"[:500],
                            proyecto_id,
                        )
                        await sesion_auditoria.commit()
                except Exception as error_auditoria:  # noqa: BLE001
                    # Si ni el evento de la reunión que falló se puede
                    # escribir, no se debe tumbar el resto del barrido: las
                    # demás reuniones pueden capturarse. Se avisa, y la tarea
                    # continúa.
                    LOG.error(
                        "no se pudo registrar el evento de auditoría de la reunión "
                        "no capturable %s: %s",
                        identificador,
                        error_auditoria,
                    )
                continue

            guardada = await _persistir(
                organizacion_id,
                resultado,
                proyecto_id=proyecto_id,
                cuenta=cuenta,
            )
            if guardada:
                capturadas += 1
            else:
                ya_existentes += 1
            detalles.append(
                {
                    "reunion_id": identificador,
                    "segmentos": len(resultado.segmentos),
                    "participantes": len(resultado.participantes),
                    "guardada": guardada,
                }
            )

        return {
            "organizador": cuenta,
            "reuniones_consideradas": len(objetivos),
            "capturadas": capturadas,
            "ya_existentes": ya_existentes,
            "fallidas": fallidas,
            "detalles": detalles,
        }
    finally:
        # El cliente se cierra siempre, también si el barrido
        # falla. Su pool de conexiones y sus sockets TLS se
        # quedan abiertos si no, y en un worker de larga vida
        # eso acaba en agotamiento de descriptores de fichero.
        cliente.close()



def _a_datetime(valor: str) -> dt.datetime | None:
    """Convierte una fecha ISO 8601 de Graph en un datetime.

    Graph devuelve las fechas en UTC con el formato
    ``2026-10-23T09:00:00Z``. La ``Z`` final es un desfase cero, no
    una zona horaria, así que hay que ponerla explícitamente: sin
    eso, el valor se interpretaría como hora local.
    """
    if not valor:
        return None
    try:
        return dt.datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _persistir(
    organizacion_id: str,
    resultado: ResultadoDeReunion,
    *,
    proyecto_id: str | None,
    cuenta: str,
) -> bool:
    """Guarda la reunión. Devuelve `True` si se guardó, `False` si ya estaba.

    La idempotencia la da la base de datos: si ya hay una fila con el
    mismo ``(organizador, reunion_id)``, no se inserta otra. No se
    comprueba antes con un `SELECT`, porque entre la comprobación y la
    inserción hay una ventana; que lo resuelva la restricción es más
    seguro.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.core.db.models import ReunionCapturada

    org = uuid.UUID(str(organizacion_id))
    huella = huella_de_transcripcion(resultado)

    async with AsyncSessionLocal() as sesion:
        # La organización tiene que existir. Se comprueba aquí, y no se
        # deja que lo diga la base de datos al insertar: un error de
        # clave foránea con el volcado de la sentencia no dice qué
        # hacer, y una organización que no existe es un problema de
        # configuración, no un fallo de la captura.
        from app.core.db.models import Organizacion

        organizacion = await sesion.execute(
            select(Organizacion.id).where(Organizacion.id == org)
        )
        if organizacion.scalar_one_or_none() is None:
            raise ErrorDeCaptura(
                f"la organización {organizacion_id} no existe. La captura se "
                f"atribuye a una organización real: revisa el identificador."
            )

        existente = await sesion.execute(
            select(ReunionCapturada).where(
                ReunionCapturada.organizador == cuenta,
                ReunionCapturada.reunion_id == resultado.reunion_id,
            )
        )
        fila = existente.scalars().first()
        if fila is not None:
            await _auditar(
                sesion,
                org,
                "REUNION_YA_CAPTURADA",
                resultado.reunion_id,
                f"La reunión {resultado.reunion_id} ya estaba capturada.",
                proyecto_id,
            )
            await sesion.commit()
            return False

        clave_audio, tamano = await _guardar_audio(sesion, resultado, org)

        fila = ReunionCapturada(
            organizacion_id=org,
            proyecto_id=uuid.UUID(str(proyecto_id)) if proyecto_id else None,
            organizador=cuenta,
            reunion_id=resultado.reunion_id,
            asunto=resultado.asunto or "",
            transcripcion_id=resultado.transcripcion_id,
            segmentos=[s.como_dict() for s in resultado.segmentos],
            participantes=resultado.participantes,
            audio_objeto=clave_audio,
            formato_audio=resultado.formato_audio,
            audio_bytes=tamano,
            segmentos_totales=len(resultado.segmentos),
            huella=huella,
            inicio=_a_datetime(resultado.inicio),
            fin=_a_datetime(resultado.fin),
        )
        sesion.add(fila)
        try:
            # `flush` inserta sin confirmar. La restricción de unicidad
            # salta aquí, y así la fila y su evento de auditoría van en
            # la **misma** transacción: o se guardan los dos, o no se
            # guarda ninguno. Con un `commit` antes de auditar, una
            # auditoría fallida dejaba la fila guardada y sin rastro.
            await sesion.flush()
        except IntegrityError as error:
            await sesion.rollback()
            # No todas las violaciones de integridad son la misma cosa.
            # La de unicidad (`23505`) significa que otra ejecución
            # guardó la reunión antes, y eso no es un error. La de clave
            # foránea (`23503`) significa que la organización o el
            # proyecto no existen, y **sí** es un error: si se traga
            # aquí, la captura se reporta como «ya existía» cuando en
            # realidad no se ha guardado nada, y el fallo pasa
            # desapercibido. Por eso se distinguen por su código.
            codigo = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            if codigo == "23505":
                # Otra ejecución la guardó primero. No es un error.
                return False
            raise ErrorDeCaptura(
                f"no se pudo guardar la reunión {resultado.reunion_id}: {error.orig}"
            ) from error

        await _auditar(
            sesion,
            org,
            "REUNION_CAPTURADA",
            resultado.reunion_id,
            (
                f"Reunión {resultado.reunion_id} capturada: "
                f"{len(resultado.segmentos)} segmentos, "
                f"{len(resultado.participantes)} participantes, "
                f"{tamano} bytes de audio."
            ),
            proyecto_id,
            entidad_id=str(fila.id),
        )
        await sesion.commit()
        return True


async def _auditar(
    sesion: Any,
    organizacion_id: uuid.UUID,
    tipo: str,
    entidad: str,
    detalle: str,
    proyecto_id: str | None,
    *,
    entidad_id: str | None = None,
) -> None:
    """Registra el evento en la cadena de auditoría.

    **El fallo de la auditoría se propaga.** Se puede escribir en
    la cadena lo que se puede, y lo que no se puede, no se
    escribe en ninguna parte: ni la fila, ni el evento.
    """
    try:
        await auditoria.registrar(
            sesion,
            auditoria.Evento(
                tipo=tipo,
                entidad=entidad,
                id=entidad_id or "",
                actor="worker:projecta.integraciones.teams",
                organizacion_id=str(organizacion_id),
                detalle=detalle,
                proyecto_id=proyecto_id,
            ),
        )
    except Exception as error:
        # Antes este fallo se tragaba y la fila se guardaba **sin
        # rastro**: la captura de audio y transcripción quedaba
        # persistida, la API la devolvía, y no existía ninguna traza de
        # que se había capturado. Un fallo de auditoría se convertía en un
        # éxito silencioso, que es justo lo contrario de lo que este
        # producto vende.
        #
        # La llamada ocurre **antes** del `commit`, así que propagar el
        # fallo hace que la transacción se revierta entera: o se guardan
        # la fila y su evento, o no se guarda ninguno.
        LOG.error("no se pudo registrar el evento de auditoría %s: %s", tipo, error)
        raise


@celery_app.task(
    bind=True,
    name=TAREA_CAPTURA_TEAMS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def capturar_reuniones(self: Task, organizacion_id: str, **kwargs: Any) -> dict[str, Any]:
    """Tarea de Celery que barre las reuniones del organizador y las guarda.

    Los errores de **configuración** no se reintentan: si falta el
    certificado, reintentar no lo va a arreglar, y solo retrasa el
    aviso. Los errores **transitorios** de Graph sí.
    """
    from app.core.asyncio_bridge import ejecutar

    try:
        return ejecutar(capturar_una_vez(organizacion_id, **kwargs))
    except ErrorDeCaptura as error:
        # Un error de configuración no se reintenta: reintentar no lo
        # arregla, y un reintento infinito solo retrasa el aviso.
        #
        # **No se devuelve un resultado normal.** Devolverlo hacía que
        # Celery marcara la tarea como SUCCESS, y quien vigile el
        # resultado —un panel, una alerta, un contador— veía un éxito
        # donde no hubo captura. Se lanza una excepción que no se
        # reintenta, de modo que la tarea figura como fallida y el motivo
        # llega a quien tiene que enterarse.
        LOG.error("Captura de reuniones no ejecutada: %s", error)
        raise ErrorDeConfiguracion(str(error)) from error
    except Exception as error:  # noqa: BLE001
        LOG.warning("Captura de reuniones fallida: %s", error)
        raise self.retry(exc=error)
