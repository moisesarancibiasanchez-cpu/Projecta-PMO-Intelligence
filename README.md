# Projecta PMO Intelligence

> **La capa inteligente para gobernar proyectos, portafolios y decisiones.**
> *Decide con evidencia. Avanza con control.*

Este es el **esqueleto del repositorio** de Projecta PMO Intelligence: la base sobre la que se
construye el producto. No es el producto terminado; es el andamiaje que deja demostrado que el
stack levanta, con las piezas reales en su sitio.

---

## Qué es el producto

**Projecta PMO Intelligence** es una plataforma de inteligencia para PMO que convierte reuniones,
correos, tickets, documentos y planes de proyecto en decisiones trazables, tareas ejecutables y
reportes executives.

> **Convierte datos, decisiones y compromisos en control ejecutivo.**

Actúa como una capa inteligente sobre los sistemas que ya utiliza una organización: Jira, Azure
DevOps, MS Project, Oracle APEX, Teams, SharePoint, correo y bases de datos corporativas. Su
objetivo es cerrar la brecha entre lo que ocurre en el día a día de un proyecto y lo que queda
registrado, controlado y visible para la PMO y la jefatura.

Esta sección describe **el producto que se está construyendo**. Lo que hay hoy en el
repositorio es el esqueleto que lo sostiene: la capa técnica sobre la que se monta. La
distinción importa, y el README marca en cada punto qué está validado y qué no.

## Funcionalidades del producto

### Captura inteligente
- Ingesta de transcripciones, grabaciones, correos, documentos y tickets.
- Procesamiento de archivos Word, PDF, Excel y XML de MS Project.
- Búsqueda semántica sobre actas, documentos, tareas y evidencias.
- Integración con Microsoft Teams, SharePoint, Jira, Azure DevOps y sistemas internos.

### Documentación PMO
- Generación de actas de reunión y minutas ejecutivas.
- Detección de acuerdos, responsables, fechas y compromisos.
- Creación de informes de avance y reportes semanales.
- Generación de historias de usuario y criterios de aceptación.
- Elaboración de matrices de riesgos, supuestos y dependencias.
- Preparación de presentaciones para comités y reportes ejecutivos.

### Gestión de compromisos
- Conversión de acuerdos en tareas o tickets.
- Asignación de responsables, fechas y prioridades.
- Alertas por compromisos vencidos o próximos a vencer.
- Seguimiento del estado de avance por proyecto y responsable.

### Gestión de riesgos
- Identificación de riesgos, bloqueos y dependencias.
- Clasificación por impacto, probabilidad y criticidad.
- Registro de planes de mitigación y responsables.
- Alertas tempranas ante desvíos o riesgos sin tratamiento.

### Inteligencia de portafolio
- Dashboards por proyecto, programa y portafolio.
- Semáforos de estado, avance, riesgo y cumplimiento.
- Análisis de backlog, antigüedad de tareas y cuellos de botella.
- Comparación entre planificación y ejecución real.
- Visión consolidada para comités y gerencia.

### Trazabilidad y auditoría
- Registro de la fuente original de cada decisión o tarea.
- Versionamiento de documentos y cambios aprobados.
- Historial de acciones realizadas por usuarios y agentes IA.
- Control de accesos, permisos y evidencia auditable.

---

## Arquitectura conceptual

```text
+--------------------------------------------------+
|              Capa de presentación                 |
|   Oracle APEX · Web · Teams · Slack · Dashboards  |
+--------------------------------------------------+
                         |
+--------------------------------------------------+
|             Capa de orquestación IA               |
|   Agentes · RAG · LLM Router · Workflows          |
+--------------------------------------------------+
                         |
+--------------------------------------------------+
|                Capa de dominio PMO                |
|  Proyectos · Tareas · Riesgos · Decisiones · HU   |
+--------------------------------------------------+
                         |
+--------------------------------------------------+
|               Capa de integración                 |
| Jira · Azure DevOps · MS Project · Teams · Drive  |
+--------------------------------------------------+
                         |
+--------------------------------------------------+
|            Capa de datos y seguridad              |
| Base de datos · Vector Store · Logs · Permisos    |
+--------------------------------------------------+
```

---

---

## Qué hay aquí

