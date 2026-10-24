# Entregable — Capa asíncrona Celery de **Projecta PMO Intelligence**

**Proyecto:** Projecta PMO Intelligence
**Bajada:** La capa inteligente para gobernar proyectos, portafolios y decisiones.
**Tarea:** `celery-core` — instancia Celery, colas, tareas y planificador.
**Contrato de referencia:** `docs/skeleton/design.md` — sección **d) Contrato de Celery**.

---

## 1. Archivos escritos

| Archivo | Qué hace |
|---|---|
| `app/core/celery_app.py` | Instancia `Celery("projecta", broker=…, backend=…)`, las 6 colas, los enrutamientos, los parámetros de robustez y el `beat_schedule`. |
| `app/workers/tasks/smoke.py` | Tarea `projecta.smoke` (cola `ia`): devuelve `status`, `app`, `timestamp` y `echo`. |
| `app/workers/tasks/maintenance.py` | Tarea `projecta.mantenimiento.ping` (cola `mantenimiento`): comprueba la conectividad con Redis y devuelve el resultado. |

**No se ha creado ni modificado ningún `__init__.py`, ni `app/main.py`, ni `app/core/config.py`,
ni nada bajo `app/api/`, ni `docker-compose.yml` o `Dockerfile`.** La configuración se consume
desde `app.core.config.settings` sin tocar ese archivo.

---

## 2. Contrato cumprido

### 2.1 Instancia y robustez

```python
celery_app = Celery(
    "projecta",
    broker=BROKER_URL,      # settings.celery_broker_url > CELERY_BROKER_URL > redis://localhost:6379/1
    backend=BACKEND_URL,    # settings.celery_result_backend > CELERY_RESULT_BACKEND > redis://localhost:6379/2
    include=["app.workers.tasks.smoke", "app.workers.tasks.maintenance"],
)
```

| Parámetro | Valor exigido | Valor implementado |
|---|---|---|
| `task_acks_late` | `True` | `True` |
| `task_reject_on_worker_lost` | `True` | `True` |
| `worker_prefetch_multiplier` | `1` | `1` |
| `task_soft_time_limit` | `600` | `600` |
| `task_time_limit` | `900` | `900` |
| `broker_connection_retry_on_startup` | `True` | `True` |
| `task_default_queue` | explícito | `ia` |

### 2.2 Las 6 colas

`ingesta`, `documentos`, `ia`, `integraciones`, `reportes`, `mantenimiento`.

Se declaran de forma explícita en `task_queues` (objetos `kombu.Queue` sobre un exchange directo
`projecta`) **y** en `task_routes`, que además cubre con patrones glob (`projecta.ingesta.*`,
`projecta.documentos.*`, `projecta.ia.*`, `projecta.integraciones.*`, `projecta.reportes.*`,
`projecta.mantenimiento.*`) los carriles futuros. Las claves exactas de `task_routes`
(`projecta.smoke`, `projecta.mantenimiento.ping`) tienen prioridad sobre los patrones.

### 2.3 Tareas registradas

| Nombre | Cola | Efecto |
|---|---|---|
| `projecta.smoke` | `ia` | Ninguno. Idempotente y sin efectos secundarios. |
| `projecta.mantenimiento.ping` | `mantenimiento` | Ninguno. Solo comprueba Redis. |

### 2.4 Planificador

```python
"mantenimiento-ping-cada-5-minutos": {
    "task": "projecta.mantenimiento.ping",
    "schedule": crontab(minute="*/5"),
    "options": {"queue": "mantenimiento"},
}
```

---

## 3. Decisiones de diseño

1. **`task_default_queue = "ia"`.** El contrato exige que sea explícito pero no fija el valor.
   `ia` es el carril de procesamiento general (clasificación, extracción de entidades, generación
   de artefactos), así que es el destino natural de cualquier tarea que no declare el suyo.
