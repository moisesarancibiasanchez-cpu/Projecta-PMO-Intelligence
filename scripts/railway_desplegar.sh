#!/usr/bin/env bash
# =============================================================================
#  Despliegue de Projecta PMO Intelligence en Railway.
#
#  Crea la topología completa del proyecto en Railway, configura cada
#  servicio y despliega. Todo es reproducible: el script no depende de
#  que alguien haya pulsado botones en el panel.
#
#  Topología (seis servicios):
#
#    · api      → dominio PÚBLICO. La única puerta de entrada.
#    · worker   → sin dominio. Consume la cola `integraciones`.
#    · beat     → sin dominio. Planificador.
#    · postgres → pgvector, volumen persistente, red privada.
#    · redis    → red privada.
#    · minio    → red privada, volumen persistente.
#
#  Todos se hablan por la **red privada** de Railway
#  (`<servicio>.railway.internal`), que va cifrada con WireGuard y
#  aislada por entorno. Solo la API tiene dominio público, y porque el
#  navegador del operador no puede alcanzar la red privada.
#
#  Uso:
#    railway_desplegar.sh --proyecto <id> [--entorno produccion] [--dry-run]
#
#  Antes:
#    railway login            # o export RAILWAY_TOKEN=...
# =============================================================================
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROYECTO=""
ENTORNO="produccion"
DRY_RUN=0

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
fallo() { printf '\n\033[31mFALLO: %s\033[0m\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --proyecto) PROYECTO="$2"; shift 2 ;;
        --entorno)  ENTORNO="$2";  shift 2 ;;
        --dry-run)  DRY_RUN=1;    shift ;;
        -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
        *) fallo "opción desconocida: $1" ;;
    esac
done

[ -n "$PROYECTO" ] || fallo "falta --proyecto"

# ─────────────────────────── comprobaciones previas ───────────────────────────

command -v railway >/dev/null 2>&1 || fallo "el CLI de Railway no está en el PATH"
[ -n "${RAILWAY_TOKEN:-}" ] || railway whoami >/dev/null 2>&1 || \
    fallo "no hay sesión de Railway: ejecuta 'railway login' o exporta RAILWAY_TOKEN"

ejecutar() {
    if [ "$DRY_RUN" -eq 1 ]; then
        info "(simulado) $*"
    else
        info "$*"
        "$@"
    fi
}

# ─────────────────────────── proyecto y servicios ───────────────────────────

log "Proyecto $PROYECTO · entorno $ENTORNO"
ejecutar railway link --project "$PROYECTO" --environment "$ENTORNO"

log "Servicios de datos"
# PostgreSQL: el servicio de base de datos documentado. La extensión
# `vector` **no** se instala aquí: la habilita el `preDeployCommand`
# (`scripts/railway_preparar.py`), que ejecuta
# `CREATE EXTENSION IF NOT EXISTS vector`. Si la imagen del servicio no
# trae la biblioteca, ese comando falla y el despliegue para, con un
# mensaje que dice exactamente qué hacer.
ejecutar railway add --database postgres --service postgres
ejecutar railway add --database redis --service redis
# MinIO: el almacenamiento de objetos, en la red privada, con volumen.
ejecutar railway add --image minio/minio --service minio

log "Servicios de aplicación"
ejecutar railway add --service api
ejecutar railway add --service worker
ejecutar railway add --service beat

# ─────────────────────────── configuración por servicio ───────────────────────────

configurar() {
    local servicio="$1" inicio="$2" pre="$3" salud="$4"
    log "Configuración de '$servicio'"
    ejecutar railway environment edit --service-config "$servicio" \
        --environment "$ENTORNO" \
        build.builder DOCKERFILE
    ejecutar railway environment edit --service-config "$servicio" \
        --environment "$ENTORNO" \
        build.dockerfilePath Dockerfile
    [ -z "$inicio" ] || ejecutar railway environment edit --service-config "$servicio" \
        --environment "$ENTORNO" \
        deploy.startCommand "$inicio"
    [ -z "$pre" ] || ejecutar railway environment edit --service-config "$servicio" \
        --environment "$ENTORNO" \
        deploy.preDeployCommand "$pre"
    [ -z "$salud" ] || ejecutar railway environment edit --service-config "$servicio" \
        --environment "$ENTORNO" \
        deploy.healthcheckPath "$salud"
}

configurar api \
    'uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT' \
    'python -m scripts.railway_preparar' \
    '/ready'

configurar worker \
    'celery -A app.core.celery_app.celery_app worker --loglevel=INFO --concurrency=2 -Q integraciones,procesos,default' \
    'python -m scripts.railway_preparar --solo-migracion' \
    ''

