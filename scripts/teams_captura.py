#!/usr/bin/env python3
"""CLI de captura de reuniones de Teams.

Es la herramienta para las **pruebas funcionales**: desde la terminal,
sin levantar la aplicación, sin tocar el tenant real, y con un doble de
Graph disponible para ensayar el ciclo completo.

Ejemplos
--------

Ver si la integración está bien configurada::

    python scripts/teams_captura.py salud

Levantar el doble de Graph y capturar contra él::

    python scripts/teams_captura.py capturar --base http://127.0.0.1:8099/v1.0

Capturar contra el tenant real::

    python scripts/teams_captura.py capturar --organizador ana@banco.example

Listar y leer las reuniones capturadas::

    python scripts/teams_captura.py listar
    python scripts/teams_captura.py ver <REUNION_ID>

El doble se levanta solo con ``--doble``: es la forma más rápida de
comprobar que el ciclo entero funciona antes de tocar el tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import uuid
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _configuracion() -> Any:
    """Devuelve la configuración de la aplicación."""
    from app.core.config import settings

    return settings


def _salir(codigo: int = 0) -> None:
    """Termina el proceso con el código indicado."""
    raise SystemExit(codigo)


# ─────────────────────────── salud ───────────────────────────


def cmd_salud(argumentos: argparse.Namespace) -> int:
    """Dice si la captura está configurada y qué falta."""
    config = _configuracion()
    faltan = []
    for nombre, valor in (
        ("GRAPH_TENANT_ID", config.graph_tenant_id),
        ("GRAPH_CLIENT_ID", config.graph_client_id),
        ("GRAPH_CERTIFICADO", config.graph_certificado),
        ("GRAPH_CLAVE_PRIVADA", config.graph_clave_privada),
        ("TEAMS_ORGANIZADOR", config.teams_organizador),
    ):
        if not valor:
            faltan.append(nombre)

    print("Estado de la captura de reuniones de Teams")
    print("=" * 52)
    if faltan:
        print("  NO CONFIGURADA. Falta:")
        for nombre in faltan:
            print(f"    · {nombre}")
    else:
        print("  Configurada.")
    print(f"  Organizador   : {config.teams_organizador or '(sin definir)'}")
    print(f"  Base de Graph : {config.graph_base_url}")
    print(f"  Ventana       : {config.teams_captura_ventana_horas} h")
    print(f"  Con audio     : {config.teams_captura_con_audio}")
    print(f"  Cola          : integraciones")
    return 0 if not faltan else 1


# ─────────────────────────── doble ───────────────────────────


def _levantar_doble() -> tuple[Any, str]:
    """Levanta el doble de Graph y devuelve el servidor y su base."""
    from scripts.mock_graph import crear_servidor_mock

    return crear_servidor_mock(0)


# ─────────────────────────── capturar ───────────────────────────


def cmd_capturar(argumentos: argparse.Namespace) -> int:
    """Captura las reuniones del organizador."""
    servidor = None
    base = argumentos.base
    if argumentos.doble:
        servidor, base = _levantar_doble()
        print(f"Doble de Graph levantado en {base}")

    if base:
        os.environ["GRAPH_BASE_URL"] = base

    if argumentos.organizador:
        os.environ["TEAMS_ORGANIZADOR"] = argumentos.organizador

    # El import va **después** de fijar las variables de entorno: la
    # configuración se construye la primera vez que se importa, y si el
    # import ocurre antes, `settings` se queda con la URL de Graph real
    # y el doble no se usa.
    from app.core.graph_teams import ErrorDeGraph, ErrorDeTranscripcionDeshabilitada
    from app.workers.tasks.captura_teams import ErrorDeCaptura, capturar_una_vez

    try:
        organizacion_id = argumentos.organizacion
        if not organizacion_id:
            print(
                "  Falta --organizacion con el UUID de la organización.\n"
                "  Se puede ver en la tabla `organizaciones` de la base de datos.",
                file=sys.stderr,
            )
            return 2

        resultado = asyncio.run(
            capturar_una_vez(
                organizacion_id,
                horas=argumentos.horas,
                organizador=argumentos.organizador,
                reunion_id=argumentos.reunion,
            )
        )
    except ErrorDeTranscripcionDeshabilitada as error:
        print(f"\n  {error}", file=sys.stderr)
        print(
            "\n  Es una decisión del administrador del tenant, no un fallo del "
            "conector.\n  Para activarlas: Centro de administración de Teams → "
            "Directivas de reunión.",
            file=sys.stderr,
        )
        return 3
    except ErrorDeGraph as error:
        print(f"\n  Graph devolvió un error: {error}", file=sys.stderr)
        return 4
    except ErrorDeCaptura as error:
        print(f"\n  La captura no se pudo ejecutar: {error}", file=sys.stderr)
        return 5
    finally:
        if servidor is not None:
            servidor.shutdown()

    print()
    print("Resultado de la captura")
    print("=" * 52)
    print(f"  Organizador           : {resultado['organizador']}")
    print(f"  Reuniones consideradas: {resultado['reuniones_consideradas']}")
    print(f"  Capturadas            : {resultado['capturadas']}")
    print(f"  Ya existían           : {resultado['ya_existentes']}")
    print(f"  Fallidas               : {resultado['fallidas']}")
    if resultado["detalles"]:
        print()
        for detalle in resultado["detalles"]:
            if "error" in detalle:
                print(f"    · {detalle['reunion_id']}: {detalle['error']}")
            else:
                print(
                    f"    · {detalle['reunion_id']}: "
                    f"{detalle['segmentos']} segmentos, "
                    f"{detalle['participantes']} participantes"
                )
    return 0


# ─────────────────────────── consulta ───────────────────────────


def cmd_listar(argumentos: argparse.Namespace) -> int:
    """Lista las reuniones capturadas."""
    from sqlalchemy import select

    from app.core.db.models import ReunionCapturada
    from app.core.db.session import AsyncSessionLocal

    async def _listar() -> list[Any]:
        async with AsyncSessionLocal() as sesion:
            consulta = (
                select(ReunionCapturada)
                .order_by(ReunionCapturada.capturado_en.desc())
                .limit(argumentos.limite)
            )
            if argumentos.proyecto:
                consulta = consulta.where(
                    ReunionCapturada.proyecto_id == uuid.UUID(argumentos.proyecto)
                )
            resultado = await sesion.execute(consulta)
            return list(resultado.scalars())

    filas = asyncio.run(_listar())
    if not filas:
        print("No hay ninguna reunión capturada todavía.")
        return 0

    print(f"{len(filas)} reunión(es) capturada(s)")
    print("=" * 78)
    for fila in filas:
        audio = f"audio {fila.audio_bytes:,} B" if fila.audio_objeto else "sin audio"
        print(
            f"  {fila.reunion_id[:36]:38s} {fila.segmentos_totales:>4} seg  "
            f"{audio:>16s}  {fila.asunto[:30]}"
        )
    return 0


def cmd_ver(argumentos: argparse.Namespace) -> int:
    """Muestra la transcripción de una reunión."""
    from sqlalchemy import select

    from app.core.db.models import ReunionCapturada
    from app.core.db.session import AsyncSessionLocal

    async def _leer() -> Any:
        async with AsyncSessionLocal() as sesion:
            resultado = await sesion.execute(
                select(ReunionCapturada).where(
                    ReunionCapturada.reunion_id == argumentos.reunion_id
                )
            )
            return resultado.scalars().first()

    fila = asyncio.run(_leer())
    if fila is None:
        print(f"No hay ninguna reunión capturada con el identificador {argumentos.reunion_id}.",
              file=sys.stderr)
        return 1

    print(f"Reunión   : {fila.reunion_id}")
    print(f"Asunto    : {fila.asunto}")
    print(f"Organizador: {fila.organizador}")
    print(f"Segmentos : {fila.segmentos_totales}")
    print(f"Participantes: {len(fila.participantes or [])}")
    print(f"Audio     : {fila.audio_objeto or '(sin audio)'}")
    print(f"Huella    : {fila.huella}")
    print("=" * 78)
    for segmento in fila.segmentos or []:
        hablante = segmento.get("hablante", "hablante desconocido")
        print(f"[{hablante}] {segmento.get('texto', '')}")
    return 0


def cmd_json(argumentos: argparse.Namespace) -> int:
    """Vuelca la configuración efectiva en JSON, para revisarla rápido."""
    config = _configuracion()
    datos = {
        "graph_base_url": config.graph_base_url,
        "organizador": config.teams_organizador,
        "ventana_horas": config.teams_captura_ventana_horas,
        "con_audio": config.teams_captura_con_audio,
        "directorio_capturas": config.teams_directorio_capturas,
        "cola": "integraciones",
    }
    print(json.dumps(datos, ensure_ascii=False, indent=2))
    return 0


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    """Punto de entrada de la CLI."""
    analizador = argparse.ArgumentParser(
        description="Captura de reuniones de Teams en Projecta PMO Intelligence"
    )
    sub = analizador.add_subparsers(dest="comando", required=True)

    p_salud = sub.add_parser("salud", help="Estado de la integración")
    p_salud.set_defaults(func=cmd_salud)

    p_capturar = sub.add_parser("capturar", help="Captura las reuniones")
    p_capturar.add_argument("--organizador", help="UPN del organizador")
    p_capturar.add_argument("--organizacion", help="UUID de la organización")
    p_capturar.add_argument("--horas", type=int, help="Ventana hacia atrás, en horas")
    p_capturar.add_argument("--reunion", help="Capturar una reunión concreta")
    p_capturar.add_argument("--base", help="Base de Graph (para el doble)")
    p_capturar.add_argument(
        "--doble",
        action="store_true",
        help="Levanta el doble de Graph y captura contra él.",
    )
    p_capturar.set_defaults(func=cmd_capturar)

    p_listar = sub.add_parser("listar", help="Lista las reuniones capturadas")
    p_listar.add_argument("--proyecto", help="Filtra por proyecto")
    p_listar.add_argument("--limite", type=int, default=50)
    p_listar.set_defaults(func=cmd_listar)

    p_ver = sub.add_parser("ver", help="Muestra la transcripción de una reunión")
    p_ver.add_argument("reunion_id")
    p_ver.set_defaults(func=cmd_ver)

    p_json = sub.add_parser("config", help="Vuelca la configuración efectiva")
    p_json.set_defaults(func=cmd_json)

    argumentos = analizador.parse_args()
    return int(argumentos.func(argumentos))


if __name__ == "__main__":
    raise SystemExit(main())