2. **Autocarga de los módulos de tareas al importar la app.** `include` está declarado como exige
   el contrato, pero Celery solo importa esos módulos al arrancar `celery worker` o `celery beat`.
   Para que «las tareas se autocomprueben al importar la app» sea cierto de verdad, el módulo
   termina con `celery_app.loader.import_default_modules()`. Así, `celery_app.tasks` ya contiene
   `projecta.smoke` y `projecta.mantenimiento.ping` nada más importar `app.core.celery_app`.
3. **Importación tolerante de `app.core.config`.** Si la configuración tipada no existe todavía o
   falla al validarse (faltan `SECRET_KEY`, `DATABASE_URL`, `MINIO_*`), el módulo **no** rompe:
   avisa por log y resuelve las URLs con la precedencia *settings → variable de entorno → valor por
   defecto de desarrollo*. Así, el worker y el beat siguen siendo importables en cualquier entorno
   y el fallo de configuración sigue siendo visible en el log.
4. **Precedencia de las URLs.** El contrato fija el fallback de desarrollo en
   `redis://localhost:6379/1` (broker) y `redis://localhost:6379/2` (backend); la tarea de
   implementación mencionaba `/0` y `/1`. **Gana el contrato**: se implementan `/1` y `/2`. El
   subfijo `/0` se reserva para el Redis de aplicación (`REDIS_URL`), que es el que comprueba
   `projecta.mantenimiento.ping`.
5. **`projecta.mantenimiento.ping` nunca propaga la excepción.** Si Redis no responde, la tarea
   devuelve `status: "error"` con el detalle del fallo, en lugar de reintentar o tumbar el worker.
6. **Alias en `snake_case`.** Cada tarea expone además un alias del nombre completo
   (`projecta_smoke`, `projecta_mantenimiento_ping`) para poder invocarla desde Python sin pasar
   por el registro de Celery.
7. **`broker_transport_options["visibility_timeout"] = 1800`.** Con `acks_late` y Redis, el
   tiempo de invisibilidad debe superar el `task_time_limit` (900 s) o se podrían duplicar tareas.
8. **Nombre visible siempre completo.** El campo `app` de ambas tareas devuelve
   `settings.app_name` («Projecta PMO Intelligence»), nunca «Projecta» a secas.

---

## 4. Verificaciones ejecutadas

Ejecutadas con `/workspace/.venv/bin/python` desde `/workspace/repo`.

### 4.1 V1 — Importar y configurar la app Celery (6 colas + beat schedule)

```console
$ /workspace/.venv/bin/python -c "from app.core.celery_app import celery_app; print(celery_app.conf.task_routes); print(celery_app.conf.beat_schedule)"
{'projecta.smoke': {'queue': 'ia'}, 'projecta.mantenimiento.ping': {'queue': 'mantenimiento'}, 'projecta.ingesta.*': {'queue': 'ingesta'}, 'projecta.documentos.*': {'queue': 'documentos'}, 'projecta.ia.*': {'queue': 'ia'}, 'projecta.integraciones.*': {'queue': 'integraciones'}, 'projecta.reportes.*': {'queue': 'reportes'}, 'projecta.mantenimiento.*': {'queue': 'mantenimiento'}}
{'mantenimiento-ping-cada-5-minutos': {'task': 'projecta.mantenimiento.ping', 'schedule': <crontab: */5 * * * * (m/h/dM/MY/d)>, 'options': {'queue': 'mantenimiento'}}}
[exit code: 0]
```

Las 6 colas aparecen en `task_routes` y el `beat_schedule` apunta a
`projecta.mantenimiento.ping` con `crontab(minute="*/5")`.

> Nota: en este entorno, sin `SECRET_KEY`/`DATABASE_URL`/`MINIO_*` exportadas, `app.core.config`
> falla al validarse y el módulo lo avisa por log (`No se pudo cargar 'app.core.config' …`) antes de
> resolver las URLs por entorno o por defecto. El import **no** falla: `exit code: 0`.

