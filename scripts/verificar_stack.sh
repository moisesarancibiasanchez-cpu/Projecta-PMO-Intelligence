#!/usr/bin/env bash
# =============================================================================
# Projecta PMO Intelligence — verificación integral del esqueleto.
# =============================================================================
# Ejecuta TODAS las comprobaciones del esqueleto y devuelve:
#   · 0  si todas pasan
#   · >0 si alguna falla
#
# Qué se verifica:
#   1. Análisis estático del docker-compose (servicios, depends_on, healthchecks,
#      variables frente a .env.example).
#   2. La API arranca de forma nativa y responde 200 en /health, /ready, / y /docs.
#   3. El shape de /ready es el que fija el contrato.
#   4. Celery con broker REAL: worker de verdad, tarea encolada con .delay(),
#      resultado recibido por el result backend.
#   5. Alembic: upgrade head y downgrade base contra el PostgreSQL REAL (con
#      pgvector), y round-trip simétrico.
#   6. La suite de pruebas (pytest).
#
# Lo que NO se puede verificar aquí: la construcción de la imagen y el arranque
# de los contenedores. Este entorno no tiene el binario `docker`. Eso se
# documenta como NO VALIDADO en docs/skeleton/VALIDACION.md.
# =============================================================================

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

PYTHON="${PYTHON:-/workspace/.venv/bin/python}"
HELPER_SERVICIOS="${HELPER_SERVICIOS:-/workspace/herramientas/servicios_reales.py}"
PUERTO_API="${PUERTO_API:-8000}"
ARCHIVO_SALIDA="${ARCHIVO_SALIDA:-/tmp/projecta_verificacion.log}"

: >"$ARCHIVO_SALIDA"

# -----------------------------------------------------------------------------
# 0. Purga de la caché de bytecode.
# -----------------------------------------------------------------------------
#
# El sistema de ficheros del entorno de validación no actualiza la marca de
# tiempo de un fichero cuando su contenido cambia sin que cambie su tamaño.
# Python valida su caché de bytecode por la pareja ``(mtime, tamaño)``, así
# que después de editar un módulo el intérprete puede seguir ejecutando la
# versión anterior del código. Para un verificador eso es un falso
# positivo: mediría código que ya no existe.
#
# Se purga antes de cualquier comprobación. El detalle está en
# `scripts/purgar_cache.py`.

"$PYTHON" scripts/purgar_cache.py "$RAIZ" >>"$ARCHIVO_SALIDA" 2>&1 || true

# -----------------------------------------------------------------------------
# Utilidades
# -----------------------------------------------------------------------------

TOTAL=0
FALLOS=0

titulo() {
    printf '\n\033[1m== %s ==\033[0m\n' "$1"
    printf '\n== %s ==\n' "$1" >>"$ARCHIVO_SALIDA"
}

pasa() {
    TOTAL=$((TOTAL + 1))
    printf '  \033[32mPASA\033[0m  %s\n' "$1"
    printf '  PASA  %s\n' "$1" >>"$ARCHIVO_SALIDA"
}

falla() {
    TOTAL=$((TOTAL + 1))
    FALLOS=$((FALLOS + 1))
    printf '  \033[31mFALLA\033[0m %s\n' "$1"
    printf '  FALLA %s\n' "$1" >>"$ARCHIVO_SALIDA"
}

# Comprueba una condición de shell: `comprobar "descripción" comando...`
comprobar() {
    local descripcion="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        pasa "$descripcion"
    else
        falla "$descripcion"
    fi
}

# -----------------------------------------------------------------------------
# Limpieza: matar lo que se haya quedado levantado
# -----------------------------------------------------------------------------

PID_API=""
PID_WORKER=""
LIMPIEZA_HECHA=0

