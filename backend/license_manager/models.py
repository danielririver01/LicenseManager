"""Modelo relacional de License Manager (JHO-97).

Tablas exigidas por los criterios de aceptacion de JHO-97: usuarios,
verdulerias, equipos, licencias, ingresos y auditoria. Se incluyen ademas
tecnicos/paquetes_mayoristas (JHO-104) y liquidaciones (JHO-103) porque el
esquema es la base sobre la que se apoyan y definirlo ahora evita migraciones
de reestructuracion despues.

Convenciones heredadas de Frubber POS (ADR-009, ADR-011):

* El dinero son enteros en COP. Nunca float ni decimal.
* Los timestamps son del servidor en UTC; el operador nunca digita fechas.
* La auditoria es append-only y encadenada por hash: cualquier borrado o
  edicion rompe la cadena y queda detectable.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from license_manager.db import Base


def utcnow() -> datetime:
    """Marca de tiempo del servidor. El usuario nunca la ingresa."""
    return datetime.now(UTC)


def to_utc(value: datetime | None) -> datetime | None:
    """Normaliza a UTC consciente de zona.

    PostgreSQL devuelve fechas aware; SQLite devuelve fechas naive. Sin este
    normalizador las comparaciones explotan y, peor, el hash de la auditoria
    se calcula sobre un texto distinto al que se registro y la cadena parece
    rota cuando no lo esta.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Role(enum.StrEnum):
    """Jerarquia de acceso del doc 04 v2.0 (Sociedad de 2 Titulares)."""

    OWNER = "owner"  # Titular Principal: acceso total y gestion de cuentas.
    COOWNER = "coowner"  # Co-Titular / Socio: acceso total.
    ADMIN = "admin"  # Administrador Delegado: emite y consulta, no gestiona cuentas.

    @property
    def is_titular(self) -> bool:
        return self in (Role.OWNER, Role.COOWNER)


class EmissionType(enum.StrEnum):
    """Tipo de emision registrado en UI-05."""

    COMMERCIAL = "comercial"  # Venta al PVP $450.000 COP
    TRIAL = "prueba"  # Prueba 30 dias a $0 COP
    REPLACEMENT = "reemplazo"  # Reposicion de hardware dañado, $0 COP
    COURTESY = "cortesia"  # Cortesia de la sociedad, $0 COP


class DeliveryStatus(enum.StrEnum):
    """Estado de entrega/activacion comunicada manualmente (no es señal de POS)."""

    ISSUED = "emitida"
    DELIVERED = "entregada"
    CUSTOMER_CONFIRMED = "cliente_confirmo"


class ChargeStatus(enum.StrEnum):
    """Cronometro de cobro del ciclo de prueba (LM-RF-008)."""

    NOT_APPLICABLE = "no_aplica"
    TRIAL_ACTIVE = "prueba_activa"  # dia 1 a 30
    PAYMENT_PENDING = "cobro_pendiente"  # dia 31 a 33, periodo de gracia
    OVERDUE = "vencida"  # dia 34+, congelado
    PAID = "pagado"  # cronometro congelado definitivamente


class PaymentStatus(enum.StrEnum):
    PENDING = "pendiente"
    PAID = "pagado"
    PARTIAL = "parcial"
    REFUNDED = "reembolsado"


class IncomeCategory(enum.StrEnum):
    """Categoria separada de la venta cobrada para no contabilizar gratos."""

    SALE = "venta"
    TRIAL_CHARGE = "cobro_prueba"
    WHOLESALE_PACKAGE = "paquete_mayorista"
    COURTESY = "gratuito"
    REPLACEMENT = "reemplazo"
    ANNULLMENT = "anulacion"
    REFUND = "reembolso"


class PaymentMethod(enum.StrEnum):
    CASH = "efectivo"
    NEQUI = "nequi"
    DAVIPLATA = "daviplata"
    BANCOLOMBIA = "bancolombia"
    OTHER = "otro"


# --------------------------------------------------------------------------- #
# Usuarios y permisos (JHO-98 / JHO-99)
# --------------------------------------------------------------------------- #


class User(Base):
    __tablename__ = "usuarios"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    nombre_completo: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[str] = mapped_column(String(16), nullable=False, default=Role.ADMIN.value)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    creado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    creado_por_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    desactivado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ultimo_acceso_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (CheckConstraint("rol IN ('owner','coowner','admin')", name="ck_usuarios_rol"),)