| Capa | Tecnología | Qué aporta |
|---|---|---|
| API | FastAPI 0.115.5 + Uvicorn | Rutas HTTP, health checks, documentación OpenAPI |
| Plantillas | Jinja2 3.1.4 | Interfaz web renderizada en el servidor, con escapado siempre activo |
| Datos | PostgreSQL 16 + pgvector 0.6.2 | Base de datos relacional con búsqueda vectorial |
| Asíncrono | Celery 5.4.0 + Redis 7 | 6 colas de trabajo, planificador y broker |
| Migraciones | Alembic 1.14.0 | Versionado del esquema, reversible |
| Configuración | Pydantic Settings 2.6.1 | Variables tipadas y validadas al arrancar |
| Despliegue | Docker Compose | 6 servicios con healthchecks y dependencias ordenadas |

**Sin dependencia de nube.** Todo el stack es open source y autoalojable: nada de Oracle, nada de
servicios Azure en el runtime. Es un requisito, no una preferencia: en banca y entornos regulados
el despliegue privado no es negociable, y el lock-in de nube es un coste de salida permanente.

---

## Estado de la validación

| Componente | Estado | Cómo se validó |
|---|:---:|---|
| API y rutas | **VALIDADO** | Servidor real, `curl` a `/health`, `/ready`, `/`, `/docs` |
| Plantillas y escapado | **VALIDADO** | Render real; `<script>` malicioso sale escapado |
| Migraciones | **VALIDADO** | `upgrade` → `downgrade` → `upgrade` contra PostgreSQL real con pgvector |
| Celery | **VALIDADO** | Worker real consumiendo del broker Redis real, resultado por el backend |
| Compose | **VALIDADO (estático)** | 6 servicios, `depends_on`, healthchecks y variables |
| Pruebas | **VALIDADO** | 20 pruebas en verde |
| **Construcción de la imagen** | **NO VALIDADO** | No hay binario `docker` en el entorno |
| **Arranque de los contenedores** | **NO VALIDADO** | No hay binario `docker` en el entorno |

El detalle completo, con la salida literal de cada comando, está en
**[`docs/skeleton/VALIDACION.md`](docs/skeleton/VALIDACION.md)**.

---

## Puesta en marcha

### Opción A — con Docker (despliegue real)

```bash
cp .env.example .env
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env

docker compose up -d
docker compose ps
curl -s http://localhost:8000/ready
```

### Opción B — sin Docker (desarrollo y validación)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple/ -r requirements.txt

cp .env.example .env
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env

