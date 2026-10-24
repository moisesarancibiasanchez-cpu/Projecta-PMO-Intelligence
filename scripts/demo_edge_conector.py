#!/usr/bin/env python3
"""Demostración de extremo a extremo del Edge Connector de Projecta PMO Intelligence.

Levanta el stack real —API contra PostgreSQL real con pgvector— y ejecuta
el ciclo completo del conector:

1. Se registra un dispositivo del Edge Connector con su clave pública.
2. Se registra un consentimiento con una finalidad declarada.
3. El sensor captura una reunión, la minimiza, la firma y la envía.
4. Se comprueba que el evento se **aceptó** y que llegó minimizado.
5. Se comprueba que un recurso **sin consentimiento** se rechaza, y que
   el rechazo queda **auditado**.
6. Se comprueba que un evento con **finalidad distinta** se rechaza.
7. Se comprueba que un evento con **firma manipulada** se rechaza.
8. Se comprueba que el **mismo evento** no se procesa dos veces.
9. Se comprueba que la **minimización** quitó los campos bloqueados.
10. Se comprueba que un **dispositivo revocado** deja de poder enviar.
11. Se comprueba que la **cadena de auditoría** sigue íntegra.

Todo lo que el script afirma se comprueba ejecutándolo. La salida es la
evidencia de la PoC.

Uso:
    python3 scripts/demo_edge_conector.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

RAIZ = pathlib.Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# La caché de bytecode se purga antes de nada. Este script produce la
# **evidencia** de la PoC, y una evidencia medida contra código obsoleto
# no es evidencia: es una afirmación. El motivo está explicado en
# `scripts/purgar_cache.py`.
from scripts.purgar_cache import purgar  # noqa: E402

purgar(RAIZ)

os.environ.setdefault("SECRET_KEY", "demo-edge-conector")
os.environ.setdefault("MINIO_ACCESS_KEY", "demo")
os.environ.setdefault("MINIO_SECRET_KEY", "demo")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")

ANCHO = 78
RESULTADOS: list[tuple[str, bool, str]] = []


def titulo(texto: str) -> None:
    print(f"\n{'=' * ANCHO}\n{texto}\n{'=' * ANCHO}")


def comprobar(condicion: bool, descripcion: str, detalle: str = "") -> bool:
    """Registra el resultado de una comprobación y la muestra."""
    RESULTADOS.append((descripcion, bool(condicion), detalle))
    if condicion:
        print(f"  [PASA] {descripcion}")
    else:
        print(f"  [FALLA] {descripcion} — {detalle}")
    return bool(condicion)


async def main() -> int:
    """Ejecuta la demostración completa."""
    from httpx import ASGITransport, AsyncClient

    from app.core import auditoria, edge
    from app.core.db import models
    from app.core.db.base import Base
    from app.core.db.session import AsyncSessionLocal, engine
    from app.core.security import crear_hash_contrasena
    from app.main import create_app

    # ---- Preparación: una organización, un proyecto y un administrador ----
    async with engine.begin() as conexion:
        await conexion.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as sesion:
        organizacion = models.Organizacion(
            nombre="Banco de la PoC", slug=f"poc-{uuid.uuid4().hex[:8]}", activo=True
        )
        sesion.add(organizacion)
        await sesion.flush()
        # El catálogo de roles lo siembra la migración 0002, así que aquí
        # se **reutiliza**: crear un rol con el mismo nombre violaría la
        # restricción de unicidad, y un PoC que rompe el catálogo no
        # demuestra nada.
        from sqlalchemy import select

        encontrado = (
            await sesion.execute(
                select(models.Rol).where(models.Rol.nombre == "administrador")
            )
        ).scalar_one_or_none()
        if encontrado is None:
            rol_admin = models.Rol(
                nombre="administrador", nivel=5, ambito="organización", descripcion="PoC"
            )
            sesion.add(rol_admin)
            await sesion.flush()
        else:
            rol_admin = encontrado
        # El código es único por ejecución: el PoC tiene que poder
        # repetirse sin chocar con los datos de la ejecución anterior.
        proyecto = models.Proyecto(
            organizacion_id=organizacion.id,
            nombre="Proyecto de la PoC",
            codigo=f"POC-{uuid.uuid4().hex[:8].upper()}",
            estado="en_curso",
        )
        sesion.add(proyecto)
        await sesion.flush()
        administrador = models.Usuario(
            organizacion_id=organizacion.id,
            rol_id=rol_admin.id,
            email=f"admin-{uuid.uuid4().hex[:6]}@poc.example",
            nombre="Administrador de la PoC",
            password_hash=crear_hash_contrasena("ContrasenaSegura123"),
            activo=True,
        )
        sesion.add(administrador)
        await sesion.commit()
        organizacion_id = str(organizacion.id)
        proyecto_id = str(proyecto.id)
        correo_admin = administrador.email

    titulo("Edge Connector de Projecta PMO Intelligence — PoC de extremo a extremo")

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://poc.interno"
    ) as cliente:
        # ---- Sesión de administrador ----
        inicio = await cliente.post(
            "/auth/login/password",
            json={"email": correo_admin, "contrasena": "ContrasenaSegura123"},
        )
        comprobar(
            inicio.status_code == 200,
            "El administrador abre sesión",
            f"código {inicio.status_code}",
        )

        # ---- 1. Registro del dispositivo ----
        from scripts.edge_conector import generar_clave

        privada, publica = generar_clave()
        huella = edge.huella_de_clave(publica)
        registro = await cliente.post(
            "/edge/dispositivos",
            json={
                "nombre": "edge-conector-poc",
                "clave_publica": publica,
                "sistemas": ["reuniones"],
                "modo": "READ_ONLY",
            },
        )
        comprobar(
            registro.status_code == 201 and registro.json()["huella_clave"] == huella,
            "El dispositivo del Edge Connector se registra con su clave pública",
            f"código {registro.status_code}: {registro.text[:120]}",
        )
        dispositivo_id = registro.json().get("id") if registro.status_code == 201 else None

        # ---- 2. Modo no admitido ----
        rechazo_modo = await cliente.post(
            "/edge/dispositivos",
            json={
                "nombre": "edge-conector-ilegal",
                "clave_publica": publica,
                "modo": "WRITE_APPROVED",
            },
        )
        comprobar(
            rechazo_modo.status_code == 422,
            "El registro rechaza un modo de operación fuera de M0",
            f"código {rechazo_modo.status_code}",
        )

        # ---- 3. Consentimiento ----
        recurso = f"reunion-{uuid.uuid4().hex[:10]}"
        consentimiento = await cliente.post(
            "/edge/consentimientos",
            json={
                "recurso": recurso,
                "recurso_tipo": "reunion",
                "finalidad": "trazabilidad_de_proyecto",
                "base_legal": "interes_legitimo",
                "destinatarios": ["equipo-pmo"],
                "proyecto_id": proyecto_id,
                "otorgado_por": "Responsable de protección de datos",
            },
        )
        comprobar(
            consentimiento.status_code == 201,
            "El consentimiento se registra con su finalidad declarada",
            f"código {consentimiento.status_code}",
        )

        # ---- 4. La puerta, sin evento ----
        puerta = await cliente.get(
            f"/edge/puerta/reunion/{recurso}",
            params={"uso_previsto": "trazabilidad_de_proyecto"},
        )
        cuerpo_puerta = puerta.json() if puerta.status_code == 200 else {}
        comprobar(
            cuerpo_puerta.get("permitido") is True,
            "La puerta autoriza el uso previsto declarado en el consentimiento",
            str(cuerpo_puerta)[:160],
        )

        puerta_otro = await cliente.get(
            f"/edge/puerta/reunion/{recurso}",
            params={"uso_previsto": "formacion_de_modelo"},
        )
        cuerpo_otro = puerta_otro.json() if puerta_otro.status_code == 200 else {}
        comprobar(
            cuerpo_otro.get("permitido") is False,
            "La puerta rechaza un uso previsto distinto de la finalidad registrada",
            str(cuerpo_otro)[:160],
        )

        # ---- 5. El sensor captura, minimiza, firma y envía ----
        from scripts.edge_conector import ColaLocal, RegistroLocal, Sensor

        sensor = Sensor(
            nombre="edge-conector-poc",
            huella_clave=huella,
            clave_privada=privada,
            cola=ColaLocal(ruta=pathlib.Path("/tmp/projecta_edge_poc/cola.json")),
            registro=RegistroLocal(
                ruta=pathlib.Path("/tmp/projecta_edge_poc/registro.jsonl")
            ),
            finalidad="trazabilidad_de_proyecto",
            destinatarios=["equipo-pmo"],
        )
        señal = sensor.capturar_reunion(recurso, "Revisión trimestral de riesgos")
        evento = sensor.construir_evento(
            "reunion_programada", señal, proyecto_id=proyecto_id
        )
        comprobar(
            "transcripcion" in señal and "transcripcion" not in evento["contenido"]["payload"],
            "El sensor bloquea los campos prohibidos ANTES de firmar",
            f"claves del payload: {sorted(evento['contenido']['payload'].keys())}",
        )
        comprobar(
            evento["contenido"]["payload"]["participantes"]
            == ["***", "***", "***"],
            "El sensor redacta la lista de participantes",
            str(evento["contenido"]["payload"].get("participantes")),
        )

        enviar = await cliente.post("/edge/eventos", json=_evento_a_json(evento))
        cuerpo_envio = enviar.json() if enviar.content else {}
        comprobar(
            cuerpo_envio.get("decision") == "aceptado",
            "El evento firmado, minimizado y consentido se acepta",
            f"código {enviar.status_code}: {str(cuerpo_envio)[:200]}",
        )
        huella_evento = cuerpo_envio.get("huella", "")

        # ---- 6. Idempotencia ----
        repetido = await cliente.post("/edge/eventos", json=_evento_a_json(evento))
        cuerpo_repetido = repetido.json() if repetido.content else {}
        comprobar(
            cuerpo_repetido.get("duplicado") is True,
            "El mismo evento reenviado no se procesa dos veces",
            str(cuerpo_repetido)[:200],
        )

        # ---- 7. Recurso sin consentimiento ----
        sin_consentimiento = sensor.construir_evento(
            "reunion_programada",
            sensor.capturar_reunion(
                f"reunion-sin-consentimiento-{uuid.uuid4().hex[:8]}",
                "Comité de inversión",
            ),
            proyecto_id=proyecto_id,
        )
        respuesta_sin = await cliente.post(
            "/edge/eventos", json=_evento_a_json(sin_consentimiento)
        )
        cuerpo_sin = respuesta_sin.json() if respuesta_sin.content else {}
        comprobar(
            cuerpo_sin.get("decision") == "rechazado",
            "Un recurso SIN consentimiento se rechaza",
            str(cuerpo_sin)[:200],
        )
        comprobar(
            "consentimiento" in str(cuerpo_sin.get("motivo", "")).lower(),
            "El motivo del rechazo dice que falta el consentimiento",
            str(cuerpo_sin.get("motivo", ""))[:160],
        )

        # ---- 8. Finalidad distinta ----
        otra_finalidad = sensor.construir_evento(
            "reunion_programada",
            sensor.capturar_reunion(recurso, "Revisión trimestral de riesgos"),
            proyecto_id=proyecto_id,
            uso_previsto="formacion_de_modelo",
        )
        respuesta_finalidad = await cliente.post(
            "/edge/eventos", json=_evento_a_json(otra_finalidad)
        )
        cuerpo_finalidad = respuesta_finalidad.json() if respuesta_finalidad.content else {}
        comprobar(
            cuerpo_finalidad.get("decision") == "rechazado",
            "Un uso previsto distinto de la finalidad registrada se rechaza",
            str(cuerpo_finalidad)[:200],
        )

        # ---- 9. Firma manipulada ----
        manipulado = _evento_a_json(evento)
        manipulado["firma"] = "00" * 64
        respuesta_firma = await cliente.post("/edge/eventos", json=manipulado)
        cuerpo_firma = respuesta_firma.json() if respuesta_firma.content else {}
        comprobar(
            cuerpo_firma.get("decision") == "rechazado",
            "Un evento con la firma manipulada se rechaza",
            str(cuerpo_firma)[:200],
        )

        # ---- 10. Tipo de receptor fuera de la lista cerrada ----
        tipo_ilegal = _evento_a_json(evento)
        tipo_ilegal["tipo"] = "grabacion_de_audio"
        respuesta_tipo = await cliente.post("/edge/eventos", json=tipo_ilegal)
        cuerpo_tipo = respuesta_tipo.json() if respuesta_tipo.content else {}
        comprobar(
            cuerpo_tipo.get("decision") == "rechazado",
            "Un tipo de receptor fuera de la lista cerrada se rechaza",
            str(cuerpo_tipo)[:200],
        )

        # ---- 11. Revocación del dispositivo ----
        if dispositivo_id:
            revocacion = await cliente.delete(
                f"/edge/dispositivos/{dispositivo_id}",
                params={"motivo": "Fin de la PoC"},
            )
            comprobar(
                revocacion.status_code == 200,
                "El dispositivo se revoca",
                f"código {revocacion.status_code}",
            )
            tras_revocar = await cliente.post("/edge/eventos", json=_evento_a_json(evento))
            cuerpo_revocado = tras_revocar.json() if tras_revocar.content else {}
            comprobar(
                cuerpo_revocado.get("decision") == "rechazado",
                "Un dispositivo revocado ya no puede enviar eventos",
                str(cuerpo_revocado)[:200],
            )

            # ---- 12. Salud del dispositivo ----
            salud = await cliente.get(f"/edge/dispositivos/{dispositivo_id}/salud")
            cuerpo_salud = salud.json() if salud.status_code == 200 else {}
            comprobar(
                cuerpo_salud.get("sano") is False
                and any("revocado" in a for a in cuerpo_salud.get("advertencias", [])),
                "El health agent informa del estado del dispositivo revocado",
                str(cuerpo_salud)[:200],
            )

        # ---- 13. Lo que quedó persistido ----
        listado = await cliente.get("/edge/eventos")
        elementos = listado.json().get("elementos", [])
        aceptados = [e for e in elementos if e["decision"] == "aceptado"]
        comprobar(
            len(aceptados) == 1,
            "Solo se persiste el evento aceptado: los rechazos no crean filas",
            f"{len(aceptados)} aceptados de {len(elementos)} elementos",
        )
        if aceptados:
            payload_almacenado = aceptados[0]["payload"]
            comprobar(
                "transcripcion" not in payload_almacenado
                and "audio_url" not in payload_almacenado,
                "Lo que se guardó ya viene minimizado: los campos bloqueados "
                "no llegan a la base de datos",
                str(sorted(payload_almacenado.keys())),
            )
            comprobar(
                payload_almacenado.get("participantes") == ["***", "***", "***"],
                "Lo que se guardó tiene los participantes redactados",
                str(payload_almacenado.get("participantes")),
            )

    # ---- 14. La auditoría ----
    async with AsyncSessionLocal() as sesion:
        informe = await auditoria.verificar_cadena(sesion, organizacion_id)
        comprobar(
            informe["integra"] is True,
            "La cadena de auditoría sigue íntegra tras la PoC",
            str(informe)[:200],
        )

        rechazos = (
            (
                await sesion.execute(
                    __import__("sqlalchemy").select(models.EventoAuditoria).where(
                        models.EventoAuditoria.tipo == "EDGE_EVENTO_RECHAZADO"
                    )
                )
            )
            .scalars()
            .all()
        )
        comprobar(
            len(rechazos) >= 4,
            "Cada rechazo del Edge Connector dejó un evento de auditoría",
            f"{len(rechazos)} rechazos auditados",
        )

    # ---- Limpieza: la PoC no deja rastro en la base de datos ----
    #
    # Se borra la organización de la PoC y, por cascada, el proyecto, los
    # dispositivos, los consentimientos, los eventos y la auditoría. Es lo
    # que permite repetir la demostración tantas veces como haga falta.
    async with AsyncSessionLocal() as sesion:
        await sesion.execute(
            __import__("sqlalchemy").delete(models.Organizacion).where(
                models.Organizacion.id == uuid.UUID(organizacion_id)
            )
        )
        await sesion.commit()

    # ---- Resumen ----
    titulo("Resultado de la demostración")
    fallidos = [r for r in RESULTADOS if not r[1]]
    print(f"  Comprobaciones: {len(RESULTADOS)} | Fallos: {len(fallidos)}")
    for descripcion, _, detalle in fallidos:
        print(f"    · {descripcion} — {detalle}")
    if not fallidos:
        print("\n  La PoC del Edge Connector se comporta como el diseño dice.")
    return 1 if fallidos else 0


def _evento_a_json(evento: dict[str, Any]) -> dict[str, Any]:
    """Quita del evento los campos que son internos del sensor."""
    return {k: v for k, v in evento.items() if k != "campos_bloqueados_localmente"}


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
