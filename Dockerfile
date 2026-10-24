# syntax=docker/dockerfile:1.7
# =============================================================================
#  Projecta PMO Intelligence — imagen de la API, del worker y del planificador.
#  «Decide con evidencia. Avanza con control.»
#
#  Construcción multi-etapa:
#    1. `builder` — compila e instala las dependencias de Python en un venv
#                   aislado (/opt/venv) con las cabeceras de compilación.
#    2. `runtime` — imagen final: solo el venv, el código y las librerías de
#                   sistema que el stack necesita. Corre como usuario NO root.
#
#  Construcción:  docker build -t projecta-pmo-intelligence:local .
# =============================================================================

ARG PYTHON_VERSION=3.11
ARG DEBIAN_FRONTEND=noninteractive

# -----------------------------------------------------------------------------
#  Etapa 1 — builder
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Cabeceras de compilación. `libxml2-dev` queda preparado para `lxml`
# (TO-BE: parseo de XML de MS Project); el runtime solo necesita `libxml2`.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /build/requirements.txt
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r /build/requirements.txt

# -----------------------------------------------------------------------------
#  Etapa 2 — runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:${PATH}" \
    LANG=C.UTF-8 \
    TZ=UTC

# Librerías de sistema del stack:
#   - WeasyPrint (exportación a PDF): libpango, libpangoft2, libcairo2,
#     libgdk-pixbuf y fuentes base.
#   - Tesseract (OCR): tesseract-ocr + tesseract-ocr-spa (español).
#   - lxml: libxml2 en runtime (libxml2-dev solo hace falta al compilar).
#   - curl: healthcheck de la imagen.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        libcairo2 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libxml2 \
        shared-mime-info \
        fonts-dejavu-core \
        tesseract-ocr \
        tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Usuario y grupo sin privilegios (uid/gid 1001).
RUN groupadd --system --gid 1001 projecta \
    && useradd --system --uid 1001 --gid projecta \
        --home-dir /app --shell /usr/sbin/nologin projecta

WORKDIR /app

# Código fuente y artefactos de despliegue (el .dockerignore excluye .env,
# .git, .venv y cachés: la imagen nunca lleva secretos).
COPY --chown=projecta:projecta . /app/

# El usuario no root no escribe bytecode en el código montado.
RUN mkdir -p /app/app/static/css \
    && chown -R projecta:projecta /app

USER projecta

EXPOSE 8000

# Liveness de la imagen. El compose define su propio healthcheck por servicio
# (api, worker, beat) y este valor es el fallback cuando se ejecuta la imagen
# sin compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Arranque por defecto: API. `worker` y `beat` sobrescriben el comando desde
# docker-compose.yml.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