# --------------------------------------------------------------------------- #
# Clientes y equipos (JHO-100)
# --------------------------------------------------------------------------- #


class Customer(Base):
    """Verduleria / fruteria cliente."""

    __tablename__ = "verdulerias"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    nombre: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    propietario: Mapped[str | None] = mapped_column(String(160), nullable=True)
    telefono: Mapped[str | None] = mapped_column(String(40), nullable=True)
    direccion: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ciudad: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    creado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    creado_por_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )

    equipos: Mapped[list[Equipment]] = relationship(back_populates="cliente")


class Equipment(Base):
    """Maquina fisica identificada por su codigo de hardware de 16 hex."""

    __tablename__ = "equipos"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    cliente_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("verdulerias.id", ondelete="SET NULL"), nullable=True, index=True
    )
    hardware_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    creado_por_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )

    cliente: Mapped[Customer | None] = relationship(back_populates="equipos")
    licencias: Mapped[list[License]] = relationship(back_populates="equipo")

    __table_args__ = (
        CheckConstraint(
            "LENGTH(hardware_code) = 16",
            name="ck_equipos_hardware_code_largo",
        ),
    )


# --------------------------------------------------------------------------- #
# Licencias (JHO-101) e inventario mayorista (JHO-104)
# --------------------------------------------------------------------------- #


class Technician(Base):
    """Tecnico balancero / distribuidor con paquetes prepago."""

    __tablename__ = "tecnicos"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    telefono: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    paquetes: Mapped[list[WholesalePackage]] = relationship(back_populates="tecnico")


class WholesalePackage(Base):
    """Paquete prepago de emisiones comprado por un tecnico (Opcion B)."""

    __tablename__ = "paquetes_mayoristas"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    tecnico_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("tecnicos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tipo_paquete: Mapped[str] = mapped_column(String(24), nullable=False)
    licencias_total: Mapped[int] = mapped_column(Integer, nullable=False)
    licencias_usadas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    costo_unitario_cop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valor_total_cop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    comprado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    registrado_por_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )

    tecnico: Mapped[Technician] = relationship(back_populates="paquetes")

    __table_args__ = (
        CheckConstraint("licencias_total > 0", name="ck_paquetes_total_positivo"),
        CheckConstraint(
            "licencias_usadas >= 0 AND licencias_usadas <= licencias_total",
            name="ck_paquetes_usadas_en_rango",
        ),
        CheckConstraint("tipo_paquete IN ('inicial','profesional','master')", name="ck_paquetes_tipo"),
    )


class License(Base):
    """Emision de una licencia FRB1 firmada."""

    __tablename__ = "licencias"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    key_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    equipo_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("equipos.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    licencia_text: Mapped[str] = mapped_column(Text, nullable=False)

    tipo_emision: Mapped[str] = mapped_column(String(16), nullable=False)
    estado_entrega: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DeliveryStatus.ISSUED.value
    )
    estado_registro: Mapped[str] = mapped_column(String(16), nullable=False, default="vigente")
    estado_cobro: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ChargeStatus.NOT_APPLICABLE.value
    )

    emitida_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    emitida_fecha: Mapped[date] = mapped_column(Date, nullable=False)
    # Congelamiento del cronometro: se fija al marcar Pagado o al vencer.
    cronometro_congelado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tecnico_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("tecnicos.id", ondelete="SET NULL"), nullable=True, index=True
    )
    paquete_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("paquetes_mayoristas.id", ondelete="SET NULL"), nullable=True
    )
    cupo_descontado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    creado_por_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True, index=True
    )

    equipo: Mapped[Equipment] = relationship(back_populates="licencias")
    ingresos: Mapped[list[Income]] = relationship(back_populates="licencia")

    __table_args__ = (
        CheckConstraint(
            "tipo_emision IN ('comercial','prueba','reemplazo','cortesia')",
            name="ck_licencias_tipo",
        ),
        CheckConstraint(
            "estado_entrega IN ('emitida','entregada','cliente_confirmo')",
            name="ck_licencias_estado_entrega",
        ),
        CheckConstraint(
            "estado_registro IN ('vigente','anulada')",
            name="ck_licencias_estado_registro",
        ),
        CheckConstraint(
            "estado_cobro IN ('no_aplica','prueba_activa','cobro_pendiente','vencida','pagado')",
            name="ck_licencias_estado_cobro",
        ),
        # Un equipo no puede tener dos licencias comerciales vigentes a la vez.
        Index("uq_licencias_equipo_comercial_vigente", "equipo_id", "tipo_emision", "estado_registro"),
    )


