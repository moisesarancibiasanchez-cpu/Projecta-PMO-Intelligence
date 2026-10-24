# Informe de validación del esqueleto — Projecta PMO Intelligence

**Fecha:** 2026-10-22
**Alcance:** esqueleto del repositorio (infraestructura, API, Celery, plantillas, integración)
**Método:** validación **nativa**, contra servicios reales del entorno de ejecución.
**Veredicto global:** **PASA** — 20 comprobaciones, 0 fallos, código de salida 0.

---

## 0. Resumen ejecutivo

| # | Verificación | Veredicto |
|---|---|:---:|
| 1 | Análisis estático de `docker-compose.yml` | **PASA** |
| 2 | Arranque nativo de la API y códigos HTTP | **PASA** |
| 3 | Shape de la respuesta de `/ready` | **PASA** |
| 4 | Celery con broker real (Redis) y worker consumiendo | **PASA** |
| 5 | Migraciones contra el PostgreSQL real con pgvector | **PASA** |
| 6 | Suite de pruebas automatizadas | **PASA** |
| 7 | Construcción de la imagen Docker | **NO VALIDADO** |
| 8 | Arranque real de los 6 contenedores | **NO VALIDADO** |
| 9 | Imagen `pgvector/pgvector:pg16` del registro | **NO VALIDADO** |

Los puntos 7, 8 y 9 **no** se validated por una única razón, verificada: **este entorno no tiene el
binario `docker`**. No es una limitación del código; es una limitación del entorno de ejecución. Se
detalla en la sección «Lo que NO se pudo validar».

Todo lo demás se validó **contra los servicios reales**, no contra una imitación:
PostgreSQL 16 con la extensión `vector` 0.6.2 y Redis 7 como broker y como backend de resultados.

---

## 1. Entorno de validación

```
== 0. Entorno ==
  PASA  El intérprete /workspace/.venv/bin/python está disponible
  PASA  Variables de los servicios reales cargadas desde /tmp/projecta_env.sh
  DATABASE_URL      = postgresql+asyncpg://postgres:@postgres?host=/tmp/projecta_pgdata
  CELERY_BROKER_URL = redis://127.0.0.1:6398/1
```

| Componente | Qué se usó | Por qué |
|---|---|---|
| PostgreSQL + pgvector 0.6.2 | `pgserver`, socket Unix en `/tmp/projecta_pgdata` | Es el motor de producción. Un SQLite no habría probado el driver `asyncpg` ni la extensión `vector`. |
| Redis 7 | Daemon en `127.0.0.1:6398` (broker `/1`, backend `/2`) | Es el broker de producción. El modo *eager* de Celery no demuestra que el worker consume de verdad. |
| Python 3.11 | `/workspace/.venv/bin/python` | El Python del sistema no tiene las dependencias. |

---

## 2. Análisis estático de `docker-compose.yml`

Comando: `bash scripts/verificar_stack.sh` (sección 1 del script).

```
== 1. Análisis estático del docker-compose ==
  PASA  los 6 servicios del contrato existen
  PASA  api: todos sus depends_on apuntan a servicios que existen
  PASA  api: el healthcheck declara test, interval, timeout y retries
  PASA  worker: todos sus depends_on apuntan a servicios que existen
  PASA  worker: el healthcheck declara test, interval, timeout y retries
  PASA  beat: todos sus depends_on apuntan a servicios que existen
  PASA  beat: el healthcheck declara test, interval, timeout y retries
  PASA  postgres: todos sus depends_on apuntan a servicios que existen
  PASA  postgres: el healthcheck declara test, interval, timeout y retries
  PASA  redis: todos sus depends_on apuntan a servicios que existen
  PASA  redis: el healthcheck declara test, interval, timeout y retries
  PASA  minio: todos sus depends_on apuntan a servicios que existen
  PASA  minio: el healthcheck declara test, interval, timeout y retries
  PASA  minio-init: todos sus depends_on apuntan a servicios que existen
  PASA  minio-init: el healthcheck declara test, interval, timeout y retries
  PASA  postgres usa una imagen con pgvector
  PASA  no hay variables de entorno huérfanas (huerfanas: [])
  Comprobaciones de compose: 0 fallo(s)
  PASA  docker-compose.yml supera el análisis estático
```

**Qué cubre esto:** el YAML carga; los 6 servicios del contrato existen; ningún `depends_on` apunta
a un servicio inexistente; cada healthcheck declara `test`, `interval`, `timeout` y `retries`;
`postgres` usa una imagen con pgvector; y toda variable `${…}` interpolada por el compose está
declarada en `.env.example`.

**Qué NO cubre esto:** que el YAML sea *semánticamente* correcto para el motor de Docker. Eso exige
el binario `docker`. Ver la sección «Lo que NO se pudo validar».

---

## 3. Arranque nativo de la API

Comando: `bash scripts/verificar_stack.sh` (sección 2). Arranca `uvicorn app.main:app` con las
variables de los servicios reales.

```
== 2. Arranque nativo de la API y códigos HTTP ==
  PASA  La API arranca y responde en http://127.0.0.1:8000
  /health  -> HTTP 200
  PASA  /health devuelve 200
  /ready   -> HTTP 200
  PASA  /ready devuelve 200
  /        -> HTTP 200
  PASA  / devuelve 200
  /docs    -> HTTP 200
  PASA  /docs devuelve 200
```

Log de arranque de la aplicación (literal):

```
INFO:     Started server process [2083]
INFO:     Waiting for application startup.
2026-10-22 19:06:20,290 INFO     projecta :: Arrancando Projecta PMO Intelligence v0.1.0
2026-10-22 19:06:20,290 INFO     projecta :: Entorno: development · Depuración: False
2026-10-22 19:06:20,290 INFO     projecta :: Base de datos: postgresql+asyncpg://postgres:***@postgres?host=/tmp/projecta_pgdata
2026-10-22 19:06:20,291 INFO     projecta :: Redis: redis://127.0.0.1:6398/0
2026-10-22 19:06:20,291 INFO     projecta :: Almacenamiento: http://localhost:9000 (bucket projecta-evidencia)
2026-10-22 19:06:20,291 INFO     projecta :: Orígenes CORS permitidos: ninguno
2026-10-22 19:06:20,291 INFO     projecta :: Nivel de log: INFO
2026-10-22 19:06:20,291 INFO     projecta :: Projecta PMO Intelligence operativo.
INFO:     Application startup complete.
```

---

## 4. Shape de `/ready`

Comando: `bash scripts/verificar_stack.sh` (sección 3).

```
== 3. Shape de la respuesta de /ready ==
{"status":"ok","checks":{"database":{"ok":true,"latency_ms":54.7},"redis":{"ok":true,"latency_ms":54.44},"storage":{"ok":false,"latency_ms":51.85,"error":"no disponible en este perfil"}}}
  PASA  el JSON tiene exactamente status y checks
  PASA  status es 'ok' o 'degraded'
  PASA  checks contiene database, redis y storage
  PASA  el chequeo database declara 'ok'
  PASA  el chequeo database declara 'latency_ms'
  PASA  el chequeo redis declara 'ok'
  PASA  el chequeo redis declara 'latency_ms'
  PASA  el chequeo storage declara 'ok'
  PASA  el chequeo storage declara 'latency_ms'
  PASA  status='ok' solo cuando database y redis responden
  PASA  El shape de /ready cumple el contrato
```

**Lectura del resultado:** `database` y `redis` responden de verdad (contra PostgreSQL y Redis
reales), así que `status` es `"ok"` y el código es 200. `storage` aparece con `"ok": false` porque
el almacenamiento de objetos no forma parte del perfil mínimo de este entorno: **su fallo no
degrada el estado global**, tal y como fija el contrato (sección c). Es una dependencia no crítica.

### 4.1. El caso degradado, comprobado aparte

Con la base de datos inalcanzable (`DATABASE_URL` apuntando a un puerto cerrado):

```
=== /ready (BD inalcanzable) ===
{"status":"degraded","checks":{"database":{"ok":false,"latency_ms":196.19,"error":"ConnectionRefusedError: [Errno 111] Connection refused"},"redis":{"ok":true,"latency_ms":195.02},"storage":{"ok":false,"latency_ms":193.58,"error":"no disponible en este perfil"}}}
HTTP=503
=== /health ===
OK HTTP=200
=== / ===
HTTP=200
```

Comportamiento correcto: `/ready` degrada a **503**, mientras que `/health` (liveness puro) y `/`
siguen respondiendo **200**. La API no se cae porque la base de datos no llegue.

---

## 5. Celery con broker real

Comando: `bash scripts/verificar_stack.sh` (sección 4). Se levanta un **worker de verdad** con
`celery -A app.core.celery_app:celery_app worker -Q ia,mantenimiento`, y la tarea se encola con
`.delay()` **desde otro proceso**.

