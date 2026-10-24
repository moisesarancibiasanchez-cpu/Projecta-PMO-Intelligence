# Runbook — Projecta PMO Intelligence

Manual de operación del esqueleto. Dos caminos, según el entorno:

- **Perfil Docker** — en un entorno con demonio Docker. Es el camino de despliegue real.
- **Perfil nativo** — sin Docker. Es el camino de desarrollo y el que se usó para validar el
  esqueleto en el entorno de construcción.

---

## 1. Requisitos

| Componente | Versión | Nota |
|---|---|---|
| Python | 3.11 o superior | El intérprete del sistema puede no tener las dependencias: usar el entorno virtual. |
| PostgreSQL | 16 con la extensión `vector` | Motor de producción. |
| Redis | 7 | Broker de Celery y backend de resultados. |
| Docker | 24 o superior (opcional) | Solo para el perfil de despliegue. |
| Make | cualquier | Atajos de operación. |

---

## 2. Preparación

```bash
cd /workspace/repo

# Entorno virtual
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple/ -r requirements.txt

# Configuración
cp .env.example .env
# Generar la clave de firma: es OBLIGATORIA y no tiene valor por defecto.
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env
```

> **`.env` nunca se versiona.** Ya está excluido en `.gitignore` y en `.dockerignore`. Al
> repositorio solo entra `.env.example`, que es una plantilla sin secretos.

Variables obligatorias: `SECRET_KEY`, `DATABASE_URL`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`. Si
falta alguna, el proceso **falla al arrancar nombrando la variable exacta**. No hay valores por
defecto para la clave de firma: es deliberado.

---

## 3. Perfil Docker (despliegue real)

```bash
# Perfil mínimo de piloto: api + worker + beat + postgres + redis
docker compose up -d

# Con almacenamiento de objetos
docker compose --profile minio up -d

# Estado y salud de cada contenedor
docker compose ps
docker compose logs -f api
```

Puntos de entrada:

| Servicio | URL | Puerto |
|---|---|---|
| API | `http://localhost:8000` | 8000 |
| Documentación interactiva | `http://localhost:8000/docs` | 8000 |
| PostgreSQL | `localhost:5432` | 5432 |
| Redis | `localhost:6379` | 6379 |
| Consola de MinIO | `http://localhost:9001` | 9001 |

Los tres servicios de aplicación (`api`, `worker`, `beat`) esperan a `postgres` y `redis` con
`condition: service_healthy`: no arrancan hasta que sus dependencias están sanas.

```bash
docker compose down          # parar
docker compose down -v       # parar y borrar los volúmenes (DESTRUCTIVO)
```

---

## 4. Perfil nativo (desarrollo y validación)

```bash
cd /workspace/repo

# Arrancar la API en primer plano
bash scripts/levantar_stack.sh

# O en segundo plano
bash scripts/levantar_stack.sh --background
bash scripts/levantar_stack.sh --stop
```

El script carga las variables de los servicios reales si existe `/tmp/projecta_env.sh`, y genera
una `SECRET_KEY` efímera si no se ha definido ninguna.

### 4.1. Los tres procesos del stack

```bash
# 1. API
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2. Worker de Celery (consume de las 6 colas)
.venv/bin/python -m celery -A app.core.celery_app:celery_app worker --loglevel=info

# 3. Planificador (dispara el ping de mantenimiento cada 5 minutos)
.venv/bin/python -m celery -A app.core.celery_app:celery_app beat --loglevel=info
```

Las 6 colas: `ingesta`, `documentos`, `ia`, `integraciones`, `reportes`, `mantenimiento`.

---

## 5. Verificación

```bash
bash scripts/verificar_stack.sh
echo "código de salida: $?"
```

El script es autónomo y **debe terminar con código 0**. Comprueba, en este orden: análisis estático
del compose, arranque de la API y códigos HTTP, shape de `/ready`, Celery con broker real y worker
consumiendo, migraciones contra el PostgreSQL real, y la suite de pruebas.

Salida detallada:

```bash
.venv/bin/python -m pytest tests -q
```

Comprobaciones rápidas a mano:

