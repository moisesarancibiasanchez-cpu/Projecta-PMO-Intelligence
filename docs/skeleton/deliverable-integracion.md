# Entregable — Integración y validación del esqueleto

**Capa:** `integracion`
**Alcance:** `tests/*`, `scripts/*`, `docs/skeleton/VALIDACION.md`, `docs/skeleton/RUNBOOK.md`,
`README.md`.

---

## 1. Archivos escritos

| Archivo | Líneas | Qué hace |
|---|---:|---|
| `tests/conftest.py` | 106 | Aislamiento: variables de entorno fijadas **antes** de importar `app.*`, base SQLite en memoria por prueba, Celery en modo *eager* sin broker. |
| `tests/test_smoke_stack.py` | 330 | 16 pruebas (20 casos contando las parametrizadas) que cubren importación, rutas, validación de variables, plantillas, escapado, marca, shape de `/ready`, Celery y round-trip de migraciones. |
| `scripts/verificar_stack.sh` | 540 | Verificación integral en 7 secciones, código de salida 0 si todo pasa. |
| `scripts/levantar_stack.sh` | 128 | Arranque nativo en primer plano o en segundo plano, con `--stop`. |
| `docs/skeleton/VALIDACION.md` | 348 | Informe con la salida literal de cada comando. |
| `docs/skeleton/RUNBOOK.md` | 262 | Operación, diagnóstico, respaldo y restauración. |
| `README.md` | 205 | Entrada al repositorio. |

---

## 2. Decisión de fondo: validar contra lo real, no contra una imitación

La primera versión del plan de integración preveía validar con SQLite y el modo *eager* de Celery.
Al revisar el entorno se comprobó que **había servicios reales disponibles**: un PostgreSQL con
pgvector 0.6.2 y un Redis 7. Cambiar la estrategia de validación no fue un detalle: decidiría qué
se demuestra y qué no.

| | Con servicios reales | Con SQLite + modo eager |
|---|---|---|
| Driver de producción (`asyncpg`) | Probado | No probado |
| Extensión `vector` | Probada | No probada |
| El worker consume del broker | Probado | No probado (se ejecuta en el proceso) |
| El resultado vuelve por el backend | Probado | No probado |
| Aislamiento de las pruebas | Cada prueba aislada | Cada prueba aislada |

La suite de pruebas **sí** usa SQLite en memoria y Celery en modo *eager*: tiene que ser rápida y
aislada para poder correrla mil veces. La **validación de arranque** usa los servicios reales. Son
dos cosas distintas, y cada una con su alcance.

---

## 3. Resultado de la validación

```
== Resumen ==
  Comprobaciones: 20 | Fallos: 0
  NO VALIDADO en este entorno (no existe el binario `docker`):
    · la construcción real de la imagen
    · el arranque real de los 6 contenedores
    · la imagen pgvector/pgvector:pg16 del registro
RESULTADO: todas las comprobaciones pasan
```

**Código de salida de `scripts/verificar_stack.sh`: `0`.**

Detalle completo, con la salida literal de cada comando, en
[`docs/skeleton/VALIDACION.md`](VALIDACION.md).

---

## 4. Lo validado, en una línea cada cosa

| Verificación | Resultado |
|---|---|
| Análisis estático del compose (6 servicios, `depends_on`, healthchecks, variables) | PASA |
| API nativa: `/health`, `/ready`, `/`, `/docs` en 200 | PASA |
| Shape de `/ready` conforme al contrato | PASA |
| Caso degradado: `/ready` en 503 con la BD caída, `/health` sigue en 200 | PASA |
| Celery: 6 colas exactas, 2 tareas exactas, robustez, beat cada 5 min | PASA |
| Worker real consumiendo una tarea encolada con `.delay()` | PASA |
| Migraciones contra PostgreSQL real con pgvector, round-trip simétrico | PASA |
| 20 pruebas automatizadas | PASA |

---

## 5. Defectos encontrados y corregidos

Siete. Todos aparecieron **ejecutando** el stack, no leyéndolo, y todos están revalidados. La tabla
completa está en `VALIDACION.md`, sección 10. Los dos que más importan:

1. **`alembic upgrade head` fallaba siempre.** `alembic.ini` declaraba
   `sqlalchemy.connect_args = {}`; ConfigParser lo lee como la cadena `"{}"` y
   `create_async_engine` hace `dict.update("{}")` → `ValueError`. La opción se comenta en
   `alembic.ini`.

2. **Alembic no funcionaba con la URL de SQLite.** `env.py` pasaba la URL síncrona a
   `async_engine_from_config`: funciona por casualidad con `psycopg` (que tiene modo asíncrono) y
   revienta con `pysqlite`. Ahora usa la URL asíncrona.

Los otros cinco: la migración no portable (`sa.text("now()")` → `sa.func.now()`), el `.env` con
credenciales sin `.gitignore`, la ambigüedad del contrato sobre el estado global de `/ready`, el
colgamiento del script de verificación por nodos Celery duplicados, y el aborto del script en el
primer fallo.

---

## 6. Lo NO validado

Tres puntos, y solo por una causa comprobable: **este entorno no tiene el binario `docker`**.

1. La construcción real de la imagen.
2. El arranque real de los 6 contenedores.
3. La imagen `pgvector/pgvector:pg16` del registro.

Matiz que no conviene perder: la extensión `vector` **sí está validada** (PostgreSQL real con
`vector` 0.6.2). Lo que no se ha probado es el empaquetado de la imagen, no el comportamiento de
pgvector.

---

## 7. Reproducibilidad

```bash
cd /workspace/repo
bash scripts/verificar_stack.sh
echo "código de salida: $?"
```

Una sola orden, sin dependencias de estado entre ejecuciones: el script levanta la API, levanta el
worker, ejecuta las migraciones, pasa la suite y limpia todo al terminar.
