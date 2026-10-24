"""API de Projecta Capture: sesiones de captura en navegador.

Las rutas del MVP, agrupadas por lo que hacen:

· **Sesiones** — crear, consultar, actualizar y eliminar. Toda sesión
  pertenece a un proyecto, y el acceso se comprueba contra ese
  proyecto.
· **Autorización** — registrar la declaración del operador. Es la
  puerta: sin ella no se puede empezar a grabar.
· **Captura** — transicionar el estado de la captura, con la puerta
  comprobada en el servidor y no solo en el navegador.
· **Carga** — subir fragmentos, finalizar la carga y consultar su
  estado. Idempotente por índice de fragmento.
· **Procesamiento** — pedir la transcripción y el análisis, y seguir su
  estado. Desacoplado: nunca se procesa dentro de una petición.
· **Resultados** — la transcripción, los elementos extraídos y su
  revisión.
· **Exportación** — acta en Markdown, compromisos en CSV y resultado
  estructurado en JSON.
· **Notas** — las notas manuales de la sesión.

La autorización se comprueba en **cada** ruta, contra el proyecto de la
sesión. No hay ninguna ruta que confíe en que otra ya lo ha hecho.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from typing import Annotated, Any

import jinja2
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DependenciaActor, DependenciaSesion
from app.core import rbac
from app.core.captura import servicio
from app.core.captura.estados import (
    ESTADOS_DE_CAPTURA,
    ESTADOS_DE_PROCESAMIENTO,
    MODOS_DE_CAPTURA,
)
from app.core.captura.exportacion import (
    FORMATEOS,
    exportar_csv,
    exportar_json,
    exportar_markdown,
)
from app.core.db.models_captura import (
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

LOG = logging.getLogger("projecta.captura.api")

router = APIRouter(prefix="/captura", tags=["projecta capture"])

#: Directorio de plantillas. El mismo que usa la portada.
DIRECTORIO_PLANTILLAS = pathlib.Path(__file__).resolve().parents[2] / "templates"

#: Entorno Jinja2 con escapado siempre activo, igual que en la portada.
_ENTORNO_JINJA = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(DIRECTORIO_PLANTILLAS)),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)

templates = Jinja2Templates(env=_ENTORNO_JINJA)

#: Nombre de la plantilla de captura.
PLANTILLA_CAPTURA = "captura/nueva_sesion.html"


# ─────────────────────────── esquemas ───────────────────────────


class SesionCrear(BaseModel):
    """Petición de creación de una sesión de captura."""

    proyecto_id: str
    titulo: str = Field(min_length=1, max_length=200)
    descripcion: str = ""
    finalidad: str = Field(default="", max_length=400)
    participantes: list[dict[str, Any]] = Field(default_factory=list)
    modo_solicitado: str = "MIC_ONLY"
    politica: dict[str, Any] = Field(default_factory=dict)


class SesionActualizar(BaseModel):
    """Petición de actualización de los metadatos de una sesión."""

    titulo: str | None = Field(default=None, max_length=200)
    descripcion: str | None = None
    finalidad: str | None = Field(default=None, max_length=400)


class AutorizacionRegistrar(BaseModel):
    """Petición de registro de la declaración de autorización."""

    tipo: str = "declaracion_autorizacion"
    texto: str | None = None


class ParticipanteAnadir(BaseModel):
    """Petición de alta de un participante."""

    nombre: str = Field(min_length=1, max_length=160)
    rol: str | None = Field(default=None, max_length=80)
    identidad: str | None = None
    organizador: bool = False
    informado: bool = False


class TransicionSolicitada(BaseModel):
    """Petición de transición de estado de la captura."""

    estado: str
    modo_efectivo: str | None = None
    duracion_ms: int | None = Field(default=None, ge=0)
    detencion_inmediata: bool = False


class CargaIniciar(BaseModel):
    """Petición de inicio de una carga de audio."""

    formato: str
    codec: str = ""


class FragmentoSubir(BaseModel):
    """Petición de subida de un fragmento.

    Los **bytes** vienen en base64, porque el transporte es JSON y
    meter audio crudo en un cuerpo JSON lo convertiría en un 33 % más
    grande. En la versión de producción, este endpoint debería aceptar
    ``multipart/form-data``; se deja así en el MVP para que el cliente
    pueda probarlo sin dependencias de multipart.
    """

    indice: int = Field(ge=0)
    contenido_base64: str


class FinalizarSolicitado(BaseModel):
    """Petición de finalización de una carga."""

    duracion_ms: int = Field(default=0, ge=0)
    #: Cuántos fragmentos grabó el cliente. Sin este dato, el servidor
    #: no puede saber que falta el último: solo ve los que llegaron.
    fragmentos_esperados: int | None = Field(default=None, ge=0)


class ProcesamientoSolicitar(BaseModel):
    """Petición de procesamiento de un artefacto."""

    tipo: str = "transcripcion"
    proveedor: str = "simulado"


class NotaAnadir(BaseModel):
    """Petición de alta de una nota manual."""

    texto: str = Field(min_length=1)
    marca_ms: int = Field(default=0, ge=0)


class RevisionSolicitada(BaseModel):
    """Petición de revisión de un elemento extraído."""

    accion: str
    texto_nuevo: str | None = None
    motivo: str | None = None


# ─────────────────────────── utilidades ───────────────────────────


def _uuid(valor: str, que: str) -> uuid.UUID:
    """Convierte un identificador o falla con un mensaje utilizable."""
    try:
        return uuid.UUID(str(valor))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"El {que} '{valor}' no es un identificador válido.",
        )


def _exigir_administrador(actor: rbac.Actor) -> None:
    """Exige nivel de administrador de organización.

    Crear sesiones de captura es una operación que afecta a la
    organización entera, porque implica permisos de dispositivo en el
    equipo del operador y una declaración de autorización.
    """
    decision = rbac.evaluar(actor, rbac.administrar)
    if not decision.permitido:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.motivo)


def _exigir_analista(actor: rbac.Actor, proyecto_id: str | None) -> None:
    """Exige poder analizar en el proyecto de la sesión."""
    decision = rbac.evaluar(actor, rbac.analizar, proyecto_id=proyecto_id)
    if not decision.permitido:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.motivo)


async def _obtener_sesion(
    sesion: Any, actor: rbac.Actor, sesion_id: uuid.UUID
) -> SesionDeCaptura:
    """Carga la sesión y comprueba que el actor puede verla.

    Se comprueba **la organización y el proyecto**: una sesión de otra
    organización no existe a ojos de este actor, ni siquiera devuelve
    un 403 que confirme que existe. Un 404 es más honesto que un 403
    cuando lo que se protege es la existencia del recurso.
    """
    resultado = await sesion.execute(
        select(SesionDeCaptura).where(SesionDeCaptura.id == sesion_id)
    )
    encontrada = resultado.scalars().first()
    if encontrada is None or str(encontrada.organizacion_id) != str(actor.organizacion_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe la sesión de captura {sesion_id}.",
        )
    _exigir_analista(actor, str(encontrada.proyecto_id))
    return encontrada


# ─────────────────────────── pantalla ───────────────────────────


@router.get(
    "/sesiones/{sesion_id}/pantalla",
    response_class=HTMLResponse,
    summary="Pantalla de captura de la sesión",
)
async def pantalla_captura(
    sesion_id: str,
    request: Request,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> HTMLResponse:
    """Sirve la pantalla «Nueva sesión de captura» de una sesión.

    La plantilla se renderiza en el servidor con el escapado activo: lo
    que llegue de la base de datos (títulos, nombres de participantes) no
    puede inyectar HTML.
    """
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )

    resultado = await sesion.execute(
        select(ParticipanteDeSesion).where(ParticipanteDeSesion.sesion_id == fila.id)
    )
    participantes = [
        {
            "nombre": p.nombre,
            "rol": p.rol,
            "informado": p.informado,
        }
        for p in resultado.scalars().all()
    ]

    resultado = await sesion.execute(
        select(PoliticaDeRetencion).where(PoliticaDeRetencion.sesion_id == fila.id)
    )
    politica = resultado.scalars().first()

    resultado = await sesion.execute(
        select(ConsentimientoDeCaptura).where(
            ConsentimientoDeCaptura.sesion_id == fila.id,
            ConsentimientoDeCaptura.tipo == "declaracion_autorizacion",
            ConsentimientoDeCaptura.aceptado.is_(True),
        )
    )
    autorizada = resultado.scalars().first() is not None

    contexto = {
        "request": request,
        "sesion": {
            "id": str(fila.id),
            "titulo": fila.titulo,
            "descripcion": fila.descripcion,
            "finalidad": fila.finalidad,
        },
        "proyecto_id": str(fila.proyecto_id),
        "participantes": participantes,
        "politica": {
            "dias_transcripcion": politica.dias_transcripcion if politica else 90,
            "dias_elementos": politica.dias_elementos if politica else 365,
            "dias_notas": politica.dias_notas if politica else 365,
            "dias_audio": politica.dias_audio if politica else 0,
            "conservar_audio": politica.conservar_audio if politica else False,
        },
        "autorizada": autorizada,
        "aviso_privacidad": servicio.TEXTO_AVISO_PRIVACIDAD,
        "texto_autorizacion": servicio.TEXTO_DECLARACION_AUTORIZACION,
    }
    return templates.TemplateResponse(request, PLANTILLA_CAPTURA, contexto)


# ─────────────────────────── sesiones ───────────────────────────


@router.post("/sesiones", summary="Crea una sesión de captura")
async def crear_sesion(
    cuerpo: SesionCrear,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Crea una sesión de captura en estado ``DRAFT``."""
    _exigir_administrador(actor)
    proyecto_id = _uuid(cuerpo.proyecto_id, "proyecto")

    # El proyecto tiene que ser de la organización del actor.
    from app.core.db.models import Proyecto

    resultado = await sesion.execute(
        select(Proyecto).where(
            Proyecto.id == proyecto_id,
            Proyecto.organizacion_id == uuid.UUID(str(actor.organizacion_id)),
        )
    )
    if resultado.scalars().first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el proyecto {proyecto_id} en esta organización.",
        )

    try:
        nueva = await servicio.crear_sesion(
            sesion,
            organizacion_id=uuid.UUID(str(actor.organizacion_id)),
            proyecto_id=proyecto_id,
            titulo=cuerpo.titulo,
            descripcion=cuerpo.descripcion,
            finalidad=cuerpo.finalidad,
            participantes=cuerpo.participantes,
            modo_solicitado=cuerpo.modo_solicitado,
            politica=cuerpo.politica,
            creado_por=uuid.UUID(str(actor.usuario_id)) if actor.usuario_id else None,
        )
    except servicio.ErrorDePeticion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    await sesion.commit()
    return {"id": str(nueva.id), "estado": nueva.estado, "titulo": nueva.titulo}


