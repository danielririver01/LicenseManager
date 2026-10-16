"""Modelo mayorista: tecnicos, paquetes prepago y saldo de cupos (JHO-104).

Reglas que este modulo materializa (doc 04 v2.1, secciones 3, 5 y 6):

* El tecnico balancero COMPRA cupos por adelantado. La sociedad cobra el
  paquete completo en ese momento (un ingreso por ``paquete_mayorista``) y el
  tecnico revende cada licencia al PVP de $ 450.000 COP: la sociedad ya no
  vuelve a cobrar cuando la emision se descuenta.
* El saldo de un tecnico es ``suma(licencias_total) - suma(licencias_usadas)``:
  se calcula, no se guarda a mano, asi que nunca puede desincronizarse de los
  paquetes reales.
* Cada emision comercial a nombre de un tecnico descuenta UN cupo del paquete
  mas antiguo con saldo (FIFO). Sin saldo no hay emision: se rechaza entero.
* Una reemision por reemplazo fisico justificado (doc 04, seccion 6.2) va a
  $ 0 COP y NO descuenta cupo: la licencia original ya estaba pagada. Solo la
  ampliacion de cajas (seccion 6.3) vuelve a consumir cupo regular.
* El descuento de cupo es dinero que ya entro: se registra como movimiento de
  $ 0 con categoria ``paquete_mayorista`` para documentarlo sin inflar la
  recaudacion de UI-07 / UI-07B.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from license_manager.models import (
    Income,
    IncomeCategory,
    PaymentStatus,
    Technician,
    User,
    WholesalePackage,
    utcnow,
)
from license_manager.services.directory import format_cop
from license_manager.services.ingresos import ETIQUETAS_METODO, IngresoError, parse_metodo

_LARGO_COMPROBANTE = 120


class MayoristaError(ValueError):
    """Rechazo del modelo mayorista con mensaje para mostrar en pantalla."""


# --------------------------------------------------------------------------- #
# Catalogo de paquetes (doc 04 v2.1, seccion 3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TipoPaquete:
    """Paquete prepago ofrecido por la sociedad."""

    valor: str
    etiqueta: str
    licencias: int
    valor_total_cop: int
    costo_unitario_cop: int

    @property
    def resumen(self) -> str:
        return (
            f"{self.licencias} licencias por {format_cop(self.valor_total_cop)} "
            f"({format_cop(self.costo_unitario_cop)} c/u)"
        )


TIPOS_PAQUETE: tuple[TipoPaquete, ...] = (
    TipoPaquete(
        "inicial", "Paquete Inicial · 3 licencias", 3, 960_000, 320_000
    ),
    TipoPaquete(
        "profesional", "Paquete Profesional · 5 licencias", 5, 1_450_000, 290_000
    ),
    TipoPaquete(
        "master", "Paquete Máster · 10 licencias", 10, 2_600_000, 260_000
    ),
)


def tipo_paquete_por(valor: str) -> TipoPaquete | None:
    limpio = (valor or "").strip().lower()
    for tipo in TIPOS_PAQUETE:
        if tipo.valor == limpio:
            return tipo
    return None


def etiqueta_paquete(valor: str) -> str:
    tipo = tipo_paquete_por(valor)
    return tipo.etiqueta if tipo else valor


# --------------------------------------------------------------------------- #
# Lecturas para la pantalla (UI-10)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TecnicoFila:
    """Fila de la tabla de tecnicos con su saldo consolidado."""

    id: str
    nombre: str
    telefono: str | None
    email: str | None
    activo: bool
    paquetes_n: int
    cupos_totales: int
    cupos_usados: int
    cupos_disponibles: int
    creado_at: object


@dataclass(frozen=True)
class PaqueteFila:
    """Fila del historial de compras de paquetes prepago."""

    id: str
    tecnico_id: str
    tecnico_nombre: str
    tipo: str
    tipo_etiqueta: str
    licencias_total: int
    licencias_usadas: int
    cupos_disponibles: int
    costo_unitario_cop: int
    valor_total_cop: int
    comprado_at: object
    registrado_por: str | None


@dataclass(frozen=True)
class ResumenMayorista:
    """KPI de cabecera de UI-10."""

    tecnicos_activos_n: int
    cupos_vendidos: int
    cupos_usados: int
    cupos_disponibles: int
    recaudado_cop: int


@dataclass(frozen=True)
class TecnicoOpcion:
    """Tecnico ofrecido en el selector de UI-05."""

    id: str
    nombre: str
    cupos_disponibles: int
    activo: bool


def tecnico_de(db: Session, tecnico_id: str) -> Technician | None:
    return db.get(Technician, tecnico_id) if tecnico_id else None


def _agregado_paquetes(db: Session):
    """Subconsulta con los totales por tecnico, para no repetir el GROUP BY."""
    return (
        select(
            WholesalePackage.tecnico_id.label("tecnico_id"),
            func.coalesce(func.sum(WholesalePackage.licencias_total), 0).label("total"),
            func.coalesce(func.sum(WholesalePackage.licencias_usadas), 0).label("usadas"),
            func.count(WholesalePackage.id).label("paquetes"),
        )
        .group_by(WholesalePackage.tecnico_id)
        .subquery()
    )


def listar_tecnicos(db: Session, busqueda: str = "") -> list[TecnicoFila]:
    agregados = _agregado_paquetes(db)
    query = select(
        Technician,
        func.coalesce(agregados.c.total, 0),
        func.coalesce(agregados.c.usadas, 0),
        func.coalesce(agregados.c.paquetes, 0),
    ).outerjoin(agregados, agregados.c.tecnico_id == Technician.id)

    termino = (busqueda or "").strip()
    if termino:
        patron = f"%{termino.lower()}%"
        query = query.where(
            func.lower(Technician.nombre).like(patron)
            | func.lower(func.coalesce(Technician.email, "")).like(patron)
            | func.coalesce(Technician.telefono, "").like(f"%{termino}%")
        )

    filas: list[TecnicoFila] = []
    for tecnico, total, usadas, paquetes in db.execute(
        query.order_by(Technician.nombre.asc())
    ).all():
        filas.append(
            TecnicoFila(
                id=tecnico.id,
                nombre=tecnico.nombre,
                telefono=tecnico.telefono,
                email=tecnico.email,
                activo=tecnico.activo,
                paquetes_n=int(paquetes),
                cupos_totales=int(total),
                cupos_usados=int(usadas),
                cupos_disponibles=int(total) - int(usadas),
                creado_at=tecnico.creado_at,
            )
        )
    return filas


def listar_paquetes(db: Session, *, tecnico_id: str | None = None) -> list[PaqueteFila]:
    query = (
        select(WholesalePackage, Technician.nombre, User.nombre_completo)
        .join(Technician, Technician.id == WholesalePackage.tecnico_id, isouter=True)
        .join(User, User.id == WholesalePackage.registrado_por_id, isouter=True)
        .order_by(WholesalePackage.comprado_at.desc(), WholesalePackage.id.desc())
    )
    if tecnico_id:
        query = query.where(WholesalePackage.tecnico_id == tecnico_id)

    filas: list[PaqueteFila] = []
    for paquete, nombre, registrado in db.execute(query).all():
        total = int(paquete.licencias_total)
        usadas = int(paquete.licencias_usadas)
        filas.append(
            PaqueteFila(
                id=paquete.id,
                tecnico_id=paquete.tecnico_id,
                tecnico_nombre=nombre or "Cuenta eliminada",
                tipo=paquete.tipo_paquete,
                tipo_etiqueta=etiqueta_paquete(paquete.tipo_paquete),
                licencias_total=total,
                licencias_usadas=usadas,
                cupos_disponibles=total - usadas,
                costo_unitario_cop=int(paquete.costo_unitario_cop),
                valor_total_cop=int(paquete.valor_total_cop),
                comprado_at=paquete.comprado_at,
                registrado_por=registrado,
            )
        )
    return filas


def resumen_mayorista(db: Session) -> ResumenMayorista:
    activos = int(
        db.execute(
            select(func.count(Technician.id)).where(Technician.activo.is_(True))
        ).scalar_one()
    )
    vendidos, usados, recaudado = db.execute(
        select(
            func.coalesce(func.sum(WholesalePackage.licencias_total), 0),
            func.coalesce(func.sum(WholesalePackage.licencias_usadas), 0),
            func.coalesce(func.sum(WholesalePackage.valor_total_cop), 0),
        )
    ).one()
    vendidos = int(vendidos)
    usados = int(usados)
    return ResumenMayorista(
        tecnicos_activos_n=activos,
        cupos_vendidos=vendidos,
        cupos_usados=usados,
        cupos_disponibles=vendidos - usados,
        recaudado_cop=int(recaudado),
    )


def tecnicos_para_emision(db: Session) -> list[TecnicoOpcion]:
    """Tecnicos vivos con su saldo, para el selector de UI-05."""
    agregados = _agregado_paquetes(db)
    query = (
        select(
            Technician,
            func.coalesce(agregados.c.total, 0),
            func.coalesce(agregados.c.usadas, 0),
        )
        .outerjoin(agregados, agregados.c.tecnico_id == Technician.id)
        .order_by(Technician.nombre.asc())
    )
    return [
        TecnicoOpcion(
            id=tecnico.id,
            nombre=tecnico.nombre,
            cupos_disponibles=int(total) - int(usadas),
            activo=tecnico.activo,
        )
        for tecnico, total, usadas in db.execute(query).all()
    ]


# --------------------------------------------------------------------------- #
# Alta de tecnicos y registro de paquetes
# --------------------------------------------------------------------------- #


def crear_tecnico(
    db: Session,
    *,
    actor_id: str | None,
    nombre: str,
    telefono: str | None = None,
    email: str | None = None,
) -> Technician:
    """Alta de un tecnico balancero. Solo el nombre es obligatorio."""
    limpio = (nombre or "").strip()
    if not limpio:
        raise MayoristaError("El nombre del tecnico es obligatorio.")

    existente = db.execute(
        select(Technician).where(func.lower(Technician.nombre) == limpio.lower())
    ).scalar_one_or_none()
    if existente is not None and existente.activo:
        raise MayoristaError(f"Ya existe un tecnico activo llamado {limpio}.")

    tecnico = Technician(
        nombre=limpio,
        telefono=(telefono or "").strip() or None,
        email=(email or "").strip() or None,
        activo=True,
    )
    db.add(tecnico)
    db.flush()
    return tecnico


def set_tecnico_activo(db: Session, *, tecnico: Technician, activo: bool) -> None:
    """Desactiva sin borrar: sus paquetes y saldo se conservan."""
    tecnico.activo = activo
    db.flush()


def registrar_paquete(
    db: Session,
    *,
    actor_id: str | None,
    tecnico_id: str,
    tipo: str,
    metodo_pago: str | None = None,
    comprobante: str | None = None,
) -> tuple[WholesalePackage, Income]:
    """Registra la compra de un paquete y su ingreso mayorista.

    La sociedad cobra el paquete completo AHORA: se crea el ingreso
    ``paquete_mayorista`` por el valor total. Las emisiones posteriores solo
    consumen cupo y no vuelven a generar dinero. Devuelve el paquete y el
    ingreso para que la ruta pueda auditar las cifras reales.
    """
    tecnico = tecnico_de(db, tecnico_id)
    if tecnico is None:
        raise MayoristaError("Elige el tecnico que compra el paquete.")
    if not tecnico.activo:
        raise MayoristaError(
            f"La cuenta de {tecnico.nombre} esta desactivada: no se registran "
            "paquetes nuevos, pero su saldo historico se conserva."
        )

    seleccion = tipo_paquete_por(tipo)
    if seleccion is None:
        raise MayoristaError(
            "Elige un paquete valido: Inicial (3), Profesional (5) o Master (10)."
        )

    try:
        canal = parse_metodo(metodo_pago)
    except IngresoError as exc:
        raise MayoristaError(str(exc)) from exc

    referencia = (comprobante or "").strip()
    if len(referencia) > _LARGO_COMPROBANTE:
        raise MayoristaError(
            f"El comprobante no puede pasar de {_LARGO_COMPROBANTE} caracteres."
        )

    paquete = WholesalePackage(
        tecnico_id=tecnico.id,
        tipo_paquete=seleccion.valor,
        licencias_total=seleccion.licencias,
        licencias_usadas=0,
        costo_unitario_cop=seleccion.costo_unitario_cop,
        valor_total_cop=seleccion.valor_total_cop,
        registrado_por_id=actor_id,
    )
    db.add(paquete)
    db.flush()

    hoy = utcnow().date()
    nota = f"{seleccion.etiqueta} para {tecnico.nombre}."
    if referencia:
        nota = f"{nota} Comprobante: {referencia}."
    ingreso = Income(
        licencia_id=None,
        categoria=IncomeCategory.WHOLESALE_PACKAGE.value,
        monto_cop=seleccion.valor_total_cop,
        moneda="COP",
        estado_pago=PaymentStatus.PAID.value,
        metodo_pago=canal,
        nota=nota,
        periodo=hoy,
        pagado_at=utcnow(),
        creado_por_id=actor_id,
    )
    db.add(ingreso)
    db.flush()
    return paquete, ingreso


# --------------------------------------------------------------------------- #
# Saldo y consumo de cupos
# --------------------------------------------------------------------------- #


def saldo_tecnico(db: Session, tecnico_id: str) -> int:
    """Cupos disponibles: la suma de los paquetes menos lo ya consumido."""
    total, usadas = db.execute(
        select(
            func.coalesce(func.sum(WholesalePackage.licencias_total), 0),
            func.coalesce(func.sum(WholesalePackage.licencias_usadas), 0),
        ).where(WholesalePackage.tecnico_id == tecnico_id)
    ).one()
    return int(total) - int(usadas)


def paquete_con_saldo(db: Session, tecnico_id: str) -> WholesalePackage | None:
    """Paquete mas antiguo con cupo libre: el orden FIFO del descuento."""
    return db.execute(
        select(WholesalePackage)
        .where(
            WholesalePackage.tecnico_id == tecnico_id,
            WholesalePackage.licencias_usadas < WholesalePackage.licencias_total,
        )
        .order_by(WholesalePackage.comprado_at.asc(), WholesalePackage.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def consumir_cupo(db: Session, tecnico: Technician) -> WholesalePackage:
    """Descuenta UN cupo del paquete mas antiguo con saldo.

    Se ejecuta dentro de la transaccion de la emision: si algo falla despues,
    el ``rollback`` devuelve el cupo junto con la licencia.
    """
    paquete = paquete_con_saldo(db, tecnico.id)
    if paquete is None:
        raise MayoristaError(
            f"{tecnico.nombre} no tiene cupos disponibles. Registra un paquete "
            "prepago nuevo antes de emitir a su nombre."
        )
    paquete.licencias_usadas = int(paquete.licencias_usadas) + 1
    db.flush()
    return paquete


__all__ = [
    "ETIQUETAS_METODO",
    "MayoristaError",
    "PaqueteFila",
    "ResumenMayorista",
    "TIPOS_PAQUETE",
    "TecnicoFila",
    "TecnicoOpcion",
    "TipoPaquete",
    "consumir_cupo",
    "crear_tecnico",
    "etiqueta_paquete",
    "listar_paquetes",
    "listar_tecnicos",
    "paquete_con_saldo",
    "registrar_paquete",
    "resumen_mayorista",
    "saldo_tecnico",
    "set_tecnico_activo",
    "tecnico_de",
    "tecnicos_para_emision",
    "tipo_paquete_por",
]