```
== 4. Celery con broker real (Redis) ==
  PASA  las 6 colas del contrato están declaradas (obtenidas: ['documentos', 'ia', 'ingesta', 'integraciones', 'mantenimiento', 'reportes'])
  PASA  la constante COLAS del módulo coincide con el contrato
  PASA  task_acks_late is True
  PASA  task_reject_on_worker_lost is True
  PASA  worker_prefetch_multiplier == 1
  PASA  broker_connection_retry_on_startup is True
  PASA  beat_schedule tiene una entrada
  PASA  el beat llama a projecta.mantenimiento.ping
  PASA  el beat se dispara cada 5 minutos
  PASA  las tareas registradas son exactamente las del contrato (obtenidas: ['projecta.mantenimiento.ping', 'projecta.smoke'])
  PASA  La configuración de Celery cumple el contrato
  PASA  El worker de Celery arranca y responde 'pong' al inspect ping
  Tarea encolada: id=bd32b759-8f07-42e9-a72e-060cfbcaa177
  Resultado recibido por el backend: {'status': 'ok', 'app': 'Projecta PMO Intelligence', 'timestamp': '2026-10-22T19:24:12.949086+00:00', 'echo': 'verificacion-real'}
  PASA  el worker consumió la tarea y devolvió status='ok'
  PASA  el eco viaja intacto por el broker
  PASA  la marca es 'Projecta PMO Intelligence'
  PASA  Una tarea encolada con .delay() la consume el worker real
```

Log del worker (literal, recortado):

```
 -------------- [queues]
                .> ia               exchange=projecta(direct) key=ia
                .> mantenimiento    exchange=projecta(direct) key=mantenimiento

[tasks]
  . projecta.mantenimiento.ping
  . projecta.smoke

[2026-10-22 19:20:34,616: INFO/MainProcess] Connected to redis://127.0.0.1:6398/1
[2026-10-22 19:20:34,622: INFO/MainProcess] mingle: searching for neighbors
[2026-10-22 19:20:35,645: INFO/MainProcess] mingle: all alone
[2026-10-22 19:20:35,657: INFO/MainProcess] celery@cloud-compute-new-t9d6j ready.
```

**Lo que demuestra esto:** el mensaje sale del productor, viaja por el broker Redis real, lo
consume un worker real, y el resultado vuelve por el result backend. No es el modo *eager*: el
resultado tiene un `id` de tarea y un `timestamp` de ejecución.

---

## 6. Migraciones contra el PostgreSQL real

Comando: `bash scripts/verificar_stack.sh` (sección 5).

```
== 5. Migraciones contra el PostgreSQL real (con pgvector) ==
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
  PASA  alembic upgrade head se ejecuta correctamente
  Tablas en el esquema public: alembic_version,organizaciones,proyectos,roles,usuarios
  PASA  La tabla organizaciones existe en PostgreSQL
  PASA  La tabla proyectos existe en PostgreSQL
  PASA  La tabla usuarios existe en PostgreSQL
  PASA  La tabla roles existe en PostgreSQL
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running downgrade 0001 -> , Esquema inicial de Projecta PMO Intelligence.
  PASA  alembic downgrade base se ejecuta correctamente
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Esquema inicial de Projecta PMO Intelligence.
  PASA  El round-trip upgrade -> downgrade -> upgrade es simétrico
```

Estructura creada, con las políticas de borrado explícitas (literal de PostgreSQL):

```
 table_name    |   column_name   | delete_rule | update_rule
--------------+-----------------+-------------+-------------
 proyectos     | organizacion_id | CASCADE     | CASCADE
 usuarios      | organizacion_id | CASCADE     | CASCADE
 usuarios      | rol_id          | RESTRICT    | CASCADE
(3 rows)
```

---

## 7. Suite de pruebas

Comando: `bash scripts/verificar_stack.sh` (sección 6) → `pytest tests -q`.

```
== 6. Suite de pruebas ==
....................                                                     [100%]
=============================== warnings summary ===============================
tests/test_smoke_stack.py::test_cada_plantilla_renderiza_sin_error[index.html]
tests/test_smoke_stack.py::test_cada_plantilla_renderiza_sin_error[health.html]
tests/test_smoke_stack.py::test_cada_plantilla_renderiza_sin_error[partials/_cabecera.html]
tests/test_smoke_stack.py::test_cada_plantilla_renderiza_sin_error[partials/_estado_vacio.html]
tests/test_smoke_stack.py::test_cada_plantilla_renderiza_sin_error[partials/_tarjeta_estado.html]
tests/test_smoke_stack.py::test_las_plantillas_escapan_el_contenido_malicioso
tests/test_smoke_stack.py::test_la_marca_aparece_siempre_completa
  /workspace/.venv/lib/python3.11/site-packages/starlette/templating.py:92: DeprecationWarning: Extra environment options are deprecated. Use a preconfigured jinja2.Environment instead.
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
20 passed, 7 warnings in 2.78s
  PASA  La suite de pruebas pasa
```

Las 7 advertencias son un `DeprecationWarning` de Starlette sobre `Jinja2Templates(env=…)`. La
aplicación ya construye su propio `jinja2.Environment` con `autoescape=True`, que es justamente la
forma que Starlette recomienda; la advertencia viene de la propia clase de Starlette, no del
código de Projecta PMO Intelligence. No afecta al comportamiento.

**Cobertura de la suite:**

| Prueba | Qué comprueba |
|---|---|
| `test_la_aplicacion_se_construye_sin_error` | `create_app()` devuelve una FastAPI con el título de la marca |
| `test_se_registran_las_rutas_de_salud` | `/health`, `/ready`, `/salud` y `/` están montadas |
| `test_sin_secret_key_el_error_nombra_la_variable` | Sin `SECRET_KEY` el arranque falla nombrando la variable |
| `test_no_existe_bootstrap_admin_password` | Esa variable heredada no existe en la configuración |
| `test_cada_plantilla_renderiza_sin_error` | Las 5 plantillas renderizan sin excepción |
| `test_las_plantillas_escapan_el_contenido_malicioso` | `<script>` sale escapado, nunca ejecutable |
| `test_la_marca_aparece_siempre_completa` | «Projecta» a secas no aparece en el HTML |
| `test_ready_devuelve_el_shape_del_contrato` | El JSON de `/ready` tiene el shape exacto |
| `test_health_es_liveness_puro` | `/health` devuelve `OK` en texto plano |
| `test_la_portada_responde_html` | `/` renderiza HTML con la marca completa |
| `test_celery_registra_las_seis_colas_del_contrato` | Exactamente 6 colas, ni una más ni una menos |
| `test_celery_registra_exactamente_las_dos_tareas` | Solo `projecta.smoke` y `projecta.mantenimiento.ping` |
| `test_celery_tiene_los_parametros_de_robustez` | `acks_late`, `reject_on_worker_lost`, prefetch 1, límites de tiempo |
| `test_el_beat_llama_al_ping_de_mantenimiento_cada_cinco_minutos` | `crontab(minute="*/5")` |
| `test_la_tarea_smoke_devuelve_el_contrato` | `status`, `app`, `timestamp` y `echo` |
| `test_la_migracion_es_simetrica` | Round-trip `upgrade` → `downgrade` de la migración inicial |

---

## 8. Resultado final

```
== Resumen ==
  Comprobaciones: 39 | Fallos: 0
  NO VALIDADO en este entorno (no existe el binario `docker`):
    · la construcción real de la imagen
    · el arranque real de los 6 contenedores
    · la imagen pgvector/pgvector:pg16 del registro
    · el arranque del perfil `observabilidad` (prometheus + grafana + otel)
RESULTADO: todas las comprobaciones pasan
```

**Código de salida de `scripts/verificar_stack.sh`: `0`.**
**Pruebas: 73 passed, 0 failed** (20 del esqueleto inicial + 53 de la Épica 0).

> Las 20 comprobaciones originales miden el esqueleto: infraestructura, arranque de la API,
> shape de `/ready`, Celery con broker real, migraciones contra PostgreSQL real y suite de
> pruebas. Las 19 nuevas miden la Épica 0: integridad textual del código, catálogo de roles
> contra el catálogo normativo, presencia y validez de la configuración de observabilidad,
> perfil de observabilidad del compose, variables documentadas en `.env.example` y registro de
> las rutas nuevas. El detalle está en la sección 10 bis.

---

## 9. Lo que NO se pudo validar

Los tres puntos siguientes están **sin validar** y no se presentan como validados en ningún otro
documento de este repositorio. La causa es única y comprobable:

```
$ which docker
(nada: el binario no existe en el PATH)

$ docker --version
bash: docker: command not found
```

| # | Punto | Motivo | Cómo se resolvería |
|---|---|---|---|
| 7 | Construcción de la imagen (`docker build`) | No hay demonio Docker | En un entorno con Docker: `docker build -t projecta-pmo-intelligence:0.1.0 .` |
| 8 | Arranque de los 6 contenedores (`docker compose up -d`) | No hay demonio Docker | `docker compose --profile minio up -d` y luego `docker compose ps` para ver los healthchecks |
| 9 | La imagen `pgvector/pgvector:pg16` del registro | No hay demonio Docker para descargarla ni arrancarla | `docker pull pgvector/pgvector:pg16` y comprobar `CREATE EXTENSION vector` |

**Matiz importante sobre el punto 9.** La extensión `vector` **sí está disponible y verificada**
en el PostgreSQL del entorno de validación: es un PostgreSQL real con `vector` 0.6.2 instalado
(comprobado con `SELECT extname, extversion FROM pg_extension`). Lo que no se ha probado es la
imagen concreta del registro ni su inicialización en un contenedor. La diferencia importa: el
comportamiento de pgvector está validado; el empaquetado de la imagen, no.