@router.get("/sesiones", summary="Lista las sesiones de captura")
async def listar_sesiones(
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    proyecto_id: str | None = Query(default=None),
    limite: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Lista las sesiones de captura de la organización."""
    consulta = select(SesionDeCaptura).where(
        SesionDeCaptura.organizacion_id == uuid.UUID(str(actor.organizacion_id))
    )
    if proyecto_id:
        consulta = consulta.where(
            SesionDeCaptura.proyecto_id == _uuid(proyecto_id, "proyecto")
        )
    consulta = consulta.order_by(SesionDeCaptura.creado_en.desc()).limit(limite)

    resultado = await sesion.execute(consulta)
    filas = list(resultado.scalars())
    visibles = []
    for fila in filas:
        decision = rbac.evaluar(
            actor, rbac.analizar, proyecto_id=str(fila.proyecto_id)
        )
        if not decision.permitido:
            continue
        visibles.append(
            {
                "id": str(fila.id),
                "titulo": fila.titulo,
                "proyecto_id": str(fila.proyecto_id),
                "estado": fila.estado,
                "modo_solicitado": fila.modo_solicitado,
                "modo_efectivo": fila.modo_efectivo,
                "captura_completa": fila.captura_completa,
                "duracion_ms": fila.duracion_ms,
                "creado_en": fila.creado_en.isoformat() if fila.creado_en else None,
            }
        )
    return {"sesiones": visibles, "total": len(visibles)}


@router.get("/sesiones/{sesion_id}", summary="Consulta una sesión de captura")
async def consultar_sesion(
    sesion_id: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Devuelve la sesión con su estado, sus participantes y su política."""
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )

    resultado = await sesion.execute(
        select(ParticipanteDeSesion).where(ParticipanteDeSesion.sesion_id == fila.id)
    )
    participantes = [
        {
            "id": str(p.id),
            "nombre": p.nombre,
            "rol": p.rol,
            "identidad": p.identidad,
            "organizador": p.organizador,
            "informado": p.informado,
        }
        for p in resultado.scalars().all()
    ]

    resultado = await sesion.execute(
        select(ConsentimientoDeCaptura).where(
            ConsentimientoDeCaptura.sesion_id == fila.id
        )
    )
    consentimientos = [
        {
            "id": str(c.id),
            "tipo": c.tipo,
            "aceptado": c.aceptado,
            "declarado_por_nombre": c.declarado_por_nombre,
            "creado_en": c.creado_en.isoformat() if c.creado_en else None,
        }
        for c in resultado.scalars().all()
    ]

    resultado = await sesion.execute(
        select(PoliticaDeRetencion).where(PoliticaDeRetencion.sesion_id == fila.id)
    )
    politica = resultado.scalars().first()

    return {
        "id": str(fila.id),
        "titulo": fila.titulo,
        "descripcion": fila.descripcion,
        "finalidad": fila.finalidad,
        "proyecto_id": str(fila.proyecto_id),
        "estado": fila.estado,
        "modo_solicitado": fila.modo_solicitado,
        "modo_efectivo": fila.modo_efectivo,
        "captura_completa": fila.captura_completa,
        "detencion_inmediata": fila.detencion_inmediata,
        "duracion_ms": fila.duracion_ms,
        "inicio_captura": fila.inicio_captura.isoformat() if fila.inicio_captura else None,
        "fin_captura": fila.fin_captura.isoformat() if fila.fin_captura else None,
        "participantes": participantes,
        "autorizaciones": consentimientos,
        "politica": {
            "dias_transcripcion": politica.dias_transcripcion if politica else 90,
            "dias_elementos": politica.dias_elementos if politica else 365,
            "dias_audio": politica.dias_audio if politica else 0,
            "conservar_audio": politica.conservar_audio if politica else False,
        },
        "estados_validos": sorted(servicio.ESTADOS_DE_CAPTURA),
    }


