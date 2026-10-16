"""Lectura del historial de auditoria (JHO-105, UI-08).

Este modulo NO escribe: el unico camino de escritura del log es
``services.audit.record_event``. Aqui se traduce cada evento guardado a algo
legible para una persona (quien, cuando, que entidad, que paso) y se ofrecen
los filtros de la pantalla.

Reglas:

* La fecha que se muestra es la del SERVIDOR (UTC), la misma que entra en el
  hash del evento: nadie puede reescribirla sin romper la cadena.
* El rol del usuario se muestra tal como quedo congelado en el evento, no como
  esta hoy: si a alguien lo degradaron despues, el historial sigue diciendo
  con que rol ejecuto esa accion.
* El hash corto se muestra junto a la fecha: es la evidencia de que la fila
  pertenece a la cadena.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from license_manager.models import AuditEvent, to_utc
from license_manager.services.directory import format_cop, format_hardware_code
from license_manager.services.users import rol_etiqueta

# Tope de filas que pinta la pantalla: el historial es append-only y crece
# para siempre, asi que se muestra la cola mas reciente.
LIMITE_EVENTOS = 300

_LARGO_HASH = 12


# --------------------------------------------------------------------------- #
# Catalogo de acciones: etiqueta humana y tono del badge
# --------------------------------------------------------------------------- #

ACCIONES: dict[str, tuple[str, str]] = {
    "usuario.configuracion_inicial": ("Inicializacion del sistema", "sistema"),
    "login.exitoso": ("Inicio de sesion", "sesion"),
    "login.fallido": ("Intento de sesion fallido", "alerta"),
    "login.rechazado_cuenta_inactiva": ("Sesion rechazada (cuenta inactiva)", "alerta"),
    "login.sesion_cerrada": ("Cierre de sesion", "sesion"),
    "usuario.creado": ("Usuario creado", "cuenta"),
    "usuario.activado": ("Usuario activado", "cuenta"),
    "usuario.desactivado": ("Usuario desactivado", "cuenta"),
    "usuario.clave_restablecida": ("Clave restablecida por el Titular", "cuenta"),
    "usuario.clave_propia_cambiada": ("Contrasena propia cambiada", "cuenta"),
    "cliente.creado": ("Verduleria registrada", "directorio"),
    "cliente.activado": ("Verduleria activada", "directorio"),
    "cliente.desactivado": ("Verduleria desactivada", "directorio"),
    "equipo.creado": ("Equipo registrado", "directorio"),
    "equipo.vinculado": ("Equipo asignado a verduleria", "directorio"),
    "equipo.desvinculado": ("Equipo desasignado", "directorio"),
    "licencia.emitida": ("Emision de licencia FRB1", "emision"),
    "ingreso.registrado": ("Cobro registrado", "cobro"),
    "ingreso.cuenta_actualizada": ("Estado de cuenta actualizado", "cobro"),
    "cierre.exportado": ("Cierre mensual exportado", "cierre"),
    "liquidacion.creada": ("Liquidacion creada", "liquidacion"),
    "liquidacion.recalculada": ("Liquidacion recalculada", "liquidacion"),
    "liquidacion.pagada": ("Comision liquidada y pagada", "liquidacion"),
    "tecnico.creado": ("Tecnico registrado", "mayorista"),
    "tecnico.actualizado": ("Tecnico actualizado", "mayorista"),
    "paquete.registrado": ("Paquete mayorista registrado", "mayorista"),
    "auditoria.verificada": ("Cadena de auditoria verificada", "sistema"),
}

# Tono del catalogo -> clase del badge ya existente en el CSS.
TONO_BADGE = {
    "emision": "azul",
    "cobro": "verde",
    "cuenta": "ambar",
    "sesion": "gris",
    "directorio": "azul",
    "mayorista": "ambar",
    "liquidacion": "verde",
    "cierre": "azul",
    "sistema": "gris",
    "alerta": "rojo",
    "otro": "gris",
}

# Filtros de la barra de UI-08 (mockup 08-historial-auditoria).
GRUPOS: tuple[tuple[str, str], ...] = (
    ("", "Todos los eventos"),
    ("licencias", "Emisiones de licencias"),
    ("cobros", "Cobros y cierres"),
    ("usuarios", "Usuarios y sesiones"),
    ("mayorista", "Modelo mayorista"),
    ("liquidaciones", "Liquidaciones"),
    ("directorio", "Verdulerias y equipos"),
    ("sistema", "Sistema"),
)

_PREFIJOS_GRUPO: dict[str, tuple[str, ...]] = {
    "licencias": ("licencia.",),
    "cobros": ("ingreso.", "cierre."),
    "usuarios": ("usuario.", "login."),
    "mayorista": ("tecnico.", "paquete."),
    "liquidaciones": ("liquidacion.",),
    "directorio": ("cliente.", "equipo."),
    "sistema": ("auditoria.",),
}


def etiqueta_accion(accion: str) -> str:
    return ACCIONES.get(accion, (accion, "otro"))[0]


def tono_accion(accion: str) -> str:
    return TONO_BADGE[ACCIONES.get(accion, (accion, "otro"))[1]]


# --------------------------------------------------------------------------- #
# Traduccion del detalle a una frase
# --------------------------------------------------------------------------- #


def _dinero(valor: object) -> str:
    try:
        return format_cop(int(valor))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(valor)


def _equipo(codigo: object) -> str:
    texto = str(codigo or "")
    return format_hardware_code(texto) if len(texto) == 16 else texto


def resumen_detalle(accion: str, detalle: dict | None) -> str:
    """Frase legible del evento. Cae al JSON crudo si no hay traductor."""
    datos = detalle or {}
    if not datos:
        return ""

    if accion == "licencia.emitida":
        partes = [f"Tipo {datos.get('tipo_emision', '—')}"]
        if datos.get("cliente_nombre"):
            partes.append(f"verduleria {datos['cliente_nombre']}")
        if datos.get("hardware_code"):
            partes.append(f"equipo {_equipo(datos['hardware_code'])}")
        if datos.get("cupo_descontado"):
            partes.append("cupo prepago descontado")
        if datos.get("tecnico_nombre"):
            partes.append(f"tecnico {datos['tecnico_nombre']}")
        if datos.get("justificacion"):
            partes.append(f"justificacion: {datos['justificacion']}")
        return " · ".join(partes)

    if accion == "ingreso.registrado":
        partes = [
            f"Categoria {datos.get('categoria', '—')}",
            _dinero(datos.get("monto_cop", 0)),
            f"estado {datos.get('estado_pago', '—')}",
        ]
        if datos.get("metodo_pago"):
            partes.append(f"metodo {datos['metodo_pago']}")
        if datos.get("estado_cobro"):
            partes.append(f"cuenta {datos['estado_cobro']}")
        return " · ".join(partes)

    if accion == "ingreso.cuenta_actualizada":
        partes = [f"Nuevo estado de la cuenta: {datos.get('estado_cobro', '—')}"]
        if datos.get("monto_cop"):
            partes.append(_dinero(datos["monto_cop"]))
        if datos.get("metodo_pago"):
            partes.append(f"metodo {datos['metodo_pago']}")
        return " · ".join(partes)

    if accion == "cierre.exportado":
        return (
            f"Periodo {datos.get('periodo', '—')} · "
            f"{_dinero(datos.get('total_cop', 0))} · "
            f"{datos.get('movimientos_n', 0)} movimiento(s)"
        )

    if accion.startswith("liquidacion."):
        partes = [f"Periodo {datos.get('periodo', '—')}"]
        if datos.get("licencias") is not None:
            partes.append(f"{datos['licencias']} licencia(s)")
        if datos.get("comision_cop") is not None:
            partes.append(f"comision {_dinero(datos['comision_cop'])}")
        if datos.get("metodo_pago"):
            partes.append(f"canal {datos['metodo_pago']}")
        if datos.get("comprobante"):
            partes.append(f"comprobante {datos['comprobante']}")
        return " · ".join(partes)

    if accion == "paquete.registrado":
        partes = [
            f"Tipo {datos.get('tipo_paquete', '—')}",
            f"{datos.get('licencias', 0)} cupo(s)",
            _dinero(datos.get("valor_total_cop", 0)),
        ]
        if datos.get("tecnico_nombre"):
            partes.append(f"tecnico {datos['tecnico_nombre']}")
        if datos.get("metodo_pago"):
            partes.append(f"canal {datos['metodo_pago']}")
        return " · ".join(partes)

    if accion in ("tecnico.creado", "tecnico.actualizado"):
        partes = []
        if datos.get("nombre"):
            partes.append(f"Tecnico {datos['nombre']}")
        if datos.get("activo") is not None:
            partes.append("activo" if datos["activo"] else "desactivado")
        return " · ".join(partes)

    if accion == "usuario.creado":
        return f"Rol {datos.get('rol', '—')} · correo {datos.get('email_creado', '—')}"

    if accion in ("usuario.activado", "usuario.desactivado"):
        correo = datos.get("email_afectado")
        return f"Cuenta {correo}" if correo else ""

    if accion in ("usuario.clave_restablecida", "usuario.clave_propia_cambiada"):
        return "Nunca se registra la contrasena, ni hasheada ni en claro."

    if accion == "login.fallido":
        return f"Correo intentado: {datos.get('email', '—')}"

    if accion == "cliente.creado":
        return f"Verduleria {datos.get('nombre') or datos.get('cliente_nombre') or '—'}"

    if accion.startswith("equipo."):
        codigo = datos.get("hardware_code")
        return f"Equipo {_equipo(codigo)}" if codigo else ""

    if accion == "auditoria.verificada":
        estado = "cadena integra" if datos.get("ok") else "cadena ROTA"
        return f"{estado} · {datos.get('total_events', 0)} evento(s)"

    # Sin traductor propio: se muestra el JSON tal cual quedo en el log.
    return json.dumps(datos, ensure_ascii=False, sort_keys=True)


def _entidad(
    entidad_tipo: str | None, entidad_id: str | None, datos: dict
) -> str:
    """Columna "Entidad / Negocio" del mockup."""
    for clave in ("cliente_nombre", "tecnico_nombre", "nombre", "email_creado"):
        if datos.get(clave):
            return str(datos[clave])
    if entidad_tipo and entidad_id:
        return f"{entidad_tipo} · {entidad_id[:8]}"
    if entidad_tipo:
        return entidad_tipo
    if entidad_id:
        return entidad_id[:8]
    return "—"


# --------------------------------------------------------------------------- #
# Filas de la pantalla
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EventoFila:
    """Fila de la tabla de UI-08."""

    id: int
    fecha_at: object
    usuario_email: str | None
    usuario_nombre: str
    rol_etiqueta: str
    accion: str
    accion_etiqueta: str
    tono: str
    entidad: str
    resumen: str
    hash_corto: str
    prev_hash_corto: str | None


def _usuario_nombre(db: Session, email: str | None) -> str:
    """Nombre actual del operador; el email del evento manda si ya no existe."""
    if not email:
        return "Sistema"
    from license_manager.models import User

    nombre = db.execute(
        select(User.nombre_completo).where(User.email == email)
    ).scalar_one_or_none()
    return nombre or email


def listar_eventos(
    db: Session,
    *,
    busqueda: str = "",
    grupo: str = "",
    usuario: str = "",
    limite: int = LIMITE_EVENTOS,
) -> list[EventoFila]:
    query = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limite)

    termino = (busqueda or "").strip()
    if termino:
        patron = f"%{termino.lower()}%"
        query = query.where(
            or_(
                func.lower(func.coalesce(AuditEvent.accion, "")).like(patron),
                func.lower(func.coalesce(AuditEvent.usuario_email, "")).like(patron),
                func.lower(func.coalesce(AuditEvent.detalle, "")).like(patron),
                func.lower(func.coalesce(AuditEvent.entidad_id, "")).like(patron),
            )
        )

    prefijos = _PREFIJOS_GRUPO.get(grupo)
    if prefijos:
        query = query.where(
            or_(*(AuditEvent.accion.like(f"{prefijo}%") for prefijo in prefijos))
        )

    if usuario:
        query = query.where(AuditEvent.usuario_email == usuario)

    filas: list[EventoFila] = []
    for evento in db.execute(query).scalars():
        datos: dict = {}
        if evento.detalle:
            try:
                datos = json.loads(evento.detalle)
            except json.JSONDecodeError:
                datos = {"detalle": evento.detalle}
        filas.append(
            EventoFila(
                id=evento.id,
                fecha_at=to_utc(evento.fecha_at),
                usuario_email=evento.usuario_email,
                usuario_nombre=_usuario_nombre(db, evento.usuario_email),
                rol_etiqueta=(
                    rol_etiqueta(evento.usuario_rol) if evento.usuario_rol else "Sistema"
                ),
                accion=evento.accion,
                accion_etiqueta=etiqueta_accion(evento.accion),
                tono=tono_accion(evento.accion),
                entidad=_entidad(evento.entidad_tipo, evento.entidad_id, datos),
                resumen=resumen_detalle(evento.accion, datos),
                hash_corto=evento.hash[:_LARGO_HASH],
                prev_hash_corto=(
                    evento.prev_hash[:_LARGO_HASH] if evento.prev_hash else None
                ),
            )
        )
    return filas


def total_eventos(db: Session) -> int:
    return int(db.execute(select(func.count(AuditEvent.id))).scalar_one())


def usuarios_con_eventos(db: Session) -> list[str]:
    """Correos que aparecen en el log, para el filtro por operador."""
    filas = db.execute(
        select(AuditEvent.usuario_email)
        .where(AuditEvent.usuario_email.is_not(None))
        .group_by(AuditEvent.usuario_email)
        .order_by(func.count(AuditEvent.id).desc())
    ).scalars()
    return [correo for correo in filas if correo]
