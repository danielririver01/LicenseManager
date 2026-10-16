"""Ingresos manuales y ciclo de cobro de la prueba (JHO-102, UI-07).

Reglas que este modulo materializa (doc 01 LM-RF-008, doc 02 seccion 5.3):

* La moneda es COP y el dinero es entero: ni float ni decimales.
* Ciclo de la prueba de 30 dias, contado en calendario desde la fecha de
  emision (el dia de emision es el dia 1):

    * dia 1 a 30  -> ``prueba_activa`` a $ 0 COP.
    * dia 31      -> ``cobro_pendiente`` a $ 450.000 COP con periodo de gracia
                     y una fila de ingreso pendiente creada por el sistema.
    * dia 34      -> ``vencida`` con alerta y cronometro CONGELADO en el 34.
    * ``pagado``  -> cronometro congelado definitivamente y venta consolidada.

* Los estados congelados no se recalculan jamas: un dia pagado o vencido no
  vuelve a correr aunque el sitio este apagado una semana.
* El alta manual de pagos (efectivo, Nequi, Daviplata, Bancolombia, otro) se
  registra sobre la licencia/operacion, con categoria y estado de pago, y se
  audita igual que el resto de las acciones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from license_manager.models import (
    ChargeStatus,
    Customer,
    EmissionType,
    Equipment,
    Income,
    IncomeCategory,
    License,
    PaymentMethod,
    PaymentStatus,
    utcnow,
)
from license_manager.services.audit import record_event
from license_manager.services.directory import format_hardware_code
from license_manager.services.licencias import (
    PRECIO_VENTA_COP,
    EmissionError,
    parse_importe_cop,
)

# Ciclo de prueba (LM-RF-008): 30 dias gratis, gracia desde el 31, vencida
# en el 34 con el cronometro detenido ahi mismo.
DIAS_PRUEBA = 30
DIA_COBRO_PENDIENTE = 31
DIA_VENCIMIENTO = 34

# Estados que el ciclo todavia puede recalcular. ``pagado`` y ``vencida``
# estan congelados y ``no_aplica`` no participa en el ciclo.
ESTADOS_VIVOS = (ChargeStatus.TRIAL_ACTIVE.value, ChargeStatus.PAYMENT_PENDING.value)

ETIQUETAS_CATEGORIA = {
    IncomeCategory.SALE.value: "Venta",
    IncomeCategory.TRIAL_CHARGE.value: "Cobro de prueba",
    IncomeCategory.WHOLESALE_PACKAGE.value: "Paquete mayorista",
    IncomeCategory.COURTESY.value: "Gratuito / cortesia",
    IncomeCategory.REPLACEMENT.value: "Reemplazo",
    IncomeCategory.ANNULLMENT.value: "Anulacion",
    IncomeCategory.REFUND.value: "Reembolso",
}

ETIQUETAS_ESTADO = {
    PaymentStatus.PENDING.value: "Pendiente",
    PaymentStatus.PAID.value: "Pagado",
    PaymentStatus.PARTIAL.value: "Parcial",
    PaymentStatus.REFUNDED.value: "Reembolsado",
}

TONOS_ESTADO = {
    PaymentStatus.PENDING.value: "ambar",
    PaymentStatus.PAID.value: "verde",
    PaymentStatus.PARTIAL.value: "azul",
    PaymentStatus.REFUNDED.value: "gris",
}

# La columna "Estado de la cuenta" de UI-07 informa del ESTADO DE LA CUENTA
# (el ciclo de cobro de la licencia), no del movimiento: la fila automatica de
# $ 0 de una prueba esta "pagada" por definicion y aun asi la cuenta sigue
# corriendo. Etiquetas del mockup de UI-07.
ETIQUETAS_CUENTA = {
    ChargeStatus.TRIAL_ACTIVE.value: "En periodo de prueba",
    ChargeStatus.PAYMENT_PENDING.value: "Cobro Pendiente",
    ChargeStatus.OVERDUE.value: "Cuenta Vencida",
    ChargeStatus.PAID.value: "Pagado",
    ChargeStatus.NOT_APPLICABLE.value: "Sin cobro",
}

TONOS_CUENTA = {
    ChargeStatus.TRIAL_ACTIVE.value: "azul",
    ChargeStatus.PAYMENT_PENDING.value: "ambar",
    ChargeStatus.OVERDUE.value: "rojo",
    ChargeStatus.PAID.value: "verde",
    ChargeStatus.NOT_APPLICABLE.value: "gris",
}

ETIQUETAS_METODO = {
    PaymentMethod.CASH.value: "Efectivo",
    PaymentMethod.NEQUI.value: "Nequi",
    PaymentMethod.DAVIPLATA.value: "Daviplata",
    PaymentMethod.BANCOLOMBIA.value: "Bancolombia",
    PaymentMethod.OTHER.value: "Otro",
}

# Categorias que por definicion no llevan dinero.
CATEGORIAS_A_CERO = (
    IncomeCategory.COURTESY.value,
    IncomeCategory.REPLACEMENT.value,
)


class IngresoError(ValueError):
    """Rechazo de negocio con un mensaje pensado para mostrarse en pantalla."""


def hoy_utc() -> date:
    return utcnow().astimezone(UTC).date()


# --------------------------------------------------------------------------- #
# Parseo de lo que digita el operador
# --------------------------------------------------------------------------- #


def parse_monto(texto: str, *, requerido: bool = True) -> int | None:
    """Entero de COP a partir de lo digitado (acepta ``450.000``)."""
    limpio = (texto or "").strip()
    if not limpio:
        if requerido:
            raise IngresoError("Escribe el importe en pesos colombianos.")
        return None
    try:
        return parse_importe_cop(limpio)
    except EmissionError as exc:
        raise IngresoError(str(exc)) from exc


def parse_metodo(texto: str) -> str | None:
    """Metodo de pago opcional; vacio devuelve ``None``."""
    valor = (texto or "").strip().lower()
    if not valor:
        return None
    try:
        return PaymentMethod(valor).value
    except ValueError as exc:
        raise IngresoError(
            "Elige un metodo de pago valido: efectivo, Nequi, Daviplata, "
            "Bancolombia u otro."
        ) from exc


def parse_categoria(texto: str) -> str:
    valor = (texto or "").strip().lower()
    try:
        return IncomeCategory(valor).value
    except ValueError as exc:
        raise IngresoError("Elige una categoria de ingreso valida.") from exc


def parse_estado_pago(texto: str) -> str:
    valor = (texto or "").strip().lower()
    try:
        return PaymentStatus(valor).value
    except ValueError as exc:
        raise IngresoError("Elige un estado de pago valido.") from exc


# --------------------------------------------------------------------------- #
# Ciclo de cobro (dia 1 -> 31 -> 34 -> pagado)
# --------------------------------------------------------------------------- #


def dias_de_prueba(licencia: License, hoy: date) -> int:
    """Dia del cronometro. El dia de emision es el dia 1."""
    base = licencia.emitida_fecha
    return max(1, (hoy - base).days + 1)


def vence_prueba(licencia: License) -> date:
    """Ultimo dia dentro de los 30 gratis (emision + 30 dias, ver mockup)."""
    return licencia.emitida_fecha + timedelta(days=DIAS_PRUEBA)


def calcular_ciclo(licencia: License, hoy: date) -> str:
    """Estado de cobro que corresponde HOY, sin escribir en la base.

    Los estados congelados (``pagado`` y ``vencida``) se devuelven tal cual:
    esa es precisamente la regla del congelamiento.
    """
    estado = licencia.estado_cobro
    if estado in (ChargeStatus.PAID.value, ChargeStatus.NOT_APPLICABLE.value):
        return estado
    if estado == ChargeStatus.OVERDUE.value:
        return estado

    dias = dias_de_prueba(licencia, hoy)
    if dias >= DIA_VENCIMIENTO:
        return ChargeStatus.OVERDUE.value
    if dias >= DIA_COBRO_PENDIENTE:
        return ChargeStatus.PAYMENT_PENDING.value
    return ChargeStatus.TRIAL_ACTIVE.value


@dataclass(frozen=True)
class CambioCiclo:
    """Transicion aplicada por la sincronizacion, para avisar en pantalla."""

    licencia_id: str
    equipo_id: str | None
    cliente_nombre: str | None
    hardware_code: str
    dias: int
    desde: str
    hasta: str
    aviso: str


def _ingreso_de_prueba_pendiente(db: Session, licencia_id: str) -> Income | None:
    return db.execute(
        select(Income)
        .where(
            Income.licencia_id == licencia_id,
            Income.categoria == IncomeCategory.TRIAL_CHARGE.value,
            Income.estado_pago == PaymentStatus.PENDING.value,
        )
        .order_by(Income.creado_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _asegurar_cobro_pendiente(db: Session, licencia: License, actor_id: str | None) -> None:
    """Crea la fila de cobro del dia 31 si el sistema no la habia creado."""
    if _ingreso_de_prueba_pendiente(db, licencia.id) is not None:
        return
    hoy = hoy_utc()
    db.add(
        Income(
            licencia_id=licencia.id,
            categoria=IncomeCategory.TRIAL_CHARGE.value,
            monto_cop=PRECIO_VENTA_COP,
            moneda="COP",
            estado_pago=PaymentStatus.PENDING.value,
            metodo_pago=None,
            nota=(
                f"Cobro pendiente generado automaticamente al dia "
                f"{DIA_COBRO_PENDIENTE} de la prueba (30 dias cumplidos)."
            ),
            periodo=hoy,
            creado_por_id=actor_id,
        )
    )
    db.flush()


def sincronizar_ciclo(
    db: Session, *, hoy: date | None = None, actor_id: str | None = None
) -> list[CambioCiclo]:
    """Aplica las transiciones automaticas del ciclo de prueba pendientes.

    Se llama en cada visita a Ingresos (y al panel). No hace commit: la ruta
    confirma o revierte junto con el resto de la peticion. Cada transicion deja
    evento de auditoria, porque un cambio de estado de cobro es exactamente lo
    que la auditoria debe poder reconstruir.
    """
    dia = hoy or hoy_utc()
    cambios: list[CambioCiclo] = []

    licencias = db.execute(
        select(License)
        .where(
            License.estado_registro == "vigente",
            License.estado_cobro.in_(ESTADOS_VIVOS),
        )
        .order_by(License.emitida_at.asc())
    ).scalars()

    for licencia in licencias:
        destino = calcular_ciclo(licencia, dia)
        if destino == licencia.estado_cobro:
            continue

        origen = licencia.estado_cobro
        dias = dias_de_prueba(licencia, dia)
        licencia.estado_cobro = destino

        detalle: dict[str, object] = {
            "desde": origen,
            "hasta": destino,
            "dias": dias,
            "tipo_emision": licencia.tipo_emision,
        }

        if destino == ChargeStatus.PAYMENT_PENDING.value:
            _asegurar_cobro_pendiente(db, licencia, actor_id)
            aviso = (
                f"Cobro pendiente: la prueba de {DIA_COBRO_PENDIENTE - 1} dias "
                f"termino y la cuenta paso a $ {PRECIO_VENTA_COP:,} COP con "
                f"periodo de gracia."
            ).replace(",", ".")
        elif destino == ChargeStatus.OVERDUE.value:
            # El cronometro se detiene EXACTAMENTE en el dia 34, aunque la
            # sincronizacion llegue tarde (el sitio pudo estar apagado).
            licencia.cronometro_congelado_at = utcnow()
            # La deuda del dia 31 existe aunque el sitio se hubiera saltado
            # la gracia: sin esa fila no hay nada que cobrar ni mostrar.
            _asegurar_cobro_pendiente(db, licencia, actor_id)
            detalle["dias"] = DIA_VENCIMIENTO
            aviso = (
                f"Cuenta vencida: pasaron {DIA_VENCIMIENTO} dias sin confirmacion "
                "de pago. El cronometro quedo detenido y la alerta permanece "
                "hasta que se registre el pago."
            )
        else:  # pragma: no cover - el ciclo no vuelve a prueba_activa solo
            aviso = "Estado de cobro recalculado."

        equipo = db.get(Equipment, licencia.equipo_id)
        cliente = db.get(Customer, equipo.cliente_id) if equipo and equipo.cliente_id else None

        record_event(
            db,
            action="ingreso.ciclo_prueba",
            entity_type="licencia",
            entity_id=licencia.id,
            detail=detalle,
        )
        cambios.append(
            CambioCiclo(
                licencia_id=licencia.id,
                equipo_id=equipo.id if equipo else None,
                cliente_nombre=cliente.nombre if cliente else None,
                hardware_code=equipo.hardware_code if equipo else "",
                dias=dias,
                desde=origen,
                hasta=destino,
                aviso=aviso,
            )
        )

    return cambios


# --------------------------------------------------------------------------- #
# Consolidacion de cobros
# --------------------------------------------------------------------------- #


def ingreso_pendiente_de(db: Session, licencia_id: str) -> Income | None:
    return db.execute(
        select(Income)
        .where(
            Income.licencia_id == licencia_id,
            Income.estado_pago == PaymentStatus.PENDING.value,
        )
        .order_by(Income.creado_at.asc())
    ).scalars().first()


def ingreso_venta_pagada_de(db: Session, licencia_id: str) -> Income | None:
    """Cobro ya registrado por la misma licencia (venta o cobro de prueba).

    Existe para que un segundo registro de pago sobre la misma cuenta no cree
    una fila de dinero duplicada: el sistema reutiliza la que ya hay.
    """
    return db.execute(
        select(Income)
        .where(
            Income.licencia_id == licencia_id,
            Income.categoria.in_(
                (IncomeCategory.SALE.value, IncomeCategory.TRIAL_CHARGE.value)
            ),
            Income.estado_pago == PaymentStatus.PAID.value,
            Income.monto_cop > 0,
        )
        .limit(1)
    ).scalar_one_or_none()


def _nota_conjunta(*partes: str | None) -> str | None:
    texto = " · ".join(p.strip() for p in partes if p and p.strip())
    return texto or None


def marcar_cuenta_pagada(
    db: Session,
    *,
    licencia: License,
    actor_id: str | None,
    importe_cop: int | None = None,
    metodo_pago: str | None = None,
    nota: str | None = None,
) -> Income:
    """Congela el cronometro y consolida la venta (LM-RF-008).

    * Si la prueba ya tenia su fila pendiente del dia 31, esa misma se marca
      pagada (no se crea un segundo cobro por el mismo concepto).
    * Si se cobra durante los 30 dias gratis, se crea la venta de $ 450.000 COP
      que hasta ese momento no existia.
    * Un ``importe`` vacio toma la tarifa oficial de referencia.
    """
    hoy = hoy_utc()
    monto = importe_cop if importe_cop is not None else PRECIO_VENTA_COP
    if monto <= 0:
        raise IngresoError("El importe de un cobro debe ser mayor que cero.")

    pendiente = ingreso_pendiente_de(db, licencia.id)
    if pendiente is not None:
        pendiente.monto_cop = monto
        pendiente.estado_pago = PaymentStatus.PAID.value
        pendiente.metodo_pago = metodo_pago
        pendiente.pagado_at = utcnow()
        pendiente.periodo = hoy
        pendiente.nota = _nota_conjunta(
            pendiente.nota,
            nota or "Cobro confirmado desde Ingresos.",
        )
        db.flush()
        ingreso = pendiente
    else:
        existente = ingreso_venta_pagada_de(db, licencia.id)
        if existente is not None:
            # La venta ya estaba cobrada: solo se completa el metodo si faltaba.
            if metodo_pago and not existente.metodo_pago:
                existente.metodo_pago = metodo_pago
                db.flush()
            ingreso = existente
        else:
            ingreso = Income(
                licencia_id=licencia.id,
                categoria=IncomeCategory.SALE.value,
                monto_cop=monto,
                moneda="COP",
                estado_pago=PaymentStatus.PAID.value,
                metodo_pago=metodo_pago,
                nota=nota or "Cobro confirmado desde Ingresos.",
                periodo=hoy,
                pagado_at=utcnow(),
                creado_por_id=actor_id,
            )
            db.add(ingreso)
            db.flush()

    # Congelamiento definitivo del cronometro.
    licencia.estado_cobro = ChargeStatus.PAID.value
    if licencia.cronometro_congelado_at is None:
        licencia.cronometro_congelado_at = utcnow()
    db.flush()
    return ingreso


def cambiar_estado_cuenta(
    db: Session,
    *,
    licencia: License,
    estado_deseado: str,
    actor_id: str | None,
    importe_cop: int | None = None,
    metodo_pago: str | None = None,
    nota: str | None = None,
) -> str:
    """Cambia manualmente la cuenta de la licencia (modal de UI-07).

    Solo se admiten los estados que el documento contempla:
    ``pagado``, ``cobro_pendiente`` y ``vencida``. El reinicio de la prueba no
    existe: la fecha de emision esta sellada dentro de la cadena FRB1 que ya
    se entrego al POS y reescribirla desincronaria el sitio de la licencia
    firmada. Una prueba nueva es un equipo nuevo.
    """
    estado = (estado_deseado or "").strip().lower()
    if estado == ChargeStatus.PAID.value:
        marcar_cuenta_pagada(
            db,
            licencia=licencia,
            actor_id=actor_id,
            importe_cop=importe_cop,
            metodo_pago=metodo_pago,
            nota=nota,
        )
        return ChargeStatus.PAID.value

    if estado == ChargeStatus.PAYMENT_PENDING.value:
        if licencia.estado_cobro == ChargeStatus.PAID.value:
            raise IngresoError(
                "Esta cuenta ya esta marcada como Pagada: su cronometro esta "
                "congelado y no se puede volver a abrir."
            )
        licencia.estado_cobro = ChargeStatus.PAYMENT_PENDING.value
        licencia.cronometro_congelado_at = None
        _asegurar_cobro_pendiente(db, licencia, actor_id)
        db.flush()
        return estado

    if estado == ChargeStatus.OVERDUE.value:
        if licencia.estado_cobro == ChargeStatus.PAID.value:
            raise IngresoError(
                "Esta cuenta ya esta marcada como Pagada: no se puede marcar "
                "vencida sin antes abrir el cobro."
            )
        licencia.estado_cobro = ChargeStatus.OVERDUE.value
        if licencia.cronometro_congelado_at is None:
            licencia.cronometro_congelado_at = utcnow()
        db.flush()
        return estado

    raise IngresoError(
        "Estado de cuenta no valido. Admitidos: Pagado, Cobro pendiente o "
        "Vencida."
    )


# --------------------------------------------------------------------------- #
# Alta manual de ingresos (doc 02, seccion 5.3)
# --------------------------------------------------------------------------- #


def registrar_ingreso(
    db: Session,
    *,
    actor_id: str | None,
    licencia_id: str,
    categoria: str,
    importe_cop: int,
    estado_pago: str,
    metodo_pago: str | None = None,
    nota: str | None = None,
) -> Income:
    """Registra un pago manual contra una licencia / operacion."""
    licencia = db.get(License, licencia_id) if licencia_id else None
    if licencia is None:
        raise IngresoError("Elige la licencia u operacion a la que va el ingreso.")

    cat = parse_categoria(categoria)
    estado = parse_estado_pago(estado_pago)
    if importe_cop < 0:
        raise IngresoError("El importe no puede ser negativo.")
    if cat in CATEGORIAS_A_CERO and importe_cop != 0:
        raise IngresoError(
            "Una categoria gratuita o de reemplazo se registra en $ 0 COP. "
            "Si hubo cobro, usa la categoria Venta."
        )

    hoy = hoy_utc()

    # Un pago cobrado CONSOLIDA la cuenta que esta corriendo (dia 1 a 34):
    # el dinero llego, asi que el cronometro se congela y la venta se cierra
    # sobre la MISMA fila de deuda. Solo las categorias que llevan dinero
    # propio (venta / cobro de prueba) consolidan; el resto es un movimiento
    # contable aparte que no toca el ciclo.
    if (
        estado == PaymentStatus.PAID.value
        and cat in (IncomeCategory.SALE.value, IncomeCategory.TRIAL_CHARGE.value)
        and importe_cop > 0
        and licencia.estado_cobro
        not in (ChargeStatus.NOT_APPLICABLE.value, ChargeStatus.PAID.value)
    ):
        # marcar_cuenta_pagada reutiliza la fila pendiente del dia 31 (o crea
        # la venta si el cobro llego dentro de los 30 dias gratis): nunca se
        # abren dos filas por la misma deuda.
        return marcar_cuenta_pagada(
            db,
            licencia=licencia,
            actor_id=actor_id,
            importe_cop=importe_cop,
            metodo_pago=metodo_pago,
            nota=nota or "Cobro confirmado desde Ingresos.",
        )

    ingreso = Income(
        licencia_id=licencia.id,
        categoria=cat,
        monto_cop=importe_cop,
        moneda="COP",
        estado_pago=estado,
        metodo_pago=metodo_pago,
        nota=_nota_conjunta(nota),
        periodo=hoy,
        pagado_at=utcnow() if estado == PaymentStatus.PAID.value else None,
        creado_por_id=actor_id,
    )
    db.add(ingreso)
    db.flush()
    return ingreso


# --------------------------------------------------------------------------- #
# Lecturas para la pantalla (UI-07)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CicloFila:
    """Cronometro interno de una fila de UI-07."""

    texto: str
    detalle: str
    tono: str  # verde | ambar | rojo | gris


def _ciclo_de(licencia: License | None, hoy: date) -> CicloFila | None:
    if licencia is None:
        return None

    estado = licencia.estado_cobro
    if estado == ChargeStatus.NOT_APPLICABLE.value:
        return CicloFila("—", "Sin cronometro de prueba", "gris")
    if estado == ChargeStatus.PAID.value:
        return CicloFila("✓ Detenido", "No suma mas dias", "verde")
    if estado == ChargeStatus.OVERDUE.value:
        # Detenido EXACTAMENTE en el dia 34, sea cual sea el dia real.
        return CicloFila(
            f"Detenido al Día {DIA_VENCIMIENTO}",
            "Cronometro congelado",
            "rojo",
        )

    dias = dias_de_prueba(licencia, hoy)
    if estado == ChargeStatus.PAYMENT_PENDING.value:
        return CicloFila(
            f"Día {dias} (Gracia)",
            f"{DIA_COBRO_PENDIENTE - 1} dias cumplidos",
            "ambar",
        )
    return CicloFila(
        f"Día {dias} de {DIAS_PRUEBA}",
        f"Vence prueba: {vence_prueba(licencia).strftime('%d/%m/%Y')}",
        "verde",
    )


@dataclass(frozen=True)
class FilaIngreso:
    """Fila de la tabla de UI-07, ya con todo lo derivado resuelto."""

    id: str
    licencia_id: str | None
    periodo: date
    cliente_nombre: str
    hardware_code: str | None
    licencia_tipo: str | None
    licencia_key: str | None
    categoria: str
    categoria_etiqueta: str
    monto_cop: int
    estado_pago: str
    # Estado de la CUENTA (ciclo de cobro de la licencia) de la columna
    # "Estado de la cuenta"; cae al estado del movimiento si no hay licencia.
    cuenta_etiqueta: str
    cuenta_tono: str
    # "Pago: X" solo cuando el estado del movimiento CONTRADICE al de la
    # cuenta (un cobro pendiente sobre una cuenta ya pagada, por ejemplo).
    pago_nota: str | None
    metodo_pago: str | None
    metodo_etiqueta: str
    nota: str | None
    ciclo: CicloFila | None
    estado_cobro: str | None
    puede_cobrar: bool


def _licencia_tipo_etiqueta(tipo: str | None) -> str | None:
    return {
        EmissionType.COMMERCIAL.value: "Comercial",
        EmissionType.TRIAL.value: "Prueba",
        EmissionType.REPLACEMENT.value: "Reemplazo",
        EmissionType.COURTESY.value: "Cortesia",
    }.get(tipo or "", tipo)


# Estado del movimiento que cada estado de cuenta da por supuesto. Si el
# movimiento real difiere (un cobro pendiente anotado sobre una cuenta ya
# pagada, un reembolso, etc.) la fila lo dice debajo de la pildora: el filtro
# es por estado de PAGO y la columna es por estado de CUENTA.
PAGO_IMPLICADO = {
    ChargeStatus.PAID.value: PaymentStatus.PAID.value,
    ChargeStatus.PAYMENT_PENDING.value: PaymentStatus.PENDING.value,
    ChargeStatus.OVERDUE.value: PaymentStatus.PENDING.value,
    ChargeStatus.TRIAL_ACTIVE.value: PaymentStatus.PAID.value,
    ChargeStatus.NOT_APPLICABLE.value: PaymentStatus.PAID.value,
}


def _cuenta_de(licencia: License | None, ingreso: Income) -> tuple[str, str, str | None]:
    """(etiqueta, tono, nota) de la columna "Estado de la cuenta".

    La columna informa del ciclo de cobro de la licencia, no del movimiento:
    la fila automatica de $ 0 de una prueba esta "pagada" por definicion y la
    cuenta sigue corriendo. Sin licencia no hay cuenta que informar y queda el
    estado del propio movimiento.
    """
    if licencia is None:
        etiqueta = ETIQUETAS_ESTADO.get(ingreso.estado_pago, ingreso.estado_pago)
        return etiqueta, TONOS_ESTADO.get(ingreso.estado_pago, "gris"), None

    cuenta = licencia.estado_cobro or ""
    etiqueta = ETIQUETAS_CUENTA.get(cuenta)
    if etiqueta is None:  # pragma: no cover - estado fuera de catalogo
        return (
            ETIQUETAS_ESTADO.get(ingreso.estado_pago, ingreso.estado_pago),
            TONOS_ESTADO.get(ingreso.estado_pago, "gris"),
            None,
        )

    tono = TONOS_CUENTA[cuenta]
    nota = None
    # Un movimiento de $ 0 (gratuito, reemplazo, cortesia) no dice nada sobre
    # el pago: la fila automatica de la prueba sigue "pagada" aunque la cuenta
    # entre en gracia, y anotarlo solo ensuciaria la columna.
    if ingreso.monto_cop > 0 and ingreso.estado_pago != PAGO_IMPLICADO[cuenta]:
        nota = "Pago: " + ETIQUETAS_ESTADO.get(
            ingreso.estado_pago, ingreso.estado_pago
        )
    return etiqueta, tono, nota


def listar_ingresos(
    db: Session,
    *,
    busqueda: str = "",
    estado: str = "",
    categoria: str = "",
    hoy: date | None = None,
) -> list[FilaIngreso]:
    """Movimientos con sus filtros, del mas reciente al mas antiguo."""
    dia = hoy or hoy_utc()

    query = (
        select(Income, License, Equipment, Customer.nombre)
        .join(License, License.id == Income.licencia_id, isouter=True)
        .join(Equipment, Equipment.id == License.equipo_id, isouter=True)
        .join(Customer, Customer.id == Equipment.cliente_id, isouter=True)
        .order_by(Income.creado_at.desc(), Income.id.desc())
    )

    termino = (busqueda or "").strip()
    if termino:
        limpio = "".join(ch for ch in termino.upper() if ch not in " -")
        patron = f"%{termino.lower()}%"
        query = query.where(
            func.lower(func.coalesce(Customer.nombre, "")).like(patron)
            | func.lower(func.coalesce(Income.nota, "")).like(patron)
            | func.lower(func.coalesce(License.key_id, "")).like(patron)
            | func.upper(func.coalesce(Equipment.hardware_code, "")).like(f"%{limpio}%")
        )

    estado_sel = (estado or "").strip().lower()
    if estado_sel:
        if estado_sel not in ETIQUETAS_ESTADO:
            raise IngresoError("El filtro de estado de pago no es valido.")
        query = query.where(Income.estado_pago == estado_sel)

    categoria_sel = (categoria or "").strip().lower()
    if categoria_sel:
        if categoria_sel not in ETIQUETAS_CATEGORIA:
            raise IngresoError("El filtro de categoria no es valido.")
        query = query.where(Income.categoria == categoria_sel)

    filas: list[FilaIngreso] = []
    for ingreso, licencia, equipo, cliente_nombre in db.execute(query).all():
        ciclo = _ciclo_de(licencia, dia) if licencia is not None else None
        cuenta_etiqueta, cuenta_tono, pago_nota = _cuenta_de(licencia, ingreso)
        filas.append(
            FilaIngreso(
                id=ingreso.id,
                licencia_id=ingreso.licencia_id,
                periodo=ingreso.periodo,
                cliente_nombre=cliente_nombre or "Sin verduleria",
                hardware_code=equipo.hardware_code if equipo else None,
                licencia_tipo=_licencia_tipo_etiqueta(licencia.tipo_emision if licencia else None),
                licencia_key=licencia.key_id if licencia else None,
                categoria=ingreso.categoria,
                categoria_etiqueta=ETIQUETAS_CATEGORIA.get(
                    ingreso.categoria, ingreso.categoria
                ),
                monto_cop=int(ingreso.monto_cop),
                estado_pago=ingreso.estado_pago,
                cuenta_etiqueta=cuenta_etiqueta,
                cuenta_tono=cuenta_tono,
                pago_nota=pago_nota,
                metodo_pago=ingreso.metodo_pago,
                metodo_etiqueta=ETIQUETAS_METODO.get(
                    ingreso.metodo_pago or "", ingreso.metodo_pago or "Sin metodo"
                ),
                nota=ingreso.nota,
                ciclo=ciclo,
                estado_cobro=licencia.estado_cobro if licencia is not None else None,
                puede_cobrar=bool(
                    licencia is not None
                    and licencia.estado_cobro
                    in ESTADOS_VIVOS + (ChargeStatus.OVERDUE.value,)
                ),
            )
        )
    return filas


@dataclass(frozen=True)
class ResumenIngresos:
    """Tarjetas de resumen de recaudacion de UI-07."""

    cobradas_cop: int
    cobradas_n: int
    pendientes_cop: int
    pendientes_n: int
    vencidas_n: int
    deuda_vencida_cop: int
    pruebas_activas_n: int


def resumen_ingresos(db: Session) -> ResumenIngresos:
    """Totales de recaudacion y cuentas en el ciclo de cobro.

    Lee los estados YA sincronizados: la ruta evalua el ciclo antes de pedir
    los totales, para que ninguna cifra muestre un dia atrasado.
    """

    # "Cobradas" son ventas de verdad: los $ 0 gratuitos o de reemplazo ya
    # estan pagados por definicion, pero no son dinero recaudado.
    cobrado, cobradas_n = db.execute(
        select(
            func.coalesce(func.sum(Income.monto_cop), 0),
            func.count(Income.id),
        ).where(
            Income.estado_pago == PaymentStatus.PAID.value,
            Income.monto_cop > 0,
        )
    ).one()

    # Pendientes de cuentas que NO estan vencidas: una cuenta vencida sale en
    # su propia tarjeta, igual que en el mockup de UI-07.
    pendientes_cop, pendientes_n = db.execute(
        select(
            func.coalesce(func.sum(Income.monto_cop), 0),
            func.count(Income.id),
        )
        .join(License, License.id == Income.licencia_id, isouter=True)
        .where(
            Income.estado_pago == PaymentStatus.PENDING.value,
            or_(
                License.estado_cobro.is_(None),
                License.estado_cobro != ChargeStatus.OVERDUE.value,
            ),
        )
    ).one()

    vencidas_n = int(
        db.execute(
            select(func.count(License.id)).where(
                License.estado_cobro == ChargeStatus.OVERDUE.value
            )
        ).scalar_one()
    )
    deuda_vencida_cop = int(
        db.execute(
            select(func.coalesce(func.sum(Income.monto_cop), 0))
            .join(License, License.id == Income.licencia_id, isouter=True)
            .where(
                Income.estado_pago == PaymentStatus.PENDING.value,
                License.estado_cobro == ChargeStatus.OVERDUE.value,
            )
        ).scalar_one()
    )
    pruebas_n = int(
        db.execute(
            select(func.count(License.id)).where(
                License.estado_cobro == ChargeStatus.TRIAL_ACTIVE.value
            )
        ).scalar_one()
    )

    return ResumenIngresos(
        cobradas_cop=int(cobrado),
        cobradas_n=int(cobradas_n),
        pendientes_cop=int(pendientes_cop),
        pendientes_n=int(pendientes_n),
        vencidas_n=vencidas_n,
        deuda_vencida_cop=deuda_vencida_cop,
        pruebas_activas_n=pruebas_n,
    )


@dataclass(frozen=True)
class OperacionOpcion:
    """Opcion del selector de licencias/operaciones de UI-07."""

    id: str
    etiqueta: str
    estado_cobro: str


def listar_operaciones(db: Session) -> list[OperacionOpcion]:
    """Licencias vigentes a las que se les puede acreditar un ingreso."""
    query = (
        select(License, Equipment, Customer.nombre)
        .join(Equipment, Equipment.id == License.equipo_id, isouter=True)
        .join(Customer, Customer.id == Equipment.cliente_id, isouter=True)
        .where(License.estado_registro == "vigente")
        .order_by(License.emitida_at.desc())
    )
    opciones: list[OperacionOpcion] = []
    for licencia, equipo, cliente_nombre in db.execute(query).all():
        codigo = (
            format_hardware_code(equipo.hardware_code) if equipo else "sin equipo"
        )
        tipo = _licencia_tipo_etiqueta(licencia.tipo_emision) or licencia.tipo_emision
        opciones.append(
            OperacionOpcion(
                id=licencia.id,
                etiqueta=(
                    f"{cliente_nombre or 'Sin verduleria'} · {tipo} · {codigo} · "
                    f"emitida {licencia.emitida_fecha.strftime('%d/%m/%Y')}"
                ),
                estado_cobro=licencia.estado_cobro,
            )
        )
    return opciones


def licencia_de(db: Session, licencia_id: str) -> License | None:
    return db.get(License, licencia_id) if licencia_id else None