@router.patch("/sesiones/{sesion_id}", summary="Actualiza los metadatos de una sesión")
async def actualizar_sesion(
    sesion_id: str,
    cuerpo: SesionActualizar,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Actualiza título, descripción o finalidad."""
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )
    if cuerpo.titulo is not None:
        fila.titulo = cuerpo.titulo.strip()
    if cuerpo.descripcion is not None:
        fila.descripcion = cuerpo.descripcion
    if cuerpo.finalidad is not None:
        fila.finalidad = cuerpo.finalidad
    await sesion.commit()
    return {"id": str(fila.id), "actualizada": True}


@router.delete(
    "/sesiones/{sesion_id}",
    summary="Elimina una sesión de captura y su contenido",
)
async def eliminar_sesion(
    sesion_id: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Elimina la sesión y todo lo que le pertenece.

    Es el borrado explícito. La purga por política de conservación es
    otra operación, y no se mezclan.
    """
    _exigir_administrador(actor)
    identificador = _uuid(sesion_id, "sesión de captura")
    await _obtener_sesion(sesion, actor, identificador)
    resultado = await servicio.eliminar_sesion(sesion, sesion_id=identificador)
    await sesion.commit()
    return resultado


# ─────────────────────────── autorización ───────────────────────────


@router.post(
    "/sesiones/{sesion_id}/autorizacion",
    summary="Registra la declaración de autorización del operador",
)
async def registrar_autorizacion(
    sesion_id: str,
    cuerpo: AutorizacionRegistrar,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Registra la declaración del organizador.

    Es una **declaración del operador**, no un consentimiento de los
    participantes. El texto que se acepta lo dice, y el registro
    conserva el texto exacto.
    """
    _exigir_administrador(actor)
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )
    texto = cuerpo.texto or servicio.TEXTO_DECLARACION_AUTORIZACION
    registro = await servicio.registrar_autorizacion(
        sesion,
        sesion_id=fila.id,
        actor_id=uuid.UUID(str(actor.usuario_id)) if actor.usuario_id else None,
        actor_nombre=actor.rol,
        tipo=cuerpo.tipo,
        texto=texto,
    )
    await sesion.commit()
    return {
        "id": str(registro.id),
        "tipo": registro.tipo,
        "aceptado": registro.aceptado,
        "texto": registro.texto,
    }


@router.post(
    "/sesiones/{sesion_id}/participantes",
    summary="Añade un participante a la sesión",
)
async def anadir_participante(
    sesion_id: str,
    cuerpo: ParticipanteAnadir,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Añade un participante declarado a mano."""
    _exigir_administrador(actor)
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )
    existente = await sesion.execute(
        select(ParticipanteDeSesion).where(
            ParticipanteDeSesion.sesion_id == fila.id,
            ParticipanteDeSesion.nombre == cuerpo.nombre.strip(),
        )
    )
    if existente.scalars().first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya hay un participante llamado '{cuerpo.nombre}' en la sesión.",
        )
    persona = ParticipanteDeSesion(
        sesion_id=fila.id,
        nombre=cuerpo.nombre.strip(),
        rol=cuerpo.rol,
        identidad=cuerpo.identidad,
        organizador=cuerpo.organizador,
        informado=cuerpo.informado,
    )
    sesion.add(persona)
    await sesion.commit()
    return {"id": str(persona.id), "nombre": persona.nombre}


