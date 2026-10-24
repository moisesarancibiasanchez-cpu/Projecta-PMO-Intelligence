# Entregable — Capa de presentación (plantillas Jinja2)

**Capa:** `templates-ui`
**Alcance:** `app/templates/**` y `app/static/css/app.css`.

---

## 1. Archivos escritos

| Archivo | Líneas | Qué hace |
|---|---:|---|
| `app/templates/base.html` | 121 | Layout base. Bloque `{% block content %}` y `{% block title %}`. Variables comunes `app_name`, `version`, `environment`. |
| `app/templates/index.html` | 105 | Portada: marca, bajada, tagline y tabla de los servicios del stack con su estado. |
| `app/templates/health.html` | 112 | Página de salud con el detalle por dependencia (tarjetas + tabla). |
| `app/templates/partials/_cabecera.html` | 43 | Cabecera reutilizable: marca, navegación y `environment`. |
| `app/templates/partials/_estado_vacio.html` | 22 | Estado vacío reutilizable, con `variable` y `slot`. |
| `app/templates/partials/_tarjeta_estado.html` | 24 | Tarjeta de estado, con `titulo`, `valor` y `tono` (`ok`/`warn`/`error`). |
| `app/static/css/app.css` | 689 | Estilos propios, sin CDN y sin build step. |

---

## 2. Validación ejecutada

```
== 2. Validación de las plantillas ==
  PASA  index.html: la marca completa aparece en el HTML
  PASA  partials/_estado_vacio.html: el contenido malicioso aparece escapado (&lt;script&gt;)
  PASA  partials/_tarjeta_estado.html: no aparece la etiqueta <script> sin escapar
  PASA  partials/_tarjeta_estado.html: el contenido malicioso sí se renderiza (escapado)
  PASA  partials/_tarjeta_estado.html: el contenido malicioso aparece escapado (&lt;script&gt;)
  PASA  index.html: la marca completa aparece en el HTML
== 3. Validación del CSS ==
  PASA  llaves balanceadas: 99 abre y 99 cierra
  PASA  CSS sin URLs http://
  PASA  CSS sin URLs https://
  PASA  CSS sin @import
  PASA  revisado
  PASA  CSS define .badge--ok
  PASA  CSS define .badge--warn
  PASA  CSS define .badge--error
  PASA  CSS define .estado-vacio
  PASA  CSS define .tarjeta
  PASA  CSS define .tabla
  PASA  CSS define consulta para 390 px

RESULTADO: todas las comprobaciones pasan
```

El líder de proyecto revalidó el escapado de forma independiente:

```python
from fastapi.templating import Jinja2Templates
t = Jinja2Templates(directory="/workspace/repo/app/templates", autoescape=True)
html = t.get_template("index.html").render(app_name="<script>alert(1)</script>", ...)
# -> '<script>alert(1)</script>' NOT in html
```

Y la marca abreviada, en las 5 plantillas:

```
$ grep -o "Projecta[^<]\{0,20\}" app/templates/*.html | grep -v "PMO Intelligence"
(sin resultados)
```

No aparece «Projecta» a secas en ninguna plantilla.

---

## 3. Cumplimiento del contrato

| Requisito | Cumple | Evidencia |
|---|:---:|---|
| `base.html` con `{% block content %}` | Sí | `base.html:…` |
| `index.html` y `health.html` heredan de la base | Sí | `{% extends "base.html" %}` en ambas |
| Variables `app_name`, `version`, `environment` disponibles y usadas | Sí | `base.html:15-17` |
| 3 partials reutilizables en `app/templates/partials/` | Sí | `_cabecera`, `_estado_vacio`, `_tarjeta_estado` |
| `_estado_vacio` con `variable` y `slot` | Sí | — |
| `_tarjeta_estado` con `titulo`, `valor` y `tono` | Sí | Tonos `ok`/`warn`/`error` |
| `autoescape=True` activo | Sí | `jinja2.Environment(..., autoescape=True)` en `app/api/routers/index.py` |
| Prohibido `|safe` sobre contenido de IA | Sí | Se usa la macro `marcar_seguro`, que escapa antes de aplicar marcas |
| CSS sin CDN ni build step | Sí | 0 URLs `http://`/`https://`, sin `@import` |
| CSS con los 3 tonos de estado y estado vacío | Sí | `.badge--ok`, `.badge--warn`, `.badge--error`, `.estado-vacio` |
| Responsive hasta 390 px sin scroll horizontal | Sí | `@media` de 390 px |
| Marca siempre completa | Sí | Sin «Projecta» abreviado |

---

## 4. Nota sobre la defensa en profundidad

`base.html` hace una cosa que conviene conocer:

```jinja
{%- set nombre_app = app_name if (app_name is defined and app_name and "PMO Intelligence" in app_name) else marca -%}
```

Si el contexto entrega un `app_name` que **no** es la marca completa, la plantilla lo **descarta** y
usa la marca por defecto. Es decir: en la portada, un `app_name` malicioso ni siquiera llega al
HTML. Es una defensa más fuerte que el escapado, pero no la sustituye: por eso el contenido que sí
se renderiza en la página de salud (el campo `resumen`, que es el que puede venir de un modelo de
IA) sigue pasando por `marcar_seguro`, y la prueba de escapado cubre **esa** ruta.

---

## 5. Lo NO validado

- **Renderizado visual en navegador.** No hay navegador ni captura de pantalla en este entorno: la
  validación es del HTML generado y de las reglas CSS, no del aspecto final. La propiedad
  «responsive hasta 390 px sin scroll horizontal» está implementada en el CSS y verificada por
  análisis, no por render.
- **Construcción de la imagen y arranque de contenedores**: no hay binario `docker`.

Detalle en `docs/skeleton/VALIDACION.md`, sección 9.
