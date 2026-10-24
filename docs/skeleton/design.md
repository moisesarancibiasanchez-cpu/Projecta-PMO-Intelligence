# Contrato técnico compartido — Esqueleto de Projecta PMO Intelligence

> Documento normativo. Todas las tareas de implementación del esqueleto se construyen contra
> este contrato. Si una instrucción de tarea discrepa de este documento, **gana este documento**.

**Proyecto:** Projecta PMO Intelligence
**Bajada:** La capa inteligente para gobernar proyectos, portafolios y decisiones.
**Tagline:** Decide con evidencia. Avanza con control.
**Versión del contrato:** 1.0

---

## a) Layout del paquete

Rutas exactas bajo `/workspace/repo/`. La columna «Responsable» indica qué tarea escribe el archivo.

```
/workspace/repo/
├── app/
│   ├── __init__.py                    [contrato]  — vacío
│   ├── main.py                        [api-core]  — factory de la app FastAPI
│   ├── core/
│   │   ├── __init__.py                [contrato]  — vacío
│   │   ├── config.py                  [api-core]  — settings tipados
│   │   ├── celery_app.py              [celery-core] — instancia Celery
│   │   └── db/
│   │       ├── __init__.py            [contrato]  — vacío
│   │       ├── base.py                [api-core]  — DeclarativeBase
│   │       ├── session.py             [api-core]  — engine async + sessionmaker
│   │       └── models.py              [api-core]  — modelos ORM mínimos
│   ├── api/
│   │   ├── __init__.py                [contrato]  — vacío
│   │   ├── deps.py                    [api-core]  — dependencias compartidas
│   │   └── routers/
│   │       ├── __init__.py            [contrato]  — vacío
│   │       ├── health.py              [api-core]  — /health, /ready, /salud
│   │       └── index.py               [api-core]  — GET /
│   ├── workers/
│   │   ├── __init__.py                [contrato]  — vacío
│   │   └── tasks/
│   │       ├── __init__.py            [contrato]  — vacío
│   │       ├── smoke.py               [celery-core] — task projecta.smoke
│   │       └── maintenance.py         [celery-core] — task projecta.mantenimiento.ping
│   ├── templates/                     [templates-ui]
│   │   ├── base.html
│   │   ├── index.html
│   │   ├── health.html
│   │   └── partials/
│   │       ├── _cabecera.html
│   │       ├── _estado_vacio.html
│   │       └── _tarjeta_estado.html
│   └── static/css/app.css             [templates-ui]
├── alembic/
│   ├── env.py                         [api-core]
│   ├── script.py.mako                 [api-core]
│   └── versions/0001_esquema_inicial.py  [api-core]
├── tests/                             [integracion]
├── scripts/                           [integracion]
├── deploy/README.md                   [infra-compose]
├── docs/skeleton/                     — este documento + entregables
├── Dockerfile                         [infra-compose]
├── docker-compose.yml                 [infra-compose]
├── alembic.ini                        [api-core]
├── pyproject.toml                     [api-core]
├── requirements.txt                   [infra-compose]
├── .env.example                       [infra-compose]
├── .dockerignore                      [infra-compose]
├── .gitignore                         [contrato]  — nunca versionar .env, .venv, .data
└── Makefile                           [infra-compose]
```

**Regla dura:** los `__init__.py` los crea **solo** la tarea de contrato. Ninguna otra tarea crea ni
modifica un `__init__.py`.

---

## b) Contrato de configuración

Clase `Settings(BaseSettings)` en `app/core/config.py`, instancia de módulo `settings = Settings()`.

| Variable | Tipo | Obligatoria | Default | Propósito |
|---|---|:---:|---|---|
| `APP_NAME` | str | no | `Projecta PMO Intelligence` | Nombre visible en la interfaz y en OpenAPI |
| `APP_ENV` | str | no | `development` | `development` \| `staging` \| `production` |
| `DEBUG` | bool | no | `false` | Modo depuración |
| `SECRET_KEY` | str | **sí** | — | Clave de firma. **Sin valor por defecto.** Generar con `openssl rand -hex 32` |
| `DATABASE_URL` | str | **sí** | — | `postgresql+asyncpg://…` (producción) o `sqlite+aiosqlite:///…` (desarrollo) |
| `REDIS_URL` | str | no | `redis://localhost:6379/0` | Redis de aplicación (caché, locks) |
| `CELERY_BROKER_URL` | str | no | `redis://localhost:6379/1` | Broker de Celery |
| `CELERY_RESULT_BACKEND` | str | no | `redis://localhost:6379/2` | Resultados de Celery |
| `MINIO_ENDPOINT` | str | no | `http://localhost:9000` | Endpoint de MinIO |
| `MINIO_ACCESS_KEY` | str | **sí** | — | Credencial de acceso. Sin default real |
| `MINIO_SECRET_KEY` | str | **sí** | — | Credencial de acceso. Sin default real |
| `MINIO_BUCKET` | str | no | `projecta-evidencia` | Bucket de evidencia |
| `JWT_ALGORITHM` | str | no | `HS256` | Algoritmo de firma de token |
| `JWT_EXPIRATION_MINUTES` | int | no | `480` | Duración de sesión (8 h) |
| `CORS_ORIGINS` | list[str] | no | `[]` | Orígenes permitidos |
| `LOG_LEVEL` | str | no | `INFO` | Nivel de log |

