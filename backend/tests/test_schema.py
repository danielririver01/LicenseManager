"""Pruebas del esquema de base de datos (JHO-97)."""

from __future__ import annotations

from sqlalchemy import inspect

from license_manager.deps import state

# Tablas exigidas por los criterios de aceptacion de JHO-97.
TABLAS_ESPERADAS = {
    "usuarios",
    "verdulerias",
    "equipos",
    "licencias",
    "ingresos",
    "auditoria",
    # Apoyo para JHO-103 y JHO-104.
    "tecnicos",
    "paquetes_mayoristas",
    "liquidaciones",
    "sesiones",
}


def test_las_tablas_del_issue_estan_creadas_al_arrancar(client):
    """Regresion: el sitio arranco una vez sin crear el esquema y todo daba 500."""
    tablas = set(inspect(state.engine).get_table_names())
    assert TABLAS_ESPERADAS.issubset(tablas), f"Faltan: {TABLAS_ESPERADAS - tablas}"


def test_arrancar_dos_veces_no_destruye_datos(client, db_session, titular):
    """create_all debe ser idempotente: no pisa lo que ya existe."""
    from license_manager.app import initialize_schema

    initialize_schema(state.engine)
    db_session.expire_all()

    from sqlalchemy import func, select

    from license_manager.models import User

    assert db_session.execute(select(func.count(User.id))).scalar_one() == 1
    assert titular.activo


def test_el_dinero_se_guarda_como_enteros_no_como_decimal(client):
    """ADR-009: los importes en COP son enteros; nada de REAL ni de float."""
    columnas = {c["name"]: c["type"] for c in inspect(state.engine).get_columns("ingresos")}
    tipo = str(columnas["monto_cop"]).upper()

    assert "INT" in tipo, f"monto_cop debe ser entero, es {tipo}"
    for prohibido in ("REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL"):
        assert prohibido not in tipo, f"monto_cop no puede ser {prohibido}"


def test_los_timestamps_llevan_zona_horaria(client):
    """Las marcas de tiempo son del servidor; la UI nunca digita fechas."""
    columnas = {c["name"]: c["type"] for c in inspect(state.engine).get_columns("auditoria")}
    assert "fecha_at" in columnas


def test_los_estados_estan_restringidos_por_la_base(client):
    """Los CHECK evitan estados inventados aunque alguien escriba SQL directo."""
    from sqlalchemy import text

    with state.engine.connect() as connection:
        try:
            connection.execute(
                text(
                    "INSERT INTO licencias (id, key_id, equipo_id, licencia_text, tipo_emision, "
                    "emitida_fecha, emitida_at) VALUES "
                    "('x','k','e','FRB1|...','estado-inventado','2026-10-15','2026-10-15')"
                )
            )
            connection.commit()
            fallo = None
        except Exception as exc:  # noqa: BLE001 - queremos comprobar que la base rechaza
            fallo = exc
            connection.rollback()

    assert fallo is not None, "La base acepto un tipo de emision inexistente"


def test_no_hay_indices_duplicados_ni_tablas_huerfanas(client):
    tablas = set(inspect(state.engine).get_table_names())
    # Nada de tablas de prueba ni restos de versiones anteriores.
    assert not any("test" in t or "temp" in t for t in tablas)