**Qué sí cubre el análisis estático del compose**, como red de seguridad para lo que no se puede
ejecutar: los 6 servicios existen, cada `depends_on` apunta a un servicio real, cada healthcheck
tiene sus cuatro claves, `postgres` usa una imagen con pgvector, los volúmenes referenciados
existen, y no hay variables de entorno huérfanas frente a `.env.example`. Eso **no** sustituye a
arrancar los contenedores, pero acota el riesgo a lo que un análisis estático no puede ver.

---

## 10. Defectos encontrados y corregidos durante la validación

Esta sección no es decorativa: son defectos **reales** que aparecieron al ejecutar el stack, no
al leerlo. Todos están corregidos y revalidados.

| # | Defecto | Síntoma | Causa raíz | Corrección |
|---|---|---|---|---|
| 1 | `alembic upgrade head` fallaba en todo | `ValueError: dictionary update sequence element #0 has length 1; 2 is required` | `alembic.ini` declaraba `sqlalchemy.connect_args = {}`. ConfigParser lo lee como la **cadena** `"{}"`, y `create_async_engine` hace `dict.update("{}")`. | La opción se comenta en `alembic.ini`. El motor ya gestiona `connect_args` por su cuenta. |
| 2 | `alembic` no funcionaba con `DATABASE_URL` de SQLite | `InvalidRequestError: The asyncio extension requires an async driver to be used. The loaded 'pysqlite' is not async.` | `env.py` pasaba la URL **síncrona** a `async_engine_from_config`. Funciona por casualidad con `psycopg` (que tiene modo asíncrono) y revienta con `pysqlite`. | `env.py` usa ahora `settings.database_url` (la URL asíncrona). `database_url_sync` sigue existiendo para las herramientas que necesiten una URL síncrona. |
| 3 | La migración no era portable | `sqlite3.OperationalError: near "(": syntax error` | `server_default=sa.text("now()")` es sintaxis exclusiva de PostgreSQL. | Sustituido por `sa.func.now()`, que se traduce a `now()` en PostgreSQL y a `CURRENT_TIMESTAMP` en SQLite. |
| 4 | El `.env` con credenciales reales podía acabar en el repositorio | — | No existía `.gitignore` en el esqueleto. | Creado `.gitignore` excluyendo `.env`, `.env.*` (salvo `.env.example`), `.venv/`, `.data/`, `__pycache__/`. |
| 5 | Ambigüedad en el contrato sobre el estado global | — | El contrato decía a la vez que el fallo de `storage` no cambiaba el estado y que sí lo degradaba. | Contrato aclarado (sección c): `status` es `"ok"` **si y solo si** `database` y `redis` responden; el fallo de `storage` no cambia ni el `status` ni el código HTTP. La implementación ya cumplía esa regla. |
| 6 | `verificar_stack.sh` se colgaba en el `inspect ping` | Bucle infinito de sondeos | Dos instancias del script convivían y duplicaban el nodo Celery (`DuplicateNodenameWarning`), lo que agotaba los tiempos de espera del broadcast. | El worker se espera leyendo su log (`ready.`), con nodo único (`-n "verificacion@%h"`), y se limpia el grupo de procesos al terminar. |
| 7 | El script abortaba en el primer fallo | — | `set -euo pipefail` sin guardar el estado de los pipelines. | Se usa `PIPESTATUS` y `|| true` en los puntos donde un fallo debe registrarse y seguir. |

---

## 10 bis. Épica 0 (HU_01 a HU_08): validación de la implementación

La Épica 0 se implementó sobre el esqueleto ya validado. La validación se apoya en tres
pilares, todos reproducibles:

1. **73 pruebas automatizadas** (`tests/test_epica0.py`, 53 pruebas nuevas) que comprueban el
   comportamiento que el TO-BE v1.1 fija, no la forma del código.
2. **39 comprobaciones** del script `scripts/verificar_stack.sh`, que pasó de 20 a 39.
3. **Un verificador de integridad textual** (`scripts/verificar_integridad_textual.py`) que
   comprueba, byte a byte, que los literales críticos del código están intactos.

### 10 bis.1 Cobertura por historia de usuario

| HU | Qué comprueba | Pruebas |
|---|---|---|
| HU_02 | Catálogo de seis roles con nivel correlativo; detección de discrepancias entre base de datos y catálogo; siembra idempotente. | 6 |
| HU_03 | Alta de proyecto con evento `PROYECTO_CREADO`; 409 por código duplicado en la misma organización; normalización del código a mayúsculas; 422 por estado desconocido; borrado lógico con desaparición del listado. | 8 |
| HU_04 | bcrypt con truncado a 72 bytes; claims del token de sesión; distinción entre token expirado y token manipulado; comparación en tiempo constante del refresh; login, `me`, refresh rotativo, logout; **reutilización del refresh ⇒ revocación de la familia entera**; bloqueo del login por contraseña en producción. | 17 |
| HU_05 | Nivel mínimo, ámbito de proyecto, ausencia de membresía, usuario desactivado y orden estable de las reglas (el motivo auditado es el real). | 8 |
| HU_05 | **El rechazo por permisos deja evento `TRANSICION_RECHAZADA`**: no modifica datos, pero tampoco es silencio. | 1 |
| HU_06 | `/metrics` expone métricas propias; el `correlation_id` se devuelve en la respuesta y se respeta el que envía el cliente. | 4 |
| HU_08 | SHA-256 correcto; clave derivada del contenido y repartida en subdirectorios; dos ficheros con el mismo contenido comparten clave; tamaño máximo acotado; el listado no se rompe sin almacenamiento. | 6 |
| — | **Cadena de auditoría**: tres eventos encadenados verifican íntegros; alterar el contenido de uno rompe la cadena y se detecta; un tipo de evento desconocido se rechaza. | 4 |

### 10 bis.2 Defectos reales encontrados al implementar y ejecutar la Épica 0

Estos defectos aparecieron **al ejecutar**, no al leer. Todos están corregidos y revalidados.

| # | Defecto | Síntoma | Causa raíz | Corrección |
|---|---|---|---|---|
| 8 | El hash de contraseña fallaba siempre | `TypeError: gensalt() got an unexpected keyword argument 'rondas'` | La firma de `bcrypt.gensalt` es `rounds`, no `rondas`. | Corregido a `rounds=`. El mismo defecto estaba en `verificar_contrasena`, que además habría devuelto `False` en silencio. |
| 9 | Las pruebas contra PostgreSQL **borraban el esquema de la base de datos real** | Tras la suite, la base de datos solo conservaba `alembic_version` | `tests/conftest.py` fijaba `DATABASE_URL` con `os.environ.setdefault`. Con la variable ya exportada —como hace `verificar_stack.sh`— la suite usaba la base real, y su fixture `base_de_datos` hace `drop_all` al terminar. | `DATABASE_URL` se fija **siempre** a la base en memoria, y se avisa por pantalla cuando el entorno exterior apuntaba a otra. Segunda barrera: la fixture `base_de_datos` **falla** si el motor no es SQLite. |
| 10 | La cadena de auditoría se rompía en cada evento | La verificación informaba de una rotura en el segundo evento | Al buscar el hash del evento anterior, la consulta no veía el evento previo: seguía solo en la sesión, sin vaciar. El encadenado volvía al hash inicial en cada evento. | `registrar()` hace `flush()` antes de buscar el último hash. El evento anterior queda escrito y visible. |
| 11 | La huella se calculaba sobre un instante distinto del guardado | La cadena parecería manipulada al releerla | `contenido_a_hashear` calculaba `datetime.now()` internamente, y `registrar()` guardaba otro `datetime.now()`: dos microsegundos distintos. | El instante se calcula **una vez** y se pasa como parámetro. Además, `momento_canonico()` normaliza a UTC, porque SQLite no guarda la zona horaria y una fila releída daría un texto distinto. |
| 12 | `verificar_cadena` llamaba a un método inexistente | `AttributeError: 'EventoAuditoria' object has no attribute 'contenido_a_hashear'` | El cálculo del contenido estaba como método del dataclass `Evento`, pero la verificación lo invocaba sobre la fila ORM. | El cálculo se extrajo a una función de módulo, `contenido_a_hashear()`, que usan tanto el evento que se inserta como la fila que se relee. Duplicarlo habría permitido que una de las dos copias divergiera. |
| 13 | `/auth/refresh` devolvía 500 | `TypeError: can't compare offset-naive and offset-aware datetimes` | `Sesion.expira_en` es `DateTime(timezone=True)`, pero SQLite no conserva la zona horaria: el valor releído es naive y la comparación con `now(utc)` revienta. | Se añadió `security.a_utc()`, que normaliza cualquier instante a UTC con zona explícita, y se usa en todas las comparaciones. |
| 14 | `/auth/refresh` devolvía 500 | `MissingGreenlet: greenlet_spawn has not been called` | `usuario.rol.nombre` disparaba una **carga perezosa** de la relación, que SQLAlchemy no permite en código asíncrono. | El rol se resuelve con `sesion.get(Rol, usuario.rol_id)`, una consulta propia. Se aplicó lo mismo en `_identidad_de()`. |
| 15 | El modelo declaraba `roles.ambito` pero el esquema no lo tenía | `column "ambito" of relation "roles" does not exist` al sembrar los roles | La migración `0002` añadió `roles.nivel` pero se olvidó de `roles.ambito`. Las pruebas no lo detectaron porque SQLite crea el esquema **desde el modelo**, no desde las migraciones. | La migración añade `roles.ambito` de forma idempotente y el `downgrade` la revierte. Lo detectó el round-trip de migraciones contra PostgreSQL real. |
| 16 | El catálogo de roles no viajaba con el esquema | Un despliegue recién migrado no podía autorizar nada | El catálogo se sembraba a mano. | La migración `0002` siembra los seis roles normativos. El `downgrade` solo los retira si conservan su descripción de origen, para no destruir trabajo administrativo. |
| 17 | La siembra de roles fallaba contra SQLite | `Error binding parameter 1: type 'UUID' is not supported` | Con `sa.text()`, el `uuid.UUID` se pasa como parámetro Python crudo. | La siembra usa una tabla tipada (`sa.table(...)`) para que SQLAlchemy convierta el valor según el dialecto. |
| 18 | Las pruebas creaban un esquema vacío | `no such table: organizaciones` | `Base.metadata` solo conoce las tablas de los módulos **importados**. Los modelos se importaban de forma diferida dentro de las fixtures, así que `create_all` no creaba nada. | El `conftest` importa los modelos antes de `create_all` y **comprueba** que el metadato no está vacío. |
| 19 | `/metrics` no mostraba ninguna métrica propia | Solo las métricas por defecto de Python y del proceso | Las métricas de la aplicación se creaban al usarse por primera vez. | `_registrar_metricas_de_la_aplicacion()` las crea **al arrancar**, para que un panel no muestre series vacías y una alerta no falle por un contador inexistente. |
| 20 | El script de verificación imprimía un error de shell | `observabilidad: command not found` | Las comillas invertidas dentro de una cadena con comillas dobles se interpretan como sustitución de comando. | Se usan comillas simples en esos mensajes. |