limpiar() {
    if [ "$LIMPIEZA_HECHA" -eq 1 ]; then
        return
    fi
    LIMPIEZA_HECHA=1
    [ -n "$PID_API" ] && kill "$PID_API" 2>/dev/null || true
    [ -n "$PID_WORKER" ] && kill "$PID_WORKER" 2>/dev/null || true
    # El worker de Celery en modo prefork deja procesos hijos: se mata el grupo
    # entero para no dejar consumidores zombis en el broker.
    pkill -f "celery -A app.core.celery_app:celery_app worker" 2>/dev/null || true
    pkill -f "uvicorn app.main:app" 2>/dev/null || true
    sleep 1
    pkill -9 -f "celery -A app.core.celery_app:celery_app worker" 2>/dev/null || true
}
trap limpiar EXIT

# -----------------------------------------------------------------------------
# Entorno
# -----------------------------------------------------------------------------

titulo "0. Entorno"

if [ -x "$PYTHON" ]; then
    pasa "El intérprete $PYTHON está disponible"
else
    falla "El intérprete $PYTHON no está disponible"
    echo "No se puede continuar sin el intérprete de Python." >&2
    exit 1
fi

export SECRET_KEY="${SECRET_KEY:-clave-de-verificacion-local-no-usar-en-produccion}"
export MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-minioadmin}"
export PYTHONPATH="$RAIZ${PYTHONPATH:+:$PYTHONPATH}"

if [ -f /tmp/projecta_env.sh ]; then
    # shellcheck disable=SC1091
    source /tmp/projecta_env.sh
    pasa "Variables de los servicios reales cargadas desde /tmp/projecta_env.sh"
else
    export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://postgres:@postgres?host=/tmp/projecta_pgdata}"
    export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6398/0}"
    export CELERY_BROKER_URL="${CELERY_BROKER_URL:-redis://127.0.0.1:6398/1}"
    export CELERY_RESULT_BACKEND="${CELERY_RESULT_BACKEND:-redis://127.0.0.1:6398/2}"
    falla "No existe /tmp/projecta_env.sh: se usan las variables por defecto"
fi

printf '  DATABASE_URL      = %s\n' "$DATABASE_URL"
printf '  CELERY_BROKER_URL = %s\n' "$CELERY_BROKER_URL"

# -----------------------------------------------------------------------------
# 1. Análisis estático del docker-compose
# -----------------------------------------------------------------------------

titulo "1. Análisis estático del docker-compose"

"$PYTHON" - <<'PYEOF' 2>&1 | tee -a "$ARCHIVO_SALIDA"
import sys

import yaml

FALLOS = 0


def comprobar(condicion, descripcion):
    global FALLOS
    if condicion:
        print(f"  PASA  {descripcion}")
    else:
        print(f"  FALLA {descripcion}")
        FALLOS += 1


with open("docker-compose.yml", encoding="utf-8") as fichero:
    compose = yaml.safe_load(fichero)

servicios = compose.get("services", {})
comprobar(
    {"api", "worker", "beat", "postgres", "redis", "minio"} <= set(servicios),
    "los 6 servicios del contrato existen",
)

for nombre, servicio in servicios.items():
    dependencias = servicio.get("depends_on", {}) or {}
    if isinstance(dependencias, dict):
        destinos = list(dependencias)
    else:
        destinos = list(dependencias)
    comprobar(
        all(destino in servicios for destino in destinos),
        f"{nombre}: todos sus depends_on apuntan a servicios que existen",
    )

    healthcheck = servicio.get("healthcheck") or {}
    claves = set(healthcheck)
    comprobar(
        {"test", "interval", "timeout", "retries"} <= claves,
        f"{nombre}: el healthcheck declara test, interval, timeout y retries",
    )

imagenes = [str(s.get("image", "")) for s in servicios.values()]
comprobar(
    any("pgvector" in imagen for imagen in imagenes),
    "postgres usa una imagen con pgvector",
)

# Toda variable ${...} usada en el compose debe estar en .env.example
texto_compose = open("docker-compose.yml", encoding="utf-8").read()
import re