# ─────────────────────────── captura ───────────────────────────


@router.post(
    "/sesiones/{sesion_id}/estado",
    summary="Cambia el estado de la captura",
)
async def cambiar_estado(
    sesion_id: str,
    cuerpo: TransicionSolicitada,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Cambia el estado de la captura.

    El servidor comprueba la transición **y** la puerta de autorización.
    Que el botón del navegador esté deshabilitado no es una
    comprobación: es una cortesía.
    """
    _exigir_administrador(actor)
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )
    try:
        await servicio.transicionar_captura(
            sesion,
            sesion_id=fila.id,
            destino=cuerpo.estado,
            modo_efectivo=cuerpo.modo_efectivo,
            duracion_ms=cuerpo.duracion_ms,
            detencion_inmediata=cuerpo.detencion_inmediata,
        )
    except servicio.ErrorDeEstado as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    except Exception as error:  # noqa: BLE001 - la transición inválida es un 409
        from app.core.captura.estados import ErrorDeTransicion

        if isinstance(error, ErrorDeTransicion):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
        raise
    except servicio.ErrorDePeticion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    await sesion.commit()
    await sesion.refresh(fila)
    return {
        "id": str(fila.id),
        "estado": fila.estado,
        "modo_efectivo": fila.modo_efectivo,
        "captura_completa": fila.captura_completa,
        "duracion_ms": fila.duracion_ms,
    }


# ─────────────────────────── carga ───────────────────────────


@router.post(
    "/sesiones/{sesion_id}/artefactos",
    summary="Inicia una carga de audio",
)
async def iniciar_carga(
    sesion_id: str,
    cuerpo: CargaIniciar,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Abre un artefacto de audio para la sesión."""
    _exigir_administrador(actor)
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )
    try:
        artefacto = await servicio.iniciar_carga(
            sesion,
            sesion_id=fila.id,
            formato=cuerpo.formato,
            codec=cuerpo.codec,
        )
    except servicio.ErrorDePeticion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    await sesion.commit()
    return {"id": str(artefacto.id), "estado": artefacto.estado, "formato": artefacto.formato}