**Reglas:**

1. El proceso **falla al arrancar** si falta `SECRET_KEY`, `DATABASE_URL`, `MINIO_ACCESS_KEY` o
   `MINIO_SECRET_KEY`. El error debe **nombrar la variable que falta**, no ser un error genérico.
2. `SECRET_KEY` **nunca** tiene valor por defecto.
3. `BOOTSTRAP_ADMIN_PASSWORD` **no existe** en este proyecto. Si aparece, es un defecto.
4. `DATABASE_URL` acepta ambos drivers. Se expone además la propiedad `database_url_sync`, que
   traduce `postgresql+asyncpg://` → `postgresql+psycopg://` para Alembic.

---

## c) Contrato de la API

| Método | Ruta | Respuesta | Código | Dependencias |
|---|---|---|---|:---:|
| `GET` | `/health` | Texto plano `OK` | 200 | **Ninguna** (liveness) |
| `GET` | `/ready` | JSON de readiness | 200 / 503 | Base de datos, Redis, storage |
| `GET` | `/salud` | JSON de salud | 200 | Base de datos, Redis, storage |
| `GET` | `/` | HTML renderizado desde `index.html` | 200 | Ninguna |
| `GET` | `/docs` | OpenAPI automático de FastAPI | 200 | Ninguna |

**Shape exacto de `/ready`:**

```json
{
  "status": "ok",
  "checks": {
    "database": { "ok": true, "latency_ms": 3.2 },
    "redis":    { "ok": true, "latency_ms": 0.8 },
    "storage":  { "ok": false, "latency_ms": 0.0, "error": "no disponible en este perfil" }
  }
}
```

**Semántica del estado global (normativa, sin ambigüedad):**

- Las dependencias **críticas** son `database` y `redis`. Las **no críticas** son `storage`.
- `status` es `"ok"` **si y solo si** `database.ok` y `redis.ok` son ambos `true`.
  En cualquier otro caso es `"degraded"`.
- El código HTTP es **200** con `status: "ok"` y **503** con `status: "degraded"`.
- El fallo de `storage` **nunca** cambia `status` ni el código HTTP, porque la API puede seguir
  sirviendo lectura y panel sin almacenamiento de objetos. Se reporta dentro de
  `checks.storage` con `"ok": false` y su `error`, de modo que el operador lo ve en la respuesta
  de readiness sin sacar el servicio de rotación.
- Los chequeos usan timeout corto (2 s) y nunca lanzan excepción hacia el cliente: un fallo se
  traduce a `{"ok": false, "error": "..."}` dentro del chequeo correspondiente.
- `/health` NO toca ninguna dependencia: es liveness puro.

**Contexto de las plantillas** (siempre presente en el render de `/`):
`app_name`, `version`, `environment`.

---

## d) Contrato de Celery

Instancia `Celery("projecta", broker=…, backend=…)` en `app/core/celery_app.py`.

**Colas (exactamente 6):**

| Cola | Uso |
|---|---|
| `ingesta` | Subir documento, transcribir reunión, importar tickets |
| `documentos` | OCR, extracción de tablas, parseo de XML de MS Project |
| `ia` | Clasificación, extracción de entidades, generación de artefactos |
| `integraciones` | Escritura en Jira/Azure DevOps, sincronización de estado |
| `reportes` | Informe semanal, comité, semáforo, exportación a PDF |
| `mantenimiento` | Aging del backlog, alertas, reintentos, purgas |

**Tareas registradas (nombres exactos):**

| Nombre | Cola | Firma | Efecto |
|---|---|---|---|
| `projecta.smoke` | `ia` | `projecta.smoke(echo: str \| None = None) -> dict` | Ninguno. Devuelve `status`, `app`, `timestamp` y `echo` |
| `projecta.mantenimiento.ping` | `mantenimiento` | `projecta.mantenimiento.ping() -> dict` | Ninguno. Comprueba Redis y devuelve el resultado |

**Parámetros de robustez obligatorios:**

