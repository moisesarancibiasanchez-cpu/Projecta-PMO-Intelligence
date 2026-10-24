# Projecta PMO Intelligence — Despliegue en Railway, paso a paso

> Guía operativa. Todo lo que hay aquí está **verificado contra la CLI real**
> (`railway` v5.71.0) y contra la documentación oficial de Railway.
> Lo que **no** está verificado está marcado **NO VALIDADO**, y en este
> entorno no hay cuenta de Railway: el despliegue en sí no se ha ejecutado.

---

## 0. Resumen en una página

| # | Paso | Qué pasa | Riesgo |
|---|---|---|---|
| 1 | Publicar el código en GitHub | Railway despliega desde el repo | Bajo |
| 2 | Crear el proyecto en Railway | Panel vacío, sin servicios | Bajo |
| 3 | Conectar el repo de GitHub | Railway pide instalar su GitHub App | **Decisión de acceso** |
| 4 | Crear 6 servicios | `postgres`, `redis`, `minio`, `api`, `worker`, `beat` | Bajo |
| 5 | Crear las 2 variables compartidas | `SECRET_KEY` y `MINIO_ROOT_PASSWORD`, **selladas** | **Alto si se hace mal** |
| 6 | Fijar las variables por servicio | Referencias `${{...}}`, no valores | Medio |
| 7 | Crear los volúmenes | PostgreSQL en `/var/lib/postgresql` | **Trampa clásica** |
| 8 | Desplegar | `preDeploy` aplica migraciones con Cerrojo | Medio |
| 9 | Verificar | `/ready` y dominio | Bajo |

**Regla de oro de este despliegue**: ningún secreto **se escribe ni se copia**
a mano. Todo lo que se puede referenciar se referencia
(`${{postgres.DATABASE_URL}}`), y lo que no (la clave de firma) se genera
**en el servidor** y se sella **en el panel** para que no se pueda volver a leer.

---

## 1. Publicar el código en GitHub

El repositorio ya está listo para publicar. El `README` de la raíz describe
cómo hacerlo con seguridad, y `scripts/github-auth-seguro.sh` ofrece cuatro
vías (SSH con clave de despliegue, `gh auth login` interactivo, token desde
fichero `0600`, o push desde tu propio equipo con el *bundle*).

```bash
cd /workspace/repo
git push -u origin main
```

---

## 2. Crear el proyecto en Railway

```bash
railway login          # abre el navegador; en SSH, da un código de dispositivo
railway init           # crea el proyecto y lo enlaza a este directorio
```

Si ya tienes el proyecto:

```bash
railway link --project <ID> --environment production
```

`railway init` deja el directorio enlazado. A partir de aquí, todos los
comandos de este documento asumen que estás enlazado.

---

## 3. Conectar el repositorio de GitHub

**Aquí no se usa ningún token.** Railway se conecta a GitHub con su
**GitHub App**: tú instalas la aplicación desde GitHub y le das acceso a los
repositorios que elijas.

Desde el panel:

1. `railway.com/new` → **Deploy from GitHub repo**.
2. Eliges `moisesarancibiasanchez-cpu/Projecta-PMO-Intelligence`.
3. Si tu cuenta de Railway no está enlazada a GitHub, se te pide hacerlo:
   **Account Settings → Integrations → Connect GitHub**. Ahí eliges qué
   repositorios ve la app.

Desde la CLI (equivalente):

```bash
railway service source connect \
    --repo moisesarancibiasanchez-cpu/Projecta-PMO-Intelligence \
    --branch main \
    --service api
```

**Lo que hay que comprobar en GitHub** (Settings → Applications → Installed
GitHub Apps → Railway → Configure):

- **Repository access**: que incluya `Projecta-PMO-Intelligence`.
- **Permissions**: `Contents: Read and write` (Railway lo necesita para el
  autodespliegue).

> **Autodespliegue**: funciona solo si al menos un miembro del proyecto tiene
> una cuenta de GitHub **enlazada a Railway** con permisos de *contributor*
> sobre el repositorio. Si el push lo hace una persona sin esa cuenta,
> Railway **no** despliega y crea una **Deployment Approval** pendiente.
> Esto es una medida deliberada: impide que cualquiera que empuje a la rama
> active un despliegue.

---

## 4. Los seis servicios

```bash
# Bases de datos
railway add --database postgres --service postgres
railway add --database redis --service redis
railway add --image minio/minio --service minio

# Aplicación
railway add --service api
railway add --service worker
railway add --service beat
```

**Solo `api` tiene dominio público.** Los otros cinco se hablan por la red
privada de Railway (`*.railway.internal`).