configurar beat \
    'celery -A app.core.celery_app.celery_app beat --loglevel=INFO' \
    'python -m scripts.railway_preparar --solo-migracion' \
    ''

# ─────────────────────────── volúmenes ───────────────────────────

log "Volúmenes persistentes"
# PostgreSQL: /var/lib/postgresql, NO /var/lib/postgresql/data. Es la
# trampa clásica de PostgreSQL 18 y del contenedor oficial de pgvector.
ejecutar railway volume add --service postgres --mount-path /var/lib/postgresql
# MinIO: los objetos en /data.
ejecutar railway volume add --service minio --mount-path /data

# ─────────────────────────── variables ───────────────────────────

log "Variables"
# La API es la única que necesita el secreto de firma. Railway permite
# sellar las variables de producción para que nadie pueda leerlas.
ejecutar railway variable set \
    "DATABASE_URL=\${{postgres.DATABASE_URL}}" --service api --environment "$ENTORNO"
ejecutar railway variable set \
    "REDIS_URL=\${{redis.REDIS_URL}}" --service api --environment "$ENTORNO"
ejecutar railway variable set \
    "MINIO_ENDPOINT=http://minio.railway.internal:9000" --service api --environment "$ENTORNO"
ejecutar railway variable set \
    "MINIO_ACCESS_KEY=\${{minio.MINIO_ROOT_USER}}" --service api --environment "$ENTORNO"
ejecutar railway variable set \
    "MINIO_SECRET_KEY=\${{minio.MINIO_ROOT_PASSWORD}}" --service api --environment "$ENTORNO"
ejecutar railway variable set \
    "SECRET_KEY=\${{shared.SECRET_KEY}}" --service api --environment "$ENTORNO"

# El worker y el beat necesitan lo mismo, salvo el secreto de firma.
for servicio in worker beat; do
    ejecutar railway variable set \
        "DATABASE_URL=\${{postgres.DATABASE_URL}}" --service "$servicio" --environment "$ENTORNO"
    ejecutar railway variable set \
        "REDIS_URL=\${{redis.REDIS_URL}}" --service "$servicio" --environment "$ENTORNO"
    ejecutar railway variable set \
        "MINIO_ENDPOINT=http://minio.railway.internal:9000" --service "$servicio" --environment "$ENTORNO"
    ejecutar railway variable set \
        "MINIO_ACCESS_KEY=\${{minio.MINIO_ROOT_USER}}" --service "$servicio" --environment "$ENTORNO"
    ejecutar railway variable set \
        "MINIO_SECRET_KEY=\${{minio.MINIO_ROOT_PASSWORD}}" --service "$servicio" --environment "$ENTORNO"
done

# MinIO necesita su propio par de credenciales.
ejecutar railway variable set \
    "MINIO_ROOT_USER=projecta" --service minio --environment "$ENTORNO"
ejecutar railway variable set \
    "MINIO_ROOT_PASSWORD=\${{shared.MINIO_ROOT_PASSWORD}}" --service minio --environment "$ENTORNO"

# ─────────────────────────── despliegue ───────────────────────────

log "Despliegue"
for servicio in api worker beat; do
    ejecutar railway up --service "$servicio" --detach
done

log "Dominio público de la API"
# Solo la API. El worker, el beat y los datos **no** se exponen: lo
# que no necesita ser público, no lo es.
ejecutar railway domain --service api --json

cat <<'FIN'

Despliegue terminado.

Lo que queda por hacer, a mano, en el panel de Railway:

  1. Fijar las **variables compartidas** que se referencian arriba:
     `shared.SECRET_KEY` (un `openssl rand -hex 32`) y
     `shared.MINIO_ROOT_PASSWORD`.
  2. **Sellar** `shared.SECRET_KEY` en producción: en Variables, la
     opción de sellar. Un secreto de producción que se puede leer desde
     el panel es un secreto que ya no es secreto.
  3. Abrir `https://<dominio>/ready` y comprobar que responde `ok`.
  4. Revisar los logs del despliegue: `railway logs --service api`.

Lo que este script **no** hace, y hay que decidir antes:

  · **Quién puede alcanzar la API.** El dominio público de Railway es
    público de verdad. La política del banco era «sin aplicación
    cliente desplegada ni puertos entrantes», y Railway rompe esa
    premisa. Ver `docs/skeleton/VALIDACION.md`, §10 bis.12.

  · **Dónde residen los datos.** PostgreSQL, Redis y MinIO quedan en
    la infraestructura de Railway, no en la del banco. Es una decisión
    de protección de datos, y no es técnica.

FIN