@router.put(
    "/artefactos/{artefacto_id}/fragmentos",
    summary="Sube un fragmento de la grabación",
)
async def subir_fragmento(
    artefacto_id: str,
    cuerpo: FragmentoSubir,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Recibe un fragmento. Es idempotente por índice.

    Reenviar el mismo fragmento no lo duplica, y reenviar un
    fragmento **distinto** con el mismo índice se rechaza: el índice
    ocupa una posición en la secuencia, y sobrescribirlo rompería la
    grabación.
    """
    import base64

    _exigir_administrador(actor)
    identificador = _uuid(artefacto_id, "artefacto de audio")

    resultado = await sesion.execute(
        select(ArtefactoDeAudio).where(ArtefactoDeAudio.id == identificador)
    )
    artefacto = resultado.scalars().first()
    if artefacto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el artefacto de audio {artefacto_id}.",
        )
    await _obtener_sesion(sesion, actor, artefacto.sesion_id)

    try:
        datos = base64.b64decode(cuerpo.contenido_base64, validate=True)
    except Exception:  # noqa: BLE001 - el base64 inválido es un 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El contenido no es base64 válido.",
        )

    try:
        resultado_carga = await servicio.recibir_fragmento(
            sesion,
            artefacto_id=identificador,
            indice=cuerpo.indice,
            datos=datos,
        )
    except servicio.ErrorDePeticion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    await sesion.commit()
    return {
        "indice": resultado_carga.indice,
        "nuevo": resultado_carga.nuevo,
        "coincide": resultado_carga.coincide,
        "tamano_bytes": resultado_carga.tamano_bytes,
    }


@router.post(
    "/artefactos/{artefacto_id}/finalizar",
    summary="Finaliza la carga de audio",
)
async def finalizar_carga(
    artefacto_id: str,
    cuerpo: FinalizarSolicitado,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Finaliza la carga de forma explícita y dice qué falta."""
    _exigir_administrador(actor)
    identificador = _uuid(artefacto_id, "artefacto de audio")
    resultado = await sesion.execute(
        select(ArtefactoDeAudio).where(ArtefactoDeAudio.id == identificador)
    )
    artefacto = resultado.scalars().first()
    if artefacto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el artefacto de audio {artefacto_id}.",
        )
    await _obtener_sesion(sesion, actor, artefacto.sesion_id)

    try:
        fin = await servicio.finalizar_carga(
            sesion,
            artefacto_id=identificador,
            duracion_ms=cuerpo.duracion_ms,
            fragmentos_esperados=cuerpo.fragmentos_esperados,
        )
    except servicio.ErrorDePeticion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    await sesion.commit()
    return {
        "artefacto_id": str(fin.artefacto_id),
        "completa": fin.completa,
        "faltantes": fin.faltantes,
        "duplicados": fin.duplicados,
        "tamano_bytes": fin.tamano_bytes,
        "huella": fin.huella,
    }