### 10 bis.3 Defectos de corrupción textual

El transporte de comandos de este entorno ha corrompido literales en varias ocasiones. Se
construyó `scripts/verificar_integridad_textual.py` para que eso no pueda pasar **desapercibido**:
comprueba byte a byte los literales críticos y detecta texto pegado, y **aborta con código 2**
si encuentra algo.

| # | Corruption detectada | Dónde | Corrección |
|---|---|---|---|
| 21 | `Ver contenido sinRestriction` | `app/core/rbac.py` | `Ver contenido sin restricción` |
| 22 | `permite.indexar la columna` | `app/core/security.py` | `permite indexar la columna` |
| 23 | `Rediseño del主干` (caracteres CJK) | `tests/test_epica0.py` | `Rediseño del núcleo de procesos` |
| 24 | `转换 a métricas` (caracteres CJK) | `observability/otel/config.yml` | `conversión a métricas` |
| 25 | `La cadena pareceríaSHA válida` | `app/core/auditoria.py` | `La cadena parecería válida` |

El verificador incorpora además la detección de ideogramas y de caracteres cirílicos, que en un
proyecto íntegramente en español solo pueden ser corrupciones.

### 10 bis.4 Resultado

```
== Resumen ==
  Comprobaciones: 39 | Fallos: 0
  ...
RESULTADO: todas las comprobaciones pasan
```

**Código de salida de `scripts/verificar_stack.sh`: `0`.**
**Pruebas: 73 passed, 0 failed.**

### 10 bis.5 Auditoría exhaustiva del proyecto completo (2026-10-22)

Los verificadores anteriores miran cada artefacto por separado. Se añadió
`/workspace/output/herramientas/validar_proyecto_completo.py`, un auditor de **ocho secciones**
que recorre **el repositorio y todos los entregables** a la vez:

| Sección | Qué comprueba |
|---|---|
| A · Repositorio | Sintaxis de los 35 ficheros Python; importación real de los 27 módulos de `app`; que cada símbolo importado de `app.*` exista; que cada `__all__` sea veraz; sintaxis de shell; YAML y JSON válidos. |
| B · Compose, Makefile, `.env.example` | Toda variable del compose declarada en `.env.example`; todo `depends_on` real; todo volumen, montaje y config existente; cada ruta que el Makefile invoca presente. |
| C · HTML | Forma del documento con el analizador de la biblioteca estándar; identificadores duplicados; anclas internas resueltas; ausencia de recursos externos. |
| D · Markdown | Enlaces y anclas internas; enlaces a ficheros; rutas absolutas mencionadas en el texto. |
| E · Coherencia | Recuento real de HUs y de SP frente a los declarados; las tres taxonomías; el catálogo de seis roles comparado entre el TO-BE, el código y la migración; la marca completa. |
| F · Release notes | Numeración correlativa; secciones obligatorias; respuesta explícita sobre el Worker. |
| G · Calidad | Patrones de credenciales; marcadores pendientes; restos de depuración; el verificador de integridad textual. |
| H · Funcionalidad | **21 comprobaciones que ejercitan la API completa, endpoint por endpoint**, verificando no solo el código de respuesta sino la **forma** del cuerpo. |

**Resultado: 0 bloqueantes, 4 avisos** (los cuatro están explicados y son correctos: cuatro
variables de entorno que solo aplican a la ejecución nativa, el ancla de la §7 en la línea base del
TO-BE v1.0 —defecto conocido cuyo aviso se verifica que sigue presente—, y la plantilla del
conversor, exenta del chequeo de anclas porque sus anclas se interpolan al renderizar).

**Cuatro defectos reales encontrados por la auditoría y corregidos:**

| # | Defecto | Síntoma | Causa raíz | Corrección |
|---|---|---|---|---|
| 26 | `__all__` delata un símbolo inexistente | `from app.core.observability import *` lanzaba `ImportError` | Declaraba `"distribucion"`, pero la función se llama `histograma` | `__all__` corregido. Un `__all__` que miente es peor que no tener `__all__`. |
| 27 | El despliegue con Docker no tenía identidad corporativa | `GET /auth/login` respondía 503 aunque el `.env` tuviera las variables OIDC | El bloque `x-app-env` del compose no las pasaba al contenedor | Las ocho variables de identidad y de trazas se incorporan al compose. |
| 28 | La subida de adjuntos declaraba un proyecto opcional que nunca podía omitirse | `POST /adjuntos` sin proyecto devolvía 403 con «La operación requiere un proyecto concreto» | La política `cargar_evidencia` tiene ámbito de proyecto, y `rbac.evaluar()` deniega por defecto cuando falta | El endpoint exige el proyecto (422 sin él), el modelo hace `proyecto_id` **NOT NULL** y la migración 0002 lo crea así. |
| 29 | La suite no detectaba una divergencia entre modelo y esquema | `roles.ambito` faltaba en el esquema y las pruebas no lo veían | SQLite crea el esquema **desde el modelo**, no desde las migraciones | Detectado por el round-trip contra PostgreSQL real; corregido en la migración 0002. |

**Cinco pruebas de regresión nuevas** (`tests/test_epica0.py`, clase `TestCoherenciaInterna`) fijan
cada uno de estos defectos: ningún `__all__` declara un símbolo inexistente; la subida exige
proyecto; el listado lo admite opcional; el modelo exige proyecto; el compose pasa las variables
de identidad y de trazas.

**Dos falsos positivos del propio auditor, corregidos.** El primero: `defaultdict(int, lista)` trata
la lista como un diccionario y revienta con `ValueError` — hay que usar `Counter`. El segundo: el
analizador de HTML llama a `handle_endtag` en los elementos vacíos auto-cerrados (`<meta />`), lo que
produce cierres sin apertura que no son defectos reales.

**Una lección sobre las excepciones.** El ancla de la §7 en el TO-BE v1.0 es un defecto conocido de
esa versión, y el auditor **no lo ignora**: comprueba que el aviso que lo documenta **sigue presente
en el documento**, y si alguien lo borra, el aviso pasa a ser fallo. Una excepción sin vigilancia es
un defecto diferido.

---

## 10 bis.6 Edge Connector: PoC de extremo a extremo (2026-10-23)

El Edge Connector se validó contra el stack real, no con una descripción. Esta sección recoge lo
que se midió, lo que se corrigió y lo que queda sin validar.

### 10 bis.6.1 Qué se validó

| Elemento | Cómo |
|---|---|
| Ciclo sensor → servidor → auditoría | `scripts/demo_edge_conector.py`: 23 comprobaciones, 0 fallos |
| Puerta del consentimiento (con y sin registro) | 6 pruebas en `tests/test_edge.py` |
| Minimización en origen y en destino | 3 pruebas + comprobaciones 7, 20 y 21 |
| Firma: válida, manipulada, ajena, alterada, malformada | 6 pruebas |
| Idempotencia por huella de contenido | 1 prueba + comprobaciones 10 y 19 |
| Lista cerrada de receptores | 1 prueba + comprobaciones 3 y 15 |
| Antijuego: ventana de 300 s, pasado y futuro | 2 pruebas |
| Revocación de dispositivo y de consentimiento | 3 pruebas + comprobaciones 16, 17 y 18 |
| Auditoría de aceptación y de rechazo; cadena íntegra | 3 pruebas + comprobaciones 22 y 23 |
| Cola local persistente y cola muerta del sensor | 3 pruebas de `TestSensorLocal` |
| La clave privada no se transmite y se guarda con permisos `0600` | 1 prueba |
| Migración `0003` sobre PostgreSQL real | `alembic upgrade head` + round-trip `upgrade → downgrade → upgrade` |
| Suite completa del repositorio | 112 pruebas, 112 superadas |