### 4.2 V2 — Ejecutar una tarea en modo `eager` (sin broker)

```console
$ /workspace/.venv/bin/python -c "from app.core.celery_app import celery_app
celery_app.conf.task_always_eager = True
r = celery_app.tasks['projecta.smoke'].delay()
print('resultado:', r.get())
r2 = celery_app.tasks['projecta.smoke'].delay('hola-mundo')
print('resultado con echo:', r2.get())"
resultado: {'status': 'ok', 'app': 'Projecta PMO Intelligence', 'timestamp': '2026-10-22T19:05:58.918055+00:00', 'echo': None}
resultado con echo: {'status': 'ok', 'app': 'Projecta PMO Intelligence', 'timestamp': '2026-10-22T19:05:58.945721+00:00', 'echo': 'hola-mundo'}
[exit code: 0]
```

El dict devuelto tiene exactamente las claves `status`, `app`, `timestamp` y `echo`.

### 4.3 V3 — El registro de tareas contiene exactamente los nombres del contrato

```console
$ /workspace/.venv/bin/python -c "from app.core.celery_app import celery_app
propias = sorted(n for n in celery_app.tasks if not n.startswith('celery.'))
print('tareas propias:', propias)
print('exactamente las del contrato:', propias == ['projecta.mantenimiento.ping', 'projecta.smoke'])
print('integradas de Celery:', sorted(n for n in celery_app.tasks if n.startswith('celery.')))"
tareas propias: ['projecta.mantenimiento.ping', 'projecta.smoke']
exactamente las del contrato: True
integradas de Celery: ['celery.accumulate', 'celery.backend_cleanup', 'celery.chain', 'celery.chord', 'celery.chord_unlock', 'celery.chunks', 'celery.group', 'celery.map', 'celery.starmap']
[exit code: 0]
```

Las 9 tareas `celery.*` son las integradas del propio Celery 5.4.0; las de **Projecta PMO
Intelligence** son exactamente las dos del contrato.

### 4.4 V4 — `worker_prefetch_multiplier` y `task_acks_late`

```console
$ /workspace/.venv/bin/python -c "from app.core.celery_app import celery_app
print('worker_prefetch_multiplier =', celery_app.conf.worker_prefetch_multiplier)
print('task_acks_late =', celery_app.conf.task_acks_late)
print('task_reject_on_worker_lost =', celery_app.conf.task_reject_on_worker_lost)
print('task_soft_time_limit =', celery_app.conf.task_soft_time_limit)
print('task_time_limit =', celery_app.conf.task_time_limit)
print('broker_connection_retry_on_startup =', celery_app.conf.broker_connection_retry_on_startup)
print('task_default_queue =', celery_app.conf.task_default_queue)
print('colas declaradas =', sorted(q.name for q in celery_app.conf.task_queues))"
worker_prefetch_multiplier = 1
task_acks_late = True
task_reject_on_worker_lost = True
task_soft_time_limit = 600
task_time_limit = 900
broker_connection_retry_on_startup = True
task_default_queue = ia
colas declaradas = ['documentos', 'ia', 'ingesta', 'integraciones', 'mantenimiento', 'reportes']
[exit code: 0]
```

### 4.5 Suite completa de aserciones

`verificar_celery.py` (en el workspace del plan) comprueba, además de lo anterior, que el
enrutador de Celery lleva cada tarea a su cola y que `projecta.smoke` es idempotente:

```console
[OK ] nombre de la app Celery == 'projecta' (real: 'projecta')
[OK ] task_routes cubre las 6 colas (real: ['documentos', 'ia', 'ingesta', 'integraciones', 'mantenimiento', 'reportes'])
[OK ] task_queues declara las 6 colas (real: ['documentos', 'ia', 'ingesta', 'integraciones', 'mantenimiento', 'reportes'])
[OK ] task_default_queue explícito y válido (real: 'ia')
[OK ] projecta.smoke se enruta a la cola 'ia'
[OK ] projecta.mantenimiento.ping se enruta a la cola 'mantenimiento'
[OK ] beat_schedule tiene al menos una entrada (real: 1)
[OK ] exactamente una entrada de beat apunta a projecta.mantenimiento.ping (real: 1)
[OK ] el planificador de 'mantenimiento-ping-cada-5-minutos' usa crontab(minute='*/5') (real: <crontab: */5 * * * * (m/h/dM/MY/d)>)
[OK ] worker_prefetch_multiplier == 1
[OK ] task_acks_late is True
[OK ] task_reject_on_worker_lost is True
[OK ] task_soft_time_limit == 600
[OK ] task_time_limit == 900
[OK ] broker_connection_retry_on_startup is True
[OK ] el registro contiene exactamente las 2 tareas del contrato (real: ['projecta.mantenimiento.ping', 'projecta.smoke'])
[OK ] ambas tareas están registradas por nombre completo
[OK ] el enrutador lleva projecta.smoke a la cola 'ia'
[OK ] el enrutador lleva projecta.mantenimiento.ping a la cola 'mantenimiento'
[OK ] projecta.smoke devuelve un dict
[OK ] projecta.smoke devuelve status/app/timestamp/echo (real: ['app', 'echo', 'status', 'timestamp'])
[OK ] projecta.smoke devuelve status == 'ok'
[OK ] projecta.smoke devuelve el nombre de marca
[OK ] projecta.smoke sin argumentos devuelve echo == None
[OK ] projecta.smoke devuelve timestamp
[OK ] projecta.smoke devuelve el echo recibido
[OK ] projecta.smoke es idempotente (mismo resultado sin efectos secundarios)
[OK ] projecta.mantenimiento.ping devuelve un dict
[OK ] projecta.mantenimiento.ping informa del estado de Redis
[OK ] projecta.mantenimiento.ping devuelve un status
==============================================================================
RESULTADO: TODO OK
==============================================================================
```

### 4.6 Prueba end-to-end con broker y backend Redis reales

Con el Redis real del sandbox (`127.0.0.1:6398`, base 1 = broker, base 2 = backend) levantado con
`/workspace/herramientas/servicios_reales.py start`:

```console
broker: redis://127.0.0.1:6398/1
backend: redis://127.0.0.1:6398/2

[1] Arrancando worker real (colas: ia, mantenimiento)...

[2] celery inspect ping:
->  celery@cloud-harness-new-699tc: OK
        pong

1 node online.

[3] Enviando projecta.smoke a la cola 'ia'...
resultado smoke: {'status': 'ok', 'app': 'Projecta PMO Intelligence', 'timestamp': '2026-10-22T19:06:14.679973+00:00', 'echo': 'hola-mundo-real'}

[4] Enviando projecta.mantenimiento.ping a la cola 'mantenimiento'...
resultado ping: {'status': 'ok', 'ok': True, 'app': 'Projecta PMO Intelligence', 'redis': {'ok': True, 'url': 'redis://127.0.0.1:6398/0', 'latencia_ms': 0.64, 'pong': True, 'error': None}, 'timestamp': '2026-10-22T19:06:14.687596+00:00'}

[5] Contenido de las colas en Redis:
  cola 'ia': 0 mensajes en espera
  cola 'mantenimiento': 0 mensajes en espera
  cola 'ingesta': 0 mensajes en espera
  cola 'documentos': 0 mensajes en espera
  cola 'integraciones': 0 mensajes en espera
  cola 'reportes': 0 mensajes en espera

[6] Últimas líneas del log del worker:
   -------------- [queues]
                  .> ia               exchange=projecta(direct) key=ia
                  .> mantenimiento    exchange=projecta(direct) key=mantenimiento

  [tasks]
    . projecta.mantenimiento.ping
    . projecta.smoke

  [2026-10-22 19:06:05,641: INFO/MainProcess] Connected to redis://127.0.0.1:6398/1
  [2026-10-22 19:06:05,654: INFO/MainProcess] celery@cloud-harness-new-699tc ready.
  [2026-10-22 19:06:14,678: INFO/MainProcess] Task projecta.smoke[293b4811-b0fc-436e-ae45-482210230669] received
  [2026-10-22 19:06:14,680: INFO/ForkPoolWorker-1] Task projecta.smoke[293b4811-b0fc-436e-ae45-482210230669] succeeded in 0.001083184999970399s: {'status': 'ok', 'app': 'Projecta PMO Intelligence', 'timestamp': '2026-10-22T19:06:14.679973+00:00', 'echo': 'hola-mundo-real'}
  [2026-10-22 19:06:14,685: INFO/MainProcess] Task projecta.mantenimiento.ping[d33ddf74-d6ab-4c4e-9a9e-c21fa76a1ddb] received
  [2026-10-22 19:06:14,688: INFO/ForkPoolWorker-1] Task projecta.mantenimiento.ping[d33ddf74-d6ab-4c4e-9a9e-c21fa76a1ddb] succeeded in 0.0017383529999506209s: {'status': 'ok', 'ok': True, 'app': 'Projecta PMO Intelligence', 'redis': {'ok': True, 'url': 'redis://127.0.0.1:6398/0', 'latencia_ms': 0.64, 'pong': True, 'error': None}, 'timestamp': '2026-10-22T19:06:14.687596+00:00'}

  worker: Warm shutdown (MainProcess)

======================================================================
worker respondiendo a inspect ping : True
projecta.smoke ejecutada en 'ia'    : True
projecta.mantenimiento.ping con Redis vivo: True
======================================================================
```

