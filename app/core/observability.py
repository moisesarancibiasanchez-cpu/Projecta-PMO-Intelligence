"""Observabilidad de Projecta PMO Intelligence (HU_06).

Tres piezas que se complementan:

* **Trazas** (OpenTelemetry). Una traza une el request HTTP entrante, las
  tareas Celery que dispara y las llamadas a sistemas externos. El
  ``correlation_id`` es el hilo que las cose: viaja en la cabecera
  ``X-Correlation-Id`` y se propaga a la cola, a la auditoría y a los
  registros.
* **Métricas** (Prometheus). Se exponen en ``/metrics`` y son las que
  alimentan los paneles de Grafana y las reglas de alerta. La lista
  completa está en ``observability/METRICAS.md`` y este módulo la
  implementa.
* **Errores** (Sentry). Un error no controlado se reporta con el
  ``correlation_id``, el usuario y el proyecto en contexto.

Todas las bibliotecas son **opcionales**: si OpenTelemetry o Prometheus no
están instaladas, la aplicación arranca igual y solo se pierde la
observabilidad. Un fallo de instrumentación nunca debe tumbar el servicio.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from app.core.config import settings

__all__ = [
    "CONTEXTO_ACTUAL",
    "contador",
    "cronometrar",
    "error_inesperado",
    "establecer_correlacion",
    "histograma",
    "instalar",
    "medidor",
    "middleware_correlacion",
    "observar_peticion",
    "registrar_metrica",
]

LOG = logging.getLogger("projecta.observabilidad")

#: Cabecera por la que viaja el identificador de correlación.
CABECERA_CORRELACION = "X-Correlation-Id"

#: Contexto de la petición en curso. Se usa un ``ContextVar`` para que sea
#: seguro con la ejecución asíncrona.
CONTEXTO_ACTUAL: dict[str, str] = {}


# ─────────────────────────── correlación ───────────────────────────


def establecer_correlacion(valor: str | None = None) -> str:
    """Fija el identificador de correlación de la petición en curso."""
    identificador = valor or str(uuid.uuid4())
    CONTEXTO_ACTUAL["correlation_id"] = identificador
    return identificador


def correlation_id_actual() -> str | None:
    """Devuelve el identificador de correlación de la petición en curso."""
    return CONTEXTO_ACTUAL.get("correlation_id")


@contextmanager
def limpiar_correlacion() -> Iterator[None]:
    """Limpia el identificador de correlación al terminar la petición."""
    try:
        yield
    finally:
        CONTEXTO_ACTUAL.pop("correlation_id", None)


# ─────────────────────────── métricas ───────────────────────────

#: Métricas Prometheus. Se crean de forma perezosa para no duplicarlas si
#: el módulo se importa dos veces (por ejemplo, en los tests).
_METRICAS: dict[str, Any] = {}
_REGISTRO: Any = None


def _registro() -> Any:
    """Devuelve el registro de Prometheus, o ``None`` si no está instalada."""
    global _REGISTRO
    if _REGISTRO is None:
        try:
            from prometheus_client import CollectorRegistry, REGISTRY
        except ImportError:  # pragma: no cover - dependencia opcional
            return None
        _REGISTRO = REGISTRY
        return _REGISTRO
    return _REGISTRO


def contador(nombre: str, documentacion: str, etiquetas: list[str] | None = None) -> Any:
    """Devuelve (o crea) un contador Prometheus."""
    if nombre in _METRICAS:
        return _METRICAS[nombre]
    try:
        from prometheus_client import Counter
    except ImportError:  # pragma: no cover - dependencia opcional
        return None
    if _registro() is None:
        return None
    metrica = Counter(nombre, documentacion, etiquetas or [])
    _METRICAS[nombre] = metrica
    return metrica


def histograma(
    nombre: str, documentacion: str, etiquetas: list[str] | None = None
) -> Any:
    """Devuelve (o crea) un histograma Prometheus."""
    if nombre in _METRICAS:
        return _METRICAS[nombre]
    try:
        from prometheus_client import Histogram
    except ImportError:  # pragma: no cover - dependencia opcional
        return None
    if _registro() is None:
        return None
    metrica = Histogram(
        nombre, documentacion, etiquetas or [], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
    )
    _METRICAS[nombre] = metrica
    return metrica


def medidor(nombre: str, documentacion: str, etiquetas: list[str] | None = None) -> Any:
    """Devuelve (o crea) un medidor Prometheus (gauge)."""
    if nombre in _METRICAS:
        return _METRICAS[nombre]
    try:
        from prometheus_client import Gauge
    except ImportError:  # pragma: no cover - dependencia opcional
        return None
    if _registro() is None:
        return None
    metrica = Gauge(nombre, documentacion, etiquetas or [])
    _METRICAS[nombre] = metrica
    return metrica


#: Métricas de la aplicación. Los nombres coinciden con los que usan las
#: reglas de alerta de ``observability/alertas.yml``.
def registrar_metrica(nombre: str, valor: float, **etiquetas: str) -> None:
    """Publica el valor de una métrica de tipo medidor."""
    metrica = medidor(nombre, nombre.replace("_", " "), list(etiquetas.keys()))
    if metrica is not None:
        metrica.labels(**etiquetas).set(valor)


# ─────────────────────────── instrumentación ───────────────────────────


@contextmanager
def observar_peticion(nombre: str, **etiquetas: str) -> Iterator[None]:
    """Mide la duración de una operación y la publica como histograma.

    ``projecta_api_peticiones_latencia_segundos`` es la métrica que usan las
    alertas de latencia.
    """
    inicio = time.perf_counter()
    try:
        yield
    finally:
        duracion = time.perf_counter() - inicio
        his = histograma(
            "projecta_api_peticiones_latencia_segundos",
            "Duración de las peticiones HTTP de Projecta PMO Intelligence.",
            ["ruta", "metodo", "estado"],
        )
        if his is not None:
            his.labels(
                ruta=nombre, metodo=etiquetas.get("metodo", "GET"),
                estado=etiquetas.get("estado", "200"),
            ).observe(duracion)


def cronometrar(nombre: str, **etiquetas: str) -> Any:
    """Decorador que mide la duración de una función."""
    import functools

    def envoltorio(funcion: Any) -> Any:
        @functools.wraps(funcion)
        async def version_async(*args: Any, **kwargs: Any) -> Any:
            inicio = time.perf_counter()
            try:
                return await funcion(*args, **kwargs)
            finally:
                _publicar_duracion(nombre, inicio, etiquetas)

        @functools.wraps(funcion)
        def version_sync(*args: Any, **kwargs: Any) -> Any:
            inicio = time.perf_counter()
            try:
                return funcion(*args, **kwargs)
            finally:
                _publicar_duracion(nombre, inicio, etiquetas)

        return version_async if _es_coroutine(funcion) else version_sync

    return envoltorio


def _es_coroutine(funcion: Any) -> bool:
    import inspect

    return inspect.iscoroutinefunction(funcion)


def _publicar_duracion(nombre: str, inicio: float, etiquetas: dict[str, str]) -> None:
    """Publica la duración de una operación en el histograma."""
    duracion = time.perf_counter() - inicio
    his = histograma(
        "projecta_tareas_duracion_segundos",
        "Duración de las tareas de Projecta PMO Intelligence.",
        ["tarea", "cola"],
    )
    if his is not None:
        his.labels(
            tarea=nombre, cola=etiquetas.get("cola", "desconocida")
        ).observe(duracion)


def registrar_tarea(nombre: str, cola: str, resultado: str) -> None:
    """Cuenta la ejecución de una tarea con su resultado."""
    LOGGER_TAREAS = contador(
        "projecta_tareas_ejecutadas_total",
        "Tareas de Projecta PMO Intelligence ejecutadas.",
        ["tarea", "cola", "resultado"],
    )
    if LOGGER_TAREAS is not None:
        LOGGER_TAREAS.labels(tarea=nombre, cola=cola, resultado=resultado).inc()


def error_inesperado(excepcion: BaseException, **contexto: Any) -> None:
    """Reporta un error no controlado con su contexto.

    Si Sentry está configurado, se le entrega; si no, se registra en el log
    con el ``correlation_id``. En ambos casos, la operación que falló no
    arrastra al proceso.
    """
    identificador = correlation_id_actual()
    extra = ", ".join(f"{clave}={valor}" for clave, valor in contexto.items())
    LOG.error(
        "error no controlado en Projecta PMO Intelligence [%s] %s: %s",
        identificador or "sin-correlacion",
        extra or "sin-contexto",
        excepcion,
        exc_info=True,
    )
    sentry = os.environ.get("SENTRY_DSN")
    if sentry:
        try:  # pragma: no cover - depende de la configuración externa
            import sentry_sdk

            with sentry_sdk.push_scope() as ambito:
                ambito.set_context("projecta", dict(contexto))
                ambito.set_tag("correlation_id", identificador or "")
                sentry_sdk.capture_exception(excepcion)
        except ImportError:
            pass


# ─────────────────────────── instalación ───────────────────────────


def instalar(app: Any) -> None:
    """Instrumenta la aplicación con OpenTelemetry y expone ``/metrics``.

    Nunca lanza: si OpenTelemetry no está disponible, la aplicación sigue
    arrancando sin trazas.
    """
    _registrar_metricas_de_la_aplicacion()
    _instalar_open_telemetry(app)
    _exponer_metricas(app)


def _registrar_metricas_de_la_aplicacion() -> None:
    """Crea las métricas propias de la aplicación.

    Se registran **al arrancar**, no al usarse la primera vez. Si waited
    al primer uso, un panel de Grafana mostraría series vacías hasta que
    hubiera tráfico, y una alerta podría no dispararse en el momento
    crítico por un contador que todavía no existía.
    """
    contador(
        "projecta_peticiones_total",
        "Peticiones HTTP atendidas por Projecta PMO Intelligence.",
        ["ruta", "metodo", "estado"],
    )
    histograma(
        "projecta_api_peticiones_latencia_segundos",
        "Duración de las peticiones HTTP de Projecta PMO Intelligence.",
        ["ruta", "metodo", "estado"],
    )
    contador(
        "projecta_tareas_ejecutadas_total",
        "Tareas de Projecta PMO Intelligence ejecutadas.",
        ["tarea", "cola", "resultado"],
    )
    histograma(
        "projecta_tareas_duracion_segundos",
        "Duración de las tareas de Projecta PMO Intelligence.",
        ["tarea", "cola"],
    )
    medidor(
        "projecta_sesiones_activas",
        "Sesiones de usuario activas en Projecta PMO Intelligence.",
    )
    medidor(
        "projecta_trabajadores_celery",
        "Trabajadores de Celery conectados al broker.",
    )
    medidor(
        "projecta_almacenamiento_disponible",
        "Disponibilidad del almacenamiento de objetos (1 disponible, 0 no).",
    )
    medidor(
        "projecta_base_de_datos_disponible",
        "Disponibilidad de la base de datos (1 disponible, 0 no).",
    )
    contador(
        "projecta_auditoria_eventos_total",
        "Eventos de auditoría registrados, por tipo.",
        ["tipo"],
    )
    contador(
        "projecta_auditoria_rechazos_total",
        "Operaciones rechazadas por permisos.",
        ["regla"],
    )
    contador(
        "projecta_errores_total",
        "Errores no controlados en Projecta PMO Intelligence.",
        ["ruta"],
    )


def _instalar_open_telemetry(app: Any) -> None:
    """Configura el exportador OTLP si está disponible."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    try:  # pragma: no cover - depende del entorno
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        recurso = Resource.create(
            {"service.name": "projecta-pmo-intelligence", "service.version": settings.version}
        )
        proveedor = TracerProvider(resource=recurso)
        proveedor.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(proveedor)
        FastAPIInstrumentor.instrument_app(app)
        LOG.info("OpenTelemetry instrumentado hacia %s", endpoint)
    except Exception as exc:  # pragma: no cover - depende del entorno
        LOG.warning("no se pudo instrumentar OpenTelemetry: %s", exc)