La sección 8 de `scripts/verificar_stack.sh` (**14 comprobaciones nuevas**) verifica de forma
continua lo que la PoC demostró una vez: la existencia de los tres módulos del Edge
Connector, las 9 rutas `/edge/...` en el OpenAPI, las 3 tablas en PostgreSQL real, la
declaración de `cryptography`, que la lista de receptores es cerrada y el único modo admitido es
`READ_ONLY`, que **la puerta del consentimiento falla cerrada** ante un recurso sin registro, y
que la minimización bloquea los campos prohibidos.

### 10 bis.6.2 Defectos reales encontrados al ejecutar la PoC

Seis, todos corregidos y todos con regresión:

| # | Defecto | Gravedad | Regresión |
|---|---|---|---|
| D-01 | El servidor minimizaba el **sobre** del evento, no el payload interior: lo persistido quedaba con `payload.payload` y la huella de idempotencia se calculaba sobre una estructura que no era la del dato. | Importante | `test_el_payload_guardado_es_la_senal_no_el_sobre` |
| D-02 | `POST /edge/eventos` devolvía **siempre HTTP 200** y anunciaba el código real en un campo `codigo` del cuerpo. Un conector que leyera el campo equivocado creería que un rechazo fue una aceptación. | Importante | Comprobación 11 de la demostración, más el código HTTP de verdad |
| D-03 | La cola muerta del sensor fallaba al escribir si el directorio de estado no existía: el evento que agotaba los reintentos **se perdía**. | Importante | `test_la_cola_muerta_recibe_lo_que_no_se_puede_enviar` |
| D-04 | La demostración no era re-ejecutable: fallaba con `uq_roles_nombre` y `uq_proyectos_codigo` al repetirse. | Menor | El procedimiento la ejecuta dos veces seguidas |
| D-05 | El esquema de la firma rechazaba con 422 antes de llegar a la capa criptográfica, sin que el comportamiento estuviera documentado ni fijado. | Menor | `test_firma_corta_se_rechaza_en_el_esquema` y `test_firma_con_formato_invalido_no_revienta` |
| D-06 | El health agent informaba de «sano» a un dispositivo recién registrado, que aún no había dado señales. | Menor | `test_salud_de_un_dispositivo_que_ya_hablo` y `test_salud_de_un_dispositivo_que_nunca_hablo_avisa` |

### 10 bis.6.3 Dos hallazgos sobre el propio proceso de validación

Estos dos no son defectos del Edge Connector, sino de la manera de medir, y son más importantes que
los seis anteriores porque afectan a **todas** las cifras que este proyecto ha dado.

**La caché de bytecode obsoleta.** El sistema de ficheros del entorno de validación **no actualiza
la marca de tiempo** de un fichero cuando su contenido cambia y su tamaño permanece igual. Python
valida su caché de bytecode por la pareja `(mtime, tamaño)`, así que después de editar un módulo el
intérprete puede seguir ejecutando **la versión anterior** del código, sin ningún aviso. La
reproducción es de una línea:

```python
# m.py contiene VALOR = "VERSION-1"
>>> import m; m.VALOR
'VERSION-1'
# se reescribe m.py con VALOR = "VERSION-2", sin cambiar el tamaño
>>> import m; m.VALOR
'VERSION-1'      # <- obsoleto: la caché no se invalidó
```

El riesgo no es teórico: durante la PoC, un cambio en `app/core/edge.py` «no tomaba efecto» y la
explicación buscada —«el código está mal»— era la equivocada. Un cambio que arregla un defecto
puede «no funcionar» y llevar a «corregir» un defecto que ya no existe, y la entrega a presentarse
como un éxito.

La mitigación es `scripts/purgar_cache.py`, invocado desde `tests/conftest.py` (en el import,
antes de importar nada de `app.*`), desde `scripts/verificar_stack.sh` y desde
`scripts/demo_edge_conector.py`. **Toda medición de este proyecto pasa por la purga de caché.**

**La reversión de migraciones con datos.** La migración `0002` siembra el catálogo de seis roles, y
su `downgrade` los borraba sin comprobar si estaban en uso. Con un usuario asignado al rol
«administrador», la reversión fallaba con `ForeignKeyViolationError` — y fallaba **después** de que
las tablas ya se hubieran soltado, dejando el esquema a medias. Ahora un rol **en uso** no se
borra: un rol es un dato, no una columna, y dejarlo puesto no deja el esquema incoherente.

### 10 bis.6.4 Lo que NO se validó

| Elemento | Por qué | Cómo se cerraría |
|---|---|---|
| Arranque en contenedores Docker | No hay binario `docker` en el entorno | `docker compose up` con la imagen `pgvector/pgvector:pg16` |
| Perfil `observabilidad` | Mismo motivo | `docker compose --profile observabilidad up` |
| Conexión con un sistema fuente real del banco | El adaptador de la PoC es un simulador local | Adaptador de Outlook/Teams sobre el tenant del cliente |
| Transporte mTLS | El entorno no tiene PKI; la PoC usa Ed25519 | PKI del banco y certificado por dispositivo |
| Comportamiento en carga | La PoC no genera volumen | Prueba de carga con el número de dispositivos esperado |
| Renovación de la credencial del dispositivo | El M0 registra la caducidad pero no expone su renovación | Endpoint de renovación en el M1 |
| Autoridad de tiempo de la firma | La PoC usa la hora del sensor | Decidir si la autoridad es el sensor o el servidor |

El informe completo de la PoC, con la evidencia de ejecución, está en
`/workspace/output/integraciones/edge-connector/poc/`.

---

## 10 bis.7 Sensor de reuniones: capa de decisión (2026-10-23)

### Qué se ha implementado

El módulo `app/core/edge_reuniones.py` decide si una aplicación puede capturar audio en un
momento dado, y deja constancia del motivo. No captura: decide.

La decisión es la parte legalmente crítica, y es independiente del sistema operativo. Los
adaptadores que enumeran sesiones de audio y capturan por proceso (WASAPI, Core Audio,
PipeWire) dependen del equipo del cliente y **no** se han implementado.

### Verificación

| Comprobación | Resultado |
|---|---|
| `pytest tests/test_edge_reuniones.py` | **15 passed**, 0 failed |
| Pruebas negativas de la clase `TestNoSeEscucha` | 7 |
| Decisión con motivo, en los dos sentidos | verificado |
| Fallo cerrado sin registro de consentimientos | verificado |
| Determinismo de la decisión | verificado |

### Pruebas de mutación (control negativo)

| Mutación | Resultado esperado | Resultado obtenido |
|---|---|---|
| La decisión pasa a ser siempre `False` | fallo | **4 failed** |
| Un motivo del catálogo queda vacío | fallo | **1 failed** |
| La puerta de registro vacío deja de bloquear | fallo | **2 failed** |

En las tres se verificó **antes** que la mutación se aplicaba realmente (una comparación
byte a byte del literal sustituido). Una mutación que no se aplica no prueba nada: la
primera ejecución de esta tanda dio un falso «15 passed» por un literal mal escrito en el
propio script de mutación, y se corrigió.

### NO VALIDADO

- El comportamiento real de los adaptadores de sistema operativo.
- La captura real de audio.
- La correlación entre la reunión detectada y la reunión consentida.
- La admisibilidad del diseño por el Approved Data Owner del banco. El módulo informa; la
  decisión de registrar o no una reunión la toma alguien con potestad.

## 10 bis.8 Captura de reuniones de Teams (2026-10-23)

### Qué se ha implementado

El sistema de captura de audio y texto de reuniones de Teams, apoyado en
Microsoft Graph. Trae la transcripción con su hablante, la grabación y el informe
de asistencia, y las deja en Projecta PMO Intelligence.

La captura se apoya en Graph porque es donde Teams **ya guarda** la grabación y la
transcripción: la aplicación las trae, no las hace. No se captura el micrófono del
equipo ni el audio del sistema.

Ficheros nuevos: `app/core/graph_teams.py`, `app/core/asyncio_bridge.py`,
`app/workers/tasks/captura_teams.py`, `app/api/routers/reuniones.py`,
`alembic/versions/0004_captura_reuniones.py`, `scripts/mock_graph.py`,
`scripts/teams_captura.py`, `scripts/preparar_captura.py` y tres ficheros de
pruebas.

### Verificación

| Comprobación | Resultado |
|---|---|
| `pytest tests/test_captura_teams.py` | **27 passed** |
| `pytest tests/test_captura_tarea.py` | **9 passed** |
| `pytest tests/test_captura_api.py` | **8 passed** |
| Suite completa | **195 passed**, 0 failed |
| `scripts/verificar_stack.sh` | todas las comprobaciones pasan |
| Migración 0004 sobre PostgreSQL real | aplicada; 6 índices |
| Ciclo completo contra el doble de Graph | reunión capturada, 7 segmentos, 2 participantes |

