"""Pruebas de la configuración en entorno Railway.

Railway entrega **una** URL de base de datos y **un** Redis, y sus
variables tienen nombres y formatos que no son los del despliegue
nativo. Estas pruebas fijan el comportamiento para que la adaptación no
se deshaga.

Lo que se fija:

1. Una `DATABASE_URL` con driver **síncrono** (como la da el plugin de
   PostgreSQL de Railway) se **normaliza** a asíncrono, y la versión
   síncrona para Alembic se deriva de ella.
2. El broker y el backend de Celery se **derivan** de `REDIS_URL` con
   bases de datos distintas, cuando no vienen fijados.
3. Si vienen fijados, se **respetan**: el despliegue nativo los fija, y
   eso no se toca.
4. Un esquema **desconocido** sigue siendo un error. La adaptación no
   relaja ninguna validación.
"""

from __future__ import annotations

from typing import Any

import pytest


def _configurar(monkeypatch: Any, **variables: str) -> None:
    """Fija el entorno como lo haría Railway, sin el `.env` local."""
    for nombre, valor in variables.items():
        monkeypatch.setenv(nombre, valor)
    for nombre in (
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "DATABASE_URL",
        "REDIS_URL",
    ):
        monkeypatch.delenv(nombre, raising=False)
    for nombre, valor in variables.items():
        monkeypatch.setenv(nombre, valor)


def _settings_railway(monkeypatch: Any, **extra: str) -> Any:
    """Construye la configuración como en Railway, sin leer el `.env`."""
    from app.core import config as modulo_config

    _configurar(
        monkeypatch,
        SECRET_KEY="clave-de-prueba-suficientemente-larga-1234567890",
        MINIO_ACCESS_KEY="m",
        MINIO_SECRET_KEY="s",
        DATABASE_URL="postgresql://usuario:clave@postgres.railway.internal:5432/railway",
        REDIS_URL="redis://redis.railway.internal:6379/0",
        **extra,
    )
    # `_env_file=None` no desactiva el fichero en esta versión de
    # pydantic-settings: hay que pasar la ruta vacía.
    return modulo_config.Settings(_env_file=[])


class TestConfiguracionEnRailway:
    """La configuración nativa de Railway arranca y es correcta."""

    def test_01_la_url_sincrona_se_normaliza_a_asincrona(
        self, monkeypatch: Any
    ) -> None:
        """El plugin de PostgreSQL de Railway entrega el driver síncrono."""
        configuracion = _settings_railway(monkeypatch)
        assert configuracion.database_url == (
            "postgresql+asyncpg://usuario:clave@postgres.railway.internal:5432/railway"
        )
        assert configuracion.database_url_sync == (
            "postgresql+psycopg://usuario:clave@postgres.railway.internal:5432/railway"
        )

    def test_02_el_broker_y_el_backend_se_derivan_del_redis(
        self, monkeypatch: Any
    ) -> None:
        """Un solo Redis, tres bases de datos: aplicación, broker y backend."""
        configuracion = _settings_railway(monkeypatch)
        assert configuracion.redis_url == "redis://redis.railway.internal:6379/0"
        assert configuracion.celery_broker_url == "redis://redis.railway.internal:6379/1"
        assert configuracion.celery_result_backend == "redis://redis.railway.internal:6379/2"

    def test_03_si_vienen_fijadas_se_respetan(self, monkeypatch: Any) -> None:
        """El despliegue nativo los fija, y la derivación no los pisa."""
        configuracion = _settings_railway(
            monkeypatch,
            CELERY_BROKER_URL="redis://otro:6379/5",
            CELERY_RESULT_BACKEND="redis://otro:6379/6",
        )
        assert configuracion.celery_broker_url == "redis://otro:6379/5"
        assert configuracion.celery_result_backend == "redis://otro:6379/6"

    def test_04_un_esquema_desconocido_sigue_siendo_un_error(
        self, monkeypatch: Any
    ) -> None:
        """La adaptación no relaja ninguna validación."""
        from app.core import config as modulo_config

        _configurar(
            monkeypatch,
            SECRET_KEY="clave-de-prueba-suficientemente-larga-1234567890",
            MINIO_ACCESS_KEY="m",
            MINIO_SECRET_KEY="s",
            DATABASE_URL="mysql://usuario:clave@host/bd",
            REDIS_URL="redis://redis.railway.internal:6379/0",
        )
        with pytest.raises(modulo_config.ConfiguracionInvalidaError):
            modulo_config.Settings(_env_file=[])

    def test_05_el_redis_sin_base_de_datos_la_recibe(self, monkeypatch: Any) -> None:
        """Un `REDIS_URL` sin base de datos explícita no rompe la derivación."""
        from app.core import config as modulo_config

        _configurar(
            monkeypatch,
            SECRET_KEY="clave-de-prueba-suficientemente-larga-1234567890",
            MINIO_ACCESS_KEY="m",
            MINIO_SECRET_KEY="s",
            DATABASE_URL="postgresql://u:p@h:5432/bd",
            REDIS_URL="redis://redis.railway.internal:6379",
        )
        configuracion = modulo_config.Settings(_env_file=[])
        assert configuracion.celery_broker_url == "redis://redis.railway.internal:6379/1"
        assert configuracion.celery_result_backend == "redis://redis.railway.internal:6379/2"