```python
task_acks_late = True
task_reject_on_worker_lost = True
worker_prefetch_multiplier = 1
task_soft_time_limit = 600          # 10 min
task_time_limit = 900               # 15 min
broker_connection_retry_on_startup = True
```

**Beat:** una entrada en `beat_schedule` con `crontab(minute="*/5")` que apunte a
`projecta.mantenimiento.ping`.

**Otros:** `task_default_queue` explícito; `task_routes` con las 6 colas;
`include=["app.workers.tasks.smoke", "app.workers.tasks.maintenance"]`;
broker y backend leídos de `settings`, con fallback a `redis://localhost:6379/1` y `/2`.

---

## e) Contrato de plantillas

- Directorio: `app/templates/`. Registro: `Jinja2Templates(directory=…, autoescape=True)`.
- Plantilla base: **`base.html`**. Bloques: `{% block content %}` (y `{% block title %}` opcional).
- Variables comunes disponibles en `base.html` y heredadas: `app_name`, `version`, `environment`.
- Partials en `app/templates/partials/`:
  - `_cabecera.html` — marca, navegación, `environment`.
  - `_estado_vacio.html` — estado vacío reutilizable; variables `variable` (qué falta) y `slot`
    (contenido a mostrar).
  - `_tarjeta_estado.html` — tarjeta de estado; variables `titulo`, `valor`, `tono`
    (`ok` \| `warn` \| `error`).
- **Escapado:** `autoescape=True` activo. **Prohibido** usar `|safe` sobre contenido que pueda
  provenir de un modelo de IA. Si hace falta marcar algo, se define un filtro propio que escape
  primero y se documenta.
- El texto visible dice **siempre** «Projecta PMO Intelligence», nunca «Projecta» solo.
- `app/static/css/app.css`: estilos propios, **sin CDN y sin build step**. Responsive hasta 390 px
  sin scroll horizontal.

---

## f) Contrato de base de datos

- Motor: SQLAlchemy 2 async, `create_async_engine(settings.database_url)`.
- Base: `class Base(DeclarativeBase)` en `app/core/db/base.py`.
- Sesión: `async_sessionmaker(engine, expire_on_commit=False)` en `app/core/db/session.py`.
- Dependencia: `get_session()` como `async generator` con `commit` en éxito y `rollback` en error.
- Driver de producción: **`postgresql+asyncpg`** (asyncpg). Fallback de desarrollo local:
  `sqlite+aiosqlite`. Para las herramientas que necesiten una URL puramente síncrona
  (volcados, administración) existe la propiedad `database_url_sync`, que traduce
  `postgresql+asyncpg` → `postgresql+psycopg` y `sqlite+aiosqlite` → `sqlite`.
- Alembic: `env.py` con soporte async (`async_engine_from_config` + `run_sync`). Como el motor
  es asíncrono, `env.py` usa la URL **asíncrona** (`settings.database_url`): el motor asíncrono
  exige un driver asíncrono. Pasar la URL síncrona a `async_engine_from_config` solo funciona por
  casualidad con `psycopg` y falla con `sqlite://` («The asyncio extension requires an async
  driver»).
- Las migraciones son portables entre PostgreSQL y SQLite: los valores por defecto de marca de
  tiempo se declaran con `sa.func.now()` (no con `sa.text("now()")`, que es sintaxis exclusiva
  de PostgreSQL).
- Modelos mínimos con `Mapped[]` tipado, `__tablename__` en snake_case, PK, y FK con política
  `ondelete` **explícita** en cada relación.

**Entidades mínimas:** `Organizacion`, `Proyecto`, `Usuario`, `Rol`.

---

## g) Contrato de docker-compose

**Nombres de servicio exactos:** `api`, `worker`, `beat`, `postgres`, `redis`, `minio`
(más `minio-init` en perfil opcional `minio`).

| Servicio | Imagen / build | Puerto | Healthcheck |
|---|---|---|---|
| `api` | build local (`Dockerfile`) | `8000:8000` | `curl -f http://localhost:8000/health` |
| `worker` | build local | — | `celery -A app.core.celery_app:celery_app inspect ping` |
| `beat` | build local | — | `celery -A app.core.celery_app:celery_app inspect ping` |
| `postgres` | `pgvector/pgvector:pg16` | `5432:5432` | `pg_isready -U postgres` |
| `redis` | `redis:7-alpine` | `6379:6379` | `redis-cli ping` |
| `minio` | `minio/minio` | `9000:9000`, `9001:9001` | `mc ready local` |

- `api`, `worker` y `beat` arrancan con `depends_on` + `condition: service_healthy` hacia `postgres`
  y `redis`.