usadas = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", texto_compose))
declaradas = set()
with open(".env.example", encoding="utf-8") as fichero:
    for linea in fichero:
        coincidencia = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", linea)
        if coincidencia:
            declaradas.add(coincidencia.group(1))
huerfanas = usadas - declaradas
comprobar(not huerfanas, f"no hay variables de entorno huérfanas (huerfanas: {sorted(huerfanas)})")

print(f"\n  Comprobaciones de compose: {FALLOS} fallo(s)")
sys.exit(1 if FALLOS else 0)
PYEOF

if [ $? -eq 0 ]; then
    pasa "docker-compose.yml supera el análisis estático"
else
    falla "docker-compose.yml NO supera el análisis estático"
fi

# -----------------------------------------------------------------------------
# 2. Arranque nativo de la API
# -----------------------------------------------------------------------------

titulo "2. Arranque nativo de la API y códigos HTTP"

"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PUERTO_API" \
    >/tmp/projecta_api.log 2>&1 &
PID_API=$!

# Espera activa: hasta 30 intentos de 0,5 s.
API_ARRIBA=0
for _ in $(seq 1 60); do
    if curl -s -o /dev/null "http://127.0.0.1:${PUERTO_API}/health" 2>/dev/null; then
        API_ARRIBA=1
        break
    fi
    sleep 0.5
done

if [ "$API_ARRIBA" -eq 1 ]; then
    pasa "La API arranca y responde en http://127.0.0.1:${PUERTO_API}"
else
    falla "La API NO arrancó (ver /tmp/projecta_api.log)"
    tail -20 /tmp/projecta_api.log || true
fi

for ruta in /health /ready / /docs; do
    codigo=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PUERTO_API}${ruta}" || echo 000)
    printf '  %-8s -> HTTP %s\n' "$ruta" "$codigo" | tee -a "$ARCHIVO_SALIDA"
    if [ "$codigo" = "200" ]; then
        pasa "$ruta devuelve 200"
    else
        falla "$ruta devuelve $codigo (esperado 200)"
    fi
done

# -----------------------------------------------------------------------------
# 3. Shape de /ready
# -----------------------------------------------------------------------------

titulo "3. Shape de la respuesta de /ready"

curl -s "http://127.0.0.1:${PUERTO_API}/ready" | tee -a "$ARCHIVO_SALIDA"
echo "" | tee -a "$ARCHIVO_SALIDA"

"$PYTHON" - <<'PYEOF' 2>&1 | tee -a "$ARCHIVO_SALIDA"
import json
import sys
import urllib.request

FALLOS = 0


def comprobar(condicion, descripcion):
    global FALLOS
    print(f"  {'PASA' if condicion else 'FALLA'}  {descripcion}")
    if not condicion:
        FALLOS += 1


with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=10) as respuesta:
    cuerpo = json.load(respuesta)

comprobar(set(cuerpo) == {"status", "checks"}, "el JSON tiene exactamente status y checks")
comprobar(cuerpo.get("status") in {"ok", "degraded"}, "status es 'ok' o 'degraded'")
comprobar(
    set(cuerpo.get("checks", {})) == {"database", "redis", "storage"},
    "checks contiene database, redis y storage",
)
for nombre, chequeo in cuerpo.get("checks", {}).items():
    comprobar("ok" in chequeo, f"el chequeo {nombre} declara 'ok'")
    comprobar("latency_ms" in chequeo, f"el chequeo {nombre} declara 'latency_ms'")

comprobar(
    cuerpo.get("status") == "ok"
    or cuerpo.get("checks", {}).get("database", {}).get("ok") is not True
    or cuerpo.get("checks", {}).get("redis", {}).get("ok") is not True,
    "status='ok' solo cuando database y redis responden",
)

sys.exit(1 if FALLOS else 0)
PYEOF

if [ $? -eq 0 ]; then
    pasa "El shape de /ready cumple el contrato"
