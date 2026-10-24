# Despliegue de Projecta PMO Intelligence

> **Projecta PMO Intelligence** — *La capa inteligente para gobernar proyectos,
> portafolios y decisiones.* Decide con evidencia. Avanza con control.

Este documento explica cómo se levanta y se opera la capa de infraestructura del
esqueleto de **Projecta PMO Intelligence** en los dos escenarios previstos: el
**piloto** (stack mínimo) y el **corporativo** (stack completo con almacenamiento
de objetos).

El contrato normativo de esta capa está en
[`docs/skeleton/design.md`](../docs/skeleton/design.md), sección «Contrato de
docker-compose». Si este README y el contrato discrepan, **manda el contrato**.

---

## 1. Panorama de la arquitectura

```
                        ┌───────────────────────────────┐
   navegador / cliente ─▶  api   (FastAPI + Uvicorn)    │──▶ 8000
                        └───────────────┬───────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
      ┌───────────────┐        ┌────────────────┐       ┌────────────────┐
      │ postgres      │        │ redis          │       │ minio          │
      │ pgvector/pg16 │        │ redis:7-alpine │       │ minio/minio    │
      │ 5432          │        │ 6379           │       │ 9000 / 9001    │
      └───────┬───────┘        └───────┬────────┘       └───────┬────────┘
              │                        │                        │
              │                ┌───────┴────────┐               │
              └───────────────▶│ worker (Celery)│◀──────────────┘
                               │ beat (Celery)  │   (perfil `minio`)
                               └────────────────┘
```

| Servicio    | Imagen / build            | Puerto(s)                | Healthcheck (contrato)                                     |
|-------------|---------------------------|--------------------------|------------------------------------------------------------|
| `api`       | `Dockerfile` local         | `8000:8000`              | `curl -f http://localhost:8000/health`                     |
| `worker`    | `Dockerfile` local         | —                        | `celery -A app.core.celery_app:celery_app inspect ping`    |
| `beat`      | `Dockerfile` local         | —                        | `celery -A app.core.celery_app:celery_app inspect ping`    |
| `postgres`  | `pgvector/pgvector:pg16`  | `5432:5432`              | `pg_isready -U postgres`                                   |
| `redis`     | `redis:7-alpine`           | `6379:6379`              | `redis-cli ping`                                           |
| `minio`     | `minio/minio`              | `9000:9000`, `9001:9001` | `mc ready local`                                           |
| `minio-init`| `minio/mc`                 | —                        | verifica que el bucket existe                              |

`api`, `worker` y `beat` arrancan **solo** cuando `postgres` y `redis` están
`sanos` (`depends_on` + `condition: service_healthy`). Los volúmenes
`pgdata`, `redisdata` y `miniodata` conservan los datos entre despliegues.

**Perfiles.** El perfil `minio` activa `minio` y `minio-init`. El **stack
mínimo de piloto es `api + worker + beat + postgres + redis`**: no requiere
MinIO.

---

## 2. Requisitos previos

- Docker Engine 24+ y **Docker Compose v2.23.1 o superior** (el compose usa
  `configs.content` para inyectar el script de inicialización de `pgvector`).
- Puertos libres en el host: `8000`, `5432`, `6379` (y `9000`/`9001` si se usa
  el perfil `minio`).
- Un archivo `.env` local con los valores reales (nunca versionado).

```bash
cp .env.example .env
$EDITOR .env            # SECRET_KEY, POSTGRES_PASSWORD, MINIO_* obligatorios
```

Generación de la clave de firma:

```bash
openssl rand -hex 32    # -> SECRET_KEY
```

> **`.env` nunca se versiona.** Está excluido en `.gitignore` y en `.dockerignore`:
> contiene credenciales reales. Al repositorio solo entra `.env.example`, que
> es una plantilla sin secretos.

---

## 3. Despliegue de piloto (perfil mínimo)

Es el escenario de validación con un cliente: cinco servicios, sin almacén de
objetos.