El comando `railway add` **no tiene** ningún flag `--plugin`: la plantilla
de pgvector, si la necesitas, se añade desde el panel.

---

## 5. Las dos variables compartidas (y selladas)

Estas **no se pueden crear desde la CLI**: se crean en el panel, y se
**cierran** con la opción **Seal**.

> **Project Settings → Shared Variables**, eliges el entorno `production`,
> añades la variable y pulsas **Add**. Después, en el menú de tres puntos de
> la variable, **Seal**.

| Nombre | Valor | Cómo se genera |
|---|---|---|
| `SECRET_KEY` | Clave de firma de la aplicación | `openssl rand -hex 32` |
| `MINIO_ROOT_PASSWORD` | Contraseña del almacén de objetos | `openssl rand -hex 24` |

La clave de firma **se genera en el servidor que la va a usar** y **se pega
una sola vez** en el panel. Sellada, su valor:

- **sí** se entrega a las compilaciones y a los despliegues,
- **no** se puede volver a leer en el panel,
- **no** se puede des-sellar,
- **no** aparece en `railway variables` ni en `railway run` por la CLI.

Es decir: **una vez la escribes, nadie —tú incluido— puede volver a verla.**
Si se pierde, se genera otra y se actualiza; no se puede recuperar.

---

## 6. Variables por servicio

Todas como **referencias**, nunca como valores. Una referencia no contiene
el secreto: Railway lo resuelve al arrancar el servicio.

### Servicio `api`

```bash
railway variable set "DATABASE_URL=\${{postgres.DATABASE_URL}}"      --service api
railway variable set "REDIS_URL=\${{redis.REDIS_URL}}"              --service api
railway variable set "MINIO_ENDPOINT=http://minio.railway.internal:9000" --service api
railway variable set "MINIO_ACCESS_KEY=\${{minio.MINIO_ROOT_USER}}" --service api
railway variable set "MINIO_SECRET_KEY=\${{minio.MINIO_ROOT_PASSWORD}}" --service api
railway variable set "SECRET_KEY=\${{shared.SECRET_KEY}}"           --service api
railway variable set "APP_ENV=production"                            --service api
railway variable set "DEBUG=false"                                   --service api
railway variable set "CORS_ORIGINS=https://tu-dominio.example"       --service api
```

### Servicios `worker` y `beat`

Los mismos, **menos `SECRET_KEY`** (no firman nada):

```bash
for servicio in worker beat; do
    railway variable set "DATABASE_URL=\${{postgres.DATABASE_URL}}"           --service "$servicio"
    railway variable set "REDIS_URL=\${{redis.REDIS_URL}}"                   --service "$servicio"
    railway variable set "MINIO_ENDPOINT=http://minio.railway.internal:9000"  --service "$servicio"
    railway variable set "MINIO_ACCESS_KEY=\${{minio.MINIO_ROOT_USER}}"      --service "$servicio"
    railway variable set "MINIO_SECRET_KEY=\${{minio.MINIO_ROOT_PASSWORD}}"  --service "$servicio"
done
```

### Servicio `minio`

```bash
railway variable set "MINIO_ROOT_USER=projecta"                --service minio
railway variable set "MINIO_ROOT_PASSWORD=\${{shared.MINIO_ROOT_PASSWORD}}" --service minio
```

> **Doble comprobación**: `MINIO_ROOT_USER` lo pones tú, pero la contraseña
> sale de la compartida. La API la recibe como `MINIO_SECRET_KEY`. Los dos
> lados tienen que coincidir **exactamente**, o el acceso a objetos fallará
> con un 403 en tiempo de ejecución.

### Lo que NO hay que fijar

`PORT`, `RAILWAY_PUBLIC_DOMAIN` y `RAILWAY_PRIVATE_DOMAIN` los da Railway
solo. `CELERY_BROKER_URL` y `CELERY_RESULT_BACKEND` **se derivan** de
`REDIS_URL` (bases 1 y 2); Railway entrega un único Redis.

### Comprobación

```bash
railway variables --service api --kv
```

Las referencias se muestran; **los valores sellados no**. Que no aparezcan
es la prueba de que el sellado funciona.

---

## 7. Los volúmenes persistentes

> **Trampa clásica**: el volumen de PostgreSQL va en **`/var/lib/postgresql`**,
> **no** en `/var/lib/postgresql/data`. En PostgreSQL 18 y en el contenedor
> oficial de pgvector, montar en `data` deja la base de datos sin usar.