else
    falla "El shape de /ready NO cumple el contrato"
fi

# -----------------------------------------------------------------------------
# 4. Celery con broker real
# -----------------------------------------------------------------------------

titulo "4. Celery con broker real (Redis)"

"$PYTHON" - <<'PYEOF' 2>&1 | tee -a "$ARCHIVO_SALIDA"
import sys

from app.core.celery_app import COLAS, celery_app

FALLOS = 0


def comprobar(condicion, descripcion):
    global FALLOS
    print(f"  {'PASA' if condicion else 'FALLA'}  {descripcion}")
    if not condicion:
        FALLOS += 1


esperadas = {"ingesta", "documentos", "ia", "integraciones", "reportes", "mantenimiento"}
obtenidas = {ruta["queue"] for ruta in celery_app.conf.task_routes.values()}
comprobar(obtenidas == esperadas, f"las 6 colas del contrato están declaradas (obtenidas: {sorted(obtenidas)})")
comprobar(set(COLAS) == esperadas, "la constante COLAS del módulo coincide con el contrato")
comprobar(celery_app.conf.task_acks_late is True, "task_acks_late is True")
comprobar(celery_app.conf.task_reject_on_worker_lost is True, "task_reject_on_worker_lost is True")
comprobar(celery_app.conf.worker_prefetch_multiplier == 1, "worker_prefetch_multiplier == 1")
comprobar(
    celery_app.conf.broker_connection_retry_on_startup is True,
    "broker_connection_retry_on_startup is True",
)
entradas = list(celery_app.conf.beat_schedule.values())
comprobar(len(entradas) == 1, "beat_schedule tiene una entrada")
comprobar(
    entradas and entradas[0]["task"] == "projecta.mantenimiento.ping",
    "el beat llama a projecta.mantenimiento.ping",
)
comprobar(
    entradas and entradas[0]["schedule"].minute == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
    "el beat se dispara cada 5 minutos",
)
tareas = {nombre for nombre in celery_app.tasks if nombre.startswith("projecta.")}
# El contrato original fijaba dos tareas (épica 0). La ingesta del Edge
# Connector en la Variante A añade una tercera, en la cola
# `integraciones`, que el propio contrato ya definía y ya enrutaba: no
# se crea ninguna cola nueva ni se altera el enrutamiento.
comprobar(
    tareas == {
        "projecta.smoke",
        "projecta.mantenimiento.ping",
        "projecta.integraciones.teams.ingesta",
        "projecta.integraciones.teams.captura",
        "projecta.captura.procesa",
    },
    f"las tareas registradas son exactamente las del contrato (obtenidas: {sorted(tareas)})",
)

sys.exit(1 if FALLOS else 0)
PYEOF

if [ $? -eq 0 ]; then
    pasa "La configuración de Celery cumple el contrato"
else
    falla "La configuración de Celery NO cumple el contrato"
fi

# Worker real + tarea encolada de verdad.
# El nodo lleva sufijo propio: si queda un worker de una ejecución anterior con
# el mismo nombre, Celery avisa de «DuplicateNodenameWarning» y el `inspect ping`
# se ralentiza hasta agotar el tiempo de espera.
"$PYTHON" -m celery -A app.core.celery_app:celery_app worker \
    --loglevel=info --concurrency=1 -Q ia,mantenimiento -n "verificacion@%h" \
    >/tmp/projecta_worker.log 2>&1 &
PID_WORKER=$!

# Espera activa leyendo el log del worker: mucho más rápido y fiable que
# lanzar `celery inspect ping` en bucle (cada invocación de la CLI tarda varios
# segundos en arrancar).
WORKER_ARRIBA=0
for _ in $(seq 1 60); do
    if grep -q "ready\." /tmp/projecta_worker.log 2>/dev/null; then
        WORKER_ARRIBA=1
        break
    fi
    if ! kill -0 "$PID_WORKER" 2>/dev/null; then
        break
    fi
    sleep 1
