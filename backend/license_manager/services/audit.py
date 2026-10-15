"""Auditoria append-only encadenada por hash (JHO-105).

Cada evento calcula su hash sobre: el hash del evento anterior + todos sus
campos. Asi, editar o borrar una fila rompe la cadena y la verificacion lo
delata, incluso si alguien manipula la base directamente por SSH.

En PostgreSQL se suma ademas un trigger que rechaza UPDATE y DELETE sobre la
tabla (ver ``migrations/sql/postgres_auditoria_trigger.sql``): doble defensa.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from license_manager.models import AuditEvent, Role, User, to_utc, utcnow

_CHAIN_GENESIS = "GENESIS"


def _canonical_record(
    *,
    fecha_at: str,
    usuario_id: str | None,
    usuario_email: str | None,
    usuario_rol: str | None,
    accion: str,
    entidad_tipo: str | None,
    entidad_id: str | None,
    detalle: str | None,
) -> str:
    """Representacion estable y no ambigua del evento para el calculo de hash."""
    payload = {
        "fecha_at": fecha_at,
        "usuario_id": usuario_id,
        "usuario_email": usuario_email,
        "usuario_rol": usuario_rol,
        "accion": accion,
        "entidad_tipo": entidad_tipo,
        "entidad_id": entidad_id,
        "detalle": detalle,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_hash(prev_hash: str | None, canonical_record: str) -> str:
    material = f"{prev_hash or _CHAIN_GENESIS}|{canonical_record}".encode()
    return hashlib.sha256(material).hexdigest()


def serialize_detail(detail: dict[str, Any] | None) -> str | None:
    """Normaliza el detalle a JSON estable. Prohibido pasar secretos aqui."""
    if not detail:
        return None
    return json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def record_event(
    session: Session,
    *,
    action: str,
    user: User | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditEvent:
    """Agrega un evento a la auditoria. La sesion NO se hace commit aqui."""
    fecha_at = utcnow()
    last = session.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)).scalar_one_or_none()
    prev_hash = last.hash if last else None

    canonical = _canonical_record(
        fecha_at=fecha_at.isoformat(),
        usuario_id=user.id if user else None,
        usuario_email=user.email if user else None,
        usuario_rol=user.rol if user else None,
        accion=action,
        entidad_tipo=entity_type,
        entidad_id=entity_id,
        detalle=serialize_detail(detail),
    )

    event = AuditEvent(
        fecha_at=fecha_at,
        usuario_id=user.id if user else None,
        usuario_email=user.email if user else None,
        usuario_rol=user.rol if user else None,
        accion=action,
        entidad_tipo=entity_type,
        entidad_id=entity_id,
        detalle=serialize_detail(detail),
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, canonical),
    )
    session.add(event)
    # autoflush esta desactivado en el factory: sin este flush, el siguiente
    # evento no veria a este y todos quedarian colgados de la genesis.
    session.flush()
    return event


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    total_events: int
    broken_at: int | None = None
    reason: str | None = None


def verify_chain(session: Session) -> ChainVerification:
    """Recalcula toda la cadena. Devuelve el primer punto de ruptura si lo hay."""
    events = session.execute(select(AuditEvent).order_by(AuditEvent.id.asc())).scalars().all()
    prev_hash: str | None = None

    for event in events:
        if event.prev_hash != prev_hash:
            return ChainVerification(
                ok=False,
                total_events=len(events),
                broken_at=event.id,
                reason="El hash anterior no coincide con el evento previo: se elimino o reordenaron filas.",
            )

        canonical = _canonical_record(
            # to_utc: SQLite devuelve fechas naive y el texto del hash debe ser
            # identico al que se calculo al registrar el evento.
            fecha_at=to_utc(event.fecha_at).isoformat(),
            usuario_id=event.usuario_id,
            usuario_email=event.usuario_email,
            usuario_rol=event.usuario_rol,
            accion=event.accion,
            entidad_tipo=event.entidad_tipo,
            entidad_id=event.entidad_id,
            detalle=event.detalle,
        )
        expected = compute_hash(prev_hash, canonical)
        if expected != event.hash:
            return ChainVerification(
                ok=False,
                total_events=len(events),
                broken_at=event.id,
                reason="El contenido del evento fue alterado despues de registrarse.",
            )
        prev_hash = event.hash

    return ChainVerification(ok=True, total_events=len(events))


def assert_can_manage_users(user: User) -> None:
    """Solo los titulares gestionan cuentas (UI-09, bloqueo 403 estricto)."""
    if not Role(user.rol).is_titular:
        raise PermissionError("Solo el Titular Principal y el Co-Titular pueden gestionar cuentas.")
