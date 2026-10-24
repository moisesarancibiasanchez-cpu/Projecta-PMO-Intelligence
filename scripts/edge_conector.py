#!/usr/bin/env python3
"""Edge Connector de Projecta PMO Intelligence — el sensor, lado banco.

Este script es lo que se ejecuta **dentro del perímetro del banco**, en
la red de integraciones. Es un **sensor con contrato, no un ejecutor**:
captura un conjunto cerrado de señales, las minimiza, las firma con la
clave Ed25519 del dispositivo y las envía. No decide nada: el servidor
decide.

Lo que hace, en orden:

1. **Capture**: lee una señal del sistema fuente. En la PoC, el adaptador
   único es un simulador local de reuniones, porque un PoC que depende de
   un tenant real no se puede demostrar.
2. **Minimiza**: aplica la misma lista de campos bloqueados que el
   servidor. Se minimiza **en el origen**, no solo en el destino: si el
   dato no sale del banco, no puede filtrarse por el camino.
3. **Firma**: firma el contenido canónico con Ed25519.
4. **Encola**: guarda el evento en una cola persistente local, con
   reintentos y cola muerta.
5. **Envía**: entrega la cola a Projecta PMO Intelligence.

Uso:

    python3 scripts/edge_conector.py generar-clave
    python3 scripts/edge_conector.py enviar --dispositivo <huella> ...
    python3 scripts/edge_conector.py demostrar
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

RAIZ = pathlib.Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

#: Directorio de estado local del conector: claves, cola y registro local.
ESTADO = pathlib.Path(os.environ.get("EDGE_ESTADO", "/tmp/projecta_edge"))

#: Tamaño máximo de la cola local antes de que empiece la cola muerta.
MAXIMO_COLA = 100

#: Número de reintentos de envío antes de mandar el evento a la cola
#: muerta.
MAXIMO_REINTENTOS = 3


class ErrorDelConector(Exception):
    """El conector no puede cumplir lo que se le pide."""


# ─────────────────────────── estado local ───────────────────────────


@dataclass
class ColaLocal:
    """Cola persistente local de eventos pendientes de envío.

    Vive en un fichero JSON: si el proceso muere, la cola sigue ahí. Es
    el componente que evita perder un evento cuando el destino no está
    disponible.
    """

    ruta: pathlib.Path

    def cargar(self) -> list[dict[str, Any]]:
        if not self.ruta.exists():
            return []
        try:
            return json.loads(self.ruta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def guardar(self, elementos: list[dict[str, Any]]) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(
            json.dumps(elementos, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def encolar(self, evento: dict[str, Any]) -> None:
        elementos = self.cargar()
        elementos.append(evento)
        if len(elementos) > MAXIMO_COLA:
            # Lo que no cabe va a la cola muerta: no se descarta nada en
            # silencio.
            muertos = self.cargar_muertos()
            muertos.extend(elementos[:-MAXIMO_COLA])
            self.guardar_muertos(muertos)
            elementos = elementos[-MAXIMO_COLA:]
        self.guardar(elementos)

    def desencolar(self) -> dict[str, Any] | None:
        elementos = self.cargar()
        if not elementos:
            return None
        primero = elementos.pop(0)
        self.guardar(elementos)
        return primero

    def ruta_muertos(self) -> pathlib.Path:
        return self.ruta.with_name("cola_muerta.json")

    def cargar_muertos(self) -> list[dict[str, Any]]:
        ruta = self.ruta_muertos()
        if not ruta.exists():
            return []
        try:
            return json.loads(ruta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def guardar_muertos(self, elementos: list[dict[str, Any]]) -> None:
        self.ruta_muertos().parent.mkdir(parents=True, exist_ok=True)
        self.ruta_muertos().write_text(
            json.dumps(elementos, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def mover_a_muerta(self, evento: dict[str, Any], motivo: str) -> None:
        muertos = self.cargar_muertos()
        evento = dict(evento)
        evento["motivo_muerta"] = motivo
        muertos.append(evento)
        self.guardar_muertos(muertos)


@dataclass
class RegistroLocal:
    """Registro local de lecturas y envíos.

    Complementa la auditoría del servidor: el banco conserva su propia
    copia de lo que el conector leyó y envió. Si mañana se discute qué
    salió del perímetro, la respuesta no depende solo del servidor.
    """

    ruta: pathlib.Path

    def anotar(self, accion: str, detalle: dict[str, Any]) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        registro = {
            "momento": datetime.now(timezone.utc).isoformat(),
            "accion": accion,
            **detalle,
        }
        with self.ruta.open("a", encoding="utf-8") as fichero:
            fichero.write(json.dumps(registro, ensure_ascii=False) + "\n")

    def leer(self) -> list[dict[str, Any]]:
        if not self.ruta.exists():
            return []
        lineas = []
        for linea in self.ruta.read_text(encoding="utf-8").splitlines():
            if linea.strip():
                try:
                    lineas.append(json.loads(linea))
                except json.JSONDecodeError:
                    continue
        return lineas


# ─────────────────────────── claves ───────────────────────────


def generar_clave() -> tuple[bytes, str]:
    """Genera un par de claves Ed25519. Devuelve la privada y la pública."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    privada = Ed25519PrivateKey.generate()
    privada_bytes = privada.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    publica = privada.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return privada_bytes, publica.hex()