@router.get(
    "/artefactos/{artefacto_id}",
    summary="Consulta el estado de la carga",
)
async def estado_carga(
    artefacto_id: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Dice qué fragmentos han llegado y cuáles faltan."""
    identificador = _uuid(artefacto_id, "artefacto de audio")
    resultado = await sesion.execute(
        select(ArtefactoDeAudio).where(ArtefactoDeAudio.id == identificador)
    )
    artefacto = resultado.scalars().first()
    if artefacto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el artefacto de audio {artefacto_id}.",
        )
    await _obtener_sesion(sesion, actor, artefacto.sesion_id)

    faltantes, duplicados = await servicio.analizar_fragmentos(sesion, identificador)
    resultado = await sesion.execute(
        select(FragmentoDeSubida)
        .where(FragmentoDeSubida.artefacto_id == identificador)
        .order_by(FragmentoDeSubida.indice)
    )
    fragmentos = list(resultado.scalars())
    return {
        "id": str(artefacto.id),
        "estado": artefacto.estado,
        "finalizado": artefacto.finalizado,
        "formato": artefacto.formato,
        "tamano_bytes": artefacto.tamano_bytes,
        "huella": artefacto.huella,
        "fragmentos_recibidos": len(fragmentos),
        "fragmentos_totales": artefacto.fragmentos_totales,
        "faltantes": faltantes,
        "duplicados": duplicados,
    }


# ─────────────────────────── procesamiento ───────────────────────────


@router.post(
    "/artefactos/{artefacto_id}/procesar",
    summary="Solicita la transcripción o el análisis",
)
async def solicitar_procesamiento(
    artefacto_id: str,
    cuerpo: ProcesamientoSolicitar,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Encola el procesamiento. Es idempotente por artefacto y tipo.

    El trabajo se ejecuta en el worker: una petición HTTP no espera a
    que una transcripción termine, porque se cortaría.
    """
    _exigir_administrador(actor)
    identificador = _uuid(artefacto_id, "artefacto de audio")
    resultado = await sesion.execute(
        select(ArtefactoDeAudio).where(ArtefactoDeAudio.id == identificador)
    )
    artefacto = resultado.scalars().first()
    if artefacto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el artefacto de audio {artefacto_id}.",
        )
    await _obtener_sesion(sesion, actor, artefacto.sesion_id)

    try:
        trabajo = await servicio.solicitar_procesamiento(
            sesion,
            artefacto_id=identificador,
            tipo=cuerpo.tipo,
            proveedor=cuerpo.proveedor,
        )
    except servicio.ErrorDePeticion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    await sesion.commit()

    # La tarea se encola, pero si el broker no está disponible la
    # sesión no se pierde: el trabajo queda registrado y se puede
    # relanzar. Un error de encolado **no** es un error de la
    # petición, que ya ha hecho su trabajo.
    tarea_id = None
    try:
        from app.workers.tasks.procesa_captura import procesa_captura

        tarea = procesa_captura.apply_async(
            kwargs={"trabajo_id": str(trabajo.id)},
            queue="integraciones",
        )
        tarea_id = tarea.id
    except Exception as error:  # noqa: BLE001 - el trabajo ya está registrado
        LOG.warning("no se pudo encolar el trabajo %s: %s", trabajo.id, error)

    return {
        "trabajo_id": str(trabajo.id),
        "estado": trabajo.estado,
        "tarea_id": tarea_id,
        "cola": "integraciones",
    }


@router.get("/trabajos/{trabajo_id}", summary="Consulta el estado del procesamiento")
async def estado_trabajo(
    trabajo_id: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """El estado del trabajo de procesamiento."""
    identificador = _uuid(trabajo_id, "trabajo")
    resultado = await sesion.execute(
        select(TrabajoDeProcesamiento).where(TrabajoDeProcesamiento.id == identificador)
    )
    trabajo = resultado.scalars().first()
    if trabajo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el trabajo {trabajo_id}.",
        )
    await _obtener_sesion(sesion, actor, trabajo.sesion_id)
    return {
        "id": str(trabajo.id),
        "tipo": trabajo.tipo,
        "estado": trabajo.estado,
        "proveedor": trabajo.proveedor,
        "progreso": trabajo.progreso,
        "error": trabajo.error,
        "tarea_id": trabajo.tarea_id,
        "estados_validos": sorted(ESTADOS_DE_PROCESAMIENTO),
    }


# ─────────────────────────── resultados ───────────────────────────


