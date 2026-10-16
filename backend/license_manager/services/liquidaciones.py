"""Liquidacion y reparto de utilidades (JHO-103, UI-11).

Reglas que este modulo materializa (doc 01 LM-RF-009, doc 04 v2.1 seccion 7,
mockup 11-liquidacion-administradores):

* Pantalla EXCLUSIVA de Titulares (Titular Principal y Co-Titular). El
  administrador delegado recibe 403 estricto, igual que en UI-09.
* El total bruto es ``licencias cobradas x $ 450.000 COP``: la tarifa oficial
  del doc 04, sin decimales.
* La comision se calcula por PORCENTAJE sobre el bruto o por VALOR FIJO en
  COP por licencia. El resultado siempre es un entero de pesos.
* El pago se asienta con canal (Nequi, Daviplata, Bancolombia, efectivo u
  otro) y comprobante obligatorio; al confirmarlo la liquidacion queda
  SELLADA: sus importes no se tocan mas.
* Cada alta, recalculo y pago deja su evento de auditoria encadenado.

La liquidacion es una salida de dinero (la sociedad paga una comision), por
eso vive en su propia tabla y NO crea un registro en ``ingresos``: contarlo
como ingreso inflaria la recaudacion de UI-07 y UI-07B.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from license_manager.models import Income, PaymentMethod, PaymentStatus, Settlement, User, utcnow
from license_manager.services.directory import format_cop
from license_manager.services.ingresos import ETIQUETAS_METODO, parse_metodo
from license_manager.services.licencias import PRECIO_VENTA_COP, EmissionError, parse_importe_cop
from license_manager.services.periodos import etiqueta_periodo, fin_del_periodo

# Selector "3. Tipo de comision" del mockup.
TIPOS_COMISION = (
    ("porcentaje", "Porcentaje (%) por venta"),
    ("fijo", "Valor fijo ($ COP) por licencia"),
)

ETIQUETAS_ESTADO = {
    "pendiente": "Pendiente de pago",
    "pagado": "Liquidado y Pagado",
}

TONOS_ESTADO = {
    "pendiente": "ambar",
    "pagado": "verde",
}

# Canales que admite el modal "Registrar Pago de Comision" del mockup.
CANALES_PAGO = (
    PaymentMethod.NEQUI.value,
    PaymentMethod.DAVIPLATA.value,
    PaymentMethod.BANCOLOMBIA.value,
    PaymentMethod.CASH.value,
    PaymentMethod.OTHER.value,
)

# Pares (valor, etiqueta) para el select del modal de pago.
CANALES_PAGO_ETIQUETA = tuple(
    (canal, ETIQUETAS_METODO[canal]) for canal in CANALES_PAGO
)

_LARGO_COMPROBANTE = 120
_SOLO_DIGITOS = r"\d{1,3}"


class LiquidacionError(ValueError):
    """Rechazo de liquidacion con un mensaje pensado para mostrarse en pantalla."""


# --------------------------------------------------------------------------- #
# Parseo de lo que digita el Titular
# --------------------------------------------------------------------------- #


def parse_licencias(texto: str) -> int:
    """Cantidad de licencias cobradas (entero, al menos una)."""
    limpio = (texto or "").strip()
    if not limpio:
        raise LiquidacionError("Indica la cantidad de licencias cobradas.")
    if re.fullmatch(_SOLO_DIGITOS, limpio) is None:
        raise LiquidacionError("La cantidad de licencias debe ser un numero entero.")
    cantidad = int(limpio)
    if cantidad < 1:
        raise LiquidacionError("Se liquida a partir de una licencia cobrada.")
    return cantidad


def parse_tipo_comision(texto: str) -> str:
    valor = (texto or "").strip().lower()
    for tipo, _ in TIPOS_COMISION:
        if valor == tipo:
            return tipo
    raise LiquidacionError("Elige un tipo de comision valido: porcentaje o valor fijo.")


def parse_valor_comision(texto: str, *, tipo: str) -> int:
    """Porcentaje entero (1-100) o valor fijo entero en COP."""
    limpio = (texto or "").strip()
    if not limpio:
        raise LiquidacionError("Escribe el valor de la comision.")

    if tipo == "porcentaje":
        if re.fullmatch(r"\d{1,3}", limpio) is None:
            raise LiquidacionError("El porcentaje debe ser un numero entero entre 1 y 100.")
        valor = int(limpio)
        if not 1 <= valor <= 100:
            raise LiquidacionError("El porcentaje de comision va de 1 a 100.")
        return valor

    try:
        valor = parse_importe_cop(limpio)
    except EmissionError as exc:
        raise LiquidacionError(str(exc)) from exc
    if valor <= 0:
        raise LiquidacionError("El valor fijo de la comision debe ser mayor que cero.")
    return valor


def calcular_comision(
    *, licencias: int, tipo: str, valor: int, precio_unitario: int = PRECIO_VENTA_COP
) -> tuple[int, int, int]:
    """(bruto, comision, ganancia neta) en enteros de COP.

    El porcentaje se redondea al peso mas cercano con aritmetica entera:
    ``30% de $1.350.000`` da exactos ``$405.000``, y un porcentaje que no
    cae redondo nunca genera un centavo.
    """
    bruto = licencias * precio_unitario
    comision = (bruto * valor + 50) // 100 if tipo == "porcentaje" else licencias * valor
    return bruto, comision, bruto - comision


# --------------------------------------------------------------------------- #
# Alta y recalculo de la liquidacion
# --------------------------------------------------------------------------- #


def _beneficiario(db: Session, administrador_id: str, actor_id: str | None) -> User:
    beneficiario = db.get(User, administrador_id) if administrador_id else None
    if beneficiario is None:
        raise LiquidacionError("Elige a quien va dirigida la liquidacion.")
    if actor_id and beneficiario.id == actor_id:
        raise LiquidacionError(
            "No puedes liquidarte a ti mismo: registra la comision de otro "
            "operador o socio de la sociedad."
        )
    if not beneficiario.activo:
        raise LiquidacionError(
            f"La cuenta de {beneficiario.nombre_completo} esta desactivada: "
            "sus liquidaciones historicas se conservan, pero no se liquidan nuevas."
        )
    return beneficiario


def liquidacion_de(db: Session, liquidacion_id: str) -> Settlement | None:
    return db.get(Settlement, liquidacion_id) if liquidacion_id else None


def liquidacion_del_periodo(db: Session, *, periodo: date, administrador_id: str) -> Settlement | None:
    return db.execute(
        select(Settlement).where(
            Settlement.periodo == periodo,
            Settlement.administrador_id == administrador_id,
        )
    ).scalar_one_or_none()


def registrar_liquidacion(
    db: Session,
    *,
    actor_id: str | None,
    periodo: date,
    administrador_id: str,
    licencias: str,
    tipo: str,
    valor: str,
) -> tuple[Settlement, str]:
    """Crea o recalcula la liquidacion pendiente de un operador en el mes.

    Devuelve ``(liquidacion, accion)`` donde accion es ``creada`` o
    ``recalculada``: la auditoria distingue entre abrir una liquidacion nueva
    y ajustar una que todavia no se ha pagado.
    """
    beneficiario = _beneficiario(db, administrador_id, actor_id)
    cantidad = parse_licencias(licencias)
    tipo_limpio = parse_tipo_comision(tipo)
    valor_limpio = parse_valor_comision(valor, tipo=tipo_limpio)

    bruto, comision, neta = calcular_comision(
        licencias=cantidad, tipo=tipo_limpio, valor=valor_limpio
    )
    if comision > bruto:
        raise LiquidacionError(
            "La comision supera el total recaudado: revisa el valor acordado."
        )

    existente = liquidacion_del_periodo(
        db, periodo=periodo, administrador_id=beneficiario.id
    )
    if existente is not None and existente.pagado_at is not None:
        raise LiquidacionError(
            "La liquidacion de este periodo ya fue pagada y sellada: sus "
            "importes no se pueden modificar. Cualquier ajuste se registra en "
            "el periodo siguiente."
        )

    if existente is None:
        liquidacion = Settlement(
            periodo=periodo,
            administrador_id=beneficiario.id,
            licencias_vendidas=cantidad,
            precio_unitario_cop=PRECIO_VENTA_COP,
            tipo_comision=tipo_limpio,
            valor_comision=valor_limpio,
            monto_bruto_cop=bruto,
            comision_cop=comision,
            ganancia_neta_cop=neta,
        )
        db.add(liquidacion)
        db.flush()
        return liquidacion, "creada"

    # Una sola fila por (periodo, beneficiario): la unica deuda posible de un
    # mes se recalcula en su lugar de abrir una segunda copia.
    existente.licencias_vendidas = cantidad
    existente.precio_unitario_cop = PRECIO_VENTA_COP
    existente.tipo_comision = tipo_limpio
    existente.valor_comision = valor_limpio
    existente.monto_bruto_cop = bruto
    existente.comision_cop = comision
    existente.ganancia_neta_cop = neta
    db.flush()
    return existente, "recalculada"


def confirmar_pago(
    db: Session,
    *,
    liquidacion: Settlement,
    actor_id: str | None,
    metodo: str,
    comprobante: str,
) -> Settlement:
    """Sella la liquidacion: canal de pago + comprobante obligatorios."""
    if liquidacion.pagado_at is not None:
        raise LiquidacionError("Esta liquidacion ya esta pagada y sellada.")

    canal = parse_metodo(metodo)
    if canal is None or canal not in CANALES_PAGO:
        raise LiquidacionError(
            "Elige el canal de pago: Nequi, Daviplata, Bancolombia, efectivo u otro."
        )

    referencia = (comprobante or "").strip()
    if not referencia:
        raise LiquidacionError(
            "Agrega la referencia o comprobante del pago (por ejemplo Nequi #849201)."
        )
    if len(referencia) > _LARGO_COMPROBANTE:
        raise LiquidacionError(
            f"El comprobante no puede pasar de {_LARGO_COMPROBANTE} caracteres."
        )

    liquidacion.metodo_pago = canal
    liquidacion.nota = referencia
    liquidacion.pagado_at = utcnow()
    liquidacion.confirmado_por_id = actor_id
    db.flush()
    return liquidacion


# --------------------------------------------------------------------------- #
# Ventas del periodo (alimentan el prellenado del formulario)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VendedorPeriodo:
    """Licencias cobradas por una persona en un mes."""

    usuario_id: str
    licencias_n: int
    monto_cop: int


def ventas_del_periodo(db: Session, *, periodo: date) -> list[VendedorPeriodo]:
    """Cobros por licencia de cada persona en el mes, de mayor a menor."""
    inicio, fin = periodo, fin_del_periodo(periodo)
    filas = db.execute(
        select(
            Income.creado_por_id,
            func.count(Income.id),
            func.coalesce(func.sum(Income.monto_cop), 0),
        )
        .where(
            Income.creado_por_id.is_not(None),
            Income.periodo >= inicio,
            Income.periodo < fin,
            Income.estado_pago == PaymentStatus.PAID.value,
            Income.monto_cop > 0,
            # Solo licencias cobradas al verdulero: un paquete mayorista no
            # genera comision de operador.
            Income.categoria.in_(("venta", "cobro_prueba")),
        )
        .group_by(Income.creado_por_id)
        .order_by(func.count(Income.id).desc())
    ).all()
    return [
        VendedorPeriodo(usuario_id=usuario_id, licencias_n=int(n), monto_cop=int(monto))
        for usuario_id, n, monto in filas
    ]


# --------------------------------------------------------------------------- #
# Lecturas para la pantalla (UI-11)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LiquidacionFila:
    """Fila de la tabla "Estado de Liquidacion del Periodo"."""

    id: str
    periodo: date
    beneficiario_id: str
    beneficiario_nombre: str
    beneficiario_email: str
    licencias: int
    precio_unitario_cop: int
    tipo: str
    valor: int
    monto_bruto_cop: int
    comision_cop: int
    ganancia_neta_cop: int
    comision_acordada: str
    estado: str
    estado_etiqueta: str
    estado_tono: str
    metodo_pago: str | None
    metodo_etiqueta: str
    comprobante: str | None
    pagado_at: datetime | None
    confirmado_por: str | None

    @property
    def pagado(self) -> bool:
        return self.estado == "pagado"


def etiqueta_comision(tipo: str, valor: int) -> str:
    """"30% por venta" / "$100.000 fija / lic." (columna del mockup)."""
    if tipo == "porcentaje":
        return f"{valor}% por venta"
    return f"{format_cop(valor)} fija / lic."


def _fila_de(db: Session, liquidacion: Settlement, beneficiario: User | None) -> LiquidacionFila:
    estado = "pagado" if liquidacion.pagado_at is not None else "pendiente"
    confirmador = None
    if liquidacion.confirmado_por_id:
        cuenta = db.get(User, liquidacion.confirmado_por_id)
        confirmador = cuenta.nombre_completo if cuenta else None
    return LiquidacionFila(
        id=liquidacion.id,
        periodo=liquidacion.periodo,
        beneficiario_id=liquidacion.administrador_id,
        beneficiario_nombre=(
            beneficiario.nombre_completo if beneficiario else "Cuenta eliminada"
        ),
        beneficiario_email=beneficiario.email if beneficiario else "—",
        licencias=int(liquidacion.licencias_vendidas),
        precio_unitario_cop=int(liquidacion.precio_unitario_cop),
        tipo=liquidacion.tipo_comision,
        valor=int(liquidacion.valor_comision),
        monto_bruto_cop=int(liquidacion.monto_bruto_cop),
        comision_cop=int(liquidacion.comision_cop),
        ganancia_neta_cop=int(liquidacion.ganancia_neta_cop),
        comision_acordada=etiqueta_comision(
            liquidacion.tipo_comision, int(liquidacion.valor_comision)
        ),
        estado=estado,
        estado_etiqueta=ETIQUETAS_ESTADO[estado],
        estado_tono=TONOS_ESTADO[estado],
        metodo_pago=liquidacion.metodo_pago,
        metodo_etiqueta=ETIQUETAS_METODO.get(
            liquidacion.metodo_pago or "", liquidacion.metodo_pago or ""
        ),
        comprobante=liquidacion.nota,
        pagado_at=liquidacion.pagado_at,
        confirmado_por=confirmador,
    )


def listar_liquidaciones(db: Session, *, periodo: date) -> list[LiquidacionFila]:
    filas = db.execute(
        select(Settlement, User)
        .join(User, User.id == Settlement.administrador_id, isouter=True)
        .where(Settlement.periodo == periodo)
        .order_by(Settlement.creado_at.asc(), Settlement.id.asc())
    ).all()
    return [_fila_de(db, liquidacion, beneficiario) for liquidacion, beneficiario in filas]


def liquidacion_fila(db: Session, liquidacion_id: str) -> LiquidacionFila | None:
    """Fila unica para el soporte imprimible (UI-11)."""
    liquidacion = liquidacion_de(db, liquidacion_id)
    if liquidacion is None:
        return None
    beneficiario = db.get(User, liquidacion.administrador_id)
    return _fila_de(db, liquidacion, beneficiario)


def liquidacion_etiqueta(db: Session, liquidacion: Settlement) -> str:
    """Resumen corto para el aviso de pantalla."""
    beneficiario = db.get(User, liquidacion.administrador_id)
    nombre = beneficiario.nombre_completo if beneficiario else "beneficiario"
    return (
        f"{etiqueta_periodo(liquidacion.periodo)} · {nombre} · "
        f"{format_cop(liquidacion.comision_cop)}"
    )