def _exponer_metricas(app: Any) -> None:
    """Registra el endpoint ``/metrics`` de Prometheus."""
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:  # pragma: no cover - dependencia opcional
        return
    from fastapi import Response

    @app.get("/metrics", include_in_schema=False)
    async def metricas() -> Response:
        """Expone las métricas de Prometheus."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def middleware_correlacion(app: Any) -> None:
    """Añade el identificador de correlación a cada petición.

    Si la petición trae ``X-Correlation-Id``, se respeta; si no, se genera
    uno. En ambos casos se devuelve en la respuesta, para que quien llama
    pueda correlacionar sus registros con los del servidor.
    """

    @app.middleware("http")
    async def _con_correlacion(request: Any, call_next: Any) -> Any:
        from fastapi.responses import JSONResponse

        identificador = request.headers.get(CABECERA_CORRELACION) or str(uuid.uuid4())
        establecer_correlacion(identificador)
        estado = "500"
        try:
            respuesta = await call_next(request)
            estado = str(respuesta.status_code)
            respuesta.headers[CABECERA_CORRELACION] = identificador
            return respuesta
        except Exception as exc:
            error_inesperado(exc, ruta=str(request.url.path))
            return JSONResponse(
                status_code=500,
                content={"detalle": "Error interno", "correlation_id": identificador},
                headers={CABECERA_CORRELACION: identificador},
            )
        finally:
            _contar_peticion(request, estado)
            CONTEXTO_ACTUAL.pop("correlation_id", None)


def _contar_peticion(request: Any, estado: str) -> None:
    """Cuenta la petición en las métricas de la aplicación."""
    try:
        metodo = str(getattr(request, "method", "GET"))
        ruta = str(getattr(request.url, "path", "desconocida"))
    except Exception:
        return
    total = contador(
        "projecta_peticiones_total",
        "Peticiones HTTP atendidas por Projecta PMO Intelligence.",
        ["ruta", "metodo", "estado"],
    )
    if total is not None:
        total.labels(ruta=ruta, metodo=metodo, estado=estado).inc()
    if estado.startswith("5"):
        errores = contador(
            "projecta_errores_total",
            "Errores no controlados en Projecta PMO Intelligence.",
            ["ruta"],
        )
        if errores is not None:
            errores.labels(ruta=ruta).inc()
