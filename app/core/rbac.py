"""RBAC de Projecta PMO Intelligence: políticas reutilizables.

Implementa HU_05 — RBAC granular con políticas reutilizables.

El modelo tiene tres capas, y las tres importan:

1. **Rol y nivel.** El nivel vive en la tabla ``roles`` y se resuelve en
   runtime, nunca en el esquema ni en el código. Una política que exige un
   nivel admite todos los roles de nivel igual o superior, de modo que
   añadir un rol es insertar una fila, no desplegar código.
2. **Ámbito.** Una política puede limitarse a la organización o a un
   proyecto concreto. Un gerente de proyecto en el proyecto X no ve el
   proyecto Y: la ausencia de membresía es lo que lo impide, no un filtro
   aplicado en la consulta.
3. **Estado del usuario.** Un usuario desactivado no opera, aunque su token
   siga vigente. El token transporta identidad, no permisos.

El TO-BE v1.1 fija el catálogo de seis roles en §6.1. Este módulo lo
reproduce como **constante de referencia** para poder validar la base de
datos en los tests y en el arranque, pero la autoridad sigue siendo la
tabla ``roles``: si la base de datos y la constante discrepan, se avisa.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "CATALOGO_ROLES",
    "Actor",
    "Decision",
    "ErrorDePermiso",
    "Politica",
    "Rol",
    "actor_desde_registro",
    "administrar",
    "ambito_organizacion",
    "analizar",
    "aprobar",
    "cargar_evidencia",
    "catalogo_consistente",
    "configurar",
    "evaluar",
    "exigir",
    "id_proyecto_como_texto",
]


class ErrorDePermiso(Exception):
    """La operación no está permitida para el actor indicado."""

    def __init__(self, codigo: int, motivo: str, regla: str) -> None:
        super().__init__(motivo)
        self.codigo = codigo
        self.motivo = motivo
        self.regla = regla


@dataclass(frozen=True)
class Rol:
    """Rol del catálogo normativo con su nivel jerárquico."""

    nombre: str
    nivel: int
    ambito: str
    puede: str
    no_puede: str

    def __str__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"{self.nombre} (nivel {self.nivel})"


#: Catálogo normativo de roles del TO-BE v1.1 §6.1. El nivel se resuelve en
#: runtime contra ``roles.nivel``; esta constante sirve de referencia para
#: validar la base de datos y para los tests.
CATALOGO_ROLES: Final[dict[str, Rol]] = {
    "observador": Rol(
        nombre="Observador", nivel=0, ambito="organización",
        puede="Ver proyectos, compromisos, informes y auditoría",
        no_puede="Crear, corregir, aprobar ni transicionar",
    ),
    "colaborador": Rol(
        nombre="Colaborador", nivel=1, ambito="membresía de proyecto",
        puede="Cargar fuentes, adjuntar evidencia y actualizar sus compromisos",
        no_puede="Aprobar borradores ni aplicar cambios",
    ),
    "analista": Rol(
        nombre="Analista", nivel=2, ambito="proyecto",
        puede="Analizar evidencia y proponer borradores",
        no_puede="Aprobar ni transicionar estados",
    ),
    "gerente": Rol(
        nombre="Gerente de proyecto", nivel=3, ambito="proyecto",
        puede="Aprobar, rechazar y transicionar en su proyecto",
        no_puede="Administrar la organización ni sus roles",
    ),
    "pmo": Rol(
        nombre="PMO", nivel=4, ambito="organización",
        puede="Configurar reglas, plantillas y semáforos; auditar toda la organización",
        no_puede="Administrar usuarios ni la configuración de la plataforma",
    ),
    "administrador": Rol(
        nombre="Administrador de organización", nivel=5, ambito="organización",
        puede="Administrar usuarios, roles e integraciones de la organización",
        no_puede="Modificar la configuración global de la plataforma",
    ),
}


def catalogo_consistente(niveles_por_rol: dict[str, int]) -> list[str]:
    """Compara la base de datos con el catálogo de referencia.

    Devuelve la lista de discrepancias. Se ejecuta al arrancar y en los
    tests: si alguien cambia un nivel en la base de datos sin actualizar el
    catálogo, la diferencia tiene que verse, no deducirse.
    """
    diferencias: list[str] = []
    for nombre, rol in CATALOGO_ROLES.items():
        if nombre not in niveles_por_rol:
            diferencias.append(f"falta el rol '{nombre}' en la tabla roles")
        elif niveles_por_rol[nombre] != rol.nivel:
            diferencias.append(
                f"el rol '{nombre}' tiene nivel {niveles_por_rol[nombre]} "
                f"en la base de datos y {rol.nivel} en el catálogo"
            )
    for nombre in niveles_por_rol:
        if nombre not in CATALOGO_ROLES:
            diferencias.append(f"el rol '{nombre}' no está en el catálogo normativo")
    return diferencias


@dataclass(frozen=True)
class Politica:
    """Condición de autorización, reutilizable por cualquier endpoint.

    ``nivel_requerido`` es el mínimo del rol dentro del ámbito. La decisión
    se toma contra el nivel real del usuario, resuelto desde la base de
    datos.
    """

    nombre: str
    nivel_requerido: int
    ambito: str = "organización"
    #: Si es ``True``, el actor debe ser miembro del proyecto para operar
    #: sobre él. Es lo que impide que un gerente de un proyecto vea otro.
    exige_membresia: bool = False
    #: Si es ``True``, un usuario desactivado no puede operar.
    exige_usuario_activo: bool = True
    descripcion: str = ""

    def __str__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"{self.nombre} (nivel ≥ {self.nivel_requerido}, ámbito {self.ambito})"


@dataclass
class Actor:
    """Sujeto de la decisión de autorización."""

    usuario_id: str
    organizacion_id: str
    rol: str
    nivel: int
    activo: bool = True
    #: Proyectos de los que es miembro, como UUID en texto.
    proyectos: set[str] = field(default_factory=set)
    #: ``True`` si su rol tiene ámbito de organización (nivel ≥ 4).
    ambito_organizacion: bool = False


@dataclass
class Decision:
    """Resultado de evaluar una política."""

    permitido: bool
    regla: str
    motivo: str
    codigo: int = 200

    def __bool__(self) -> bool:
        return self.permitido


# ─────────────────────────── políticas reutilizables ───────────────────────────

#: Ver contenido sin restricción. Es el nivel base: cualquier usuario activo
#: de la organización puede leer, dentro del ámbito que le corresponda.
ambito_organizacion = Politica(
    nombre="ver",
    nivel_requerido=0,
    ambito="organización",
    exige_membresia=False,
    descripcion="Consultar información de la organización",
)

#: Adjuntar evidencia y actualizar los propios compromisos.
cargar_evidencia = Politica(
    nombre="cargar_evidencia",
    nivel_requerido=1,
    ambito="proyecto",
    exige_membresia=True,
    descripcion="Cargar fuentes y adjuntar evidencia",
)

#: Analizar evidencia y proponer borradores.
analizar = Politica(
    nombre="analizar",
    nivel_requerido=2,
    ambito="proyecto",
    exige_membresia=True,
    descripcion="Analizar evidencia y proponer borradores",
)

#: Aprobar, rechazar y transicionar.
aprobar = Politica(
    nombre="aprobar",
    nivel_requerido=3,
    ambito="proyecto",
    exige_membresia=True,
    descripcion="Aprobar y transicionar estados",
)

#: Configurar reglas, plantillas y semáforos del PMO.
configurar = Politica(
    nombre="configurar",
    nivel_requerido=4,
    ambito="organización",
    exige_membresia=False,
    descripcion="Configurar reglas y plantillas del PMO",
)

#: Administrar usuarios, roles e integraciones.
administrar = Politica(
    nombre="administrar",
    nivel_requerido=5,
    ambito="organización",
    exige_membresia=False,
    descripcion="Administrar usuarios, roles e integraciones",
)


# ─────────────────────────── evaluación ───────────────────────────


def _mismo_ambito(actor: Actor, politica: Politica, proyecto_id: str | None) -> bool:
    """Comprueba que el actor y la política comparten ámbito."""
    if politica.ambito == "organización":
        return True
    if proyecto_id is None:
        # Una política de proyecto sin proyecto no se puede evaluar: se
        # deniega, porque denegar por defecto es la opción segura.
        return False
    return True


def evaluar(
    actor: Actor,
    politica: Politica,
    *,
    proyecto_id: str | None = None,
) -> Decision:
    """Evalúa una política contra un actor y devuelve la decisión.

    El orden de las comprobaciones es intencionado y estable, porque la
    regla que se registra en la auditoría es la primera que falla. Se
    empieza por la más fundamental —el nivel del rol— para que el motivo
    que ve quien audita sea el real y no un efecto secundario:

    1. Usuario desactivado.
    2. Nivel de rol insuficiente.
    3. Ámbito incompatible.
    4. Falta de membresía en el proyecto.
    """
    if politica.exige_usuario_activo and not actor.activo:
        return Decision(
            permitido=False,
            codigo=403,
            regla=f"{politica.nombre}: usuario desactivado",
            motivo="El usuario está desactivado y no puede operar.",
        )

    if actor.nivel < politica.nivel_requerido:
        return Decision(
            permitido=False,
            codigo=403,
            regla=f"{politica.nombre}: nivel de rol insuficiente",
            motivo=(
                f"La política «{politica.nombre}» exige nivel {politica.nivel_requerido} "
                f"y el rol {actor.rol} tiene nivel {actor.nivel}."
            ),
        )

    if not _mismo_ambito(actor, politica, proyecto_id):
        return Decision(
            permitido=False,
            codigo=403,
            regla=f"{politica.nombre}: ámbito incompatible",
            motivo="La operación requiere un proyecto concreto.",
        )

    if politica.exige_membresia and proyecto_id is not None:
        if not actor.ambito_organizacion and str(proyecto_id) not in actor.proyectos:
            return Decision(
                permitido=False,
                codigo=403,
                regla=f"{politica.nombre}: sin membresía en el proyecto",
                motivo=(
                    "El usuario no es miembro de este proyecto, así que no puede "
                    "operar sobre él."
                ),
            )

    return Decision(
        permitido=True,
        regla=f"{politica.nombre}: permitida",
        motivo=f"Nivel {actor.nivel} ≥ {politica.nivel_requerido}.",
    )


def exigir(
    actor: Actor,
    politica: Politica,
    *,
    proyecto_id: str | None = None,
) -> None:
    """Evalúa una política y lanza :class:`ErrorDePermiso` si se deniega."""
    decision = evaluar(actor, politica, proyecto_id=proyecto_id)
    if not decision.permitido:
        raise ErrorDePermiso(decision.codigo, decision.motivo, decision.regla)


def actor_desde_registro(
    usuario: Any,
    *,
    organizacion_id: str | None = None,
    proyectos: set[str] | None = None,
) -> Actor:
    """Construye un :class:`Actor` a partir de un usuario del modelo ORM."""
    rol = getattr(usuario, "rol", None)
    nombre_rol = getattr(rol, "nombre", "observador")
    nivel = getattr(rol, "nivel", 0)
    return Actor(
        usuario_id=str(usuario.id),
        organizacion_id=str(organizacion_id or usuario.organizacion_id),
        rol=nombre_rol,
        nivel=int(nivel),
        activo=bool(usuario.activo),
        proyectos=set(proyectos or ()),
        ambito_organizacion=int(nivel) >= 4,
    )


def id_proyecto_como_texto(proyecto_id: uuid.UUID | str | None) -> str | None:
    """Normaliza un identificador de proyecto a texto."""
    return None if proyecto_id is None else str(proyecto_id)
