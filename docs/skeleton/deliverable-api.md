# Entregable — Núcleo FastAPI

**Capa:** `api-core`
**Alcance:** `app/main.py`, `app/core/config.py`, `app/core/db/*`, `app/api/*`, `alembic*`,
`pyproject.toml`.

---

## 1. Archivos escritos

| Archivo | Líneas | Qué hace |
|---|---:|---|
| `app/main.py` | 95 | Factory `create_app()` con `lifespan`, CORS configurable, montaje de routers y registro de la configuración al arrancar (sin secretos). |
| `app/core/config.py` | 301 | `Settings(BaseSettings)` tipada, con validación de variables obligatorias y `database_url_sync`. |
| `app/core/db/base.py` | 23 | `Base(DeclarativeBase)`. |
| `app/core/db/session.py` | 87 | `create_async_engine`, `async_sessionmaker`, `get_session` con `commit`/`rollback`, `dispose_engine()`, `crear_tablas()`. |
| `app/core/db/models.py` | 130 | `Organizacion`, `Proyecto`, `Usuario`, `Rol` con `Mapped[]` tipado. |
| `app/api/deps.py` | 47 | Dependencias compartidas. |
| `app/api/routers/health.py` | 221 | `/health`, `/ready`, `/salud`. |
| `app/api/routers/index.py` | 90 | `/` con la plantilla `index.html`. |
| `alembic.ini` | 51 | Configuración de Alembic. |
| `alembic/env.py` | 94 | Entorno async: `async_engine_from_config` + `run_sync`. |
| `alembic/script.py.mako` | 28 | Plantilla de migraciones. |
| `alembic/versions/0001_esquema_inicial.py` | 148 | Migración inicial, `upgrade()` y `downgrade()` simétricos. |
| `pyproject.toml` | 75 | Proyecto `projecta-pmo-intelligence`, config de pytest y de ruff. |

---

## 2. Validación ejecutada

### 2.1. Importación de la aplicación

```python
from app.main import create_app; create_app()
```

Sin error, con la configuración registrada al arrancar:

```
2026-10-22 19:06:20,290 INFO  projecta :: Arrancando Projecta PMO Intelligence v0.1.0
2026-10-22 19:06:20,290 INFO  projecta :: Entorno: development · Depuración: False
2026-10-22 19:06:20,290 INFO  projecta :: Base de datos: postgresql+asyncpg://postgres:***@postgres?host=/tmp/projecta_pgdata
2026-10-22 19:06:20,291 INFO  projecta :: Redis: redis://127.0.0.1:6398/0
2026-10-22 19:06:20,291 INFO  projecta :: Almacenamiento: http://localhost:9000 (bucket projecta-evidencia)
2026-10-22 19:06:20,291 INFO  projecta :: Projecta PMO Intelligence operativo.
```

### 2.2. El caso degradado: base de datos inalcanzable

```
=== /ready (BD inalcanzable) ===
{"status":"degraded","checks":{"database":{"ok":false,"latency_ms":196.19,"error":"ConnectionRefusedError: [Errno 111] Connection refused"},"redis":{"ok":true,"latency_ms":195.02},"storage":{"ok":false,"latency_ms":193.58,"error":"no disponible en este perfil"}}}
HTTP=503
=== /health ===
OK HTTP=200
=== / ===
HTTP=200
```

`/ready` degrada a 503; `/health` (liveness) y `/` siguen respondiendo 200. La API no se cae porque
la base de datos no llegue.

### 2.3. Migración contra el PostgreSQL real

```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Esquema inicial de Projecta PMO Intelligence.
```

Estructura creada:

```
   table_name    |   column_name   | delete_rule | update_rule
--------------+-----------------+-------------+-------------
 proyectos     | organizacion_id | CASCADE     | CASCADE
 usuarios      | organizacion_id | CASCADE     | CASCADE
 usuarios      | rol_id          | RESTRICT    | CASCADE
(3 rows)
```

Claves primarias `uuid`, marcas de tiempo `timestamp with time zone`, y `ondelete` explícito en las
tres relaciones.

### 2.4. Arranque sin `SECRET_KEY`

El proceso falla y **nombra la variable que falta**. Cubierto por
`test_sin_secret_key_el_error_nombra_la_variable`.

---

## 3. Cumplimiento del contrato

| Requisito | Cumple |
|---|:---:|
| `Settings(BaseSettings)` con `env_file` y `extra="ignore"` | Sí |
| `SECRET_KEY` obligatoria, **sin default** | Sí |
| `DATABASE_URL` acepta `postgresql+asyncpg` y `sqlite+aiosqlite` | Sí |
| `database_url_sync` traduce a `postgresql+psycopg` | Sí |
| `get_session` con `commit`/`rollback` | Sí |
| Modelos con `Mapped[]`, `__tablename__` snake_case, PK, FK con `ondelete` explícito | Sí |
| `/health` en texto plano, sin tocar dependencias | Sí |
| `/ready` con el shape exacto y timeout corto | Sí |
| `/salud` en JSON | Sí |
| `/` renderiza `index.html` con `app_name`, `version`, `environment` | Sí |
| `create_app()` con `lifespan` y CORS configurable | Sí |
| `env.py` async con `async_engine_from_config` + `run_sync` | Sí |
| `upgrade()` y `downgrade()` simétricos | Sí |
| Sin consultas SQL por concatenación de strings | Sí |
| `BOOTSTRAP_ADMIN_PASSWORD` ausente | Sí |

---

## 4. Defectos encontrados y corregidos durante la validación

Los tres se encontraron **ejecutando** el stack, no leyéndolo. Los corrigió el líder de proyecto.

| # | Defecto | Causa raíz | Corrección |
|---|---|---|---|
| 1 | `alembic upgrade head` fallaba con `ValueError: dictionary update sequence element #0 has length 1; 2 is required` | `alembic.ini` declaraba `sqlalchemy.connect_args = {}`. ConfigParser lo lee como la **cadena** `"{}"`, y `create_async_engine` hace `dict.update("{}")` | La opción se comenta en `alembic.ini` |
| 2 | Alembic no funcionaba con `DATABASE_URL` de SQLite: `The asyncio extension requires an async driver to be used. The loaded 'pysqlite' is not async.` | `env.py` pasaba la URL **síncrona** a `async_engine_from_config`. Funciona por casualidad con `psycopg` (tiene modo asíncrono) y revienta con `pysqlite` | `env.py` usa ahora `settings.database_url` |
| 3 | La migración no era portable: `sqlite3.OperationalError: near "(": syntax error` | `server_default=sa.text("now()")` es sintaxis exclusiva de PostgreSQL | Sustituido por `sa.func.now()` |

---

## 5. Lo NO validado

- **Construcción de la imagen y arranque de contenedores**: no hay binario `docker` en el entorno.
  La API se validó **de forma nativa** (`uvicorn` contra PostgreSQL y Redis reales), que es
  equivalente en comportamiento de aplicación.

Detalle en `docs/skeleton/VALIDACION.md`, sección 9.