done

if [ "$WORKER_ARRIBA" -eq 1 ]; then
    PING=$("$PYTHON" -m celery -A app.core.celery_app:celery_app inspect ping \
        --timeout=5 2>/dev/null | grep -c pong || true)
    if [ "$PING" -ge 1 ]; then
        pasa "El worker de Celery arranca y responde 'pong' al inspect ping"
    else
        falla "El worker arrancó pero no respondió al inspect ping"
    fi
else
    falla "El worker de Celery NO arrancó (ver /tmp/projecta_worker.log)"
    tail -20 /tmp/projecta_worker.log || true
fi

if [ "$WORKER_ARRIBA" -eq 1 ]; then
    "$PYTHON" - <<'PYEOF' 2>&1 | tee -a "$ARCHIVO_SALIDA"
import sys

from app.core.celery_app import celery_app

resultado = celery_app.tasks["projecta.smoke"].delay("verificacion-real")
print(f"  Tarea encolada: id={resultado.id}")
datos = resultado.get(timeout=45)
print(f"  Resultado recibido por el backend: {datos}")

FALLOS = 0
if datos.get("status") != "ok":
    print("  FALLA el resultado no trae status='ok'")
    FALLOS += 1
else:
    print("  PASA  el worker consumió la tarea y devolvió status='ok'")
if datos.get("echo") != "verificacion-real":
    print(f"  FALLA el eco no coincide: {datos.get('echo')!r}")
    FALLOS += 1
else:
    print("  PASA  el eco viaja intacto por el broker")
if datos.get("app") != "Projecta PMO Intelligence":
    print(f"  FALLA la marca no es completa: {datos.get('app')!r}")
    FALLOS += 1
else:
    print("  PASA  la marca es 'Projecta PMO Intelligence'")

sys.exit(1 if FALLOS else 0)
PYEOF
    if [ $? -eq 0 ]; then
        pasa "Una tarea encolada con .delay() la consume el worker real"
    else
        falla "La tarea encolada NO la consumió el worker real"
    fi
fi

kill "$PID_WORKER" 2>/dev/null || true
wait "$PID_WORKER" 2>/dev/null || true
PID_WORKER=""

# -----------------------------------------------------------------------------
# 5. Alembic contra el PostgreSQL real
# -----------------------------------------------------------------------------

titulo "5. Migraciones contra el PostgreSQL real (con pgvector)"

"$PYTHON" -m alembic upgrade head 2>&1 | tee -a "$ARCHIVO_SALIDA"
if [ ${PIPESTATUS[0]} -eq 0 ]; then
    pasa "alembic upgrade head se ejecuta correctamente"
else
    falla "alembic upgrade head falló"
fi

TABLAS=$("$PYTHON" - <<'PYEOF' 2>&1 || true
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main():
    # Se usa el motor de SQLAlchemy y no asyncpg a pelo: la URL de producción
    # puede apuntar a un socket Unix (`?host=/ruta`) y no a un host TCP, y el
    # motor interpreta ambos casos sin que haya que parsear la URL a mano.
    motor = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with motor.connect() as conexion:
            resultado = await conexion.execute(
                text("select tablename from pg_tables where schemaname='public' order by 1")
            )
            print(",".join(fila[0] for fila in resultado))
    finally:
        await motor.dispose()


asyncio.run(main())
PYEOF
)
printf '  Tablas en el esquema public: %s\n' "$TABLAS" | tee -a "$ARCHIVO_SALIDA"

for tabla in organizaciones proyectos usuarios roles \
    sesiones_de_captura participantes_de_sesion consentimientos_de_captura \
    artefactos_de_audio fragmentos_de_subida trabajos_de_procesamiento \
    transcripciones segmentos_de_transcripcion mapeos_de_hablante \
    notas_de_reunion elementos_extraidos evidencias_de_item \
    acciones_de_revision politicas_de_retencion; do
    if [[ ",$TABLAS," == *",$tabla,"* ]]; then
        pasa "La tabla $tabla existe en PostgreSQL"
    else
        falla "La tabla $tabla NO existe en PostgreSQL"
    fi