En la CLI v5, `railway volume add` **ya no tiene `--service`**: el servicio
se toma del que esté enlazado en ese momento.

```bash
railway service link postgres
railway volume add --mount-path /var/lib/postgresql

railway service link minio
railway volume add --mount-path /data
```

---

## 8. Desplegar

```bash
railway up --service api    --detach
railway up --service worker --detach
railway up --service beat   --detach
```

Cada servicio ejecuta su `preDeployCommand` **antes** de arrancar:

| Servicio | `preDeployCommand` | Qué hace |
|---|---|---|
| `api` | `python -m scripts.railway_preparar` | Crea el esquema, aplica migraciones, comprueba el contrato |
| `worker` | `python -m scripts.railway_preparar --solo-migracion` | Solo migraciones |
| `beat` | `python -m scripts.railway_preparar --solo-migracion` | Solo migraciones |

Las migraciones están serializadas con un **cerrojo de Postgres**
(`pg_advisory_lock`), así que los tres pre-deploy pueden correr a la vez sin
pisar el esquema. El comando de preparación **falla con un mensaje
explícito** si el plan de PostgreSQL no trae la extensión `vector`.

---

## 9. Verificar

```bash
# El dominio de la API
railway domain --service api

# Los logs, en vivo
railway logs --service api
railway logs --service worker
railway logs --service beat
```

Pruebas sobre el dominio público:

```bash
curl -s https://<dominio>/health
curl -s https://<dominio>/ready
curl -s -o /dev/null -w "%{http_code}\n" https://<dominio>/docs
```

`/ready` devuelve 200 solo cuando **la base de datos y Redis responden**.

---

## 10. Las cinco decisiones que bloquean el piloto

Estas **no son técnicas** y no se pueden tomar desde el repositorio. Están
detalladas en `output/railway/lo-que-falta-railway.md`.

| # | Decisión | Por qué bloquea |
|---|---|---|
| 🔴 1 | **¿Residen los datos en Railway?** | Si no, Railway es solo el entorno de pruebas |
| 🔴 2 | **¿Quién alcanza la API pública?** | Sin control de acceso, cualquiera con la URL puede intentar entrar |
| 🔴 3 | **¿Qué motor de transcripción y dónde?** | Decide si el audio sale del perímetro |
| 🔴 4 | **¿Entorno objetivo de la interfaz de captura?** | Sin él no se puede declarar compatibilidad de navegador |
| 🔴 5 | **¿Quién autoriza la publicación en el lado del cliente?** | El Edge Connector escribe en Jira y Azure DevOps |

**La número 2 es la urgente**: Railway expone la API a Internet en cuanto
generas el dominio. Mientras no haya respuesta, la opción prudente es
**generar el dominio solo cuando la decisión esté tomada**, y usar mientras
tanto la contraseña de acceso de Railway o un proxy corporativo.

---

## 11. Lo que NO está validado

- **El despliegue real.** No hay cuenta de Railway en este entorno: `railway
  up` **no se ha ejecutado**. Sí están verificados la sintaxis de todos los
  comandos contra la CLI v5.71.0 y la adaptación de la configuración.
- **Que el plan de PostgreSQL de Railway traiga `vector`.** El script de
  preparación lo comprueba y falla con un mensaje claro, pero no se ha
  comprobado en una cuenta real. La alternativa es la plantilla de pgvector.
- **El dominio público, el TLS y el sellado de secretos**, que solo se
  comprueban en el panel.
- **Los volúmenes**, que solo se comprueban con una base de datos real.

---

## 12. Recuperación: cómo volver atrás

| Problema | Qué hacer |
|---|---|
| La API no arranca | `railway logs --service api` → mira el error de arranque; el mensaje nombra la variable exacta que falta |
| `/ready` devuelve 503 | El healthcheck crítico es `database` + `redis`: mira los logs de `postgres` y `redis` |
| El despliegue se queda en «Building» | `railway logs --service api` → si falla el `preDeploy`, es el esquema: mira el mensaje del script de preparación |
| Error de extensión `vector` | El plan de PostgreSQL no trae pgvector: cambia a la plantilla de pgvector en el panel |
| Falla el acceso a objetos (403) | `MINIO_ROOT_USER` y la clave que espera `MINIO_SECRET_KEY` no coinciden |
| La migración se bloquea | El cerrojo `pg_advisory_lock` lo tiene uno de los tres pre-deploy: mira los logs de `worker` y `beat` |
| Se perdió el `SECRET_KEY` sellado | **No se puede recuperar.** Genera otra (`openssl rand -hex 32`), actualiza la compartida sellada y redespliega |
