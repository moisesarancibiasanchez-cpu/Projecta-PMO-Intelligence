# Entregable — Contrato técnico compartido y esqueleto de directorios

**Tarea:** contrato técnico compartido y esqueleto de directorios
**Responsable:** el líder de proyecto (ejecución directa, en lugar del agente de desarrollo)
**Motivo de la ejecución directa:** el agente asignado leyó el plan y la documentación de referencia
en lugar de producir, y el contrato es la pieza de la que dependen las cuatro pistas paralelas. Tomar
el contrato directamente evitó bloquear el resto del trabajo. El plan se relanzó con las cuatro
pistas independientes y la integración final.

## Qué se produjo

### 1. Árbol de directorios completo

```
/workspace/repo/
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   └── db/
│   │       ├── __init__.py
│   ├── api/
│   │   ├── __init__.py
│   │   └── routers/
│   │       ├── __init__.py
│   └── workers/
│       ├── __init__.py
│       └── tasks/
│           ├── __init__.py
├── alembic/versions/
├── tests/
├── scripts/
├── deploy/
├── docs/skeleton/
└── .data/
```

Todos los `__init__.py` quedan vacíos. Las tareas paralelas **no** deben crearlos ni
modificarlos.

### 2. `docs/skeleton/design.md` — el contrato

Documento normativo de 15,6 KB con nueve secciones:

| Sección | Contenido |
|---|---|
| a) Layout del paquete | Ruta exacta de cada archivo y qué tarea lo escribe |
| b) Contrato de configuración | 16 variables con tipo, obligatoriedad, default y propósito; reglas de arranque fallido |
| c) Contrato de la API | 5 rutas con método, respuesta, código y dependencias; shape JSON exacto de `/ready` |
| d) Contrato de Celery | 6 colas, 2 tareas con firma, 7 parámetros de robustez, beat a 5 min |
| e) Contrato de plantillas | Directorio, plantilla base, bloques, variables comunes, 3 partials, política de escapado |
| f) Contrato de base de datos | Motor async, driver de producción, fallback, sesión, Alembic, 4 entidades |
| g) Contrato de docker-compose | 6 servicios con imagen, puerto y healthcheck; volúmenes; perfil `minio` |
| h) Nomenclatura | 8 reglas de naming con ejemplo cada una |
| i) Sandbox | Intérprete, índice PyPI, ausencia de Docker, y servicios reales para validar |

## Decisiones tomadas

1. **El driver de producción es `postgresql+asyncpg`**, no `psycopg`. Asyncpg es el driver
   asíncrono nativo de PostgreSQL y encaja con SQLAlchemy 2 async. `psycopg` queda para Alembic
   (que es síncrono), mediante la propiedad `database_url_sync`.
2. **`storage` es una dependencia no crítica** en `/ready`: si MinIO no responde, el servicio
   sigue operativo en modo `degraded`. Un health check que tumba el servicio por un storage
   secundario genera más caídas que las que evita.
3. **El proyecto no tiene `BOOTSTRAP_ADMIN_PASSWORD`**, a diferencia del módulo heredado de
   Bitácora GRM. El arranque administrativo no está en el esqueleto.
4. **`SECRET_KEY` no tiene default**: el proceso falla al arrancar nombrando la variable ausente.
5. **Se documentaron los servicios reales disponibles** (PostgreSQL con pgvector 0.6.2 vía
   `pgserver` y Redis 7 daemonizado) para que la validación sea contra el stack de producción y
   no una versión degradada con SQLite y modo eager.

## Lo que queda fuera de este entregable

- Todo el código de aplicación: lo producen las cuatro pistas paralelas.
- La validación de arranque: la produce la tarea de integración final.
- La construcción de la imagen Docker y el arranque de los 6 contenedores: **no validables** en
  este entorno por ausencia del binario `docker`. Queda documentado como tal, no como LIMITADO.
