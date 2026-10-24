#!/usr/bin/env bash
# =============================================================================
# Projecta PMO Intelligence — arranque nativo del esqueleto (sin Docker).
# =============================================================================
# Levanta la API contra los servicios reales del entorno. Es el camino de
# desarrollo cuando no hay demonio Docker disponible.
#
# Uso:
#   scripts/levantar_stack.sh              # arranca la API en primer plano
#   scripts/levantar_stack.sh --background # arranca en segundo plano
#   scripts/levantar_stack.sh --stop       # detiene lo que quedara levantado
#
# Con Docker disponible, el camino equivalente es `docker compose up -d`.
# =============================================================================

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

PYTHON="${PYTHON:-/workspace/.venv/bin/python}"
PUERTO="${PUERTO:-8000}"
HOST="${HOST:-127.0.0.1}"
ARCHIVO_PID="${ARCHIVO_PID:-/tmp/projecta_api.pid}"
ARCHIVO_LOG="${ARCHIVO_LOG:-/tmp/projecta_api.log}"

detener() {
    if [ -f "$ARCHIVO_PID" ]; then
        PID="$(cat "$ARCHIVO_PID")"
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID" 2>/dev/null || true
            sleep 1
            kill -9 "$PID" 2>/dev/null || true
        fi
        rm -f "$ARCHIVO_PID"
        echo "API detenida (pid $PID)."
    else
        pkill -f "uvicorn app.main:app" 2>/dev/null || true
        echo "No había ningún PID registrado; se intentó detener cualquier uvicorn en curso."
    fi
}

case "${1:-}" in
    --stop)
        detener
        exit 0
        ;;
    --help | -h)
        sed -n '2,16p' "$0"
        exit 0
        ;;
esac

# --- Entorno -----------------------------------------------------------------
if [ -f /tmp/projecta_env.sh ]; then
    # shellcheck disable=SC1091
    source /tmp/projecta_env.sh
    echo "Variables de los servicios reales cargadas."
else
    export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://postgres:@postgres?host=/tmp/projecta_pgdata}"
    export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6398/0}"
    export CELERY_BROKER_URL="${CELERY_BROKER_URL:-redis://127.0.0.1:6398/1}"
    export CELERY_RESULT_BACKEND="${CELERY_RESULT_BACKEND:-redis://127.0.0.1:6398/2}"
    echo "No se encontró /tmp/projecta_env.sh: se usan las variables por defecto."
fi

# La clave de firma no tiene valor por defecto a propósito. Para desarrollo se
# genera una efímera si no se ha proporcionado ninguna.
if [ -z "${SECRET_KEY:-}" ]; then
    export SECRET_KEY="$(openssl rand -hex 32)"
    echo "SECRET_KEY no definida: se generó una clave efímera para esta sesión."
fi

export MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-minioadmin}"
export PYTHONPATH="$RAIZ${PYTHONPATH:+:$PYTHONPATH}"

# --- Comprobación previa -----------------------------------------------------
if ! "$PYTHON" -c "import app.main" 2>/dev/null; then
    echo "ERROR: la aplicación no se puede importar. Revisa las dependencias:" >&2
    echo "  $PYTHON -m pip install --index-url https://pypi.org/simple/ -r requirements.txt" >&2
    exit 1
fi

echo "Projecta PMO Intelligence — arrancando la API en http://${HOST}:${PUERTO}"
echo "  Base de datos: ${DATABASE_URL%%:*}://***"
echo "  Broker Celery: ${CELERY_BROKER_URL}"

if [ "${1:-}" = "--background" ]; then
    "$PYTHON" -m uvicorn app.main:app --host "$HOST" --port "$PUERTO" >"$ARCHIVO_LOG" 2>&1 &
    PID=$!
    echo "$PID" >"$ARCHIVO_PID"
    sleep 3
    if kill -0 "$PID" 2>/dev/null; then
        echo "API arrancada en segundo plano (pid $PID). Log: ${ARCHIVO_LOG}"
        echo "Comprobación: curl -s http://${HOST}:${PUERTO}/ready"
    else
        echo "ERROR: la API no arrancó. Log:" >&2
        tail -20 "$ARCHIVO_LOG" >&2
        exit 1
    fi
    exit 0
fi

exec "$PYTHON" -m uvicorn app.main:app --host "$HOST" --port "$PUERTO"