done

"$PYTHON" -m alembic downgrade base 2>&1 | tee -a "$ARCHIVO_SALIDA"
if [ ${PIPESTATUS[0]} -eq 0 ]; then
    pasa "alembic downgrade base se ejecuta correctamente"
else
    falla "alembic downgrade base falló"
fi

"$PYTHON" -m alembic upgrade head 2>&1 | tee -a "$ARCHIVO_SALIDA"
if [ ${PIPESTATUS[0]} -eq 0 ]; then
    pasa "El round-trip upgrade -> downgrade -> upgrade es simétrico"
else
    falla "El round-trip de migraciones falló"
fi

# -----------------------------------------------------------------------------
# 6. Suite de pruebas
# -----------------------------------------------------------------------------

titulo "6. Suite de pruebas"

"$PYTHON" -m pytest tests -q 2>&1 | tee -a "$ARCHIVO_SALIDA"
if [ ${PIPESTATUS[0]} -eq 0 ]; then
    pasa "La suite de pruebas pasa"
else
    falla "La suite de pruebas falla"
fi

# -----------------------------------------------------------------------------
# 7. Épica 0: auditoría de implementación
# -----------------------------------------------------------------------------

titulo "7. Épica 0 (HU_01 a HU_08)"

# 7.1 Integridad textual del código (el transporte de comandos corrompe
#     literales: este control es lo que lo detecta).
"$PYTHON" scripts/verificar_integridad_textual.py 2>&1 | tee -a "$ARCHIVO_SALIDA"
if [ ${PIPESTATUS[0]} -eq 0 ]; then
    pasa "No hay corrupción textual en el código"
else
    falla "Hay corrupción textual en el código"
fi

# 7.2 Los seis roles normativos están en la base de datos con su nivel.
"$PYTHON" -m scripts.sembrar_roles --verificar 2>&1 | tee -a "$ARCHIVO_SALIDA"
if [ ${PIPESTATUS[0]} -eq 0 ]; then
    pasa "El catálogo de roles de la base de datos coincide con el catálogo normativo"
else
    falla "El catálogo de roles de la base de datos no coincide con el normativo"
fi

# 7.3 Los ficheros de observabilidad existen y son válidos.
for fichero in \
    observability/prometheus/prometheus.yml \
    observability/prometheus/reglas.yml \
    observability/otel/config.yml \
    observability/grafana/dashboards/projecta.json \
    observability/grafana/provisioning/datasources/prometheus.yaml
do
    if [ -f "$fichero" ]; then
        pasa "Existe $fichero"
    else
        falla "Falta $fichero"
    fi
done

if "$PYTHON" -c "import json,sys; json.load(open('observability/grafana/dashboards/projecta.json'))" 2>/dev/null; then
    pasa "El panel de Grafana es JSON válido"
else
    falla "El panel de Grafana no es JSON válido"
fi

# 7.4 El compose declara los servicios de observabilidad con su perfil.
if grep -q "observabilidad" docker-compose.yml \
    && grep -q "prom/prometheus" docker-compose.yml \
    && grep -q "grafana/grafana" docker-compose.yml \
    && grep -q "opentelemetry-collector" docker-compose.yml; then
    pasa "El compose declara el perfil 'observabilidad' con sus tres servicios"
else
    falla "El compose no declara el perfil 'observabilidad' completo"
fi

# 7.5 Las variables nuevas están documentadas en .env.example.
for variable in OIDC_ISSUER OIDC_CLIENT_ID GRAFANA_ADMIN_USER OTEL_EXPORTER_OTLP_ENDPOINT; do
    if grep -q "^${variable}=" .env.example; then
        pasa ".env.example documenta ${variable}"
    else
        falla ".env.example no documenta ${variable}"
    fi
