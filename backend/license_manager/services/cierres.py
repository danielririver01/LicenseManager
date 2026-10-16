"""Cierre contable mensual de ingresos (JHO-103, UI-07B).

Reglas que este modulo materializa (doc 01 LM-RF-009, mockup 07b):

* Dinero siempre entero en COP: totales, promedios y variaciones se
  redondean a pesos, nunca a decimales.
* Un movimiento pertenece al mes de su ``periodo`` (ver ``services.periodos``).
* UI-07B es una LECTURA de lo que ya paso en UI-07: aqui no se recalcula ni se
  congela nada, asi que un mes ya cerrado puede recibir un pago tardio sin
  romper el historial.
* El cierre por persona responde "quien registro cada cobro", que es lo mismo
  que despues alimenta la liquidacion de comisiones de UI-11.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from license_manager.models import Customer, Equipment, Income, License, PaymentStatus, User
from license_manager.services.ingresos import (
    ETIQUETAS_CATEGORIA,
    ETIQUETAS_ESTADO,
    hoy_utc,
)
from license_manager.services.periodos import (
    MESES,
    clave_periodo,
    dias_transcurridos,
    etiqueta_periodo,
    fin_del_periodo,
    periodo_anterior,
    periodo_siguiente,
)

# Etiquetas cortas de las barras del comparativo semestral (mockup: "May").
_LARGO_BARRA = 3
# Altura minima de una barra con dinero: un 1% no se ve en pantalla.
_ALTO_MINIMO = 4


class CierreError(ValueError):
    """Rechazo de cierre con un mensaje pensado para mostrarse en pantalla."""


def format_millones(monto: int) -> str:
    """``$1,8M`` / ``$450k`` para las etiquetas del grafico.

    Solo presentacion: la aritmetica del cierre se hace con enteros de COP.
    """
    if monto >= 1_000_000:
        millones = monto // 1_000_000
        resto = (monto % 1_000_000) // 100_000
        return f"${millones},{resto}M"
    if monto >= 1_000:
        return f"${monto // 1_000}k"
    return f"${monto}"


def variacion_pct(actual: int, anterior: int) -> int | None:
    """Cambio entero respecto al mes anterior. ``None`` si no hay base."""
    if anterior <= 0:
        return None
    delta = actual - anterior
    pct = (abs(delta) * 100 + anterior // 2) // anterior
    return pct if delta >= 0 else -pct


# --------------------------------------------------------------------------- #
# Lecturas del mes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MovimientoCierre:
    """Fila de "Detalle de Cobros del Mes"."""

    fecha: date
    cliente_nombre: str
    hardware_code: str | None
    licencia_key: str | None
    categoria: str
    categoria_etiqueta: str
    monto_cop: int
    estado_pago: str
    estado_etiqueta: str
    registrado_por: str
    nota: str | None


@dataclass(frozen=True)
class RepartoUsuario:
    """Lo que cobro una persona en el mes (alimenta la liquidacion de UI-11)."""

    nombre: str
    monto_cop: int
    movimientos_n: int


@dataclass(frozen=True)
class BarraMes:
    """Columna del comparativo semestral."""

    clave: str
    etiqueta: str
    monto_cop: int
    alto_pct: int
    activa: bool
    maxima: bool


@dataclass(frozen=True)
class CierreMensual:
    """Todo lo que pinta UI-07B para un mes dado."""

    periodo: date
    etiqueta: str
    clave: str
    clave_anterior: str
    clave_siguiente: str

    total_cop: int
    licencias_n: int
    variacion_pct: int | None
    pendientes_cop: int
    pendientes_n: int
    gratuitos_n: int

    dias_n: int
    promedio_diario_cop: int
    reparto: list[RepartoUsuario]

    reemplazos_n: int
    reemplazos_nota: str | None
    pruebas_n: int
    pruebas_detalle: str

    barras: list[BarraMes]
    movimientos: list[MovimientoCierre]


def _rango(periodo: date) -> tuple[date, date]:
    return periodo, fin_del_periodo(periodo)


def _total_cobrado(db: Session, periodo: date) -> int:
    """Todo el dinero efectivamente cobrado en el mes."""
    inicio, fin = _rango(periodo)
    return int(
        db.execute(
            select(func.coalesce(func.sum(Income.monto_cop), 0)).where(
                Income.periodo >= inicio,
                Income.periodo < fin,
                Income.estado_pago == PaymentStatus.PAID.value,
                Income.monto_cop > 0,
            )
        ).scalar_one()
    )


def _licencias_cobradas(db: Session, periodo: date) -> int:
    """Licencias vendidas al verdulero en el mes.

    Cuenta venta y cobro de prueba (el cobro del dia 31 conserva su categoria
    original al consolidarse). Un paquete mayorista no es una licencia vendida
    al cliente final y no entra aqui.
    """
    inicio, fin = _rango(periodo)
    return int(
        db.execute(
            select(func.count(Income.id)).where(
                Income.periodo >= inicio,
                Income.periodo < fin,
                Income.estado_pago == PaymentStatus.PAID.value,
                Income.monto_cop > 0,
                Income.categoria.in_(("venta", "cobro_prueba")),
            )
        ).scalar_one()
    )


def _pendientes_del_mes(db: Session, periodo: date) -> tuple[int, int]:
    inicio, fin = _rango(periodo)
    monto, cantidad = db.execute(
        select(
            func.coalesce(func.sum(Income.monto_cop), 0),
            func.count(Income.id),
        ).where(
            Income.periodo >= inicio,
            Income.periodo < fin,
            Income.estado_pago == PaymentStatus.PENDING.value,
        )
    ).one()
    return int(monto), int(cantidad)


def _gratuitos_del_mes(db: Session, periodo: date) -> int:
    """Reemplazos y cortesias a $ 0 COP, segregados del dinero cobrado."""
    inicio, fin = _rango(periodo)
    return int(
        db.execute(
            select(func.count(Income.id)).where(
                Income.periodo >= inicio,
                Income.periodo < fin,
                Income.categoria.in_(("gratuito", "reemplazo")),
            )
        ).scalar_one()
    )


def _reemplazos_del_mes(db: Session, periodo: date) -> tuple[int, str | None]:
    """Reemplazos del mes con la nota del ultimo (el motivo del cambio)."""
    inicio, fin = _rango(periodo)
    cantidad = int(
        db.execute(
            select(func.count(Income.id)).where(
                Income.periodo >= inicio,
                Income.periodo < fin,
                Income.categoria == "reemplazo",
            )
        ).scalar_one()
    )
    nota = db.execute(
        select(Income.nota)
        .where(Income.periodo >= inicio, Income.periodo < fin, Income.categoria == "reemplazo")
        .order_by(Income.creado_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return cantidad, (nota.strip() if nota else None)


def _reparto_del_mes(db: Session, periodo: date) -> list[RepartoUsuario]:
    inicio, fin = _rango(periodo)
    filas = db.execute(
        select(
            User.nombre_completo,
            func.coalesce(func.sum(Income.monto_cop), 0),
            func.count(Income.id),
        )
        .join(User, User.id == Income.creado_por_id, isouter=True)
        .where(
            Income.periodo >= inicio,
            Income.periodo < fin,
            Income.estado_pago == PaymentStatus.PAID.value,
            Income.monto_cop > 0,
        )
        .group_by(User.id, User.nombre_completo)
        .order_by(func.sum(Income.monto_cop).desc())
    ).all()
    return [
        RepartoUsuario(
            nombre=nombre or "Sistema",
            monto_cop=int(monto),
            movimientos_n=int(cantidad),
        )
        for nombre, monto, cantidad in filas
    ]


_PRUEBA_DETALLE = (
    ("prueba_activa", "en curso"),
    ("cobro_pendiente", "en gracia"),
    ("vencida", "vencida"),
    ("pagado", "pagada"),
)


def _pruebas_del_mes(db: Session, periodo: date) -> tuple[int, str]:
    """Pruebas iniciadas en el mes y en que punto del ciclo quedaron."""
    inicio, fin = _rango(periodo)
    filas = db.execute(
        select(License.estado_cobro, func.count(License.id))
        .where(
            License.tipo_emision == "prueba",
            License.emitida_fecha >= inicio,
            License.emitida_fecha < fin,
        )
        .group_by(License.estado_cobro)
    ).all()
    por_estado = {estado: int(n) for estado, n in filas}
    total = sum(por_estado.values())
    detalle = " · ".join(
        f"{por_estado[estado]} {etiqueta}"
        for estado, etiqueta in _PRUEBA_DETALLE
        if por_estado.get(estado)
    )
    return total, detalle


def _movimientos_del_mes(db: Session, periodo: date) -> list[MovimientoCierre]:
    inicio, fin = _rango(periodo)
    query = (
        select(Income, License, Equipment, Customer.nombre, User.nombre_completo)
        .join(License, License.id == Income.licencia_id, isouter=True)
        .join(Equipment, Equipment.id == License.equipo_id, isouter=True)
        .join(Customer, Customer.id == Equipment.cliente_id, isouter=True)
        .join(User, User.id == Income.creado_por_id, isouter=True)
        .where(Income.periodo >= inicio, Income.periodo < fin)
        .order_by(Income.periodo.desc(), Income.creado_at.desc(), Income.id.desc())
    )

    movimientos: list[MovimientoCierre] = []
    for ingreso, licencia, equipo, cliente_nombre, registrado in db.execute(query).all():
        movimientos.append(
            MovimientoCierre(
                fecha=ingreso.periodo,
                cliente_nombre=cliente_nombre or "Sin verduleria",
                hardware_code=equipo.hardware_code if equipo else None,
                licencia_key=licencia.key_id if licencia else None,
                categoria=ingreso.categoria,
                categoria_etiqueta=ETIQUETAS_CATEGORIA.get(
                    ingreso.categoria, ingreso.categoria
                ),
                monto_cop=int(ingreso.monto_cop),
                estado_pago=ingreso.estado_pago,
                estado_etiqueta=ETIQUETAS_ESTADO.get(
                    ingreso.estado_pago, ingreso.estado_pago
                ),
                registrado_por=registrado or "Sistema",
                nota=ingreso.nota,
            )
        )
    return movimientos


def _barras(db: Session, periodo: date) -> list[BarraMes]:
    """Los seis meses terminados en el seleccionado, con el actual destacado."""
    meses: list[date] = []
    cursor = periodo
    for _ in range(6):
        meses.append(cursor)
        cursor = periodo_anterior(cursor)
    meses.reverse()

    montos = [_total_cobrado(db, mes) for mes in meses]
    maximo = max(montos) if montos else 0

    barras: list[BarraMes] = []
    for mes, monto in zip(meses, montos, strict=True):
        if maximo > 0 and monto > 0:
            alto = (monto * 100 + maximo // 2) // maximo
            alto = max(_ALTO_MINIMO, min(100, alto))
        else:
            alto = 0
        es_actual = mes == periodo
        barras.append(
            BarraMes(
                clave=clave_periodo(mes),
                etiqueta=MESES[mes.month - 1][:_LARGO_BARRA],
                monto_cop=monto,
                alto_pct=alto,
                activa=es_actual,
                maxima=(not es_actual and maximo > 0 and monto == maximo),
            )
        )
    return barras


def resumen_cierre(db: Session, *, periodo: date, hoy: date | None = None) -> CierreMensual:
    """Consolidado del mes: KPIs, comparativo, detalle y cierre por persona."""
    total = _total_cobrado(db, periodo)
    licencias = _licencias_cobradas(db, periodo)
    pendientes_cop, pendientes_n = _pendientes_del_mes(db, periodo)
    gratuitos_n = _gratuitos_del_mes(db, periodo)
    reemplazos_n, reemplazos_nota = _reemplazos_del_mes(db, periodo)
    pruebas_n, pruebas_detalle = _pruebas_del_mes(db, periodo)

    dias = dias_transcurridos(periodo, hoy or hoy_utc())
    anterior_total = _total_cobrado(db, periodo_anterior(periodo))

    return CierreMensual(
        periodo=periodo,
        etiqueta=etiqueta_periodo(periodo),
        clave=clave_periodo(periodo),
        clave_anterior=clave_periodo(periodo_anterior(periodo)),
        clave_siguiente=clave_periodo(periodo_siguiente(periodo)),
        total_cop=total,
        licencias_n=licencias,
        variacion_pct=variacion_pct(total, anterior_total),
        pendientes_cop=pendientes_cop,
        pendientes_n=pendientes_n,
        gratuitos_n=gratuitos_n,
        dias_n=dias,
        promedio_diario_cop=total // dias if dias else 0,
        reparto=_reparto_del_mes(db, periodo),
        reemplazos_n=reemplazos_n,
        reemplazos_nota=reemplazos_nota,
        pruebas_n=pruebas_n,
        pruebas_detalle=pruebas_detalle,
        barras=_barras(db, periodo),
        movimientos=_movimientos_del_mes(db, periodo),
    )


# --------------------------------------------------------------------------- #
# Exportacion del cierre (boton "Exportar cierre" del mockup)
# --------------------------------------------------------------------------- #


def _celda(valor: object) -> str:
    """Celda CSV segura para Excel: separador ``;`` y texto entre comillas."""
    texto = "" if valor is None else str(valor)
    if any(car in texto for car in (';', '"', "\n", "\r")):
        return '"' + texto.replace('"', '""') + '"'
    return texto


def exportar_cierre_csv(cierre: CierreMensual) -> str:
    """CSV del mes, con BOM UTF-8 y ``sep=;`` para Excel en espanol."""
    filas: list[str] = [
        "sep=;",
        ";".join(
            ("Fecha", "Verduleria / Negocio", "Codigo equipo", "Licencia",
             "Categoria", "Importe COP", "Estado de pago", "Registrado por", "Nota")
        ),
    ]
    for mov in cierre.movimientos:
        filas.append(
            ";".join(
                _celda(valor)
                for valor in (
                    mov.fecha.strftime("%d/%m/%Y"),
                    mov.cliente_nombre,
                    mov.hardware_code or "",
                    mov.licencia_key or "",
                    mov.categoria_etiqueta,
                    mov.monto_cop,
                    mov.estado_etiqueta,
                    mov.registrado_por,
                    mov.nota or "",
                )
            )
        )
    return "\ufeff" + "\r\n".join(filas) + "\r\n"