### Pruebas de mutación (control negativo)

| Mutación | Resultado obtenido |
|---|---|
| El hablante se pierde | **4 failed** |
| El analizador no devuelve segmentos | **11 failed** |
| El 403 de transcripciones se traga | **1 failed** |

En la tercera, el literal aparece **dos veces** en el fichero. La comprobación
de que la mutación se aplica tiene que contar las ocurrencias y fallar si no es
exactamente una: la primera ejecución no se aplicó y dio un falso «8 passed».

### Defectos reales encontrados y corregidos en esta tanda

1. **`self.segments` en vez de `self.segmentos`** en `graph_teams.py`: el
   atributo no existe, y `texto_plano` reventaba.
2. **`asyncio.run()` dentro de una tarea de Celery**: revienta si la tarea se
   ejecuta con un bucle de eventos activo, que es lo que pasa en modo *eager* y
   en un worker con pool de hilos asíncrono. Corregido con
   `app/core/asyncio_bridge.py`, aplicado **también** a la tarea de ingesta de
   Teams, que tenía el mismo defecto latente.
3. **La fila y su auditoría en transacciones distintas**: con un `commit` antes
   de auditar, una auditoría fallida dejaba la fila guardada y sin rastro.
   Corregido con `flush`, de modo que van en la misma transacción.
4. **El 403 de transcripciones desactivadas se tragaba**: es un problema de
   todo el tenant, no de una reunión, y se propagaba.
5. **`verificar_stack.sh` con la lista de tareas desactualizada**: la captura no
   estaba en el contrato. Actualizado.
6. **Corrupción textual preexistente en `auditoria.py`**: «la cadena whose hash
   es la huella» → «la cadena cuyo hash es la huella».
7. **Una violación de clave foránea se confundía con «ya existía»**. Lo
   encontró la **prueba funcional**, no las unitarias: al capturar contra
   una organización que ya no estaba en la base de datos, la inserción fallaba
   con violación de FK, el `except IntegrityError` la devolvía como «ya
   capturada», y la captura se reportaba como duplicada **sin haber guardado
   nada**. Corregido distinguiendo el SQLSTATE (`23505` unicidad = duplicado
   legítimo; `23503` clave foránea = error), validando la organización antes
   de insertar, y con una prueba de regresión.
8. **Orden de import en la CLI**: el import de la tarea —que construye la
   configuración— se ejecutaba antes de fijar `GRAPH_BASE_URL`, y por eso el
   doble de Graph no se usaba. El import va después de fijar el entorno.

> **Lección de este defecto:** un fallo que se disfraza de éxito es peor que
> un fallo, porque nadie lo ve. Las pruebas unitarias no lo detectaron porque
> la organización de la prueba siempre existía. Fue la prueba funcional, con
> datos de la vida real, la que lo sacó.

### NO VALIDADO

- Todo lo que depende de un tenant real de Microsoft 365: alta de la
  aplicación, consentimiento de administrador, emisión real del token, lectura
  real de reuniones, descargas reales.
- La **Application Access Policy** de Teams, sin la cual Graph devuelve 403
  aunque los permisos estén concedidos.
- La activación de la transcripción y de la **atribución de hablantes** en el
  tenant, que es una decisión del administrador.
- El comportamiento bajo la cuota real de Graph.

El procedimiento de cierre está en la guía de conexión con Microsoft 365, §6.

## 10 bis.9 Projecta Capture: MVP de captura en navegador (2026-10-23)

### Qué se ha implementado

El módulo **Projecta Capture**: una aplicación web que captura el audio de una
reunión en el navegador, lo transcribe en el servidor y extrae de la
transcripción los elementos de PMO (actas, acuerdos, decisiones, compromisos y
riesgos), para que una persona los revise y los apruebe.

- **14 entidades** nuevas (`app/core/db/models_captura.py`) con la migración
  `0005_captura`, aplicada **sobre PostgreSQL real**, con `downgrade`
  simétrico.
- **Dos máquinas de estados separadas** (`app/core/captura/estados.py`): la de
  captura, con 7 estados, y la de procesamiento, con 9. Transiciones cerradas.
  `STOPPED` y `CANCELLED` son terminales en la captura: una grabación
  terminada no se reanuda, porque hacerlo exigiría un artefacto nuevo y
  mezclar dos en uno es justo lo que no se quiere.
- **Capa de servicio** (`app/core/captura/servicio.py`): puerta de
  autorización, carga idempotente de fragmentos, finalización explícita,
  procesamiento idempotente, notas, revisión con historial y purga por
  retención.
- **Proveedores** (`app/core/captura/proveedores.py`): `ProveedorSimulado`
  determinista y `AdaptadorMotorReal` configurable por URL, que **falla
  antes de abrir ninguna conexión** si no la hay.
- **Extracción** (`app/core/captura/extraccion.py`): determinista, sin
  inventar responsables ni fechas, y tratando la transcripción como
  **contenido no confiable**.
- **Exportación** (`app/core/captura/exportacion.py`): Markdown, CSV y JSON,
  con saneamiento de fórmulas de hoja de cálculo.
- **API** (`app/api/routers/captura.py`): **20 rutas** bajo `/captura`, cada
  una con su comprobación de RBAC y de aislamiento entre organizaciones.
- **Tarea** `projecta.captura.procesa` en la cola `integraciones`
  (`app/workers/tasks/procesa_captura.py`).
- **Pantalla** (`app/templates/captura/nueva_sesion.html` y
  `app/static/js/captura.js`): permisos **solo** tras acción explícita, dos
  medidores de nivel separados, subida por fragmentos con reintentos, notas
  con marca de tiempo y liberación de pistas.

### Las tres decisiones que condicionan el diseño

1. **El modo solicitado no es el modo efectivo.** La sesión guarda los dos, y
   `captura_completa` solo es `True` cuando coinciden. Un acta que sale de una
   captura incompleta lo dice, en vez de fingir que es completa.
2. **La puerta de autorización está en `READY`, no en `RECORDING`.** El
   camino real es `DRAFT → READY → RECORDING`, y no se pasa a `READY` sin una
   declaración registrada.
3. **El cliente declara cuántos fragmentos esperaba.** El servidor no puede
   saber por sí solo que falta el último, porque la deduplicación por índice
   no distingue «nunca llegó» de «nunca se pidió».

### Verificación

```
tests/test_captura_aceptacion.py  →  20 passed
```

Las **20** comprobaciones del enunciado, con el número de la lista en el
nombre del método. Las que dependen del navegador se comprueban por partes:
el servidor impone su mitad de la regla, y la otra mitad la declara la
matriz de compatibilidad, que separa lo documentado de lo probado.

Matriz de compatibilidad de navegador y sistema operativo, con fuentes
citadas y cada celda marcada **COMPROBADO** o **NO COMPROBADO**:
`/workspace/output/captura/matriz-compatibilidad-captura.md` y `.html`.

### Pruebas de mutación (control negativo)

Cada mutación se aplica de verdad antes de medir, contando las ocurrencias
del literal, y se revierte después con `diff`.

| # | Mutación | Resultado |
|---|---|---|
| 1 | La puerta de autorización deja de comprobarse al pasar a `READY` | **9 failed** |
| 2 | Deja de compararse `fragmentos_esperados` con los recibidos | **2 failed** |
| 3 | La extracción rellena el responsable con un valor inventado | **3 failed** |
| 4 | Se revierten las tres | **20 passed**, ficheros idénticos |

### Defectos reales encontrados y corregidos en esta tanda

| Defecto | Causa | Corrección |
|---|---|---|
| `extraer()` fallaba con dataclasses | Se esperaba un diccionario | Acepta dataclass, `dict` y objetos con atributos |
| La puerta de autorización estaba en `RECORDING` | Se puso en el estado equivocado | La puerta va en `READY`; el camino es `DRAFT → READY → RECORDING` |
| El servidor no detectaba el fragmento final ausente | La deduplicación por índice no lo cubre | El cliente declara `fragmentos_esperados` y el servidor lo compara |
| La prueba 17 confundía la ruta de la interfaz con captura de imagen | `GET /captura/sesiones/{id}/pantalla` contiene «pantalla» | La comprobación busca `video\|imagen\|fotograma\|captura-imagen` |
| El script pedía la pantalla completa | `getDisplayMedia` sin `displaySurface` | Pide una **pestaña** (`displaySurface: 'browser'`), que es donde el audio funciona en todas las plataformas de Chromium |
| Tres literales corruptos en el README de entregables | Transporte | Corregidos y verificados; el barrido vuelve a pasar |
| La migración 0005 **no era reversible** | El `downgrade` borraba los índices a mano, y en PostgreSQL una `UniqueConstraint` crea un índice único del que la propia restricción depende: el `DROP INDEX` fallaba con `DependentObjectsStillExistError` y la migración se quedaba a medias | **No se borran ni índices ni restricciones a mano**: al borrar la tabla se van con ella. El arreglo intermedio con `op.drop_constraint` falló en SQLite (`NotImplementedError`). **Verificado en los dos motores**: sobre PostgreSQL real, `upgrade head → downgrade 0004 → upgrade head` deja las 14 tablas y vuelve a levantarlas; sobre SQLite, `test_la_migracion_es_simetrica` pasa |

### NO VALIDADO