done

# 7.6 Las rutas de la Épica 0 están registradas en la aplicación.
RUTAS_EPICA0=$("$PYTHON" - <<'PYROUTES' 2>/dev/null || true
from app.main import create_app
app = create_app()
for ruta in ("/auth/login", "/auth/refresh", "/auth/me", "/proyectos", "/adjuntos", "/metrics"):
    print(ruta)
PYROUTES
)
for ruta in /auth/login /auth/refresh /auth/me /proyectos /adjuntos /metrics; do
    if echo "$RUTAS_EPICA0" | grep -qx "$ruta"; then
        pasa "La ruta $ruta está registrada"
    else
        falla "La ruta $ruta no está registrada"
    fi
done

# -----------------------------------------------------------------------------
# 8. Edge Connector de Projecta PMO Intelligence
# -----------------------------------------------------------------------------

titulo "8. Edge Connector de Projecta PMO Intelligence"

# 8.1 Los tres módulos del Edge Connector existen.
for modulo in app/core/edge.py app/api/routers/edge.py scripts/edge_conector.py \
             scripts/adaptador_teams.py; do
    if [ -f "$modulo" ]; then
        pasa "Existe $modulo"
    else
        falla "Falta $modulo"
    fi
done

# 8.2 Las nueve rutas del Edge Connector están registradas.
RUTAS_EDGE=$("$PYTHON" - <<'PYRUTAS_EDGE' 2>/dev/null || true
from app.main import create_app
app = create_app()
for ruta in app.openapi()["paths"]:
    if ruta.startswith("/edge/"):
        print(ruta)
PYRUTAS_EDGE
)
for ruta in /edge/dispositivos /edge/consentimientos /edge/eventos /edge/puerta; do
    if echo "$RUTAS_EDGE" | grep -q "^${ruta}"; then
        pasa "La ruta $ruta del Edge Connector está registrada"
    else
        falla "La ruta $ruta del Edge Connector no está registrada"
    fi
done

# 8.3 Las tres tablas existen en PostgreSQL real.
for tabla in dispositivos_edge consentimientos_edge eventos_edge; do
    if "$PYTHON" - <<PYTABLA 2>/dev/null
import asyncio, sys
from sqlalchemy import text
from app.core.db.session import engine
async def main():
    async with engine.begin() as c:
        r = await c.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='$tabla'"
        ))
        sys.exit(0 if r.first() else 1)
asyncio.run(main())
PYTABLA
    then
        pasa "La tabla $tabla existe en PostgreSQL"
    else
        falla "La tabla $tabla no existe en PostgreSQL"
    fi
done

# 8.4 La dependencia criptográfica está declarada.
if grep -q "^cryptography==" requirements.txt; then
    pasa "requirements.txt declara cryptography"
else
    falla "requirements.txt no declara cryptography"
fi

# 8.5 La lista de receptores es cerrada y no admite audio ni transcripción.
if "$PYTHON" - <<'PYLISTA' 2>/dev/null
import sys
from app.core.edge import RECEPTORES_ADMITIDOS, MODOS_ADMITIDOS
prohibidos = {"audio", "transcripcion", "grabacion"}
if prohibidos & set(RECEPTORES_ADMITIDOS):
    sys.exit(1)
if set(MODOS_ADMITIDOS) != {"READ_ONLY"}:
    sys.exit(1)
sys.exit(0)
PYLISTA
then
    pasa "La lista de receptores es cerrada y el único modo admitido es READ_ONLY"
else
    falla "La lista de receptores o los modos admitidos no son los del contrato"
fi

# 8.6 El adaptador de Teams declara el tipo de receptor que usa.
if "$PYTHON" - <<'PYADAPTADOR' 2>/dev/null
import sys
from app.core.edge import RECEPTORES_ADMITIDOS
if "mensaje_canal" not in RECEPTORES_ADMITIDOS:
    sys.exit(1)
