# Entregable — Capa de infraestructura

**Capa:** `infra-compose`
**Alcance:** `docker-compose.yml`, `Dockerfile`, `.env.example`, `.dockerignore`, `Makefile`,
`requirements.txt`, `deploy/README.md`.

---

## 1. Archivos escritos

| Archivo | Líneas | Qué hace |
|---|---:|---|
| `docker-compose.yml` | 309 | 6 servicios (`api`, `worker`, `beat`, `postgres`, `redis`, `minio`) más `minio-init` en perfil opcional. `depends_on` con `condition: service_healthy`, healthcheck en cada servicio, volúmenes nombrados, perfil `minio`. |
| `Dockerfile` | 109 | Multi-stage, usuario no-root, `HEALTHCHECK` propio, librerías de sistema de WeasyPrint (`libpango`, `libpangoft2`, `libcairo2`), Tesseract (`tesseract-ocr`, `tesseract-ocr-spa`) y `libxml2-dev` para lxml. |
| `.env.example` | 75 | Las 16 variables del contrato con valores de ejemplo NO secretos (`cambiar-en-produccion`). |
| `.dockerignore` | 55 | Excluye `.env`, `.git`, `__pycache__`, `.venv`. |
| `Makefile` | 122 | `up`, `down`, `logs`, `migrate`, `seed`, `backup`, `test`, `lint`, `shell`. Comandos reales, ninguno es un stub con `echo`. |
| `requirements.txt` | 40 | Las 13 versiones fijadas en el TO-BE. |
| `deploy/README.md` | 261 | Despliegue de piloto (perfil mínimo) y corporativo. |

---

## 2. Validación ejecutada

El agente de desarrollo dejó un validador propio de 59 comprobaciones estáticas. Resultado:

```
  OK   10b. Ningún target del Makefile es un stub (solo echo)
  OK   10c. El Makefile declara .PHONY
  OK   11. requirements.txt fija las 13 versiones del TO-BE
  OK   12. .dockerignore excluye .env, .git y __pycache__
==============================================================================
Comprobaciones superadas: 59 | Fallidas: 0
EXIT=0
--- conteo de líneas ---
  309 docker-compose.yml
  109 Dockerfile
   75 .env.example
   55 .dockerignore
  122 Makefile
   40 requirements.txt
  261 deploy/README.md
  971 total
```

El líder de proyecto revalidó el compose de forma independiente, con PyYAML, dentro de
`scripts/verificar_stack.sh`:

```
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

---

## 3. Cumplimiento del contrato

| Requisito | Cumple | Evidencia |
|---|:---:|---|
| 6 servicios con los nombres exactos | Sí | `api`, `worker`, `beat`, `postgres`, `redis`, `minio` |
| `postgres` con imagen pgvector | Sí | `pgvector/pgvector:pg16` |
| `redis` con `redis:7-alpine` | Sí | — |
| `minio` con bucket inicializado por `minio-init` | Sí | Perfil `minio` |
| `depends_on` con `condition: service_healthy` | Sí | Verificado en el análisis estático |
| Healthcheck con las 4 claves en cada servicio | Sí | Verificado en el análisis estático |
| Volúmenes nombrados | Sí | `pgdata`, `redisdata`, `miniodata` |
| Puertos 8000 / 5432 / 6379 / 9000 / 9001 | Sí | Verificado |
| Perfil `minio` con el piloto en 5 servicios | Sí | — |
| Dockerfile multi-stage, no-root, HEALTHCHECK, librerías de sistema | Sí | `libpango`, `libpangoft2`, `libcairo2`, `tesseract-ocr`, `tesseract-ocr-spa`, `libxml2-dev` |
| `requirements.txt` con las 13 versiones del TO-BE | Sí | Verificado |
| `.env.example` sin secretos | Sí | Placeholders `cambiar-en-produccion` |
| Makefile sin targets stub | Sí | Verificado |
| `SECRET_KEY` sin valor por defecto | Sí | Sin default en todo el repositorio |
| `BOOTSTRAP_ADMIN_PASSWORD` ausente | Sí | No aparece |

---

## 4. Desviaciones y notas

1. **`.gitignore` añadido por el líder.** El agente ajustó la redacción de `.env.example` y
   `deploy/README.md` diciendo «añádelo a `.gitignore»», porque en ese momento el archivo no
   existía. El líder lo creó después (`/workspace/repo/.gitignore`, excluye `.env`, `.env.*` salvo
   `.env.example`, `.venv/`, `.data/`, `__pycache__/`) y **corrigió la redacción** de esos dos
   archivos para que digan que `.env` está excluido en `.gitignore` y en `.dockerignore`.

2. **`minio-init` como servicio aparte.** El contrato listaba 6 servicios; el compose tiene 7
   entradas porque `minio-init` es un servicio de un solo uso que inicializa el bucket. Está en el
   perfil `minio`, así que el perfil de piloto sigue siendo exactamente `api + worker + beat +
   postgres + redis`.

3. **`configs.content` en `docker-compose.yml`.** Obliga a Docker Compose ≥ 2.23.1. Es la forma
   canónica de declarar el `env_file` sin versionar el `.env` real.

---

## 5. Lo NO validado

- **Construcción de la imagen** (`docker build`): no hay binario `docker` en el entorno.
- **Arranque real de los 6 contenedores** (`docker compose up -d`): no hay demonio Docker.
- **Comportamiento real de los healthchecks**: se verificó que están bien declarados, no que el
  contenedor pase a `healthy`.

El detalle está en `docs/skeleton/VALIDACION.md`, sección 9.