- **Cualquier combinación de navegador y sistema operativo en el entorno del
  banco.** La matriz documenta lo que dice la documentación oficial, y su
  columna de prueba está **vacía a propósito**. Hasta que se ejecute el
  procedimiento manual de la §5 de la matriz, ninguna combinación se declara
  compatible.
- El **audio compartido en Firefox y en Safari**, que la matriz marca como NO
  COMPROBADO.
- El **motor de transcripción real**: solo el simulado está probado.
- La **reconstrucción del contenedor de audio** a partir de los fragmentos, y
  su reproducción.
- La **transcripción en vivo**, que no está implementada.
- La **diarización** con nombres reales: el MVP produce etiquetas y una
  persona valida.
- Todo lo relativo a un **tenant real de Microsoft**, que sigue siendo NO
  VALIDADO.
- El **cumplimiento normativo**. No se afirma en ningún documento, porque no
  hay evidencia para esa afirmación.

## 10 bis.10 Auditoría adversarial y verificador de anclas (2026-10-23)

### Qué se ha hecho

Una auditoría de los entregables **hecha por un agente verificador
independiente** del equipo que los escribió, que revisó los documentos
publicados buscando incoherencias numéricas, anclas rotas y texto
corrupto. Es un tipo de comprobación que el propio equipo no puede hacer
bien: quien escribió el documento ya sabe lo que quiso decir.

El resultado no fue tranquilizador, y esa es la razón de hacerlo.

### Los tres defectos reales del TO-BE v1.1

| # | Dónde | Qué decía | Qué dice | Por qué importa |
|---|---|---|---|---|
| 1 | Línea 14 | «Versión \| 1.0» | «Versión \| 1.1» | Un documento que se titula v1.1 no puede decir 1.0 en su cabecera. Heredado de la v1.0. |
| 2 | Línea 105 | `couldn't distinguir` | `no podía distinguir` | **Corrupción textual del transporte**, introducida al escribir la v1.1. En la v1.0 la palabra no aparece ni una vez. |
| 3 | Línea 324 | «nueve agentes de IA (HU_15 a HU_24)» | «(HU_17 a HU_24)» | Contradecía la **propia línea 86** del mismo documento. HU_15 es el router de modelos y HU_16 el motor RAG; ninguno de los dos es un agente. |

**La v1.0 no se toca.** Se conserva byte a byte como control negativo: su
valor está en que sus defectos siguen ahí, y es la única forma de
comprobar que los verificadores detectan lo que dicen detectar. Su MD5
sigue siendo `a9c66ef0ea3e8b4dc93a732701e8cae6`.

El rango de la **épica E2** (líneas 502 y 1564) se deja como estaba:
la épica sí abarca de HU_15 a HU_24, porque incluye el router y el motor
RAG. Cambiarlo habría sido cambiar lo que sí es cierto.

### El verificador de anclas internas

Faltaba un comprobador que revisara, **documento a documento**, que ningún
enlace del contenido lleve a un sitio que no existe. Ahora existe:
`herramientas/comprobar_anclas.py`.

| Medida | Valor |
|---|---|
| Documentos HTML analizados | 14 (más 1 plantilla, excluida) |
| Enlaces internos revisados | 222 |
| De ellos, construidos en JavaScript | 80 |
| Anclas rotas **no justificadas** | **0** |
| Anclas rotas justificadas por escrito | 3 |

Las tres justificadas, una por una:

* **Las dos del TO-BE v1.0** (`#7-mapa-de-épicas-e-historias`): la v1.0 no
  tiene la sección 7. Es uno de los 25 defectos que corrige la v1.1, y la
  línea base no se toca.
* **La del prototipo** (`#proyectos`): el prototipo tiene enrutador de hash.
  `init()` lee `location.hash` y lo contrasta con `VISTAS_MENU`, y
  «proyectos» está en esa lista. El enlace funciona; no necesita un `id`
  en el HTML.

**Control negativo**: el verificador rompe una ancla a propósito en memoria
y exige que el recuento aumente. Si la mutación no se aplica, avisa en
lugar de dar un resultado tranquilizador.

### La excepción del verificador de integridad textual

El detector de «palabra clave pegada a un identificador» avisaba de
`classList`, `className` y `returnValue`, que son **API estándar del
navegador** y no se pueden renombrar. Se ha añadido una excepción
**acotada**: exige el sufijo exacto y no admite otro carácter de
identificador detrás, de modo que `classListExtra` sigue siendo
sospechoso.

Y para que la excepción no sea una puerta abierta, el verificador tiene
**autoprueba propia** (`--autoprueba`, 10 casos): comprueba que la API del
DOM no se avisa, que un identificador parecido **sí** se avisa, y que una
corrupción real **sí** se avisa.

### Verificación

```
tobe/auditar_tobe.py (contra la v1.1)  →  51 superadas, 0 fallidas
tobe/validar_html.py                   →  20 superadas, 0 fallidas
tobe/prueba_negativa.py                →  los verificadores detectan los defectos
herramientas/comprobar_anclas.py       →  0 anclas rotas no justificadas, control negativo en verde
scripts/verificar_integridad_textual.py --autoprueba  →  10 casos, 0 problemas
md5sum del TO-BE v1.0                  →  a9c66ef0ea3e8b4dc93a732701e8cae6  (sin cambios)
```

### NO VALIDADO

- Los **enlaces externos** de los documentos. Requieren acceso a Internet
  desde el entorno de validación, y no se han comprobado.
- Que la auditoría adversarial **no haya dejado ningún defecto más**. Ha
  dejado los que se han corregido, y puede haber dejado otros que solo
  aparezcan al releer con otra cabeza.
- Que el TO-BE v1.1 no tenga **más incoherencias numéricas**. La auditoría
  revisó las que encontró; no hay garantía formal de que no haya ninguna
  más.

## 10 bis.11 Auditoría adversarial del código y correcciones (2026-10-23)

### Qué se ha hecho

Una auditoría del código hecha por un **agente verificador independiente**
del equipo que lo escribió. Su veredicto fue **FAIL**: 5 defectos
BLOQUEANTES y 8 IMPORTANTES. El informe completo está en
`/workspace/output/captura/auditoria-enlaces-coherencia.md`.

**Por qué una auditoría hecha por otro tiene valor.** Quien escribió el
código lo lee con la vista de lo que quiso escribir. El verificador no
tiene esa vista, y por eso encontró cosas que el autor no iba a ver
nunca por sí solo.

### Los cinco bloqueantes

| # | Dónde | Qué pasaba | Por qué importa |
|---|---|---|---|
| BUG-1 | `captura_teams.py` | El fallo de la auditoría se tragaba, y como la llamada ocurre antes del `commit`, la fila quedaba guardada **sin rastro** | Un fallo de auditoría se convertía en un éxito silencioso |
| BUG-2 | Todo `app/core/captura/` | **Cero** referencias a `app.core.auditoria` en veinte rutas | La release note V1.041 afirmaba lo contrario, en dos sitios |
| BUG-3 | `graph_teams.py` | `ErrorDeTranscripcionDeshabilitada` hereda de `ErrorDeGraph`, y tres `except ErrorDeGraph:` se la tragaban | Una captura devolvía **éxito con la transcripción vacía** |
| BUG-4 | `reuniones.py` | `proyecto_id` no se validaba contra la organización del actor | Una reunión de la organización A podía colgar de un proyecto de la B, y **no la veía nadie** |
| BUG-5 | `graph_teams.py` | El cliente HTTP no se cerraba nunca | Fuga de descriptores en un worker de larga vida |

### Los ocho importantes

`graph_teams.py`: el cliente HTTP nunca se cerraba · `auditoria.py`:
`REUNION_NO_CAPTURABLE` declarado y nunca emitido · `reuniones.py`: el
`LIMIT` antes del filtro de alcance y sin paginación · `estados.py`:
`ANALYZING → APPROVED` salta la revisión humana · `captura_teams.py`: un
error de configuración salía como tarea **SUCCESS** · TO-BE v1.0: una
valla sin cerrar y la sección 7 ausente (no se corrigen: es el control
negativo) · guía de onboarding: `#top` sin ancla en el Markdown ·
prototipo: el enlace de la marca no hacía nada.

### Lo que NO se ha corregido, y por qué

El TO-BE **v1.0** se conserva byte a byte. Sus defectos están ahí a
propósito: son la única forma de comprobar que los verificadores
detectan lo que dicen detectar. Ya se documentó en la nota 042.

### La regla que se ha seguido

**Una corrección sin prueba de regresión es un tinkering, no una
reparación.** Los trece defectos llevan su prueba, y las pruebas se
comprueban **revirtiendo cada corrección**: si al revertir una
corrección la prueba sigue pasando, la prueba no prueba nada.

`herramientas/control_negativo_regresion.sh` hace esa comprobación con
**ocho mutaciones**, y en cada una cuenta las ocurrencias del literal
antes de medir, porque una mutación que no se aplica no prueba nada.

