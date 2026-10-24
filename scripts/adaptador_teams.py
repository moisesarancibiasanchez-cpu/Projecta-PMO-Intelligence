#!/usr/bin/env python3
"""Interfaz de línea de comandos del adaptador de Microsoft Teams.

Esta es la **capa de operación** del Edge Connector de Projecta PMO
Intelligence, no su implementación. La implementación vive en
``app/core/edge_teams.py``, dentro del proceso de la aplicación, que es
lo que corresponde a la Variante A: todo dentro del perímetro del banco,
sin producto aparte y sin puerto.

La CLI existe para las tres cosas que hay que poder hacer desde la línea
de órdenes, normalmente en el banco y durante un piloto:

* ``probar-token`` — comprobar que la identidad funciona.
* ``leer-chat`` — ver lo que el adaptador lee y cómo lo minimiza, campo
  por campo, **antes** de enviar nada a ninguna parte.
* ``cursors`` — ver hasta dónde ha llegado la sincronización.

Y para la Variante B, en la que el conector se despliega como un proceso
independiente dentro de la red del banco, ``entregar`` construye y envía
los eventos firmados a una instancia de Projecta PMO Intelligence.

Uso:
    python3 scripts/adaptador_teams.py probar-token --tenant ... --client-id ...
    python3 scripts/adaptador_teams.py leer-chat --chat-id ...
    python3 scripts/adaptador_teams.py cursors
    python3 scripts/adaptador_teams.py entregar --chat-id ... --url ...
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from scripts.purgar_cache import purgar  # noqa: E402

purgar(RAIZ)

from app.core.edge_teams import AdaptadorTeams, token_graph  # noqa: E402


def _estado() -> pathlib.Path:
    """Directorio de estado local: cursores delta y claves."""
    return pathlib.Path(os.environ.get("EDGE_ESTADO", "/tmp/projecta_edge"))


def _sensor_cli(estado: pathlib.Path):
    """Construye el sensor de la Variante B, con su cola y su registro."""
    from scripts.edge_conector import (
        ColaLocal,
        ErrorDelConector,
        RegistroLocal,
        Sensor,
    )
    from app.core.edge import huella_de_clave

    try:
        from scripts.edge_conector import (
            cargar_clave_privada,
            clave_privada_hex,
        )

        privada = cargar_clave_privada("dispositivo-poc")
        huella = huella_de_clave(clave_privada_hex(privada))
    except ErrorDelConector:
        privada = b"0" * 32
        huella = "0" * 64
    return Sensor(
        nombre="edge-conector-teams",
        huella_clave=huella,
        clave_privada=privada,
        cola=ColaLocal(ruta=estado / "cola.json"),
        registro=RegistroLocal(ruta=estado / "registro_local.jsonl"),
    )


def main() -> int:
    """Punto de entrada de la línea de comandos."""
    analizador = argparse.ArgumentParser(
        description=(
            "Adaptador de Microsoft Teams del Edge Connector de Projecta PMO Intelligence."
        )
    )
    sub = analizador.add_subparsers(dest="comando", required=True)

    cursores = sub.add_parser("cursors", help="Muestra los cursores delta guardados.")
    cursores.add_argument("--estado", default=None, help="Directorio de estado local.")

    token = sub.add_parser("probar-token", help="Consigue un token de Graph y lo comprueba.")
    token.add_argument("--tenant", required=True, help="Identificador del tenant de Entra ID.")
    token.add_argument("--client-id", required=True, help="Client ID de la aplicación.")
    token.add_argument("--certificado", required=True, help="Ruta del certificado X.509 en PEM.")
    token.add_argument("--clave", required=True, help="Ruta de la clave privada en PEM.")

    leer = sub.add_parser("leer-chat", help="Lee un chat y muestra las señales minimizadas.")
    leer.add_argument("--chat-id", required=True, help="Identificador del chat.")
    leer.add_argument("--tenant", required=True)
    leer.add_argument("--client-id", required=True)
    leer.add_argument("--certificado", required=True)
    leer.add_argument("--clave", required=True)
    leer.add_argument("--canal-permitido", default=None)
    leer.add_argument("--estado", default=None)

    entregar = sub.add_parser(
        "entregar", help="Lee, minimiza, firma, encola y envía a Projecta PMO Intelligence."
    )
    entregar.add_argument("--chat-id", required=True, help="Identificador del chat.")
    entregar.add_argument("--tenant", required=True)
    entregar.add_argument("--client-id", required=True)
    entregar.add_argument("--certificado", required=True)
    entregar.add_argument("--clave", required=True)
    entregar.add_argument("--canal-permitido", default=None)
    entregar.add_argument("--estado", default=None)
    entregar.add_argument("--url", required=True, help="URL de la API de Projecta PMO Intelligence.")
    entregar.add_argument("--proyecto", default=None, help="Identificador del proyecto.")

    argumentos = analizador.parse_args()
    estado = (
        pathlib.Path(argumentos.estado)
        if getattr(argumentos, "estado", None)
        else _estado()
    )
    estado.mkdir(parents=True, exist_ok=True)

    if argumentos.comando == "cursors":
        encontrados = sorted(estado.glob("cursor_*.json"))
        if not encontrados:
            print("No hay cursores guardados: todavía no se ha leído ningún recurso.")
            return 0
        for fichero in encontrados:
            datos = json.loads(fichero.read_text(encoding="utf-8"))
            print(f"{fichero.name}: {datos.get('actualizado_en')} · cursor guardado")
        return 0

    token_obtenido = token_graph(
        tenant_id=argumentos.tenant,
        client_id=argumentos.client_id,
        certificado=pathlib.Path(argumentos.certificado).read_text(encoding="utf-8"),
        clave_privada_pem=pathlib.Path(argumentos.clave).read_text(encoding="utf-8"),
    )

    if argumentos.comando == "probar-token":
        import httpx

        respuesta = httpx.get(
            "https://graph.microsoft.com/v1.0/organization",
            headers={"Authorization": f"Bearer {token_obtenido}"},
            timeout=30.0,
        )
        print(f"Token conseguido. `/organization` responde {respuesta.status_code}.")
        if respuesta.status_code == 200:
            cuerpo = respuesta.json()
            print(f"Organización: {cuerpo.get('displayName')} ({cuerpo.get('id')})")
        return 0 if respuesta.status_code == 200 else 1

    sensor = _sensor_cli(estado)

    if argumentos.comando == "leer-chat":
        adaptador = AdaptadorTeams(
            token=token_obtenido,
            estado=estado,
            canal_permitido=argumentos.canal_permitido,
            registrar=lambda accion, detalle: sensor.registro.anotar(accion, detalle),
        )
        señales = adaptador.leer_chat(argumentos.chat_id)
        print(f"Señales leídas: {len(señales)}")
        for señal in señales[:5]:
            print(json.dumps(señal, ensure_ascii=False, indent=2)[:600])
        return 0

    if argumentos.comando == "entregar":
        adaptador = AdaptadorTeams(
            token=token_obtenido,
            estado=estado,
            huella_clave=sensor.huella_clave,
            clave_privada=sensor.clave_privada,
            canal_permitido=argumentos.canal_permitido,
            registrar=lambda accion, detalle: sensor.registro.anotar(accion, detalle),
        )
        señales = adaptador.leer_chat(argumentos.chat_id)
        eventos = adaptador.construir_eventos(señales, proyecto_id=argumentos.proyecto)
        print(f"Señales leídas: {len(señales)} | eventos construidos: {len(eventos)}")
        decisiones = []
        for evento in eventos:
            resultado = sensor.enviar(argumentos.url, evento)
            decisiones.append(resultado.get("decision"))
        resumen: dict[str, int] = {}
        for decision in decisiones:
            resumen[str(decision)] = resumen.get(str(decision), 0) + 1
        print(f"Decisiones del servidor: {resumen}")
        return 0 if all(d == "aceptado" for d in decisiones) else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