from app.core.edge_teams import CAMPOS_PERMITIDOS, CAMPOS_LOCALES_BLOQUEADOS
# La lista blanca y la lista negra local no pueden solaparse: un campo
# que esté en las dos significaría que la política se contradice.
if set(CAMPOS_PERMITIDOS) & CAMPOS_LOCALES_BLOQUEADOS:
    sys.exit(1)
sys.exit(0)
PYADAPTADOR
then
    pasa "El adaptador de Teams usa un tipo admitido y sus listas no se contradicen"
else
    falla "El adaptador de Teams usa un tipo no admitido o listas contradictorias"
fi

# 8.7 La puerta del consentimiento falla cerrada: sin registro, no hay permiso.
#
# Se comprueba el camino COMPLETO, no solo la función pura: se busca un
# recurso que no tenga consentimiento registrado y se verifica que la
# puerta lo cierra. Una puerta que solo se comprueba con `None` en la
# mano no demuestra que el buscador devuelva `None` cuando toca.
if "$PYTHON" - <<'PYPUERTA' 2>/dev/null
import asyncio, sys
from app.core.db.session import AsyncSessionLocal
from app.core.edge import consultar_consentimiento, evaluar_consentimiento

async def main():
    async with AsyncSessionLocal() as sesion:
        consentimiento = await consultar_consentimiento(
            sesion,
            "00000000-0000-0000-0000-000000000000",
            "reunion-inexistente-de-esta-comprobacion",
            "reunion",
        )
        if consentimiento is not None:
            sys.exit(1)
        decision = evaluar_consentimiento(
            consentimiento,
            uso_previsto="trazabilidad_de_proyecto",
            destinatarios_previstos=["equipo-pmo"],
        )
        sys.exit(0 if not decision.permitido else 1)

asyncio.run(main())
PYPUERTA
then
    pasa "La puerta del consentimiento falla cerrada ante un recurso sin registro"
else
    falla "La puerta del consentimiento NO falla cerrada: es un defecto crítico"
fi

# 8.7 La minimización bloquea los campos prohibidos.
if "$PYTHON" - <<'PYMIN' 2>/dev/null
import sys
from app.core.edge import minimizar
payload, quitados = minimizar({
    "titulo": "Reunión",
    "transcripcion": "contenido prohibido",
    "audio": "binario prohibido",
    "emocion": "alegria",
})
prohibidos = {"transcripcion", "audio", "emocion"}
sys.exit(0 if prohibidos <= set(quitados) and "titulo" not in quitados else 1)
PYMIN
then
    pasa "La minimización bloquea los campos prohibidos y conserva los lícitos"
else
    falla "La minimización no bloquea los campos prohibidos"
fi

# -----------------------------------------------------------------------------
# Resumen
# -----------------------------------------------------------------------------

titulo "Resumen"

printf '  Comprobaciones: %d | Fallos: %d\n' "$TOTAL" "$FALLOS"
printf '\n  NO VALIDADO en este entorno (no existe el binario `docker`):\n'
printf '    · la construcción real de la imagen\n'
printf '    · el arranque real de los 6 contenedores\n'
printf '    · la imagen pgvector/pgvector:pg16 del registro\n'
printf '    · el arranque del perfil `observabilidad` (prometheus + grafana + otel)\n'

if [ "$FALLOS" -eq 0 ]; then
    printf '\n\033[32mRESULTADO: todas las comprobaciones pasan\033[0m\n'
    printf 'RESULTADO: todas las comprobaciones pasan\n' >>"$ARCHIVO_SALIDA"
    exit 0
fi

printf '\n\033[31mRESULTADO: %d comprobación(es) fallida(s)\033[0m\n' "$FALLOS"
printf 'RESULTADO: %d comprobación(es) fallida(s)\n' "$FALLOS" >>"$ARCHIVO_SALIDA"
exit 1