def guardar_clave_privada(nombre: str, clave: bytes) -> pathlib.Path:
    """Guarda la clave privada en el estado local, con permisos 0600.

    La clave privada nunca sale del perímetro: por eso se guarda en el
    disco del conector y no se transmite nunca.
    """
    ESTADO.mkdir(parents=True, exist_ok=True)
    ruta = ESTADO / f"{nombre}.key"
    ruta.write_bytes(clave)
    ruta.chmod(0o600)
    return ruta


def cargar_clave_privada(nombre: str) -> bytes:
    """Carga la clave privada del estado local."""
    ruta = ESTADO / f"{nombre}.key"
    if not ruta.exists():
        raise ErrorDelConector(
            f"no hay clave privada para '{nombre}' en {ruta}. "
            f"Genera una con: python3 scripts/edge_conector.py generar-clave"
        )
    return ruta.read_bytes()


# ─────────────────────────── el sensor ───────────────────────────


@dataclass
class Sensor:
    """El sensor del Edge Connector, con su contrato."""

    nombre: str
    huella_clave: str
    clave_privada: bytes
    cola: ColaLocal
    registro: RegistroLocal
    #: La **finalidad** que el conector va a proponer. La puerta la
    #: compara con la finalidad registrada en el consentimiento.
    finalidad: str = "trazabilidad_de_proyecto"
    destinatarios: list[str] = field(default_factory=list)
    #: Sistemas fuente que el conector tiene habilitados. La política
    #: local acota lo que se puede leer.
    sistemas: list[str] = field(default_factory=lambda: ["reuniones"])
    proyectos_permitidos: list[str] = field(default_factory=list)

    # -- captura ------------------------------------------------------
    def capturar_reunion(self, identificador: str, titulo: str) -> dict[str, Any]:
        """Captura una reunión del adaptador local.

        El PoC **no** captura audio ni transcripción: el adaptador solo
        produce metadatos. Esa es la diferencia entre un sensor y un
        agente que graba.
        """
        if "reuniones" not in self.sistemas:
            raise ErrorDelConector(
                "la política local no habilita el sistema 'reuniones' "
                "para este conector: no se lee nada."
            )
        señal = {
            "recurso_id": identificador,
            "recurso_tipo": "reunion",
            "titulo": titulo,
            "inicio": datetime.now(timezone.utc).isoformat(),
            "participantes": ["persona-1", "persona-2", "persona-3"],
            "duracion_minutos": 45,
            # Campos que el sensor NUNCA debe leer, pero que se incluyen
            # a propósito para comprobar que la minimización funciona.
            "transcripcion": "contenido que no debe salir del perímetro",
            "audio_url": "https://sistema-fuente.example/audio/123",
        }
        self.registro.anotar(
            "lectura",
            {
                "recurso": identificador,
                "campos_leidos": sorted(señal.keys()),
            },
        )
        return señal

    # -- minimización -------------------------------------------------
    def minimizar(self, señal: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Minimiza la señal en el origen, antes de que salga.

        Se aplica la misma lista de campos bloqueados que el servidor.
        Minimizar en los dos extremos es deliberado: si el dato no sale
        del perímetro, no puede filtrarse por el camino.
        """
        from app.core.edge import CAMPOS_BLOQUEADOS, CAMPO_REDACTADO, MASCARA_REDACCION

        quitados: list[str] = []
        resultado: dict[str, Any] = {}
        for clave, valor in señal.items():
            if clave.strip().lower() in CAMPOS_BLOQUEADOS:
                quitados.append(clave)
                continue
            if clave == CAMPO_REDACTADO and isinstance(valor, list):
                resultado[clave] = [MASCARA_REDACCION] * len(valor)
                continue
            resultado[clave] = valor
        self.registro.anotar(
            "minimizacion",
            {"campos_bloqueados": quitados, "campos_resultantes": sorted(resultado.keys())},
        )
        return resultado, sorted(quitados)

    # -- firma --------------------------------------------------------
    def firmar(self, contenido: dict[str, Any]) -> str:
        """Firma el contenido canónico con la clave del dispositivo."""
        from app.core.edge import firma_de_evento

        return firma_de_evento(self.clave_privada, contenido)

    # -- construcción del evento --------------------------------------
    def construir_evento(
        self,
        tipo: str,
        señal: dict[str, Any],
        *,
        proyecto_id: str | None = None,
        uso_previsto: str | None = None,
    ) -> dict[str, Any]:
        """Construye el evento firmado, listo para enviar."""
        from app.core.edge import RECEPTORES_ADMITIDOS

        if tipo not in RECEPTORES_ADMITIDOS:
            raise ErrorDelConector(
                f"tipo de receptor no admitido: '{tipo}'. "
                f"La lista de receptores es cerrada."
            )
        payload, quitados = self.minimizar(señal)
        contenido = {
            "tipo": tipo,
            "payload": payload,
            "recurso": señal["recurso_id"],
            "recurso_tipo": señal["recurso_tipo"],
            "proyecto_id": proyecto_id,
            "uso_previsto": uso_previsto or self.finalidad,
            "destinatarios": self.destinatarios,
            "marca_tiempo": datetime.now(timezone.utc).isoformat(),
        }
        firma = self.firmar(contenido)
        evento = {
            "huella_clave": self.huella_clave,
            "tipo": tipo,
            "contenido": contenido,
            "firma": firma,
            "marca_tiempo": contenido["marca_tiempo"],
            "uso_previsto": contenido["uso_previsto"],
            "recurso": contenido["recurso"],
            "recurso_tipo": contenido["recurso_tipo"],
            "proyecto_id": proyecto_id,
            "destinatarios_previstos": self.destinatarios,
            "correlation_id": str(uuid.uuid4()),
            "campos_bloqueados_localmente": quitados,
        }
        self.registro.anotar(
            "preparacion",
            {
                "tipo": tipo,
                "recurso": contenido["recurso"],
                "campos_bloqueados_localmente": quitados,
            },
        )
        return evento

    # -- envío --------------------------------------------------------
    def enviar(self, url: str, evento: dict[str, Any]) -> dict[str, Any]:
        """Envía un evento al servidor, con reintentos y cola muerta."""
        import httpx

        url_eventos = url.rstrip("/") + "/edge/eventos"
        cuerpo = {k: v for k, v in evento.items() if k != "campos_bloqueados_localmente"}
        ultimo_error = ""
        for intento in range(1, MAXIMO_REINTENTOS + 1):
            try:
                respuesta = httpx.post(url_eventos, json=cuerpo, timeout=15.0)
                respuesta.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                ultimo_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5 * intento)
                continue
            resultado = respuesta.json()
            self.registro.anotar(
                "envio",
                {
                    "recurso": evento["recurso"],
                    "decision": resultado.get("decision"),
                    "codigo": respuesta.status_code,
                    "intento": intento,
                },
            )
            return resultado
        self.cola.mover_a_muerta(evento, ultimo_error)
        self.registro.anotar(
            "cola_muerta",
            {"recurso": evento["recurso"], "motivo": ultimo_error},
        )
        return {"decision": "cola_muerta", "motivo": ultimo_error}


# ─────────────────────────── demostración ───────────────────────────


def demostrar(url: str) -> int:
    """Demostración de extremo a extremo del Edge Connector.

    Ejecuta el ciclo completo —capturar, minimizar, firmar, enviar— y
    comprueba los cuatro resultados que importan: lo que se acepta, lo
    que la puerta rechaza, lo que la minimización quita y lo que la
    idempotencia evita.
    """
    from app.core.edge import huella_de_clave

    ESTADO.mkdir(parents=True, exist_ok=True)
    cola = ColaLocal(ruta=ESTADO / "cola.json")
    registro = RegistroLocal(ruta=ESTADO / "registro_local.jsonl")

    print("=" * 78)
    print("Edge Connector de Projecta PMO Intelligence — demostración de la PoC")
    print("=" * 78)

    # 1. El dispositivo genera su identidad.
    privada, publica = generar_clave()
    guardar_clave_privada("dispositivo-poc", privada)
    huella = huella_de_clave(publica)
    sensor = Sensor(
        nombre="edge-conector-poc",
        huella_clave=huella,
        clave_privada=privada,
        cola=cola,
        registro=registro,
    )
    print(f"\n[1] Dispositivo generado")
    print(f"    nombre        : {sensor.nombre}")
    print(f"    clave pública : {publica[:32]}…")
    print(f"    huella        : {huella[:32]}…")
    print(f"    la clave privada se queda en {ESTADO} (permisos 0600)")

    # 2. Captura y minimización en el origen.
    print(f"\n[2] Captura y minimización en el origen")
    señal = sensor.capturar_reunion(
        "reunion-2026-10-23-001", "Revisión trimestral de riesgos"
    )
    payload, quitados = sensor.minimizar(señal)
    print(f"    campos leídos      : {sorted(señal.keys())}")
    print(f"    campos transmitidos: {sorted(payload.keys())}")
    print(f"    campos bloqueados  : {quitados}")
    print(f"    participantes       : {payload['participantes']} (redactados)")

    # 3. Firma.
    evento = sensor.construir_evento("reunion_programada", señal)
    print(f"\n[3] Evento firmado")
    print(f"    tipo       : {evento['tipo']}")
    print(f"    firma      : {evento['firma'][:48]}… ({len(evento['firma'])//2} bytes)")
    print(f"    correlación: {evento['correlation_id']}")

    # 4. Envío al servidor.
    print(f"\n[4] Envío a {url}")
    resultado = sensor.enviar(url, evento)
    print(f"    decisión   : {resultado.get('decision')}")
    print(f"    motivo     : {resultado.get('motivo')}")
    print(f"    huella     : {resultado.get('huella', '')[:32]}…")

    # 5. Idempotencia: el mismo evento, otra vez.
    print(f"\n[5] Reenvío del mismo evento (idempotencia)")
    repetido = sensor.enviar(url, evento)
    print(f"    decisión   : {repetido.get('decision')}")
    print(f"    duplicado  : {repetido.get('duplicado')}")
    print(f"    motivo     : {repetido.get('motivo')}")

    # 6. La puerta: un recurso sin consentimiento.
    print(f"\n[6] La puerta del consentimiento ante un recurso sin registro")
    sin_consentimiento = sensor.construir_evento(
        "reunion_programada",
        sensor.capturar_reunion("reunion-sin-consentimiento", "Comité de inversión"),
    )
    respuesta_puerta = sensor.enviar(url, sin_consentimiento)
    print(f"    decisión   : {respuesta_puerta.get('decision')}")
    print(f"    motivo     : {respuesta_puerta.get('motivo')}")

    # 7. El registro local.
    print(f"\n[7] Registro local del conector (copia del banco)")
    for entrada in registro.leer():
        extra = entrada.get("campos_bloqueados") or entrada.get("campos_bloqueados_localmente")
        detalle = f" · {entrada['accion']}"
        if extra:
            detalle += f" · bloqueados: {extra}"
        if entrada.get("decision"):
            detalle += f" · {entrada['decision']}"
        print(f"    {entrada['momento'][:19]}{detalle}")

    print(f"\n{'=' * 78}")
    print("Demostración completada.")
    print(f"{'=' * 78}")
    return 0


def main() -> int:
    """Punto de entrada del conector."""
    analizador = argparse.ArgumentParser(
        description="Edge Connector de Projecta PMO Intelligence — sensor del lado banco."
    )
    sub = analizador.add_subparsers(dest="comando", required=True)

    sub.add_parser("generar-clave", help="Genera un par de claves Ed25519.")

    enviar = sub.add_parser("enviar", help="Captura, minimiza, firma y envía un evento.")
    enviar.add_argument("--url", default="http://127.0.0.1:8000", help="URL del servidor.")
    enviar.add_argument("--dispositivo", default="dispositivo-poc", help="Nombre del dispositivo.")
    enviar.add_argument("--recurso", default="reunion-2026-10-23-001", help="Identificador del recurso.")
    enviar.add_argument("--titulo", default="Reunión de prueba", help="Título de la reunión.")
    enviar.add_argument(
        "--finalidad", default="trazabilidad_de_proyecto", help="Uso previsto que se propone."
    )
    enviar.add_argument("--proyecto", default=None, help="Identificador del proyecto.")

    demostrar_cmd = sub.add_parser("demostrar", help="Demostración de extremo a extremo.")
    demostrar_cmd.add_argument(
        "--url", default="http://127.0.0.1:8000", help="URL del servidor."
    )

    argumentos = analizador.parse_args()

    if argumentos.comando == "generar-clave":
        from app.core.edge import huella_de_clave

        privada, publica = generar_clave()
        ruta = guardar_clave_privada("dispositivo-poc", privada)
        print(f"Clave Ed25519 generada.")
        print(f"  clave pública : {publica}")
        print(f"  huella        : {huella_de_clave(publica)}")
        print(f"  clave privada : {ruta} (permisos 0600, no se transmite nunca)")
        return 0

    if argumentos.comando == "enviar":
        from app.core.edge import huella_de_clave

        privada = cargar_clave_privada(argumentos.dispositivo)
        sensor = Sensor(
            nombre=argumentos.dispositivo,
            huella_clave=huella_de_clave(clave_privada_hex(privada)),
            clave_privada=privada,
            cola=ColaLocal(ruta=ESTADO / "cola.json"),
            registro=RegistroLocal(ruta=ESTADO / "registro_local.jsonl"),
            finalidad=argumentos.finalidad,
        )
        señal = sensor.capturar_reunion(argumentos.recurso, argumentos.titulo)
        evento = sensor.construir_evento(
            "reunion_programada", señal, proyecto_id=argumentos.proyecto
        )
        resultado = sensor.enviar(argumentos.url, evento)
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
        return 0 if resultado.get("decision") == "aceptado" else 1

    if argumentos.comando == "demostrar":
        return demostrar(argumentos.url)

    return 1


def clave_privada_hex(privada: bytes) -> str:
    """Deriva la clave pública a partir de la privada."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    publica = Ed25519PrivateKey.from_private_bytes(privada).public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return publica.hex()


if __name__ == "__main__":
    sys.exit(main())
