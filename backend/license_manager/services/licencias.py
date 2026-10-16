"""Emision de licencias FRB1 (JHO-101, UI-05).

Reglas que este modulo materializa:

* Una licencia es vitalicia y esta firmada para UNA maquina: un equipo no
  puede acumular dos licencias vigentes. El reemplazo de hardware siempre
  corresponde a un equipo fisico nuevo, no a reutilizar el registro.
* El precio oficial al publico es $ 450.000 COP por caja. El importe que se
  registra en la emision es dinero entero en COP, nunca float.
* La firma usa exactamente el mismo ``sign_license`` del contrato con el POS:
  aqui no se reinterpreta nada, se llama y se guarda el texto resultante.
* La auditoria NO lleva el texto de la licencia: lleva tipo, monto, equipo y
  verduleria. La cadena FRB1 vive en ``licencias`` y se entrega por pantalla.
* UI-05 recibe el codigo de equipo PEGADO (doc 01 criterio 7, doc 02 seccion
  5.2): 16 hex validados aqui mismo. Un codigo repetido no emite: se advierte
  y se lleva a la ficha existente en vez de decidir un reemplazo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC

from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.orm import Session

from license_manager.crypto.frb1 import Frb1Error, normalize_machine_code, sign_license
from license_manager.models import (
    ChargeStatus,
    Customer,
    EmissionType,
    Equipment,
    Income,
    IncomeCategory,
    License,
    PaymentStatus,
    utcnow,
)
from license_manager.services.directory import (
    DirectoryError,
    create_equipment,
    format_hardware_code,
)

# Precio oficial al publico (doc 04 v2.1, seccion 3). Referencia de la pantalla,
# no un tope: la venta puede registrarse por otro valor acordado.
PRECIO_VENTA_COP = 450_000

# Ciclo de prueba (LM-RF-008): 30 dias sin cobro.
DIAS_PRUEBA = 30


class EmissionError(ValueError):
    """Rechazo de negocio con mensaje pensado para mostrarse en pantalla."""


# --------------------------------------------------------------------------- #
# Tipos de emision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TipoEmision:
    """Lo que UI-05 ofrece en el selector, ya resuelto."""

    valor: str
    etiqueta: str
    es_pagable: bool


TIPOS_EMISION: tuple[TipoEmision, ...] = (
    TipoEmision(EmissionType.COMMERCIAL.value, "Venta estandar (Comercial)", True),
    TipoEmision(EmissionType.TRIAL.value, "Prueba (30 dias)", False),
    TipoEmision(EmissionType.REPLACEMENT.value, "Reemplazo de equipo ($0)", False),
    TipoEmision(EmissionType.COURTESY.value, "Cortesia de la sociedad ($0)", False),
)


def tipo_emision_por(valor: str) -> TipoEmision | None:
    valor = (valor or "").strip().lower()
    for tipo in TIPOS_EMISION:
        if tipo.valor == valor:
            return tipo
    return None


def parse_importe_cop(texto: str) -> int:
    """Convierte lo que digito el operador a un entero de COP.

    Acepta ``450000`` y los separadores de miles ``450.000`` / ``450,000``.
    Rechaza decimales, negativos y cualquier otra cosa: ``450.5`` NO puede
    convertirse en 4505 pesos, eso seria cobrar de mas por un error de tipeo.
    """
    limpio = (texto or "").strip().replace(" ", "")
    if not limpio:
        raise EmissionError("Escribe el importe cobrado en pesos colombianos.")
    patron = r"^\d{1,3}(?:[.,]\d{3})*$"
    if re.fullmatch(patron, limpio) is None:
        raise EmissionError(
            "El importe debe ser un numero entero de pesos, sin centavos "
            "(por ejemplo 450000 o 450.000)."
        )
    return int(re.sub(r"[.,]", "", limpio))


# --------------------------------------------------------------------------- #
# Emision
# --------------------------------------------------------------------------- #


def _estado_cobro_de(tipo: str) -> str:
    return {
        EmissionType.COMMERCIAL.value: ChargeStatus.PAID.value,
        EmissionType.TRIAL.value: ChargeStatus.TRIAL_ACTIVE.value,
        EmissionType.REPLACEMENT.value: ChargeStatus.NOT_APPLICABLE.value,
        EmissionType.COURTESY.value: ChargeStatus.NOT_APPLICABLE.value,
    }[tipo]


def _categoria_de(tipo: str) -> str:
    return {
        EmissionType.COMMERCIAL.value: IncomeCategory.SALE.value,
        EmissionType.TRIAL.value: IncomeCategory.COURTESY.value,
        EmissionType.REPLACEMENT.value: IncomeCategory.REPLACEMENT.value,
        EmissionType.COURTESY.value: IncomeCategory.COURTESY.value,
    }[tipo]


def _validar_importe(tipo: TipoEmision, importe_cop: int | None) -> int:
    if not tipo.es_pagable:
        # Prueba, reemplazo y cortesia no generan ingreso: $0 por definicion.
        return 0
    if importe_cop is None:
        raise EmissionError("Escribe el importe cobrado en pesos colombianos.")
    if importe_cop <= 0:
        raise EmissionError("El importe de una venta debe ser mayor que cero.")
    return importe_cop


def _asegurar_equipo_disponible(db: Session, equipo: Equipment) -> None:
    vigente = db.execute(
        select(License)
        .where(License.equipo_id == equipo.id, License.estado_registro == "vigente")
        .limit(1)
    ).scalar_one_or_none()
    if vigente is not None:
        raise EmissionError(
            "Este equipo ya tiene una licencia vigente. Si la maquina se danio, "
            "registra el equipo nuevo y emite un Reemplazo."
        )


def emit_license(
    db: Session,
    *,
    actor_id: str | None,
    signing_key,
    equipo: Equipment,
    tipo: str,
    importe_cop: int | None = None,
) -> License:
    """Firma una licencia FRB1 para el equipo y registra su ingreso.

    Devuelve la licencia ya agregada a la sesion (sin commit): la ruta decide
    si confirma o revierte segun el resultado del resto de la transaccion.
    """
    seleccion = tipo_emision_por(tipo)
    if seleccion is None:
        raise EmissionError("El tipo de licencia indicado no existe.")

    if signing_key is None or not isinstance(signing_key.private_key, ec.EllipticCurvePrivateKey):
        raise EmissionError(
            "La llave de firma no esta disponible en el servidor. "
            "No es posible emitir licencias hasta configurarla."
        )

    _asegurar_equipo_disponible(db, equipo)
    monto = _validar_importe(seleccion, importe_cop)

    emitida_fecha = utcnow().astimezone(UTC).date()
    licencia_text = sign_license(
        signing_key.private_key,
        signing_key.key_id,
        equipo.hardware_code,
        emitida_fecha,
    )

    licencia = License(
        key_id=signing_key.key_id,
        equipo_id=equipo.id,
        licencia_text=licencia_text,
        tipo_emision=seleccion.valor,
        estado_entrega="emitida",
        estado_registro="vigente",
        estado_cobro=_estado_cobro_de(seleccion.valor),
        emitida_fecha=emitida_fecha,
        creado_por_id=actor_id,
    )
    db.add(licencia)
    db.flush()

    db.add(
        Income(
            licencia_id=licencia.id,
            categoria=_categoria_de(seleccion.valor),
            monto_cop=monto,
            moneda="COP",
            estado_pago=PaymentStatus.PAID.value,
            metodo_pago=None,
            nota=f"Registro automatico al emitir licencia {seleccion.valor}.",
            periodo=emitida_fecha,
            pagado_at=utcnow() if monto > 0 else None,
            creado_por_id=actor_id,
        )
    )
    db.flush()
    return licencia


# --------------------------------------------------------------------------- #
# Lecturas para la pantalla (UI-05)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClienteOpcion:
    """Opcion del selector de verdulerias de UI-05."""

    id: str
    nombre: str
    propietario: str | None


def listar_clientes_activos(db: Session) -> list[ClienteOpcion]:
    """Verdulerias activas en orden alfabetico.

    Solo las activas: una verduleria dada de baja no recibe licencias nuevas.
    Sus equipos y sus licencias historicas siguen completos en su ficha.
    """
    clientes = db.execute(
        select(Customer)
        .where(Customer.activo.is_(True))
        .order_by(Customer.nombre.asc())
    ).scalars()
    return [
        ClienteOpcion(id=c.id, nombre=c.nombre, propietario=c.propietario)
        for c in clientes
    ]


def licencia_resultado(db: Session, licencia_id: str) -> License | None:
    """Licencia a mostrar como resultado de la emision."""
    return db.get(License, licencia_id)


# --------------------------------------------------------------------------- #
# Codigo de equipo pegado (doc 02, seccion 5.2)
# --------------------------------------------------------------------------- #

MOTIVO_INVALIDO = "invalido"
MOTIVO_NUEVO = "nuevo"
MOTIVO_LIBRE = "libre"
MOTIVO_VIGENTE = "vigente"


@dataclass(frozen=True)
class DiagnosticoCodigo:
    """Lo que significa el codigo pegado, antes de firmar nada."""

    # None cuando la entrada no cumple los 16 caracteres hexadecimales.
    codigo: str | None
    formateado: str
    equipo: Equipment | None
    cliente_nombre: str | None
    # invalido | nuevo | libre | vigente
    motivo: str
    # Mensaje pensado para pantalla cuando el motivo bloquea la emision.
    texto: str | None


def diagnosticar_codigo(db: Session, codigo_raw: str) -> DiagnosticoCodigo:
    """Clasifica el codigo pegado sin escribir en la base de datos.

    * ``invalido``: no son 16 hex → no se firma nada (doc 01, criterio 7).
    * ``nuevo``: la maquina no esta en el inventario → se da de alta aqui.
    * ``libre``: ya esta registrada y no tiene licencia vigente → se reutiliza;
      no se crea una segunda fila para la misma maquina fisica.
    * ``vigente``: ya tiene licencia → se advierte y se lleva a la ficha. Este
      modulo NO decide un reemplazo ni una reemision por su cuenta: esa
      politica se aprueba aparte (doc 02, seccion 7).
    """
    try:
        codigo = normalize_machine_code(codigo_raw or "")
    except Frb1Error as exc:
        return DiagnosticoCodigo(None, "", None, None, MOTIVO_INVALIDO, str(exc))

    formateado = format_hardware_code(codigo)
    equipo = db.execute(
        select(Equipment).where(Equipment.hardware_code == codigo)
    ).scalar_one_or_none()

    if equipo is None:
        return DiagnosticoCodigo(codigo, formateado, None, None, MOTIVO_NUEVO, None)

    cliente = db.get(Customer, equipo.cliente_id) if equipo.cliente_id else None
    nombre_cliente = cliente.nombre if cliente else None

    vigente = db.execute(
        select(License)
        .where(License.equipo_id == equipo.id, License.estado_registro == "vigente")
        .limit(1)
    ).scalar_one_or_none()
    if vigente is not None:
        texto = (
            f"El codigo {formateado} ya tiene una licencia {vigente.tipo_emision} "
            f"vigente{f' de {nombre_cliente}' if nombre_cliente else ''}. "
            "Si la maquina se danio, registra el equipo nuevo en Equipos y "
            "emitelo ahi como Reemplazo."
        )
        return DiagnosticoCodigo(
            codigo, formateado, equipo, nombre_cliente, MOTIVO_VIGENTE, texto
        )

    return DiagnosticoCodigo(
        codigo, formateado, equipo, nombre_cliente, MOTIVO_LIBRE, None
    )


def preparar_equipo_para_emision(
    db: Session, *, codigo_raw: str, cliente_id: str, actor_id: str
) -> Equipment:
    """Resuelve (y si hace falta da de alta) la maquina que va a firmarse.

    Se llama despues de que la pantalla valido cliente, tipo e importe. Cualquier
    rechazo sale como ``EmissionError`` y la ruta revierte la sesion completa,
    asi que nunca queda un equipo a medias en el inventario.
    """
    cliente = db.get(Customer, cliente_id) if cliente_id else None
    if cliente is None:
        raise EmissionError("Elige la verduleria o negocio al que pertenece la maquina.")
    if not cliente.activo:
        raise EmissionError(
            f"La verduleria {cliente.nombre} esta desactivada. "
            "Reactivala en Clientes o elige otra."
        )

    diagnostico = diagnosticar_codigo(db, codigo_raw)
    if diagnostico.motivo in (MOTIVO_INVALIDO, MOTIVO_VIGENTE):
        raise EmissionError(diagnostico.texto)

    if diagnostico.motivo == MOTIVO_NUEVO:
        try:
            return create_equipment(
                db,
                actor_id=actor_id,
                hardware_code_raw=diagnostico.codigo,
                cliente_id=cliente.id,
            )
        except DirectoryError as exc:
            raise EmissionError(str(exc)) from exc

    # MOTIVO_LIBRE: la maquina ya esta en el inventario.
    equipo = diagnostico.equipo
    if equipo.cliente_id and equipo.cliente_id != cliente.id:
        raise EmissionError(
            f"El codigo {diagnostico.formateado} ya esta registrado a nombre de "
            f"{diagnostico.cliente_nombre}. Elige esa verduleria o abre la ficha "
            "del equipo para revisarlo."
        )
    if equipo.cliente_id is None:
        equipo.cliente_id = cliente.id
        db.flush()
    return equipo