@router.get(
    "/sesiones/{sesion_id}/transcripcion",
    summary="Obtiene la transcripción de la sesión",
)
async def obtener_transcripcion(
    sesion_id: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Devuelve la transcripción con sus segmentos, hablantes y tiempos."""
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )
    resultado = await sesion.execute(
        select(Transcripcion).where(Transcripcion.sesion_id == fila.id)
    )
    transcripcion = resultado.scalars().first()
    if transcripcion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Esta sesión todavía no tiene transcripción.",
        )

    resultado = await sesion.execute(
        select(SegmentoDeTranscripcion)
        .where(SegmentoDeTranscripcion.transcripcion_id == transcripcion.id)
        .order_by(SegmentoDeTranscripcion.indice)
    )
    segmentos = [
        {
            "indice": s.indice,
            "hablante": s.hablante,
            "inicio_ms": s.inicio_ms,
            "fin_ms": s.fin_ms,
            "texto": s.texto,
            "confianza": s.confianza,
            "corregido": s.corregido,
        }
        for s in resultado.scalars().all()
    ]

    resultado = await sesion.execute(
        select(MapeoHablante).where(MapeoHablante.transcripcion_id == transcripcion.id)
    )
    mapeos = [
        {
            "hablante": m.hablante,
            "nombre": m.nombre,
            "identidad": m.identidad,
            "validado": m.validado,
        }
        for m in resultado.scalars().all()
    ]

    return {
        "id": str(transcripcion.id),
        "idioma": transcripcion.idioma,
        "revisada": transcripcion.revisada,
        "editada": transcripcion.editada,
        "confianza": transcripcion.confianza,
        "segmentos": segmentos,
        "mapeos_hablante": mapeos,
        "texto": transcripcion.texto,
    }


@router.patch(
    "/sesiones/{sesion_id}/notas",
    summary="Añade una nota manual con su marca de tiempo",
)
async def anadir_nota(
    sesion_id: str,
    cuerpo: NotaAnadir,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Añade una nota manual. Es lo que escribe la persona, no el modelo."""
    _exigir_administrador(actor)
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )
    try:
        nota = await servicio.anadir_nota(
            sesion,
            sesion_id=fila.id,
            texto=cuerpo.texto,
            marca_ms=cuerpo.marca_ms,
            autor_id=uuid.UUID(str(actor.usuario_id)) if actor.usuario_id else None,
        )
    except servicio.ErrorDePeticion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    await sesion.commit()
    return {"id": str(nota.id), "marca_ms": nota.marca_ms, "texto": nota.texto}


@router.get(
    "/sesiones/{sesion_id}/elementos",
    summary="Lista los elementos extraídos de la transcripción",
)
async def listar_elementos(
    sesion_id: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
    solo_pendientes: bool = Query(default=False),
) -> dict[str, Any]:
    """Los resultados del análisis, con su evidencia y su estado de revisión."""
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )
    consulta = select(ElementoExtraido).where(ElementoExtraido.sesion_id == fila.id)
    if solo_pendientes:
        consulta = consulta.where(ElementoExtraido.requiere_revision.is_(True))
    consulta = consulta.order_by(ElementoExtraido.marca_ms)

    resultado = await sesion.execute(consulta)
    elementos = list(resultado.scalars())

    respuesta: list[dict[str, Any]] = []
    for elemento in elementos:
        resultado = await sesion.execute(
            select(ParticipanteDeSesion).where(
                ParticipanteDeSesion.id == elemento.responsable_id
            )
        )
        responsable = resultado.scalars().first()

        resultado = await sesion.execute(
            select(EvidenciaDeItem).where(EvidenciaDeItem.elemento_id == elemento.id)
        )
        evidencias = [
            {
                "segmento_indice": e.segmento_indice,
                "texto": e.texto,
                "marca_ms": e.marca_ms,
                "desactualizada": e.desactualizada,
            }
            for e in resultado.scalars().all()
        ]

        respuesta.append(
            {
                "id": str(elemento.id),
                "clave": elemento.clave,
                "tipo": elemento.tipo,
                "descripcion": elemento.descripcion,
                "texto_corregido": elemento.texto_corregido,
                "responsable_id": str(elemento.responsable_id)
                if elemento.responsable_id
                else None,
                "responsable_nombre": responsable.nombre if responsable else None,
                "hablante": elemento.hablante,
                "fecha_propuesta": elemento.fecha_propuesta,
                "marca_ms": elemento.marca_ms,
                "segmentos_evidencia": elemento.segmentos_evidencia,
                "requiere_revision": elemento.requiere_revision,
                "motivo_revision": elemento.motivo_revision,
                "estado_revision": elemento.estado_revision,
                "aprobado": elemento.aprobado,
                "evidencias": evidencias,
            }
        )
    return {"elementos": respuesta, "total": len(respuesta)}


@router.post(
    "/elementos/{elemento_id}/revision",
    summary="Aprobar, rechazar o corregir un elemento extraído",
)
async def revisar_elemento(
    elemento_id: str,
    cuerpo: RevisionSolicitada,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> dict[str, Any]:
    """Registra la acción de revisión y su historial."""
    _exigir_administrador(actor)
    identificador = _uuid(elemento_id, "elemento extraído")
    resultado = await sesion.execute(
        select(ElementoExtraido).where(ElementoExtraido.id == identificador)
    )
    elemento = resultado.scalars().first()
    if elemento is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el elemento extraído {elemento_id}.",
        )
    await _obtener_sesion(sesion, actor, elemento.sesion_id)

    try:
        await servicio.revisar_elemento(
            sesion,
            elemento_id=identificador,
            accion=cuerpo.accion,
            actor_id=uuid.UUID(str(actor.usuario_id)) if actor.usuario_id else None,
            texto_nuevo=cuerpo.texto_nuevo,
            motivo=cuerpo.motivo,
        )
    except servicio.ErrorDePeticion as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        )
    await sesion.commit()
    return {
        "id": str(elemento.id),
        "estado_revision": elemento.estado_revision,
        "aprobado": elemento.aprobado,
        "requiere_revision": elemento.requiere_revision,
    }


