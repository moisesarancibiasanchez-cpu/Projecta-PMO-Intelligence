"""Ingesta de Microsoft Teams en Projecta PMO Intelligence.

Esta tarea es el **Edge Connector en la Variante A**: el adaptador de
Teams corre **dentro del worker**, dentro de la red del banco, y no es
un producto aparte.

Por qué una tarea y no un agente desplegado
--------------------------------------------

La Variante B —un conector desplegado en la red del banco que habla con
Projecta PMO Intelligence por HTTPS— tiene sentido cuando el banco
admite una aplicación cliente en su red. Cuando la política de seguridad
y las restricciones de firewall **no** lo admiten, esa Variante queda
descartada, y con ella se va el problema de fondo: el adaptador pasa a
ser un módulo de la aplicación, con su credencial, su despliegue y su
ciclo de revisión.

En esta Variante:

* No hay proceso nuevo, ni servicio nuevo, ni puerto, ni segunda
  aprobación de TI.
* La credencial de Microsoft es la de la propia aplicación, ya
  aprobada.
* La minimización ocurre en el mismo proceso que escribe en la base de
  datos.
* La única conexión saliente es a Microsoft Graph, y solo de salida.

La firma Ed25519 **se mantiene**: en esta Variante no acredita que el
evento viene de lejos, porque no hay distancia, pero sí acredita la
**procedencia auditada**, y —lo que es más importante— hace que el
camino de validación del servidor (`app.core.edge.ingerir_evento`) **no
cambie ni una línea** de lo que ya está probado y auditado.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

from celery import Task

from app.core import edge
from app.core.celery_app import celery_app
from app.core.db.session import AsyncSessionLocal
from app.core.edge_teams import AdaptadorTeams, token_graph

LOG = logging.getLogger("projecta.edge.teams")

#: Nombre exacto de la tarea. Entra en la cola `integraciones`, que el
#: contrato del proyecto ya define y ya enruta.
TAREA_INGESTA_TEAMS = "projecta.integraciones.teams.ingesta"


class ErrorDeIngesta(Exception):
    """La ingesta de Teams no se pudo completar."""


def _configuracion() -> Any:
    """Devuelve la configuración de la aplicación."""
    from app.core.config import settings

    return settings


def _dispositivo_organizacion(organizacion_id: str) -> Any:
    """Busca el dispositivo del Edge Connector de esta organización.

    En la Variante A hay **un solo dispositivo**: la propia aplicación
    hace de sensor de Teams, y su clave pública está registrada en la
    tabla `dispositivos_edge`. No hay un registro por canal ni por
    proyecto: el modelo de autorización lo da el consentimiento, que sí
    es por recurso.
    """
    from sqlalchemy import select

    from app.core.db.models import DispositivoEdge

    async def _buscar() -> Any:
        async with AsyncSessionLocal() as sesion:
            resultado = await sesion.execute(
                select(DispositivoEdge).where(
                    DispositivoEdge.organizacion_id
                    == __import__("uuid").UUID(str(organizacion_id)),
                    DispositivoEdge.estado == "activo",
                )
            )
            return resultado.scalars().first()

    return _buscar()


async def _ingestar_una_vez(
    organizacion_id: str,
    *,
    chat_id: str | None = None,
    canal_id: str | None = None,
    equipo_id: str | None = None,
    proyecto_id: str | None = None,
) -> dict[str, Any]:
    """Lee de Teams, minimiza, firma e ingiere. Una pasada."""
    config = _configuracion()

    if not config.graph_tenant_id or not config.graph_client_id:
        raise ErrorDeIngesta(
            "Microsoft Graph no está configurado: faltan GRAPH_TENANT_ID y "
            "GRAPH_CLIENT_ID. Sin ellos no hay integración con Teams."
        )
    if not config.graph_certificado or not config.graph_clave_privada:
        raise ErrorDeIngesta(
            "Microsoft Graph no está configurado: faltan GRAPH_CERTIFICADO y "
            "GRAPH_CLAVE_PRIVADA. La clave se lee del vault, nunca del código."
        )

    # El certificado y la clave se leen del disco en el momento de usarlos,
    # no se cachean en memoria más de lo que dura la llamada.
    token = token_graph(
        tenant_id=config.graph_tenant_id,
        client_id=config.graph_client_id,
        certificado=pathlib.Path(config.graph_certificado).read_text(encoding="utf-8"),
        clave_privada_pem=pathlib.Path(config.graph_clave_privada).read_text(encoding="utf-8"),
    )

    # La clave Ed25519 del dispositivo.
    if not config.edge_clave_dispositivo:
        raise ErrorDeIngesta(
            "el dispositivo del Edge Connector no tiene clave: falta "
            "EDGE_CLAVE_DISPOSITIVO. Sin ella los eventos no se pueden firmar."
        )
    clave_privada = pathlib.Path(config.edge_clave_dispositivo).read_bytes()

    adaptador = AdaptadorTeams(
        token=token,
        estado=pathlib.Path(config.edge_estado),
        huella_clave=config.edge_huella_clave,
        clave_privada=clave_privada,
        canal_permitido=config.teams_canal_permitido or None,
        campos_extra={"proyecto_id": proyecto_id} if proyecto_id else {},
    )

    if chat_id:
        señales = adaptador.leer_chat(chat_id)
    elif equipo_id and canal_id:
        señales = adaptador.leer_canal(equipo_id, canal_id)
    else:
        raise ErrorDeIngesta(
            "hay que indicar el chat o el equipo y el canal que se van a leer"
        )

    eventos = adaptador.construir_eventos(señales, proyecto_id=proyecto_id)

    # Ingesta en proceso: la misma función que valida el camino de la
    # Variante B, sin red de por medio.
    aceptados = 0
    rechazados = 0
    duplicados = 0
    async with AsyncSessionLocal() as sesion:
        for evento in eventos:
            resultado = await edge.ingerir_evento(
                sesion,
                organizacion_id=organizacion_id,
                huella_clave=evento["huella_clave"],
                tipo=evento["tipo"],
                contenido=evento["contenido"],
                firma=evento["firma"],
                marca_tiempo=evento["marca_tiempo"],
                uso_previsto=evento["uso_previsto"],
                recurso=evento["recurso"],
                recurso_tipo=evento["recurso_tipo"],
                proyecto_id=evento["proyecto_id"],
                destinatarios_previstos=evento["destinatarios_previstos"],
                correlation_id=evento["correlation_id"],
            )
            if resultado.duplicado:
                duplicados += 1
            elif resultado.aceptado:
                aceptados += 1
            else:
                rechazados += 1
        await sesion.commit()

    return {
        "leidos": len(señales),
        "aceptados": aceptados,
        "rechazados": rechazados,
        "duplicados": duplicados,
    }


@celery_app.task(
    name=TAREA_INGESTA_TEAMS,
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def ingesta_teams(
    self: Task,
    organizacion_id: str,
    chat_id: str | None = None,
    canal_id: str | None = None,
    equipo_id: str | None = None,
    proyecto_id: str | None = None,
) -> dict[str, Any]:
    """Ingesta los mensajes de Teams que la política local habilita.

    La tarea vive en la cola `integraciones`, que el contrato del
    proyecto ya define. El ritmo de invocación lo decide el banco: el
    `beat` del proyecto llama a una sola tarea de mantenimiento, y esa
    ambigüedad no es un motivo para tocar un contrato que ya está
    verificado.
    """
    from app.core.asyncio_bridge import ejecutar

    try:
        return ejecutar(
            _ingestar_una_vez(
                organizacion_id,
                chat_id=chat_id,
                canal_id=canal_id,
                equipo_id=equipo_id,
                proyecto_id=proyecto_id,
            )
        )
    except ErrorDeIngesta as error:
        # Un error de configuración no se reintenta: reintentar no lo
        # arregla, y un reintento infinito oculta el problema.
        LOG.error("Ingesta de Teams no ejecutada: %s", error)
        return {"error": str(error), "leidos": 0, "aceptados": 0, "rechazados": 0}
    except Exception as error:  # noqa: BLE001
        LOG.warning("Ingesta de Teams fallida: %s", error)
        raise self.retry(exc=error)


__all__ = ["TAREA_INGESTA_TEAMS", "ErrorDeIngesta", "ingesta_teams"]