- Volúmenes nombrados: `pgdata`, `redisdata`, `miniodata`.
- Perfil `minio`: activa `minio` y `minio-init`; el perfil mínimo de piloto es
  `api + worker + beat + postgres + redis`.
- Todo healthcheck declara `test`, `interval`, `timeout` y `retries`.
- Toda variable `${…}` usada en el compose está declarada en `.env.example`.

**Dockerfile:** multi-stage, usuario no-root, `HEALTHCHECK` propio, y las librerías de sistema del
stack: `libpango`, `libpangoft2`, `libcairo2`, `tesseract-ocr`, `tesseract-ocr-spa` (WeasyPrint y
Tesseract) y `libxml2-dev` (lxml).

---

## h) Reglas de nomenclatura

| Elemento | Convención | Ejemplo |
|---|---|---|
| Módulos, paquetes, funciones, variables | `snake_case` | `app/core/db/session.py`, `get_session()` |
| Clases | `PascalCase` | `DeclarativeBase`, `Settings` |
| Constantes | `UPPER_SNAKE_CASE` | `ESTADO_APROBADO` |
| Tablas | `snake_case` plural | `organizaciones`, `proyectos` |
| Enums | `PascalCase` | `EstadoArtefacto` |
| Migraciones Alembic | `NNNN_nombre_descriptivo` | `0001_esquema_inicial.py` |
| Ramas git | `tipo/nombre` | `feat/agent-conciliacion` |
| Commits | Conventional Commits | `feat(trazabilidad): cadena de hash` |

---

## i) Variables de entorno del sandbox

> Esta sección describe el entorno donde se ejecuta el esqueleto. Léela antes de instalar nada o
> ejecutar comandos.

- **Intérprete:** `/workspace/.venv/bin/python`. El Python del sistema (`/usr/bin/python3`, 3.11.2)
  **no tiene** las dependencias instaladas. Nunca lo uses para ejecutar la app.
- **Instalar paquetes:** siempre con
  `/workspace/.venv/bin/python -m pip install --index-url https://pypi.org/simple/ <pkg>`.
  El mirror por defecto (`mirrors.cloud.aliyuncs.com` sobre HTTP) **corta la conexión y falla**.
- **No existe el binario `docker`**: no hay daemon, ni `docker build`, ni `docker compose up`. El
  `Dockerfile` y el `docker-compose.yml` se **escriben y se revisan por análisis estático**; la
  validación de arranque se hace de forma nativa.

### Servicios reales disponibles para la validación

Hay servicios **reales** disponibles de forma nativa, ya verificados, que permiten validar el stack
de producción de verdad:

| Servicio | Cómo | Detalle |
|---|---|---|
| **PostgreSQL real con pgvector** | `pgserver` | Extensión `vector` 0.6.2 disponible. Socket Unix en `/tmp/projecta_pgdata`. Persiste entre procesos |
| **Redis real 7** | binario `redislite` | Daemonizado en `127.0.0.1:6398`. Ruta del binario: `/workspace/.venv/lib/python3.11/site-packages/redislite/bin/redis-server`. Persiste entre procesos |

**Helper listo:**

```bash
/workspace/.venv/bin/python /workspace/herramientas/servicios_reales.py start   # levanta
source /tmp/projecta_env.sh                                                    # carga variables
/workspace/.venv/bin/python /workspace/herramientas/servicios_reales.py check   # comprueba
/workspace/.venv/bin/python /workspace/herramientas/servicios_reales.py stop    # detiene
```

Variables que exporta `start`:

```bash
export DATABASE_URL="postgresql+asyncpg://postgres:@postgres?host=/tmp/projecta_pgdata"
export DATABASE_URL_SYNC="postgresql+psycopg://postgres:@postgres?host=/tmp/projecta_pgdata"
export REDIS_URL="redis://127.0.0.1:6398/0"
export CELERY_BROKER_URL="redis://127.0.0.1:6398/1"
export CELERY_RESULT_BACKEND="redis://127.0.0.1:6398/2"
```

Guía completa: `/workspace/herramientas/GUIA_SERVICIOS_REALES.md`.

**Consecuencia:** es posible validar el stack real — PostgreSQL con pgvector, driver asyncpg,
broker Redis real, worker Celery consumiendo de verdad.

### Lo que sigue SIN poder validarse

Únicamente por ausencia del binario `docker`:

1. La construcción real de la imagen (`docker build`).
2. El arranque real de los 6 contenedores del `docker-compose.yml`.
3. La imagen `pgvector/pgvector:pg16` del registro (es un artefacto distinto del `pgserver` local).

Todo lo demás —API, plantillas, Celery con broker real, migraciones contra PostgreSQL real,
health checks, pruebas— **sí es validable**.