El `celery inspect ping` del healthcheck de `docker-compose` (`celery -A
app.core.celery_app:celery_app inspect ping`) responde `pong` correctamente.

### 4.7 El planificador dispara la tarea cada 5 minutos

`celery -A app.core.celery_app:celery_app beat` en marcha real:

```console
[2026-10-22 19:04:25,688: INFO/MainProcess] beat: Starting...
[2026-10-22 19:05:00,006: INFO/MainProcess] Scheduler: Sending due task mantenimiento-ping-cada-5-minutos (projecta.mantenimiento.ping)
```

El mensaje se encoló en la cola `mantenimiento` de Redis (1 mensaje pendiente, sin worker
consumiendo en la prueba).

---

## 5. Notas para el verificador

- **Importación sin configuración:** `from app.core.celery_app import celery_app` funciona incluso
  si `app.core.config` no existe o falla al validarse; en ese caso se emite un `WARNING` en el log
  y se resuelven las URLs por variable de entorno o por los valores por defecto de desarrollo.
  El módulo nunca deja la app sin registrar sus tareas.
- **Valores por defecto:** broker `redis://localhost:6379/1`, backend `redis://localhost:6379/2`
  (los del contrato), Redis de aplicación `redis://localhost:6379/0`.
- **Sin efectos secundarios:** ninguna de las dos tareas escribe en la base de datos, publica
  mensajes ni llama a servicios externos. `projecta.smoke` es idempotente; el ping de
  mantenimiento solo hace un `PING` a Redis con timeouts de 2 s.
- **Integración con `docker-compose`:** el servicio `worker` debe consumir las colas con
  `-Q ia,mantenimiento` (o todas). Con la configuración actual, un worker que arranque sin `-Q`
  consume solo la cola por defecto `ia`.
- **Pendiente de otras pistas:** `docker-compose.yml` (infra-compose) debe apuntar el servicio
  `worker` a `app.core.celery_app:celery_app` y `beat` al mismo módulo; `requirements.txt`
  (infra-compose) necesita `celery[redis]` y `redis`.
- **Ficheros de apoyo de la verificación** (fuera del repo, en el workspace del plan):
  `verificar_celery.py`, `e2e_celery.py`, `transcripcion.py`.
