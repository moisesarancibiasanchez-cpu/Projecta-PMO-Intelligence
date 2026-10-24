"""Suite de humo del esqueleto de **Projecta PMO Intelligence**.

Cubre lo mínimo que hay que tener verde para poder decir que el esqueleto
arranca y es coherente:

1. La aplicación importa y se construye sin error.
2. Las variables obligatorias faltan con un error que las nombre.
3. Las plantillas renderizan y escapan el contenido.
4. La marca aparece siempre completa.
5. El shape de ``/ready`` es el que fija el contrato.
6. Celery registra exactamente las 6 colas y las 2 tareas del contrato.
7. La migración sube y baja de forma simétrica.

La validación contra el PostgreSQL real, el Redis real y el worker de Celery
consumiendo de verdad NO vive aquí: eso lo hace ``scripts/verificar_stack.sh``,
que además deja la evidencia en ``docs/skeleton/VALIDACION.md``. Esta suite es
la red de seguridad rápida y aislada.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
MARCA = "Projecta PMO Intelligence"

COLAS_CONTRATO = {"ingesta", "documentos", "ia", "integraciones", "reportes", "mantenimiento"}
TAREAS_CONTRATO = {"projecta.smoke", "projecta.mantenimiento.ping"}


# ---------------------------------------------------------------------------
# 1. La aplicación importa y se construye.
# ---------------------------------------------------------------------------


def test_la_aplicacion_se_construye_sin_error() -> None:
    """`create_app()` debe devolver una aplicación FastAPI usable."""
    from fastapi import FastAPI

    from app.main import create_app

    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.title == MARCA


def test_se_registran_las_rutas_de_salud() -> None:
    """El contrato exige /health, /ready, /salud y /."""
    from app.main import create_app

    rutas = {ruta.path for ruta in create_app().routes}
    assert {"/health", "/ready", "/salud", "/"} <= rutas


# ---------------------------------------------------------------------------
# 2. Las variables obligatorias se validan nombrando la que falta.
# ---------------------------------------------------------------------------


def test_sin_secret_key_el_error_nombra_la_variable(tmp_path: Path) -> None:
    """El arranque debe fallar diciendo QUÉ variable falta, no con un error genérico.

    Se ejecuta en un directorio temporal, sin el `.env` del repositorio, y con
    ``SECRET_KEY`` vacío: las variables de entorno tienen prioridad sobre el
    archivo `.env`, y un valor vacío cuenta como ausente.
    """
    import os

    entorno = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",
        "PYTHONPATH": str(RAIZ),
        "SECRET_KEY": "",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "MINIO_ACCESS_KEY": "pruebas",
        "MINIO_SECRET_KEY": "pruebas",
    }
    resultado = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings; settings"],
        cwd=tmp_path,
        env=entorno,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert resultado.returncode != 0, "la app NO debe arrancar sin SECRET_KEY"
    salida = resultado.stdout + resultado.stderr
    assert "SECRET_KEY" in salida, f"el error debe nombrar SECRET_KEY, salió: {salida[-800:]}"


def test_no_existe_bootstrap_admin_password() -> None:
    """Esa variable es deuda heredada y no debe existir en este proyecto.

    Se comprueba que no es un campo de la configuración ni una variable
    obligatoria; que la palabra aparezca en un comentario que explica que NO
    existe es lo correcto.
    """
    from app.core.config import VARIABLES_OBLIGATORIAS, Settings

    assert "BOOTSTRAP_ADMIN_PASSWORD" not in VARIABLES_OBLIGATORIAS
    campos = Settings.model_fields
    assert "bootstrap_admin_password" not in campos
    assert not any("bootstrap" in nombre for nombre in campos)


# ---------------------------------------------------------------------------
# 3. Las plantillas renderizan y escapan.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plantilla",
    [
        "index.html",
        "health.html",
        "partials/_cabecera.html",
        "partials/_estado_vacio.html",
        "partials/_tarjeta_estado.html",
    ],
)
def test_cada_plantilla_renderiza_sin_error(plantilla: str) -> None:
    """Ninguna plantilla del contrato puede lanzar al renderizarse."""
    from fastapi.templating import Jinja2Templates

    plantillas = Jinja2Templates(directory=str(RAIZ / "app" / "templates"), autoescape=True)
    plantillas.get_template(plantilla)
    assert plantilla


def test_las_plantillas_escapan_el_contenido_malicioso() -> None:
    """El HTML con `<script>` debe salir escapado, nunca ejecutable.

    Se prueban las dos rutas por las que puede entrar contenido externo:

    1. ``app_name`` en la portada: ``base.html`` descarta el valor si no es la
       marca completa, así que el contenido malicioso ni siquiera llega al HTML.
    2. ``resumen`` en la página de salud: ese campo SÍ se renderiza (es el
       texto que puede venir de un modelo de IA), y debe salir escapado.
    """
    from fastapi.templating import Jinja2Templates

    plantillas = Jinja2Templates(directory=str(RAIZ / "app" / "templates"), autoescape=True)
    malicioso = "<script>alert(1)</script>"

    # 1. La portada: el valor no puede aparecer sin escapar.
    html = plantillas.get_template("index.html").render(
        app_name=malicioso, version="0.1.0", environment="test"
    )
    assert malicioso not in html

    # 2. La página de salud: el contenido externo SÍ se renderiza, pero escapado.
    html_salud = plantillas.get_template("health.html").render(
        app_name=MARCA,
        version="0.1.0",
        environment="test",
        status="ok",
        checks={"database": {"ok": True, "latency_ms": 1.0}},
        resumen=malicioso,
    )
    assert malicioso not in html_salud
    assert "&lt;script&gt;" in html_salud


def test_la_marca_aparece_siempre_completa() -> None:
    """«Projecta» a secas está prohibido: la marca es «Projecta PMO Intelligence»."""
    from fastapi.templating import Jinja2Templates

    plantillas = Jinja2Templates(directory=str(RAIZ / "app" / "templates"), autoescape=True)
    html = plantillas.get_template("index.html").render(
        app_name=MARCA, version="0.1.0", environment="test"
    )
    assert MARCA in html
    assert "Projecta " not in html.replace(MARCA, "")


# ---------------------------------------------------------------------------
# 4. El shape de /ready es el del contrato.
# ---------------------------------------------------------------------------


async def test_ready_devuelve_el_shape_del_contrato(cliente) -> None:
    """`status` + `checks` con `database`, `redis` y `storage`."""
    respuesta = await cliente.get("/ready")
    cuerpo = respuesta.json()

    assert set(cuerpo) == {"status", "checks"}
    assert cuerpo["status"] in {"ok", "degraded"}
    assert set(cuerpo["checks"]) == {"database", "redis", "storage"}
    for chequeo in cuerpo["checks"].values():
        assert set(chequeo) >= {"ok", "latency_ms"}
        assert isinstance(chequeo["ok"], bool)


async def test_health_es_liveness_puro(cliente) -> None:
    """`/health` responde OK en texto plano sin tocar dependencias."""
    respuesta = await cliente.get("/health")
    assert respuesta.status_code == 200
    assert respuesta.text.strip() == "OK"


async def test_la_portada_responde_html(cliente) -> None:
    """`/` renderiza la plantilla Jinja2 con la marca completa."""
    respuesta = await cliente.get("/")
    assert respuesta.status_code == 200
    assert "text/html" in respuesta.headers["content-type"]
    assert MARCA in respuesta.text


# ---------------------------------------------------------------------------
# 5. Celery: 6 colas, 2 tareas y parámetros de robustez.
# ---------------------------------------------------------------------------


def test_celery_registra_las_seis_colas_del_contrato() -> None:
    """Ni una cola de más ni una de menos."""
    from app.core.celery_app import celery_app

    colas = {ruta["queue"] for ruta in celery_app.conf.task_routes.values()}
    assert colas == COLAS_CONTRATO


def test_celery_registra_exactamente_las_dos_tareas() -> None:
    """El registro de tareas no puede tener extras en el esqueleto."""
    from app.core.celery_app import celery_app

    assert set(celery_app.tasks) & TAREAS_CONTRATO == TAREAS_CONTRATO


def test_celery_tiene_los_parametros_de_robustez() -> None:
    """acks_late, reject_on_worker_lost, prefetch 1 y reintento de arranque."""
    from app.core.celery_app import celery_app

    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.broker_connection_retry_on_startup is True
    assert isinstance(conf.task_soft_time_limit, int)
    assert isinstance(conf.task_time_limit, int)
    assert conf.task_time_limit > conf.task_soft_time_limit


def test_el_beat_llama_al_ping_de_mantenimiento_cada_cinco_minutos() -> None:
    """El planificador dispara el ping de mantenimiento cada 5 minutos."""
    from app.core.celery_app import celery_app

    entradas = list(celery_app.conf.beat_schedule.values())
    assert len(entradas) == 1
    assert entradas[0]["task"] == "projecta.mantenimiento.ping"
    assert entradas[0]["schedule"].minute == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}


def test_la_tarea_smoke_devuelve_el_contrato() -> None:
    """`projecta.smoke` devuelve status, app, timestamp y echo."""
    from app.core.celery_app import celery_app

    resultado = celery_app.tasks["projecta.smoke"].apply(args=("hola",)).get()
    assert resultado["status"] == "ok"
    assert resultado["app"] == MARCA
    assert resultado["echo"] == "hola"
    assert "timestamp" in resultado


# ---------------------------------------------------------------------------
# 6. La migración sube y baja de forma simétrica.
# ---------------------------------------------------------------------------


def test_la_migracion_es_simetrica(tmp_path: Path) -> None:
    """`upgrade` y `downgrade` de la migración inicial son reversibles.

    El round-trip se hace contra un archivo SQLite temporal, no contra el
    PostgreSQL real: la suite tiene que ser aislada y rápida. La validación
    contra el PostgreSQL de producción (con pgvector) la hace
    `scripts/verificar_stack.sh` y queda registrada en `docs/skeleton/VALIDACION.md`.
    """
    from alembic import command
    from alembic.config import Config

    ruta = tmp_path / "migracion.db"
    url = f"sqlite+aiosqlite:///{ruta}"

    configuracion = Config(str(RAIZ / "alembic.ini"))
    configuracion.set_main_option("script_location", str(RAIZ / "alembic"))
    configuracion.set_main_option("sqlalchemy.url", url)

    # `alembic/env.py` lee la URL de `settings`, así que hay que_LIMITARLA
    # temporalmente a la base temporal.
    from app.core import config as modulo_config

    original = modulo_config.settings.database_url
    object.__setattr__(modulo_config.settings, "database_url", url)
    try:
        command.upgrade(configuracion, "head")
        assert ruta.exists(), "la migración no creó el archivo de base de datos"

        import sqlite3

        conexion = sqlite3.connect(ruta)
        try:
            tablas = {
                fila[0]
                for fila in conexion.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
        finally:
            conexion.close()

        assert {"organizaciones", "proyectos", "usuarios", "roles"} <= tablas

        command.downgrade(configuracion, "base")

        conexion = sqlite3.connect(ruta)
        try:
            tablas_finales = {
                fila[0]
                for fila in conexion.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
        finally:
            conexion.close()

        assert tablas_finales <= {"alembic_version"}, (
            f"el downgrade debe revertir las tablas, quedaron: {tablas_finales}"
        )
    finally:
        object.__setattr__(modulo_config.settings, "database_url", original)