.venv/bin/python -m alembic upgrade head
bash scripts/levantar_stack.sh
```

En otra terminal, el worker y el planificador:

```bash
.venv/bin/python -m celery -A app.core.celery_app:celery_app worker --loglevel=info
.venv/bin/python -m celery -A app.core.celery_app:celery_app beat --loglevel=info
```

### Verificar

```bash
bash scripts/verificar_stack.sh
echo "código de salida: $?"     # debe ser 0
```

---

## Estructura

```
.
├── app/
│   ├── main.py                 Factory de la aplicación FastAPI
│   ├── api/
│   │   ├── deps.py             Dependencias compartidas
│   │   └── routers/
│   │       ├── health.py       /health, /ready, /salud
│   │       └── index.py        / (portada)
│   ├── core/
│   │   ├── config.py           Settings tipados y validados
│   │   ├── celery_app.py       Instancia Celery, 6 colas, robustez
│   │   └── db/
│   │       ├── base.py         DeclarativeBase
│   │       ├── models.py       Organizacion, Proyecto, Usuario, Rol
│   │       └── session.py      Engine async + sessionmaker
│   ├── workers/tasks/          projecta.smoke, projecta.mantenimiento.ping
│   ├── templates/              base.html + partials
│   └── static/css/app.css
├── alembic/                    Migraciones
├── tests/                      Suite de humo
├── scripts/                    verificar_stack.sh, levantar_stack.sh
├── deploy/README.md            Despliegue de piloto y corporativo
├── docs/skeleton/              Contrato, validación y runbook
├── docker-compose.yml
├── Dockerfile
└── Makefile
```

---

## El contrato técnico

`docs/skeleton/design.md` es el documento normativo del repositorio: fija el layout de archivos, los
nombres de las variables de entorno, las rutas de la API, el shape del JSON de `/ready`, las 6 colas
de Celery, los nombres de las plantillas y los servicios del compose.

**Si el código y el contrato discrepan, el contrato gana.** Cuando cambie una decisión de
arquitectura, se actualiza el contrato primero y el código después. Ese fue el orden que permitió
que cuatro pistas de trabajo en paralelo no se pisaran: cada una leyó el mismo contrato antes de
escribir.

---

## Rutas de la API

| Método | Ruta | Qué hace | Código |
|---|---|---|---|
| `GET` | `/health` | Liveness puro. No toca ninguna dependencia. | 200 `OK` |
| `GET` | `/ready` | Readiness. Verifica base de datos y Redis. | 200 / 503 |
| `GET` | `/salud` | Detalle de salud en JSON. | 200 |
| `GET` | `/` | Portada renderizada con Jinja2. | 200 |
| `GET` | `/docs` | Documentación OpenAPI. | 200 |

El shape de `/ready`:

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

`database` y `redis` son **críticas**: si alguna falla, `status` pasa a `"degraded"` y la respuesta
es 503. `storage` es **no crítica**: su fallo no degrada el estado global, porque la API puede
seguir sirviendo sin almacenamiento de objetos.

---

## Documentación

| Documento | Para qué |
|---|---|
| [`docs/skeleton/design.md`](docs/skeleton/design.md) | Contrato técnico normativo |
| [`docs/skeleton/VALIDACION.md`](docs/skeleton/VALIDACION.md) | Informe de validación con salidas literales |
| [`docs/skeleton/RUNBOOK.md`](docs/skeleton/RUNBOOK.md) | Manual de operación, diagnóstico y respaldo |
| [`docs/skeleton/deliverable.md`](docs/skeleton/deliverable.md) | Entregable del contrato y el esqueleto de directorios |
| [`deploy/README.md`](deploy/README.md) | Despliegue de piloto y corporativo |
| `docs/skeleton/deliverable-*.md` | Los entregables de cada capa |

---

## La marca

El nombre del proyecto es **Projecta PMO Intelligence**, siempre completo. No se abrevia a
«Projecta» a secas: ni en el código, ni en los comentarios, ni en la interfaz, ni en la
documentación. La bajada es *«La capa inteligente para gobernar proyectos, portafolios y
decisiones.»* y el tagline corto, *«Decide con evidencia. Avanza con control.»*

---

## Principios de diseño

- **Human-in-the-loop:** la IA propone, el usuario valida y el sistema registra.
- **Trazabilidad por defecto:** cada resultado conserva su fuente y evidencia.
- **Integración antes que reemplazo:** convive con las herramientas existentes.
- **Seguridad corporativa:** control de accesos, cifrado, auditoría y residencia de datos.
- **Modularidad:** cada capacidad puede implementarse y venderse como módulo independiente.

---

## Roadmap

| Fase | Entregable | Estado |
|---|---|---|
| Fase 0 | Esqueleto del repositorio, contrato técnico y despliegue en Railway | **En curso** |
| Fase 1 | Épica 0 (HU_01–HU_08): usuarios, espacios, tableros, catálogo, auditoría, SLA,configuración y notificaciones | **En curso** |
| Fase 2 | Bitácora GRM como módulo de Incidencias (Épica 7) | **En curso** |
| Fase 3 | Edge Connector: captura con Microsoft Graph y puerta de consentimiento | **En curso** |
| Fase 4 | Projecta Capture: MVP de captura de reuniones | **En curso** |
| Fase 5 | Integraciones con Jira, Azure DevOps, MS Project, SharePoint, Teams y Slack | Pendiente |
| Fase 6 | Inteligencia de portafolio, reportes ejecutivos y forecasting | Pendiente |

El detalle contractual por historia de usuario está en el **TO-BE v1.1**
(`output/projecta-pmo-intelligence-TOBE-v1.1.md`): 64 HUs, DoR, DoD y criterios
de aceptación en Gherkin.

---

## Contribuciones

1. Crea una rama: `feature/nueva-funcionalidad`.
2. Realiza los cambios y **agrega pruebas**: toda corrección necesita prueba de
   regresión, y la prueba tiene que poder fallar.
3. Abre un Pull Request con una descripción clara.

---

## Licencia

MIT. Ver [`LICENSE`](LICENSE).

---

## Contacto

Para consultas, ideas o colaboraciones, abre un Issue en este repositorio.