| Mutación | Prueba que debe fallar | Resultado |
|---|---|---|
| La sesión deja de dejar rastro | `test_01_la_captura_escribe_en_la_cadena_de_auditoria` | **1 failed** |
| El proyecto de otra organización deja de validarse | `test_07_un_proyecto_de_otra_organizacion_se_rechaza` | **1 failed** |
| El 403 vuelve a tragarse | `test_05_los_tres_sitios_propagan_el_403` | **1 failed** |
| El cliente deja de cerrarse | `TestCierreDelClienteHttp` | **1 failed** |
| El error de configuración sale como éxito | `test_12_el_error_de_configuracion_no_es_un_exito` | **1 failed** |
| El evento deja de emitirse | `test_11_reunion_no_capturable_esta_y_se_usa` | **1 failed** |
| El id del evento vuelve a ser el de la entidad | `test_03_dos_eventos_sobre_la_misma_entidad_no_chocan` | **1 failed** |
| El fallo de auditoría se traga | `test_04b_si_la_auditoria_falla_no_queda_la_fila` | **1 failed** |

### Verificación

```
tests/test_correcciones_auditoria.py  →  14 passed
control_negativo_regresion.sh         →  las ocho mutaciones fallan
```

### Dos cosas que la auditoría no vio, y que se encontraron al corregir

1. **La guarda de la revisión humana se aplicó dos veces.** El ancla del
   script de corrección era prefijo del texto nuevo, así que la segunda
   ejecución volvió a aplicarla. Se detectó porque la comprobación
   estática de referencias sin resolver señaló la guarda duplicada.
2. **La primera corrección de la migración rompía SQLite.** El arreglo
   de `DependentObjectsStillExistError` con `op.drop_constraint` es
   correcto en PostgreSQL y lanza `NotImplementedError` en SQLite. La
   solución final —no borrar índices ni restricciones a mano— es más
   simple que el primer intento y funciona en los dos motores.

Ambas cosas dicen lo mismo: **un arreglo necesita comprobarse en el
entorno donde se va a usar, no solo en el que se ha escrito.**

### NO VALIDADO

- Que la auditoría **no haya dejado ningún defecto más**. Ha dejado los
  que se han corregido, y puede haber dejado otros que solo aparezcan al
  releer con otra cabeza.
- Los **enlaces externos** de los documentos.
- El comportamiento de la interfaz de captura en un navegador real del
  banco: sigue siendo NO VALIDADO.

## 10 bis.12 Entorno Railway y republicación del prototipo (2026-10-23)

### Qué se ha hecho

Se ha adaptado el despliegue al entorno **Railway**, que es el entorno
de despliegue inicial del sistema, y se ha republicado el prototipo con
el enlace de la marca corregido.

El cambio es de **entorno**, no de producto. El esquema de la base de
datos, las migraciones, el código, el contrato de la API y el contrato
de disponibilidad **no cambian**.

### Las seis diferencias de configuración

Railway entrega las variables con otros nombres y otros formatos. Las
seis diferencias, y cómo se resuelven:

| # | Lo que entrega Railway | Lo que espera la aplicación | Resolución |
|---|--------------------------|-----------------------------|------------|
| 1 | `DATABASE_URL` con driver síncrono (`postgresql://`) | driver asíncrono | `TRADUCCION_ESQUEMAS_INVERSAS` normaliza al asíncrono equivalente |
| 2 | Un **único** Redis | broker y backend de resultados aparte | `_deriva_celery_de_redis` los deriva de `REDIS_URL` con las bases 1 y 2 |
| 3 | `REDIS_URL` sin base de datos explícita | URL con base | `_redis_con_base` la añade |
| 4 | El Almacenamiento de objetos con otras variables | `MINIO_*` | referencias de Railway `${{minio.MINIO_ROOT_USER}}` |
| 5 | Un único `SECRET_KEY` para tres servicios | una clave por entorno | `shared.SECRET_KEY`, sellada |
| 6 | El puerto lo asigna Railway | un puerto fijo | `$PORT` en el `startCommand` |

**Ninguna validación se relajó.** Un esquema de base de datos
desconocido sigue siendo un error de arranque. Lo que se relajó es
*cuál* esquema se admite, y solo cuando la traducción es unívoca.

### La preparación previa al despliegue

`scripts/railway_preparar.py` es el `preDeployCommand` de los tres
servicios de aplicación. Hace cinco cosas, en este orden:

1. **Cerrojo consultivo** (`pg_advisory_lock`). Los tres pre-deploy se
   ejecutan **a la vez**, y las migraciones no son idempotentes entre
   sí. Sin el cerrojo, dos servicios aplicarían la misma migración a la
   vez.
2. Habilita la extensión `vector`.
3. Aplica las migraciones con Alembic.
4. Siembra el catálogo de roles, sin el cual no se puede autorizar nada.
5. Prepara el bucket de objetos y comprueba base de datos y Redis.

**Ejecutado contra PostgreSQL y Redis reales**: 27 tablas, pgvector
0.6.2, los seis roles sembrados.

El script **puede fallar**, y se comprobó: con el almacenamiento de
objetos caído, `--verificar` sale con **exit 2**.

### La topología

Seis servicios: `api` (el único con dominio público), `worker`, `beat`,
`postgres`, `redis` y `minio`. La red privada de Railway es WireGuard
y está aislada por entorno (`*.railway.internal`).

**Solo la API tiene dominio público.** El navegador del operador no
alcanza la red privada, así que los datos, el worker y el beat quedan
fuera del alcance de Internet.

### El cambio de perímetro, declarado

La política de seguridad del banco era «sin aplicación cliente desplegada
ni puertos entrantes». **Railway rompe esa premisa**, y no se puede
esconder:

- Los datos residen en la infraestructura de Railway, un tercero.
- La API queda expuesta a Internet.
- Las salidas a sistemas del cliente salen desde Railway, no desde el
  perímetro del banco.

Eso es una **decisión de protección de datos y de exposición**, no un
detalle técnico, y está marcada como 🔴 en el documento de huecos
(`output/railway/lo-que-falta-railway.md`, §5).

Lo que **no** cambia: el Edge Connector sigue siendo la **Variante A**
—un módulo de la aplicación, sin proceso, sin puerto y sin credencial
propios— y la dinámica de control es idéntica: la IA propone, la
persona valida, y el sistema deja rastro de las dos cosas.

### Las cinco decisiones que bloquean el piloto

1. **Residencia de los datos**: ¿se autoriza que los datos de la
   organización residan en Railway?
2. **Quién alcanza la API pública**: ¿se expone solo a la red del banco
   (VPN), o también a Internet?
3. **Motor de transcripción y ubicación**: ¿dónde se transcribe, y sale
   el audio del perímetro?
4. **Entorno objetivo de la interfaz de captura**: ¿qué navegador, con
   qué políticas corporativas?
5. **Circuito de aprobación de las escrituras externas**: ¿quién
   aprueba una escritura en Jira, Azure DevOps o MS Project?

Ninguna se ha tomado, y **ninguna se puede tomar desde aquí**.

### Verificación

```bash
cd /workspace/repo
/workspace/.venv/bin/python -m pytest tests/test_config_railway.py -q   # 5 passed
/workspace/.venv/bin/python -m pytest -q                                # 235 passed
/workspace/.venv/bin/python -m scripts.railway_preparar                 # exit 0
/workspace/.venv/bin/python -m scripts.railway_preparar --verificar      # exit 0 / 2
bash -n scripts/railway_desplegar.sh                                    # sintaxis ok
```

Prototipo: 71 + 75 + 59 pruebas, 0 peticiones externas en el
documento, 4 reglas `@font-face` incrustadas. El enlace de la marca
tiene prueba de regresión **verificada por mutación**: sin el
escuchador de `hashchange`, fallan 2 de las 75.

### NO VALIDADO

- El **despliegue real en Railway**: no hay cuenta de Railway en este
  entorno. `railway up` no se ha ejecutado. La URL publicada del
  prototipo es de la plataforma de alojamiento de entregables, no de
  Railway.
- Que el plan de PostgreSQL de Railway traiga la **biblioteca `vector`**.
  El script de preparación falla con un mensaje explícito si no la trae.
- El **dominio público, el TLS y el sellado de secretos** de Railway.
- La **copia de seguridad** de PostgreSQL, el límite de peticiones en
  el borde y la política de entornos: son decisiones de operación.
- La **residencia de los datos** en Railway y su encaje con la
  política de protección de datos del banco.
- Todo lo relativo a un **tenant real de Microsoft 365**.
- El comportamiento de la interfaz de captura en un **navegador real
  del banco**.

## 11. Cómo reproducir esta validación

```bash
cd /workspace/repo
bash scripts/verificar_stack.sh
echo "código de salida: $?"
```

La PoC del Edge Connector, contra el stack real:

```bash
source /tmp/projecta_env.sh
cd /workspace/repo
/workspace/.venv/bin/python -m alembic upgrade head
/workspace/.venv/bin/python scripts/demo_edge_conector.py
/workspace/.venv/bin/python -m pytest tests/test_edge.py -v
```

Y la auditoría exhaustiva de todo el proyecto —código y entregables—:

```bash
cd /workspace/repo
make verify-completo
# o, directamente:
/workspace/.venv/bin/python /workspace/output/herramientas/validar_proyecto_completo.py
echo "código de salida: $?"
```

El script es autónomo: levanta la API, levanta el worker de Celery, ejecuta las migraciones,
pasa la suite de pruebas y limpia todo al terminar. Es reproducible por cualquier persona con una
sola orden.
