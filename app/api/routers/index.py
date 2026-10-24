"""Portada de la aplicación (router de ``GET /``)."""

from __future__ import annotations

import logging

import jinja2
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.config import APP_VERSION, RAIZ_PROYECTO, settings

logger = logging.getLogger("projecta.portada")

router = APIRouter(tags=["portada"])

#: Directorio de plantillas Jinja2 del proyecto.
DIRECTORIO_PLANTILLAS = RAIZ_PROYECTO / "app" / "templates"

#: Entorno Jinja2 con escapado siempre activo. Se construye de forma explícita
#: porque Starlette deprecó pasar las opciones del entorno por palabra clave
#: (``Jinja2Templates(directory=..., autoescape=True)``); el resultado es el
#: mismo: ``autoescape=True`` está activo y nada se escapa por accidente.
_ENTORNO_JINJA = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(DIRECTORIO_PLANTILLAS)),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)

templates = Jinja2Templates(env=_ENTORNO_JINJA)

#: Nombre de la plantilla de portada.
PLANTILLA_PORTADA = "index.html"


def _contexto_comun(request: Request) -> dict[str, object]:
    """Contexto siempre presente en el render de la portada."""
    return {
        "request": request,
        "app_name": settings.app_name,
        "version": APP_VERSION,
        "environment": settings.app_env,
    }


def _portada_de_respaldo(contexto: dict[str, object]) -> str:
    """HTML mínimo para cuando la plantilla todavía no está disponible.

    La tarea de plantillas escribe ``app/templates/index.html``; hasta entonces
    la portada responde 200 con esta versión de reserva para no romper el
    arranque de la API.
    """
    app_name = contexto.get("app_name", settings.app_name)
    version = contexto.get("version", APP_VERSION)
    environment = contexto.get("environment", settings.app_env)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="es">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{app_name}</title>\n"
        "</head>\n"
        "<body>\n"
        "  <main>\n"
        f"    <h1>{app_name}</h1>\n"
        "    <p>La capa inteligente para gobernar proyectos, portafolios y decisiones.</p>\n"
        f"    <p>Versión: {version} · Entorno: {environment}</p>\n"
        "    <p>La plantilla de portada todavía no está disponible.</p>\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )


@router.get("/", response_class=HTMLResponse, summary="Portada de la aplicación")
async def portada(request: Request) -> HTMLResponse:
    """Renderiza la portada con el contexto común de la aplicación."""
    contexto = _contexto_comun(request)
    try:
        return templates.TemplateResponse(request, PLANTILLA_PORTADA, contexto)
    except jinja2.TemplateNotFound:
        logger.warning(
            "No se encontró la plantilla %s en %s; se devuelve la portada de respaldo.",
            PLANTILLA_PORTADA,
            DIRECTORIO_PLANTILLAS,
        )
        return HTMLResponse(_portada_de_respaldo(contexto))