# ─────────────────────────── exportación ───────────────────────────


@router.get(
    "/sesiones/{sesion_id}/exportar/{formato}",
    summary="Exporta los resultados de la sesión",
)
async def exportar(
    sesion_id: str,
    formato: str,
    actor: DependenciaActor,
    sesion: DependenciaSesion,
) -> Response:
    """Exporta en Markdown, CSV o JSON.

    La exportación **solo** incluye esta sesión. Nunca los datos de
    otra sesión ni de otro proyecto.
    """
    if formato not in FORMATEOS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Formato desconocido: '{formato}'. Los disponibles: "
            f"{', '.join(FORMATEOS)}.",
        )
    fila = await _obtener_sesion(
        sesion, actor, _uuid(sesion_id, "sesión de captura")
    )

    resultado = await sesion.execute(
        select(ParticipanteDeSesion).where(ParticipanteDeSesion.sesion_id == fila.id)
    )
    participantes = [
        {
            "nombre": p.nombre,
            "rol": p.rol,
            "identidad": p.identidad,
            "informado": p.informado,
        }
        for p in resultado.scalars().all()
    ]

    resultado = await sesion.execute(
        select(ElementoExtraido)
        .where(ElementoExtraido.sesion_id == fila.id)
        .order_by(ElementoExtraido.marca_ms)
    )
    elementos = []
    for elemento in resultado.scalars().all():
        resultado = await sesion.execute(
            select(ParticipanteDeSesion).where(
                ParticipanteDeSesion.id == elemento.responsable_id
            )
        )
        responsable = resultado.scalars().first()
        elementos.append(
            {
                "id": str(elemento.id),
                "tipo": elemento.tipo,
                "descripcion": elemento.descripcion,
                "texto_corregido": elemento.texto_corregido,
                "responsable_nombre": responsable.nombre if responsable else None,
                "fecha_propuesta": elemento.fecha_propuesta,
                "estado_revision": elemento.estado_revision,
                "requiere_revision": elemento.requiere_revision,
                "motivo_revision": elemento.motivo_revision,
                "marca_ms": elemento.marca_ms,
                "segmentos_evidencia": elemento.segmentos_evidencia,
            }
        )

    resultado = await sesion.execute(
        select(NotaDeReunion)
        .where(NotaDeReunion.sesion_id == fila.id)
        .order_by(NotaDeReunion.marca_ms)
    )
    notas = [
        {"texto": n.texto, "marca_ms": n.marca_ms} for n in resultado.scalars().all()
    ]

    resultado = await sesion.execute(
        select(Transcripcion).where(Transcripcion.sesion_id == fila.id)
    )
    transcripcion_obj = resultado.scalars().first()
    segmentos: list[dict[str, Any]] = []
    if transcripcion_obj is not None:
        resultado = await sesion.execute(
            select(SegmentoDeTranscripcion)
            .where(SegmentoDeTranscripcion.transcripcion_id == transcripcion_obj.id)
            .order_by(SegmentoDeTranscripcion.indice)
        )
        segmentos = [
            {
                "hablante": s.hablante,
                "inicio_ms": s.inicio_ms,
                "texto": s.texto,
            }
            for s in resultado.scalars().all()
        ]

    datos_sesion = {
        "id": str(fila.id),
        "titulo": fila.titulo,
        "finalidad": fila.finalidad,
        "proyecto_id": str(fila.proyecto_id),
        "modo_solicitado": fila.modo_solicitado,
        "modo_efectivo": fila.modo_efectivo,
        "captura_completa": fila.captura_completa,
        "creado_en": fila.creado_en.isoformat() if fila.creado_en else None,
    }

    if formato == "csv":
        contenido = exportar_csv(elementos)
        media = "text/csv; charset=utf-8"
        nombre = f"captura-{fila.id}.csv"
    elif formato == "json":
        contenido = exportar_json(
            sesion=datos_sesion,
            participantes=participantes,
            elementos=elementos,
            notas=notas,
            transcripcion=segmentos,
        )
        media = "application/json; charset=utf-8"
        nombre = f"captura-{fila.id}.json"
    else:
        contenido = exportar_markdown(
            sesion=datos_sesion,
            participantes=participantes,
            elementos=elementos,
            notas=notas,
            transcripcion=segmentos,
        )
        media = "text/markdown; charset=utf-8"
        nombre = f"acta-{fila.id}.md"

    return Response(
        content=contenido,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
