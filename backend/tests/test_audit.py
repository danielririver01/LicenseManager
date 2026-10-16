"""Pruebas de la auditoria inmutable (JHO-105)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from license_manager.models import AuditEvent, AuditEventInmutable
from license_manager.services.audit import record_event, verify_chain


def _registrar(db, cantidad: int, prefijo: str = "prueba") -> None:
    for i in range(cantidad):
        record_event(db, action=f"{prefijo}.{i}", detail={"indice": i})
    db.commit()


def _primer_evento(db) -> AuditEvent:
    return db.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().first()


def test_la_cadena_valida_con_varios_eventos(db_session):
    _registrar(db_session, 5)

    result = verify_chain(db_session)
    assert result.ok, result.reason
    assert result.total_events == 5


def test_cada_evento_encadena_el_hash_del_anterior(db_session):
    _registrar(db_session, 4)

    eventos = db_session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all()
    assert eventos[0].prev_hash is None
    for anterior, actual in zip(eventos, eventos[1:], strict=False):
        assert actual.prev_hash == anterior.hash


def test_alterar_el_detalle_de_un_evento_rompe_la_cadena(db_session):
    _registrar(db_session, 3)

    evento = _primer_evento(db_session)
    # Simulamos a alguien que edita la base directamente por SSH: SQL crudo,
    # saltandose la guarda del backend.
    db_session.execute(
        text("UPDATE auditoria SET detalle = :nuevo WHERE id = :id"),
        {"nuevo": '{"indice":999}', "id": evento.id},
    )
    db_session.commit()
    # Sin expirar, la sesion devolveria la fila cacheada y no veria el cambio.
    db_session.expire_all()

    result = verify_chain(db_session)
    assert not result.ok
    assert result.broken_at == evento.id


def test_borrar_un_evento_del_medio_rompe_la_cadena(db_session):
    _registrar(db_session, 4)

    eventos = db_session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all()
    db_session.execute(text("DELETE FROM auditoria WHERE id = :id"), {"id": eventos[1].id})
    db_session.commit()

    result = verify_chain(db_session)
    assert not result.ok


def test_reordenar_fechas_rompe_la_cadena(db_session):
    """La fecha esta dentro del hash: reescribirla invalida el evento."""
    _registrar(db_session, 3)
    evento = _primer_evento(db_session)

    db_session.execute(
        text("UPDATE auditoria SET fecha_at = :fecha WHERE id = :id"),
        {"fecha": "2020-01-01 00:00:00.000000", "id": evento.id},
    )
    db_session.commit()
    db_session.expire_all()

    assert not verify_chain(db_session).ok


def test_cambiar_el_usuario_que_ejecuto_la_accion_rompe_la_cadena(db_session):
    """La trazabilidad no se puede reescribir despues del hecho."""
    _registrar(db_session, 2)
    evento = _primer_evento(db_session)

    db_session.execute(
        text("UPDATE auditoria SET usuario_email = :correo WHERE id = :id"),
        {"correo": "otra.persona@frubber.co", "id": evento.id},
    )
    db_session.commit()
    db_session.expire_all()

    assert not verify_chain(db_session).ok


# --------------------------------------------------------------------------- #
# Bloqueo del backend: la tabla es de solo escritura
# --------------------------------------------------------------------------- #


def test_el_backend_rechaza_editar_un_evento(db_session):
    _registrar(db_session, 2)
    evento = _primer_evento(db_session)
    evento.detalle = '{"indice": 999}'

    with pytest.raises(AuditEventInmutable):
        db_session.flush()
    db_session.rollback()

    # El evento sigue intacto y la cadena sana.
    assert verify_chain(db_session).ok
    assert _primer_evento(db_session).detalle == '{"indice":0}'


def test_el_backend_rechaza_borrar_un_evento(db_session):
    _registrar(db_session, 2)
    evento = _primer_evento(db_session)

    db_session.delete(evento)
    with pytest.raises(AuditEventInmutable):
        db_session.flush()
    db_session.rollback()

    assert verify_chain(db_session).ok
    assert len(db_session.execute(select(AuditEvent)).scalars().all()) == 2


def test_una_cadena_vacia_es_valida(db_session):
    result = verify_chain(db_session)
    assert result.ok
    assert result.total_events == 0


def test_el_hash_es_determinista_para_el_mismo_evento():
    from license_manager.services.audit import _canonical_record, compute_hash

    canonico = _canonical_record(
        fecha_at="2026-10-15T10:00:00+00:00",
        usuario_id=None,
        usuario_email=None,
        usuario_rol=None,
        accion="prueba",
        entidad_tipo=None,
        entidad_id=None,
        detalle=None,
    )
    assert compute_hash(None, canonico) == compute_hash(None, canonico)
    assert compute_hash("abc", canonico) != compute_hash(None, canonico)


def test_el_detalle_se_guarda_como_json_estable(db_session):
    """El orden de las claves no debe cambiar el hash entre pares."""
    record_event(db_session, action="prueba.orden", detail={"b": 2, "a": 1})
    db_session.commit()

    evento = db_session.execute(select(AuditEvent)).scalars().one()
    assert evento.detalle == '{"a":1,"b":2}'


def test_en_postgres_la_tabla_de_auditoria_no_admite_update_ni_delete(db_session):
    """Defensa en profundidad: ademas del hash, un trigger bloquea la edicion.

    SQLite no ejecuta ese trigger (especifico de PostgreSQL), asi que aqui solo
    verificamos que el script existe y que en SQLite el bloqueo lo pone la capa
    de aplicacion.
    """
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "migrations" / "sql" / "postgresa auditoria trigger.sql"
    if not script.is_file():
        script = script.with_name("postgres_auditoria_trigger.sql")

    assert script.is_file(), f"Falta el trigger de auditoria: {script}"
    contenido = script.read_text(encoding="utf-8")
    assert "RAISE EXCEPTION" in contenido
    assert "UPDATE" in contenido and "DELETE" in contenido


def test_el_registro_de_auditoria_no_expone_borrado_en_la_api(client):
    """La API de verificacion solo lee; no hay punto que borre ni edite."""
    client.post(
        "/configurar",
        data={
            "nombre": "Jhoan",
            "email": "jhoan@frubber.co",
            "password": "clave-muy-segura-1",
            "password_confirm": "clave-muy-segura-1",
        },
    )

    rutas = {getattr(r, "path", "") for r in client.app.routes}
    assert not any("auditoria" in ruta and "verificar" not in ruta for ruta in rutas)

    response = client.post("/api/auditoria/verificar")
    assert response.status_code == 200
    assert response.json()["ok"] is True
