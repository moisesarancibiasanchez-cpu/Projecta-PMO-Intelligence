"""Adaptador de Microsoft Teams del Edge Connector de Projecta PMO Intelligence.

Este módulo es el **adaptador de origen** de Teams, y vive **dentro del
proceso de la aplicación**. Esa decisión no es un detalle de empaquetado:
es la diferencia entre la Variante A (todo dentro del perímetro) y la
Variante B (un agente desplegado aparte que sale a Internet).

En la **Variante A**, que es la recomendada en banca y la que se aplica
cuando la política de seguridad del banco no admite una aplicación
cliente desplegada en su red, el conector **no es un producto aparte**:

* No se despliega nada nuevo. El adaptador es un módulo más de la
  aplicación, que se ejecuta en el worker, dentro de la red del banco.
* No hay superficie expuesta: nada entra al banco desde fuera.
* No hay segundo ciclo de despliegue, ni segunda aprobación de TI, ni
  segundo producto que mantener.
* La minimización ocurre **en el mismo proceso que escribe en la base de
  datos**, que es trivial, en vez de ser una pieza aparte que hay que
  auditar.
* La credencial de Microsoft es **la misma infraestructura** que la
  aplicación ya tiene aprobada, no una cuenta de servicio nueva.

Por qué **no** usa change notifications
---------------------------------------

Microsoft Graph exige que el endpoint de notificación sea **accesible
públicamente por Internet**: «If your endpoint isn't publicly accessible,
Microsoft Graph doesn't send notifications to your endpoint». Eso choca
con las propiedades obligatorias del Edge Connector, y con la política de
seguridad de un banco que no admite puertos entrantes.

Este adaptador resuelve el conflicto por el lado que no obliga al banco
a abrir su perímetro:

* **Lectura por delta (consulta).** El adaptador pide los cambios a Graph
  con una consulta `delta` y sigue el cursor `@odata.deltaLink`. Solo hay
  conexiones **salientes**, no hay endpoint público, no hay que
  exceptuar nada en el firewall del banco y no se pierde nada por una
  caída de la red: el cursor se guarda y la próxima pasada continúa.

Identidad
---------

El adaptador usa **credenciales de aplicación** de Microsoft Entra ID
(identidad de servicio), nunca credenciales de persona, con una
**aserción de cliente** firmada con el certificado X.509 de la
aplicación en vez de con un secreto.

La clave Ed25519 con la que se firma cada evento, en cambio, es **otra
clave distinta**: la primera acredita el servicio ante Microsoft, la
segunda acredita el evento ante la cadena de auditoría.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from datetime import datetime, timezone
from typing import Any, Iterator

#: Graph: base de la API y ámbito del token.
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"

#: Formato de la.client assertion para credenciales de certificado.
AUDIENCIA_TOKEN = "oauth2/v2.0/token"

#: Campos que el adaptador **nunca** copia del mensaje, aunque Graph los
#: devuelva. Es la política local del sensor: se aplica antes de que el
#: dato entre en la cola y, por tanto, antes de que salga del perímetro.
#:
#: `CAMPOS_BLOQUEADOS` del servidor ya cubre audio, transcripción y
#: biometría. Aquí se añade lo específico de un mensaje de Teams: las
#: reacciones, el historial y los datos de la identidad del emisor. Un
#: mensaje no necesita las reacciones para generar un acta.
CAMPOS_LOCALES_BLOQUEADOS: frozenset[str] = frozenset(
    {
        "reactions",
        "reactions_summary",
        "message_history",
        "mentions",
        "attachments",
        "hosted_contents",
        "policy_violation",
        "importance",
    }
)

#: Campos de la señal que el adaptador **sí** deja salir del perímetro.
#:
#: Es una lista **blanca** expresada sobre las claves de la señal, no
#: sobre las propiedades de Graph: la traducción de una a otra es
#: explícita, y esta lista es la última frontera antes de que el dato
#: entre en la cola. Lo que no está aquí no sale, y añadir un campo
#: nuevo es una decisión que se ve en el diff.
CAMPOS_PERMITIDOS: tuple[str, ...] = (
    "recurso_id",
    "recurso_tipo",
    "mensaje_id",
    "titulo",
    "inicio",
    "cuerpo",
    "participantes",
    "chat_id",
    "equipo_id",
    "canal_id",
    "web_url",
    "reply_to_id",
)

#: Cuántas iteraciones de `nextLink` se siguen antes de darse por vencido.
MAXIMO_PAGINAS = 50


class ErrorDeGraph(Exception):
    """Graph devolvió un error o no respondió como se esperaba."""


# ─────────────────────────── identidad ───────────────────────────


def token_graph(
    *,
    tenant_id: str,
    client_id: str,
    certificado: str,
    clave_privada_pem: str,
    ambito: str = "https://graph.microsoft.com/.default",
) -> str:
    """Consigue un token de Microsoft Graph con credenciales de certificado.

    Se usa una **aserción de cliente** firmada con el certificado X.509
    de la aplicación, en vez de un secreto. Un secreto es un factor que
    se puede copiar de un volcado de configuración; una clave privada
    en el vault del banco, con su ruta de rotación, es otra cosa.

    La aserción es un JWT firmado en RS256 con los claims que exige
    Microsoft Entra ID: ``aud`` es el emisor del endpoint de token,
    ``iss`` y ``sub`` son el identificador de la aplicación, y el
    ``x5t`` de la cabecera es la huella del certificado, que es lo que
    permite a Entra encontrarlo sin consultar la base de datos de
    certificados.
    """
    import httpx
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    import jwt

    # La huella X.509 va en la cabecera de la aserción, en base64 URL.
    certificado_bin = x509.load_pem_x509_certificate(certificado.encode("utf-8"))
    huella = certificado_bin.fingerprint(hashes.SHA1())
    huella_b64 = _base64url(huella)

    ahora = int(time.time())
    asercion = jwt.encode(
        {
            "aud": f"{LOGIN_BASE}/{tenant_id}/{AUDIENCIA_TOKEN}",
            "iss": client_id,
            "sub": client_id,
            "jti": os.urandom(16).hex(),
            "nbf": ahora,
            "exp": ahora + 600,
        },
        clave_privada_pem,
        algorithm="RS256",
        headers={"x5t": huella_b64, "typ": "JWT"},
    )

    respuesta = httpx.post(
        f"{LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_assertion_type": (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            ),
            "client_assertion": asercion,
            "scope": ambito,
        },
        timeout=30.0,
    )
    if respuesta.status_code != 200:
        raise ErrorDeGraph(
            f"Entra ID rechazó la aserción de cliente "
            f"({respuesta.status_code}): {respuesta.text[:300]}"
        )
    cuerpo = respuesta.json()
    token = cuerpo.get("access_token")
    if not token:
        raise ErrorDeGraph("Entra ID no devolvió token de acceso")
    return token


def _base64url(datos: bytes) -> str:
    """Codifica en base64 URL-safe sin relleno."""
    import base64

    return base64.urlsafe_b64encode(datos).decode("ascii").rstrip("=")


# ─────────────────────────── el adaptador ───────────────────────────


class AdaptadorTeams:
    """Lee mensajes de Teams por delta y los entrega al sensor.

    El adaptador no decide qué es importante: lee lo que la política
    permite, lo minimiza y lo entrega. La interpretación —qué de ese
    mensaje es un compromiso, un riesgo o una dependencia— es trabajo de
    Projecta PMO Intelligence, no del sensor.
    """

    def __init__(
        self,
        *,
        token: str,
        estado: pathlib.Path,
        huella_clave: str = "",
        clave_privada: bytes = b"",
        canal_permitido: str | None = None,
        campos_extra: dict[str, Any] | None = None,
        http: Any = None,
        registrar: Any = None,
    ) -> None:
        """Prepara el adaptador.

        Args:
            token: token de Microsoft Graph ya conseguido.
            estado: directorio donde se guardan los cursores delta.
            huella_clave: SHA-256 de la clave pública del dispositivo
                del Edge Connector que firma los eventos.
            clave_privada: clave Ed25519 del dispositivo. **Nunca sale**
                del perímetro: en la Variante A vive en el mismo vault
                que el certificado de Microsoft.
            canal_permitido: si se indica, **solo** se leen los canales
                cuyo identificador esté en la lista. Es la política
                local: un canal no habilitado no se lee, ni se consulta.
            campos_extra: metadatos que se añaden a todos los eventos
                (por ejemplo, el proyecto asociado).
            http: cliente HTTP con el que hablar con Graph. Se inyecta
                en las pruebas; en producción es un ``httpx.Client``.
            registrar: callback opcional para dejar traza local de cada
                lectura. En la Variante A lo suple la auditoría del
                propio servidor.
        """
        self.token = token
        self.estado = estado
        self.huella_clave = huella_clave
        self.clave_privada = clave_privada
        self.canal_permitido = canal_permitido
        self.campos_extra = campos_extra or {}
        self.registrar = registrar
        if http is None:
            import httpx

            http = httpx.Client()
        self.http = http

    # -- cursores ----------------------------------------------------
    def ruta_cursor(self, clave: str) -> pathlib.Path:
        """Ruta del fichero de cursor para un recurso."""
        seguro = clave.replace("/", "_").replace(":", "_")
        return self.estado / f"cursor_{seguro}.json"

    def leer_cursor(self, clave: str) -> str | None:
        """Devuelve el `@odata.deltaLink` guardado, si lo hay."""
        ruta = self.ruta_cursor(clave)
        if not ruta.exists():
            return None
        try:
            return json.loads(ruta.read_text(encoding="utf-8")).get("delta_link")
        except (json.JSONDecodeError, OSError):
            return None

    def guardar_cursor(self, clave: str, delta_link: str) -> None:
        """Guarda el cursor, para que la próxima pasada continúe."""
        ruta = self.ruta_cursor(clave)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(
            json.dumps(
                {
                    "delta_link": delta_link,
                    "actualizado_en": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # -- política ----------------------------------------------------
    def _permitido(self, chat_id: str | None, canal_id: str | None) -> bool:
        """¿Este recurso está habilitado por la política local?"""
        if self.canal_permitido is None:
            return True
        return bool(chat_id) and chat_id == self.canal_permitido or bool(
            canal_id
        ) and canal_id == self.canal_permitido

    # -- lectura -----------------------------------------------------
    def _peticion(self, url: str) -> dict[str, Any]:
        """Llama a Graph y devuelve el cuerpo JSON.

        Graph devuelve ``429`` con la cabecera ``Retry-After`` cuando se
        supera el límite de peticiones. Respetarlo no es opcional: el
        conector es un cliente que comparte cuota con el resto de las
        aplicaciones del tenant.
        """
        intentos = 0
        while True:
            intentos += 1
            respuesta = self.http.get(
                url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
                timeout=60.0,
            )
            if respuesta.status_code == 429:
                if intentos >= 5:
                    raise ErrorDeGraph(
                        f"Graph sigue limitando la petición tras {intentos} intentos: {url}"
                    )
                espera = int(respuesta.headers.get("Retry-After", "5"))
                time.sleep(max(1, espera))
                continue
            if respuesta.status_code != 200:
                raise ErrorDeGraph(
                    f"Graph devolvió {respuesta.status_code} para {url}: "
                    f"{respuesta.text[:200]}"
                )
            return respuesta.json()

    def _recursos(self, ruta: str, clave: str) -> Iterator[tuple[dict[str, Any], str | None]]:
        """Recorre un recurso con delta, siguiendo `nextLink` hasta el final.

        Devuelve cada elemento junto con el `deltaLink` de la ronda, que
        solo se conoce cuando la ronda termina.
        """
        cursor = self.leer_cursor(clave)
        url = cursor if cursor else f"{GRAPH_BASE}{ruta}/delta"
        elementos: list[dict[str, Any]] = []
        paginas = 0
        delta_link: str | None = None

        while url:
            paginas += 1
            if paginas > MAXIMO_PAGINAS:
                raise ErrorDeGraph(
                    f"la ronda de delta de {clave} no terminó en {MAXIMO_PAGINAS} páginas"
                )
            cuerpo = self._peticion(url)
            elementos.extend(cuerpo.get("value", []))
            url = cuerpo.get("@odata.nextLink")
            delta_link = cuerpo.get("@odata.deltaLink")

        for elemento in elementos:
            yield elemento, delta_link

    def leer_chat(self, chat_id: str) -> list[dict[str, Any]]:
        """Lee los mensajes de un chat mediante delta."""
        clave = f"chat_{chat_id}"
        leidos: list[dict[str, Any]] = []
        for elemento, delta_link in self._recursos(f"/chats/{chat_id}/messages", clave):
            if not self._permitido(chat_id, None):
                continue
            señal = self._a_señal(elemento, recurso_id=chat_id, recurso_tipo="chat")
            if señal is None:
                continue
            leidos.append(señal)
            if delta_link:
                self.guardar_cursor(clave, delta_link)
        return leidos

    def leer_canal(self, equipo_id: str, canal_id: str) -> list[dict[str, Any]]:
        """Lee los mensajes de un canal de un equipo mediante delta."""
        clave = f"canal_{equipo_id}_{canal_id}"
        leidos: list[dict[str, Any]] = []
        for elemento, delta_link in self._recursos(
            f"/teams/{equipo_id}/channels/{canal_id}/messages", clave
        ):
            if not self._permitido(None, canal_id):
                continue
            señal = self._a_señal(elemento, recurso_id=canal_id, recurso_tipo="canal")
            if señal is None:
                continue
            leidos.append(señal)
            if delta_link:
                self.guardar_cursor(clave, delta_link)
        return leidos

    # -- traducción --------------------------------------------------
    def _a_señal(
        self, elemento: dict[str, Any], *, recurso_id: str, recurso_tipo: str
    ) -> dict[str, Any] | None:
        """Traduce un mensaje de Graph a la señal del sensor.

        Se aplica la **lista blanca** de campos y se eliminan los
        bloqueados en local. Lo que sale de aquí es lo único que puede
        acabar en la cola.
        """
        canal = elemento.get("channelIdentity") or {}
        chat_id = elemento.get("chatId")
        canal_id = canal.get("channelId")
        equipo_id = canal.get("teamId")

        cuerpo = elemento.get("body") or {}
        texto = cuerpo.get("content") if isinstance(cuerpo, dict) else None

        emisor = elemento.get("from") or {}
        identidad = emisor.get("user") or emisor.get("application") or {}

        señal: dict[str, Any] = {
            "recurso_id": recurso_id,
            "recurso_tipo": recurso_tipo,
            "mensaje_id": elemento.get("id"),
            "titulo": elemento.get("subject") or f"Mensaje en {recurso_tipo} {recurso_id}",
            "inicio": elemento.get("createdDateTime")
            or datetime.now(timezone.utc).isoformat(),
            "cuerpo": texto or "",
            "participantes": [identidad.get("displayName") or "desconocido"],
            "chat_id": chat_id,
            "equipo_id": equipo_id,
            "canal_id": canal_id,
            "web_url": elemento.get("webUrl"),
            "reply_to_id": elemento.get("replyToId"),
            **self.campos_extra,
        }

        # Lista blanca: se descartan las propiedades que no están en la
        # lista, aunque Graph las devuelva.
        señal = {
            clave: valor
            for clave, valor in señal.items()
            if clave in CAMPOS_PERMITIDOS or clave in self.campos_extra
        }

        # Lista negra local: lo que la política local prohíbe.
        for campo in CAMPOS_LOCALES_BLOQUEADOS:
            señal.pop(campo, None)

        if not señal.get("mensaje_id"):
            return None
        return señal

    # -- entrega -----------------------------------------------------
    def construir_eventos(
        self, señales: list[dict[str, Any]], *, proyecto_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Construye y **firma** los eventos de las señales leídas.

        La firma es la del dispositivo del Edge Connector, y el contenido
        firmado es el mismo que valida `app.core.edge.ingerir_evento`. En
        la Variante A no hay distancia entre el que lee y el que valida,
        así que la firma ya no juega el papel de «acreditar que el evento
        viene de lejos»: su valor es la **procedencia auditada**, que
        sigue igual, y el hecho de que el camino de validación del
        servidor **no cambia ni una línea** de lo que ya está probado.
        """
        from app.core.edge import RECEPTORES_ADMITIDOS, firma_de_evento, huella_de_evento

        if "mensaje_canal" not in RECEPTORES_ADMITIDOS:
            raise ErrorDeGraph(
                "el tipo de receptor 'mensaje_canal' no está en la lista cerrada"
            )
        if not self.clave_privada:
            raise ErrorDeGraph(
                "el adaptador no tiene clave de dispositivo: no puede firmar. "
                "La clave privada vive en el vault y nunca sale del perímetro."
            )

        eventos: list[dict[str, Any]] = []
        for señal in señales:
            contenido = {
                "tipo": "mensaje_canal",
                "payload": señal,
                "recurso": señal["recurso_id"],
                "recurso_tipo": señal["recurso_tipo"],
                "uso_previsto": "trazabilidad_de_proyecto",
                "marca_tiempo": datetime.now(timezone.utc).isoformat(),
            }
            firma = firma_de_evento(self.clave_privada, contenido)
            eventos.append(
                {
                    "huella_clave": self.huella_clave,
                    "tipo": "mensaje_canal",
                    "contenido": contenido,
                    "firma": firma,
                    "marca_tiempo": contenido["marca_tiempo"],
                    "uso_previsto": contenido["uso_previsto"],
                    "recurso": contenido["recurso"],
                    "recurso_tipo": contenido["recurso_tipo"],
                    "proyecto_id": proyecto_id,
                    "destinatarios_previstos": ["equipo-pmo"],
                    "correlation_id": huella_de_evento(contenido)[:32],
                }
            )
            if self.registrar is not None:
                self.registrar(
                    "lectura_teams",
                    {
                        "recurso": señal["recurso_id"],
                        "mensaje_id": señal.get("mensaje_id"),
                        "campos": sorted(señal.keys()),
                    },
                )
        return eventos


