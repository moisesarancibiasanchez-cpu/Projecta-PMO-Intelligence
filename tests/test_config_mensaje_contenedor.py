"""Regresión — el mensaje de configuración inválida dice DÓNDE fijar las variables.

Qué falló (despliegue real en Railway, 2026-10-23):

    El servicio ``api`` se desplegó y **crasheó** al arrancar. El log decía:

        ConfiguracionInvalidaError: Configuración inválida de Projecta
        PMO Intelligence. […]
          - SECRET_KEY: no está definida.
          - DATABASE_URL: no está definida.
          - MINIO_ACCESS_KEY: no está definida.
          - MINIO_SECRET_KEY: no está definida.
        Definí esas variables en el entorno o en el archivo `.env` de la raíz
        del proyecto (/app/.env).

    El mensaje era **correcto pero inútil**: en un contenedor de Railway
    **no existe** el archivo ``/app/.env``. Le decía al operador que
    editara un fichero que no está, en lugar de decirle que fije las
    variables en el panel del servicio.

Qué fija esta prueba:

1. Sin las variables obligatorias, la aplicación **no arranca** y el error
   nombra **las cuatro** variables con su texto de ayuda.
2. El mensaje dice **dónde** fijarlas según el entorno:
   - en local, el archivo ``.env`` de la raíz del proyecto;
   - en un contenedor, el panel de variables del servicio.

Ejecución:

    python -m pytest tests/test_config_mensaje_contenedor.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

VARIABLES_OBLIGATORIAS = (
    "SECRET_KEY",
    "DATABASE_URL",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
)


class TestMensajeDeConfiguracionInvalida(unittest.TestCase):
    """El error de arranque tiene que ser accionable."""

    @staticmethod
    def _arrancar_sin_variables(*, como_contenedor: bool) -> tuple[str, str]:
        """Arranca la aplicación sin las variables obligatorias.

        Se lanza en un **subproceso** a propósito: la configuración se
        resuelve al importar el módulo, así que un import en este proceso
        envenenaría el resto de pruebas.

        Antes de importar la aplicación se **neutraliza ``load_dotenv``**:
        la configuración carga el ``.env`` de la raíz del proyecto, y en
        este repositorio ese fichero existe con valores reales. Sin
        neutralizarlo, la aplicación arrancaría bien y no habría nada que
        probar. Es la misma situación que en un contenedor, donde no hay
        ``.env``.
        """
        entorno = dict(os.environ)
        for variable in VARIABLES_OBLIGATORIAS:
            entorno.pop(variable, None)
        entorno["PYTHONPATH"] = str(RAIZ)
        if como_contenedor:
            # Railway y Docker inyectan esta variable.
            entorno["RAILWAY_ENVIRONMENT"] = "production"
        else:
            entorno.pop("RAILWAY_ENVIRONMENT", None)

        guion = (
            "import dotenv\n"
            "dotenv.load_dotenv = lambda *a, **k: False\n"
            "import app.core.config\n"
        )
        proceso = subprocess.run(
            [sys.executable, "-c", guion],
            capture_output=True,
            text=True,
            env=entorno,
            cwd="/tmp",
            timeout=180,
        )
        return proceso.stdout, proceso.stderr

    def test_01_sin_variables_la_aplicacion_no_arranca(self) -> None:
        _stdout, stderr = self._arrancar_sin_variables(como_contenedor=False)
        self.assertIn("Configuración inválida de Projecta PMO Intelligence", stderr)

    def test_02_el_error_nombra_las_cuatro_variables(self) -> None:
        _stdout, stderr = self._arrancar_sin_variables(como_contenedor=False)
        for variable in VARIABLES_OBLIGATORIAS:
            self.assertIn(
                f"{variable}: no está definida",
                stderr,
                f"el error no menciona {variable}",
            )

    def test_03_el_error_incluye_la_ayuda_de_cada_variable(self) -> None:
        _stdout, stderr = self._arrancar_sin_variables(como_contenedor=False)
        self.assertIn("Detalle de cada variable:", stderr)
        self.assertIn("openssl rand -hex 32", stderr)
        self.assertIn("postgresql+asyncpg", stderr)

    def test_04_en_un_contenedor_el_mensaje_no_pide_editar_un_fichero_que_no_existe(self) -> None:
        """En Railway no hay ``.env``: el mensaje debe decirlo así."""
        _stdout, stderr = self._arrancar_sin_variables(como_contenedor=True)
        self.assertIn("corriendo en un contenedor", stderr)
        self.assertIn("panel del servicio", stderr)
        self.assertIn("railway variable set", stderr)

    def test_05_en_local_el_mensaje_sigue_si_apuntando_al_fichero_env(self) -> None:
        _stdout, stderr = self._arrancar_sin_variables(como_contenedor=False)
        self.assertIn("`.env`", stderr)
        self.assertNotIn("corriendo en un contenedor", stderr)

    def test_06_la_deteccion_de_contenedor_es_explicita(self) -> None:
        """La detección no puede depender de casualidades del entorno."""
        from app.core import config

        self.assertIsInstance(config._EJECUTANDOSE_EN_CONTENEDOR, bool)
        # Con la variable de Railway presente, debe ser True.
        codigo = textwrap.dedent(
            """
            import os
            os.environ["RAILWAY_ENVIRONMENT"] = "production"
            from app.core import config
            print(config._EJECUTANDOSE_EN_CONTENEDOR)
            """
        )
        proceso = subprocess.run(
            [sys.executable, "-c", codigo],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(RAIZ)},
            cwd="/tmp",
            timeout=180,
        )
        self.assertIn("True", proceso.stdout, proceso.stdout + proceso.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
