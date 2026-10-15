"""Directorio de clientes e inventario de equipos (JHO-100, UI-03 / UI-04 / UI-06).

Reglas de negocio que este modulo materializa:

* El codigo de hardware es la identidad de la maquina fisica y debe ser
  exactamente 16 caracteres hexadecimales en mayusculas. Se normaliza con la
  MISMA funcion que firma, para que lo que se guarda sea byte a byte lo que
  Frubber POS va a pedir verificar.
* Un codigo de hardware es unico: no se puede registrar la misma maquina dos
  veces. El POS offline solo conoce ese codigo.
* Desactivar una verduleria no borra sus equipos ni sus licencias: el
  inventario es historico.
* Ninguna funcion de aqui devuelve la licencia firmada al navegador por
  asomo: esa decision pertenece a JHO-101.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from license_manager.crypto.frb1 import Frb1Error, normalize_machine_code
from license_manager.models import Customer, Equipment, License, PaymentStatus, to_utc


class DirectoryError(ValueError):
    """Rechazo de negocio con un mensaje pensado para mostrarse en pantalla."""


# --------------------------------------------------------------------------- #
# Verdulerias / clientes (UI-03)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CustomerRow:
    """Fila de la tabla de UI-03, ya con los conteos resueltos."""

    id: str
    nombre: str
    propietario: str | None
    telefono: str | None
    direccion: str | None
    ciudad: str | None
    activo: bool
    equipos_activos: int
    estado_comercial: str
    # Tono visual del badge de UI-03: "verde" | "azul" | "ambar" | "gris".
    estado_tono: str
    ultima_licencia_at: object | None


def _estado_comercial_de(licencia: License | None) -> tuple[str, str]:
    """Etiqueta y tono de la columna de estado comercial de UI-03.

    Se deriva del estado de cobro de la licencia mas reciente del negocio;
    sin licencia el negocio esta "Sin licencia". Devuelve tambien el tono para
    que la plantilla no tenga que reimplementar la logica de negocio.
    """
    if licencia is None:
        return "Sin licencia", "gris"
    if licencia.estado_registro == "anulada":
        return "Licencia anulada", "gris"
    etiqueta, tono = {
        PaymentStatus.PAID.value: ("Cliente al dia", "verde"),
        "prueba_activa": ("En prueba", "azul"),
        "cobro_pendiente": ("Cobro pendiente", "ambar"),
        "vencida": ("Vencida", "ambar"),
        "no_aplica": ("Sin cobro", "gris"),
    }.get(licencia.estado_cobro, (licencia.estado_cobro, "gris"))
    return etiqueta, tono


def list_customers(db: Session, busqueda: str = "") -> list[CustomerRow]:
    """Directorio con conteo de equipos y estado comercial.

    Los tres datos derivados (conteo de equipos, ultima licencia) se resuelven
    con agregados en SQL en vez de una consulta por fila: la tabla puede crecer
    y cada visita al directorio no debe recorrer la base fila por fila.
    """
    equipos_conteo = (
        select(Equipment.cliente_id, func.count(Equipment.id).label("total"))
        .group_by(Equipment.cliente_id)
        .subquery()
    )

    query = select(
        Customer,
        func.coalesce(equipos_conteo.c.total, 0),
    ).outerjoin(equipos_conteo, equipos_conteo.c.cliente_id == Customer.id)

    termino = (busqueda or "").strip()
    if termino:
        patron = f"%{termino.lower()}%"
        query = query.where(
            func.lower(Customer.nombre).like(patron)
            | func.lower(func.coalesce(Customer.propietario, "")).like(patron)
            | func.lower(func.coalesce(Customer.telefono, "")).like(patron)
        )

    # Una sola pasada para la licencia mas reciente de cada negocio.
    ultimas: dict[str, License] = {}
    licencias_query = (
        select(Equipment.cliente_id, License)
        .join(License, License.equipo_id == Equipment.id)
        .order_by(Equipment.cliente_id.asc(), License.emitida_at.desc())
    )
    for cliente_id, licencia in db.execute(licencias_query):
        if cliente_id not in ultimas:
            ultimas[cliente_id] = licencia

    filas: list[CustomerRow] = []
    for customer, total in db.execute(query.order_by(Customer.nombre.asc())).all():
        ultima = ultimas.get(customer.id)
        etiqueta, tono = _estado_comercial_de(ultima)

        filas.append(
            CustomerRow(
                id=customer.id,
                nombre=customer.nombre,
                propietario=customer.propietario,
                telefono=customer.telefono,
                direccion=customer.direccion,
                ciudad=customer.ciudad,
                activo=customer.activo,
                equipos_activos=int(total),
                estado_comercial=etiqueta,
                estado_tono=tono,
                ultima_licencia_at=to_utc(ultima.emitida_at) if ultima else None,
            )
        )
    return filas


def create_customer(
    db: Session, *, actor_id: str, nombre: str, propietario: str | None = None,
    telefono: str | None = None, direccion: str | None = None, ciudad: str | None = None,
) -> Customer:
    """Alta de una verduleria. Solo el nombre es obligatorio (UI-03)."""
    nombre = (nombre or "").strip()
    if not nombre:
        raise DirectoryError("El nombre del negocio es obligatorio.")

    existente = db.execute(
        select(Customer).where(func.lower(Customer.nombre) == nombre.lower())
    ).scalar_one_or_none()
    if existente is not None and existente.activo:
        raise DirectoryError(f"Ya existe un negocio activo llamado {nombre}.")

    customer = Customer(
        nombre=nombre,
        propietario=(propietario or "").strip() or None,
        telefono=(telefono or "").strip() or None,
        direccion=(direccion or "").strip() or None,
        ciudad=(ciudad or "").strip() or None,
        activo=True,
        creado_por_id=actor_id,
    )
    db.add(customer)
    db.flush()
    return customer


def set_customer_active(db: Session, *, customer: Customer, active: bool) -> None:
    """Desactiva sin borrar: equipos y licencias se conservan."""
    customer.activo = active
    db.flush()


# --------------------------------------------------------------------------- #
# Equipos (UI-04)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EquipmentRow:
    """Fila de la tabla de UI-04."""

    id: str
    hardware_code: str
    hardware_code_formateado: str
    cliente_nombre: str | None
    licencia_id: str | None
    licencia_tipo: str | None
    firma_presente: bool
    emitida_at: object | None


def format_hardware_code(code: str) -> str:
    """Presenta el codigo en el bloqueo de UI-06: ``7B4F-892E-...``.

    Es solo cosmético: se guarda y se firma siempre la forma continua de 16
    hex; los guiones existen para que un tecnico no se equivoque al leerlo.
    """
    return "-".join(code[i : i + 4] for i in range(0, 16, 4))


def format_cop(monto: int) -> str:
    """Presenta un entero en COP con separador de miles colombiano.

    El monto se almacena siempre como ``long``; aqui solo se formatea para
    mostrar. Ningun punto del sistema convierte el dinero a float.
    """
    return f"${int(monto):,}".replace(",", ".") + " COP"


def list_equipment(db: Session, busqueda: str = "") -> list[EquipmentRow]:
    """Inventario completo, con la licencia vigente de cada equipo."""
    query = select(Equipment, Customer.nombre).outerjoin(
        Customer, Customer.id == Equipment.cliente_id
    )

    termino = (busqueda or "").strip().upper()
    if termino:
        # Se busca tambien sin guiones: quien pega ``7B4F-892E`` debe encontrarlo.
        limpio = "".join(ch for ch in termino if ch not in " -")
        query = query.where(
            Equipment.hardware_code.like(f"%{limpio}%")
            | func.upper(func.coalesce(Customer.nombre, "")).like(f"%{termino}%")
        )

    filas: list[EquipmentRow] = []
    # Una sola pasada para la licencia vigente de todos los equipos.
    ultimas: dict[str, License] = {}
    licencias_query = (
        select(License.equipo_id, License)
        .order_by(License.equipo_id.asc(), License.emitida_at.desc())
    )
    for equipo_id, licencia in db.execute(licencias_query):
        if equipo_id not in ultimas:
            ultimas[equipo_id] = licencia

    for equipo, cliente_nombre in db.execute(query.order_by(Equipment.creado_at.desc())).all():
        licencia = ultimas.get(equipo.id)

        filas.append(
            EquipmentRow(
                id=equipo.id,
                hardware_code=equipo.hardware_code,
                hardware_code_formateado=format_hardware_code(equipo.hardware_code),
                cliente_nombre=cliente_nombre,
                licencia_id=licencia.id if licencia else None,
                licencia_tipo=licencia.tipo_emision if licencia else None,
                # Nunca se envia el texto de la licencia a la tabla: solo si
                # existe firma se muestra el indicador de UI-04.
                firma_presente=bool(licencia and licencia.licencia_text),
                emitida_at=to_utc(licencia.emitida_at) if licencia else None,
            )
        )
    return filas


def create_equipment(
    db: Session, *, actor_id: str, hardware_code_raw: str, cliente_id: str | None = None,
    notas: str | None = None,
) -> Equipment:
    """Alta de equipo. El codigo se normaliza igual que al firmar."""
    try:
        hardware_code = normalize_machine_code(hardware_code_raw)
    except Frb1Error as exc:
        raise DirectoryError(str(exc)) from exc

    existente = db.execute(
        select(Equipment).where(Equipment.hardware_code == hardware_code)
    ).scalar_one_or_none()
    if existente is not None:
        raise DirectoryError(
            f"El equipo {format_hardware_code(hardware_code)} ya esta registrado."
        )

    if cliente_id and db.get(Customer, cliente_id) is None:
        raise DirectoryError("La verduleria indicada no existe.")

    equipo = Equipment(
        hardware_code=hardware_code,
        cliente_id=cliente_id or None,
        notas=(notas or "").strip() or None,
        creado_por_id=actor_id,
    )
    db.add(equipo)
    db.flush()
    return equipo


def link_equipment_to_customer(db: Session, *, equipo: Equipment, cliente_id: str | None) -> None:
    """Vincula o desvincula un equipo de una verduleria."""
    if cliente_id and db.get(Customer, cliente_id) is None:
        raise DirectoryError("La verduleria indicada no existe.")
    equipo.cliente_id = cliente_id or None
    db.flush()


# --------------------------------------------------------------------------- #
# Ficha del equipo (UI-06)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FichaEquipo:
    """Todo lo que UI-06 muestra de una maquina, ya resuelto.

    No contiene la clave privada ni nada criptografico sensible: el algoritmo
    se muestra como etiqueta y la huella solo cuando existe llave cargada.
    """

    equipo: Equipment
    cliente: Customer | None
    licencia: License | None
    ingresos: list  # objects de tipo Income
    historial: list  # dicts legibles para la linea temporal
    # Copia del texto de licencia. Solo se llena si la licencia existe y esta
    # entregada; la decision de mostrarla es de UI-06, no del servicio.
    licencia_text: str | None
    ingresado_por: str | None


def load_equipment_file(db: Session, equipo_id: str) -> FichaEquipo | None:
    """Carga la ficha completa de un equipo. None si no existe."""
    equipo = db.get(Equipment, equipo_id)
    if equipo is None:
        return None

    cliente = db.get(Customer, equipo.cliente_id) if equipo.cliente_id else None

    licencia = db.execute(
        select(License)
        .where(License.equipo_id == equipo.id)
        .order_by(License.emitida_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    ingresos = []
    if licencia is not None:
        from license_manager.models import Income

        ingresos = list(
            db.execute(
                select(Income).where(Income.licencia_id == licencia.id).order_by(Income.creado_at.asc())
            )
            .scalars()
            .all()
        )

    historial = _build_timeline(db, equipo, licencia, ingresos)

    return FichaEquipo(
        equipo=equipo,
        cliente=cliente,
        licencia=licencia,
        ingresos=ingresos,
        historial=historial,
        licencia_text=licencia.licencia_text if licencia else None,
        ingresado_por=_nombre_usuario(db, equipo.creado_por_id),
    )


def _nombre_usuario(db: Session, user_id: str | None) -> str | None:
    if not user_id:
        return None
    from license_manager.models import User

    user = db.get(User, user_id)
    return user.nombre_completo if user else None


def _build_timeline(
    db: Session, equipo: Equipment, licencia: License | None, ingresos: list
) -> list[dict]:
    """Linea temporal de auditoria de UI-06.

    Se arma a partir de los registros de negocio (alta, emision, cobros) y no
    de la tabla ``auditoria``: esa pertenece a JHO-105 y aqui solo se muestra
    la trazabilidad que ya existe en los datos.
    """
    eventos: list[dict] = [
        {
            "titulo": "Equipo registrado en el inventario",
            "cuando": to_utc(equipo.creado_at),
            "quien": _nombre_usuario(db, equipo.creado_por_id),
            "detalle": f"Codigo {format_hardware_code(equipo.hardware_code)} dado de alta.",
            "tono": "info",
        }
    ]

    if licencia is not None:
        emisor = _nombre_usuario(db, licencia.creado_por_id)
        eventos.append(
            {
                "titulo": "Licencia emitida",
                "cuando": to_utc(licencia.emitida_at),
                "quien": emisor,
                "detalle": (
                    f"Tipo {licencia.tipo_emision}, estado de registro "
                    f"{licencia.estado_registro}."
                ),
                "tono": "exito",
            }
        )

    for ingreso in ingresos:
        emisor = _nombre_usuario(db, ingreso.creado_por_id)
        eventos.append(
            {
                "titulo": f"Ingreso registrado: {format_cop(ingreso.monto_cop)}",
                "cuando": to_utc(ingreso.creado_at),
                "quien": emisor,
                "detalle": f"{ingreso.estado_pago} · {ingreso.metodo_pago or 'metodo no registrado'}",
                "tono": "info" if ingreso.estado_pago == PaymentStatus.PAID.value else "aviso",
            }
        )

    eventos.sort(key=lambda e: e["cuando"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    return eventos