# --------------------------------------------------------------------------- #
# Ingresos manuales (JHO-102 / JHO-103)
# --------------------------------------------------------------------------- #


class Income(Base):
    __tablename__ = "ingresos"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    licencia_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("licencias.id", ondelete="SET NULL"), nullable=True, index=True
    )
    categoria: Mapped[str] = mapped_column(String(24), nullable=False)
    monto_cop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    moneda: Mapped[str] = mapped_column(String(3), nullable=False, default="COP")
    estado_pago: Mapped[str] = mapped_column(String(16), nullable=False, default=PaymentStatus.PENDING.value)
    metodo_pago: Mapped[str | None] = mapped_column(String(16), nullable=True)
    nota: Mapped[str | None] = mapped_column(Text, nullable=True)
    periodo: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    pagado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    creado_por_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )

    licencia: Mapped[License | None] = relationship(back_populates="ingresos")

    __table_args__ = (
        CheckConstraint("monto_cop >= 0", name="ck_ingresos_monto_no_negativo"),
        CheckConstraint("moneda = 'COP'", name="ck_ingresos_moneda_cop"),
        CheckConstraint(
            "categoria IN ('venta','cobro_prueba','paquete_mayorista',"
            "'gratuito','reemplazo','anulacion','reembolso')",
            name="ck_ingresos_categoria",
        ),
        CheckConstraint(
            "estado_pago IN ('pendiente','pagado','parcial','reembolsado')",
            name="ck_ingresos_estado_pago",
        ),
        CheckConstraint(
            "metodo_pago IS NULL OR metodo_pago IN ('efectivo','nequi','daviplata','bancolombia','otro')",
            name="ck_ingresos_metodo_pago",
        ),
    )


# --------------------------------------------------------------------------- #
# Liquidaciones de utilidades (JHO-103)
# --------------------------------------------------------------------------- #


class Settlement(Base):
    """Liquidacion mensual de comisiones a un administrador operativo."""

    __tablename__ = "liquidaciones"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    periodo: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    administrador_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    licencias_vendidas: Mapped[int] = mapped_column(Integer, nullable=False)
    precio_unitario_cop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tipo_comision: Mapped[str] = mapped_column(String(16), nullable=False)
    valor_comision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    monto_bruto_cop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    comision_cop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ganancia_neta_cop: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metodo_pago: Mapped[str | None] = mapped_column(String(16), nullable=True)
    nota: Mapped[str | None] = mapped_column(Text, nullable=True)

    pagado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmado_por_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    creado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("tipo_comision IN ('porcentaje','fijo')", name="ck_liquidaciones_tipo"),
        CheckConstraint("licencias_vendidas >= 0", name="ck_liquidaciones_cantidad"),
        UniqueConstraint("periodo", "administrador_id", name="uq_liquidaciones_periodo_admin"),
    )


# --------------------------------------------------------------------------- #
# Auditoria append-only (JHO-105)
# --------------------------------------------------------------------------- #


class AuditEvent(Base):
    """Registro inmutable encadenado por hash.

    No se expone ningun metodo de actualizacion. En PostgreSQL se suma un
    trigger que rechaza UPDATE y DELETE; aqui la cadena de hash permite
    detectar cualquier manipulacion aunque alguien toque la base a mano.
    """

    __tablename__ = "auditoria"

    # BigInteger en PostgreSQL; SQLite solo autoincrementa sobre INTEGER.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    fecha_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    usuario_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True, index=True)
    usuario_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    usuario_rol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    accion: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entidad_tipo: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entidad_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detalle: Mapped[str | None] = mapped_column(Text, nullable=True)

    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (Index("ix_auditoria_entidad", "entidad_tipo", "entidad_id"),)


# --------------------------------------------------------------------------- #
# Sesiones de navegador (JHO-98)
# --------------------------------------------------------------------------- #


class UserSession(Base):
    """Sesion activa. La cookie solo lleva el id; el estado vive aqui.

    Guardarla en base de datos permite revocar una sesion concreta de inmediato
    (cuenta desactivada, cambio de clave, "cerrar todas las sesiones").
    """

    __tablename__ = "sesiones"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    usuario_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    creada_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expira_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ultimo_uso_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    cerrada_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