```bash
# 1. Configurar el entorno
cp .env.example .env && $EDITOR .env

# 2. Levantar el stack (construye la imagen y arranca los servicios)
make up

# 3. Aplicar el esquema de la base de datos
make migrate

# 4. Comprobar el estado
make health
make logs
```

Comprobaciones puntuales:

```bash
curl -f http://localhost:8000/health     # liveness: responde OK
curl -s  http://localhost:8000/ready      # readiness: JSON de dependencias
```

Notas del piloto:

- **Sin MinIO**: el chequeo `storage` de `/ready` queda en `degraded` (no es
  crítico: el servicio sigue operativo, tal y como fija el contrato) hasta que
  se active el perfil `minio` o se apunte `MINIO_ENDPOINT` a un almacén
  externo.
- **Redis sin contraseña**: acceptable en un entorno de piloto aislado. En
  producción usa un Redis gestionado con autenticación y una red privada
  (ver §4).
- **Volúmenes**: `make backup` genera un volcado comprimido de PostgreSQL en
  `backups/`. `make restore` recupera el volcado más reciente.

---

## 4. Despliegue corporativo (perfil completo)

Para un entorno corporativo se activas el perfil `minio` y se ajustan las
pautas de la sección anterior.

```bash
make up-minio        # stack completo: api + worker + beat + postgres + redis + minio (+ minio-init)
make migrate
make health
```

### 4.1 Almacenamiento de objetos

- `minio` expone la API S3 en `9000` y la consola en `9001`.
- `minio-init` (perfil `minio`) crea el bucket `MINIO_BUCKET` de forma
  idempotente al arrancar, una vez que `minio` está sano.
- `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` deben coincidir con
  `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`: la aplicación autentica contra el
  servidor con ese mismo par. En el despliegue corporativo se recomienda
  provisionar un usuario de servicio con permisos solo sobre el bucket.

### 4.2 Secretos

| Variable            | Origen en producción                                        |
|---------------------|-------------------------------------------------------------|
| `SECRET_KEY`        | gestor de secretos / KMS; **sin valor por defecto**         |
| `POSTGRES_PASSWORD` | gestor de secretos; rotar con `ALTER ROLE`                 |
| `MINIO_SECRET_KEY`  | gestor de secretos                                          |

La plantilla `.env.example` usa `cambiar-en-produccion` como marcador de
posición: es un valor **inválido a propósito** para que nadie lo deje puesto
por descuido.

### 4.3 Red y exposición

- La red `projecta` (bridge) aísla los servicios: solo `api` y el panel de
  `minio` deberían publicarse.
- Sustituye `ports: ["8000:8000"]` por un proxy inverso (nginx, Traefik) con
  TLS y deja `api` sin puertos publicados (`expose: ["8000"]`).
- `5432` y `6379` no deberían publicarse en el host en un entorno corporativo:
  usa `expose` y accede a ellos desde la red privada o desde redes gestionadas
  (RDS, ElastiCache).

### 4.4 Operación y escalado

```bash
make ps                # estado de contenedores
make logs              # logs agregados en vivo
make backup            # volcado comprimido en backups/
make restore           # restauración del último volcado
make psql              # consola SQL
make redis-cli         # consola de Redis
make shell             # shell dentro del contenedor de la API
make down              # parada conservando volúmenes
make down-volumes      # parada destructiva (pide confirmación)
```

- El `worker` se escala horizontalmente con
  `docker compose up -d --scale worker=4` (la cola se reparte sola; el contrato
  fija `worker_prefetch_multiplier = 1` y `task_acks_late = True`).
- Fija la imagen por digest en el despliegue corporativo en lugar de `latest`.

---

## 5. Variables de entorno

Todas las variables del contrato están en [`.env.example`](../.env.example) y
todas las que el compose interpola (`${...}`) están declaradas allí. Resumen:

| Variable                                            | Obligatoria | Default                        |
|-----------------------------------------------------|:-----------:|--------------------------------|
| `APP_NAME`                                          |     no      | `Projecta PMO Intelligence`     |
| `APP_ENV`                                           |     no      | `development`                  |
| `DEBUG`                                             |     no      | `false`                        |
| `SECRET_KEY`                                        |    **sí**   | — (sin valor por defecto)      |
| `DATABASE_URL`                                      |    **sí**   | — (sin valor por defecto)      |
| `REDIS_URL`                                         |     no      | `redis://localhost:6379/0`      |
| `CELERY_BROKER_URL`                                 |     no      | `redis://localhost:6379/1`      |
| `CELERY_RESULT_BACKEND`                             |     no      | `redis://localhost:6379/2`      |
| `MINIO_ENDPOINT`                                    |     no      | `http://localhost:9000`         |
| `MINIO_ACCESS_KEY`                                  |    **sí**   | — (sin valor por defecto)      |
| `MINIO_SECRET_KEY`                                  |    **sí**   | — (sin valor por defecto)      |
| `MINIO_BUCKET`                                      |     no      | `projecta-evidencia`            |
| `JWT_ALGORITHM`                                     |     no      | `HS256`                        |
| `JWT_EXPIRATION_MINUTES`                            |     no      | `480`                          |
| `CORS_ORIGINS`                                      |     no      | `[]`                           |
| `LOG_LEVEL`                                         |     no      | `INFO`                         |

Variables de servicio del compose: `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`POSTGRES_DB`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`.

Si falta `SECRET_KEY`, `DATABASE_URL`, `MINIO_ACCESS_KEY` o
`MINIO_SECRET_KEY`, el proceso **falla al arrancar nombrando la variable
ausente** (no es un error genérico).

---

## 6. pgvector

La imagen `pgvector/pgvector:pg16` ya trae la extensión disponible. El compose
la habilita en el **init** del contenedor: el script
`/docker-entrypoint-initdb.d/10-ext-vector.sql` (inyectado con `configs.content`)
se ejecuta la **primera vez** que se inicializa el volumen `pgdata`.

> Si cambias de volumen o recreas `pgdata`, la extensión vuelve a crearse
> automáticamente en la siguiente inicialización. Para activarla a mano en una
> base ya existente: `make psql` y `CREATE EXTENSION IF NOT EXISTS vector;`.

---

## 7. Resolución de problemas

| Síntoma                                                | Causa probable y solución                                                                 |
|--------------------------------------------------------|------------------------------------------------------------------------------------------|
| `api` no arranca: *falta SECRET_KEY*                   | `.env` no existe o está incompleto. `cp .env.example .env` y rellénalo.                 |
| `api` reinicia en bucle                                | `DATABASE_URL` apunta a un host que no existe desde dentro de la red de Compose.          |
| `api` se queda en `degraded`                           | Falta el almacén de objetos: `/ready` marca `storage` en rojo (no crítico). Activa el perfil `minio` o corrige `MINIO_ENDPOINT`. |
| `worker` figura `unhealthy`                            | `celery inspect ping` no devuelve `pong`: el worker no está conectado al broker. Revisa `make logs worker` y `REDIS_URL`/`CELERY_BROKER_URL`. |
| `minio-init` falla al crear el bucket                  | `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` no coinciden con los del servidor.               |
| `postgres` no arranca tras cambiar `POSTGRES_PASSWORD` | La contraseña solo se aplica en la **primera** inicialización del volumen. Si ya existe, `ALTER ROLE postgres WITH PASSWORD '...';` |
| Puertos ocupados                                       | Cambia el lado host de `ports:` en el compose o libera el puerto.                        |

---

## 8. Estado de la validación

En el entorno donde se construyó este esqueleto **no existe el binario
`docker`**. Por tanto, `docker-compose.yml` y `Dockerfile` se validaron por
**análisis estático** (parsing del YAML, grafo de dependencias, healthchecks,
volúmenes, variables de entorno, usuarios y librerías del Dockerfile) y no por
arranque real de contenedores.

El detalle de los comandos ejecutados y de su salida está en
[`docs/skeleton/deliverable-infra.md`](../docs/skeleton/deliverable-infra.md).

Lo que **sí** se validó de forma nativa (fuera de Docker): API FastAPI,
plantillas, Celery con broker Redis real, migraciones Alembic contra
PostgreSQL real con pgvector y los health checks del contrato.