```bash
curl -s http://127.0.0.1:8000/health    # OK
curl -s http://127.0.0.1:8000/ready     # JSON con status y checks
curl -s http://127.0.0.1:8000/ | head   # portada renderizada
```

---

## 6. Migraciones

```bash
# Aplicar
.venv/bin/python -m alembic upgrade head

# Ver la versión actual
.venv/bin/python -m alembic current

# Revertir todo
.venv/bin/python -m alembic downgrade base
```

En Docker:

```bash
docker compose run --rm api alembic upgrade head
```

Las migraciones son portables entre PostgreSQL y SQLite. Los valores por defecto de marca de tiempo
se declaran con `sa.func.now()`, no con `sa.text("now()")`, que es sintaxis exclusiva de PostgreSQL.

---

## 7. Diagnóstico

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| El arranque falla con «falta SECRET_KEY» | La clave no está definida o está vacía | `export SECRET_KEY=$(openssl rand -hex 32)` |
| `/ready` devuelve 503 | `database` o `redis` no responden | `curl` a la `DATABASE_URL`; revisar que Redis esté en marcha |
| `/ready` devuelve 200 con `storage.ok: false` | El almacenamiento de objetos no está en el perfil | **No es un fallo.** `storage` es una dependencia no crítica: el estado global sigue siendo `"ok"`. |
| `/ready` devuelve 503 y `/health` también | La aplicación no arrancó | `docker compose logs api` o `/tmp/projecta_api.log` |
| El worker no consume tareas | El broker no es accesible, o el worker no escucha esa cola | `celery -A app.core.celery_app:celery_app inspect ping`; comprobar `-Q` |
| `DuplicateNodenameWarning` | Hay dos workers con el mismo nombre | Matar el worker antiguo: `pkill -f "celery -A app.core.celery_app"` |
| `ValueError: dictionary update sequence element #0...` al migrar | Hay una opción `sqlalchemy.*` mal formada en `alembic.ini` | Ninguna opción `sqlalchemy.` debe tener un valor que no sea una URL |
| `The asyncio extension requires an async driver` | Se pasó una URL síncrona a `async_engine_from_config` | `env.py` debe usar `settings.database_url`, no `database_url_sync` |

---

## 8. Respaldo y restauración

### 8.1. PostgreSQL

```bash
# Volcado
docker compose exec -T postgres pg_dump -U postgres -Fc projecta > projecta_$(date +%F).dump

# Restauración
docker compose exec -T postgres pg_restore -U postgres -d projecta --clean < projecta_2026-10-22.dump
```

En el perfil nativo, con `pgserver`:

```bash
/workspace/.venv/bin/python -c "
import pgserver
db = pgserver.get_server('/tmp/projecta_pgdata', cleanup_mode=None)
print(db.psql('SELECT datname FROM pg_database'))
"
```

### 8.2. Volúmenes de Docker

```bash
docker volume ls
docker run --rm -v projecta_pmo_intelligence_pgdata:/datos -v "$PWD":/respaldo \
  alpine tar czf /respaldo/pgdata.tar.gz -C /datos .
```

### 8.3. Evidencia en el almacenamiento de objetos

El bucket `projecta-evidencia` se inicializa con el servicio `minio-init` del perfil `minio`.
Respaldarlo con `mc mirror`:

```bash
docker compose run --rm minio-init mc mirror /data projecta-evidencia
```

---

## 9. Operación diaria

```bash
make up       # docker compose up -d
make down     # docker compose down
make logs     # docker compose logs -f
make migrate  # alembic upgrade head
make test     # pytest -q
make backup   # volcado de PostgreSQL
make shell    # shell en el contenedor de la API
make lint     # ruff check .
```

---

## 10. Qué NO está validado

La construcción de la imagen y el arranque de los contenedores **no se han validado**, porque el
entorno de construcción no tenía el binario `docker`. El detalle está en
`docs/skeleton/VALIDACION.md`, sección 9.

Lo que sí está validado, contra servicios reales: la API, las plantillas, el escapado, las
migraciones contra PostgreSQL con pgvector, el worker de Celery consumiendo del broker Redis real,
y la suite completa de pruebas.
