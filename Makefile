# =============================================================================
#  Projecta PMO Intelligence — Makefile
#  «Decide con evidencia. Avanza con control.»
#
#  Objetivo: abstraer Docker Compose para el día a día del equipo.
#
#  Variables configurables (por entorno o por línea de comandos):
#     COMPOSE     binario de compose            (docker compose | podman-compose)
#     PYTHON      intérprete local              (solo para los targets sin Docker)
#     BACKUP_DIR  dónde se dejan los volcados   (backups/)
#     POSTGRES_DB base de datos del stack        (projecta)
#
#  Ejemplos:
#     make up                  # stack mínimo de piloto
#     make up-minio            # stack completo con MinIO
#     make test PYTHON=/workspace/.venv/bin/python
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE    ?= docker compose
PYTHON     ?= python3
BACKUP_DIR ?= backups
POSTGRES_DB ?= projecta

.PHONY: help up up-minio up-observability down down-volumes ps logs migrate seed \
        seed-roles verify-roles backup restore test test-local lint integrity shell \
        psql redis-cli health clean observability-up observability-down verify-stack \
        verify-completo

help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- Ciclo de vida del stack -----------------------------------------------

up: ## Levanta el stack mínimo de piloto (api + worker + beat + postgres + redis)
	$(COMPOSE) up -d --build
	$(COMPOSE) ps

up-minio: ## Levanta el stack completo, con el perfil `minio` (minio + minio-init)
	$(COMPOSE) --profile minio up -d --build
	$(COMPOSE) ps

up-observability: ## Levanta el stack con el perfil `observabilidad` (prometheus + grafana + otel)
	$(COMPOSE) --profile observabilidad up -d --build
	$(COMPOSE) ps

observability-up: up-observability ## Alias de `up-observability`

observability-down: ## Detiene solo los servicios de observabilidad
	$(COMPOSE) --profile observabilidad stop prometheus grafana otel-collector

verify-stack: ## Ejecuta la verificación integral del esqueleto (exit 0 = todo pasa)
	bash scripts/verificar_stack.sh

verify-completo: ## Auditoría exhaustiva de TODO el proyecto (código + entregables)
	$(PYTHON) /workspace/output/herramientas/validar_proyecto_completo.py

integrity: ## Comprueba que no hay corrupción textual en el código (exit 2 = corrupto)
	$(PYTHON) scripts/verificar_integridad_textual.py

barrido: ## Barrido de corrupción textual sobre el código, con autoprueba
	$(PYTHON) /workspace/output/herramientas/barrido_corrupcion.py $(PWD)

poc-edge: ## Demostración de extremo a extremo del Edge Connector (23 comprobaciones)
	$(PYTHON) scripts/demo_edge_conector.py

poc-edge-verificar: ## Verifica el entregable de la PoC del Edge Connector
	$(PYTHON) /workspace/output/integraciones/edge-connector/poc/verificar_poc.py

down: ## Detiene el stack conservando los volúmenes (pgdata, redisdata, miniodata)
	$(COMPOSE) --profile minio down

down-volumes: ## Detiene el stack y BORRA los volúmenes (pide confirmación)
	@printf 'ATENCIÓN: se borran los volúmenes (pgdata, redisdata, miniodata).\n'
	@read -p "¿Confirmas? (escribe SI): " respuesta; \
	if [ "$$respuesta" != "SI" ]; then echo "Cancelado."; exit 1; fi
	$(COMPOSE) --profile minio down -v

ps: ## Muestra el estado de los contenedores del stack
	$(COMPOSE) ps

logs: ## Sigue los logs de todos los servicios (Ctrl-C para dejar de seguir)
	$(COMPOSE) logs -f --tail=100

health: ## Muestra el estado de salud (healthcheck) de cada servicio
	$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}'

# --- Datos y esquema -------------------------------------------------------

migrate: ## Aplica las migraciones de Alembic (alembic upgrade head)
	$(COMPOSE) run --rm api alembic upgrade head

seed: migrate ## Carga los datos de semilla (idempotente) en la base de datos
	@if $(COMPOSE) run --rm api python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('scripts.seed') else 1)"; then \
		$(COMPOSE) run --rm api python -m scripts.seed; \
	else \
		echo "El módulo 'scripts.seed' todavía no existe en el repositorio (hito de datos de semilla)."; \
		echo "La migración se aplicó correctamente, pero no hay datos de semilla que cargar."; \
	fi

seed-roles: migrate ## Siembra el catálogo de los seis roles normativos (idempotente)
	$(COMPOSE) run --rm api python -m scripts.sembrar_roles

verify-roles: ## Comprueba que el catálogo de roles de la base de datos coincide con el normativo
	$(COMPOSE) run --rm api python -m scripts.sembrar_roles --verificar

backup: ## Volcado comprimido de PostgreSQL en $(BACKUP_DIR)/
	@mkdir -p $(BACKUP_DIR)
	$(COMPOSE) exec -T postgres pg_dump -U postgres -d $(POSTGRES_DB) --no-owner --no-privileges \
		| gzip > $(BACKUP_DIR)/projecta_$$(date -u +%Y%m%dT%H%M%SZ).sql.gz
	@ls -1t $(BACKUP_DIR)/projecta_*.sql.gz | head -n 1

restore: ## Restaura el volcado más reciente de $(BACKUP_DIR) en PostgreSQL
	@ultimo=$$(ls -1t $(BACKUP_DIR)/projecta_*.sql.gz 2>/dev/null | head -n 1); \
	if [ -z "$$ultimo" ]; then echo "No hay volcados en $(BACKUP_DIR)/"; exit 1; fi; \
	echo "Restaurando $$ultimo en la base de datos $(POSTGRES_DB) ..."; \
	gzip -dc "$$ultimo" | $(COMPOSE) exec -T postgres psql -U postgres -d $(POSTGRES_DB) -v ON_ERROR_STOP=1

# --- Calidad ---------------------------------------------------------------

test: ## Ejecuta la suite de pruebas (en el contenedor si hay Docker; si no, en local)
	@if command -v docker >/dev/null 2>&1; then \
		$(COMPOSE) run --rm api pytest -q; \
	else \
		echo "Docker no está disponible en este entorno: se ejecutan las pruebas con el intérprete local."; \
		$(PYTHON) -m pytest -q; \
	fi

test-local: ## Ejecuta la suite de pruebas con el intérprete local (sin Docker)
	$(PYTHON) -m pytest -q

lint: ## Análisis estático (ruff si está instalado; si no, verificación de sintaxis)
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check app tests scripts; \
	else \
		echo "ruff no está instalado: se ejecuta la verificación de sintaxis (compileall)."; \
		$(PYTHON) -m compileall -q app tests scripts; \
	fi

# --- Acceso directo a los servicios ----------------------------------------

shell: ## Abre una shell dentro del contenedor de la API
	$(COMPOSE) exec api bash

psql: ## Abre psql contra el PostgreSQL del stack
	$(COMPOSE) exec postgres psql -U postgres -d $(POSTGRES_DB)

redis-cli: ## Abre el cliente de Redis contra el stack
	$(COMPOSE) exec redis redis-cli

# --- Mantenimiento ---------------------------------------------------------

clean: ## Elimina las imágenes construidas por el compose (NO toca los volúmenes)
	$(COMPOSE) --profile minio down --rmi local --remove-orphans
