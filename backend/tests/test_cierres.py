"""Pruebas del cierre contable mensual (JHO-103, UI-07B).

Lo que se verifica aqui:

* Los tres roles autenticados consultan el cierre (doc 01, LM-RF-002): no es
  una pantalla exclusiva de Titulares, a diferencia de UI-11.
* Los KPI suman solo dinero efectivamente cobrado (entero en COP): los
  pendientes y los $ 0 de reemplazos van segregados.
* El reparto por persona atribuye cada cobro a quien lo registro, que es lo
  mismo que despues alimenta la liquidacion de UI-11.
* El operador navega el mes con ``?periodo=``; nunca digita una fecha.
* La exportacion CSV lleva BOM UTF-8 y ``sep=;`` para Excel en espanol, y
  deja su evento de auditoria encadenado.
* La pantalla se rinde sin scripts inline (CSP ``script-src 'self'``).
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import select

from license_manager.models import AuditEvent, Income, PaymentStatus, Role
from license_manager.services.audit import verify_chain
from license_manager.services.cierres import (
    exportar_cierre_csv,
    format_millones,
    resumen_cierre,
    variacion_pct,
)
from license_manager.services.directory import create_customer, create_equipment
from license_manager.services.licencias import PRECIO_VENTA_COP
from license_manager.services.periodos import (
    PeriodoError,
    clave_periodo,
    mes_actual,
    parse_periodo,
    periodo_anterior,
)

CLAVE_TITULAR = "clave-segura-123"
CLAVE_ADMIN = "clave-admin-larga-1"
NOMBRE_CLIENTE = "Verduleria La Huerta Central"

# Un codigo de equipo por licencia: cada maquina admite una sola vigente.
CODIGOS = (
    "A4B9C102E8F37B1A",
    "5D2198AF44B10C3E",
    "77C1E40B9920A5D1",
    "F110B88201CA77E4",
    "3B1044F281D0C9B5",
    "99A211C077E5B6C7",
    "44E188F922B3D4A0",
    "11C400A955E8F2B3",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ubicacion(response) -> str:
    from urllib.parse import unquote

    return unquote(response.headers["location"])


def _cliente(db) -> str:
    cliente = create_customer(db, actor_id=None, nombre=NOMBRE_CLIENTE)
    db.commit()
    return cliente.id


def _ventas(db, client, cliente_id: str, cantidad: int, *, inicio: int = 0) -> None:
    """Emite ``cantidad`` licencias comerciales con la sesion activa.

    Cada emision crea su ingreso pagado de ``PRECIO_VENTA_COP`` atribuido al
    usuario autenticado, que es exactamente la base del cierre mensual.
    ``inicio`` permite emitir un segundo lote sin repetir codigos de equipo.
    """
    for indice in range(inicio, inicio + cantidad):
        create_equipment(
            db,
            actor_id=None,
            hardware_code_raw=CODIGOS[indice],
            cliente_id=cliente_id,
        )
        db.commit()
        respuesta = client.post(
            "/emitir",
            data={
                "cliente_id": cliente_id,
                "codigo": CODIGOS[indice],
                "tipo": "comercial",
                "importe": "450.000",
            },
            follow_redirects=False,
        )
        assert respuesta.status_code == 303, respuesta.text


def _mover_ingresos_a(db, periodo: date) -> None:
    """Retrocede todos los ingresos a un mes (para el comparativo)."""
    for ingreso in db.execute(select(Income)).scalars():
        ingreso.periodo = periodo
    db.commit()


def _entrada_titular(client, db):
    crear_titular(db, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)


def _entrada_admin(client, db):
    crear_usuario_rol(
        db, Role.ADMIN.value, email="admin@frubber.co", password=CLAVE_ADMIN,
        nombre="Carlos Gomez",
    )
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)


# --------------------------------------------------------------------------- #
# Acceso: los tres roles consultan UI-07B
# --------------------------------------------------------------------------- #


def test_sin_sesion_no_se_entra_al_cierre(client):
    respuesta = client.get("/cierres", follow_redirects=False)

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"


def test_el_administrador_delegado_si_consulta_el_cierre(client, db_session):
    """UI-07B no es pantalla de Titulares: el delegado consulta sus cobros."""
    _entrada_admin(client, db_session)

    respuesta = client.get("/cierres")

    assert respuesta.status_code == 200
    assert "Ingresos Mensuales y Cierres" in respuesta.text
    assert "acceso restringido" not in respuesta.text.lower()


def test_el_titular_entra_al_cierre(client, db_session):
    _entrada_titular(client, db_session)

    respuesta = client.get("/cierres")

    assert respuesta.status_code == 200
    assert "Comportamiento semestral" in respuesta.text
    assert "Exportar cierre" in respuesta.text


# --------------------------------------------------------------------------- #
# KPI del mes: solo dinero efectivamente cobrado
# --------------------------------------------------------------------------- #


def test_los_kpis_suman_lo_realmente_cobrado(client, db_session):
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 3)

    respuesta = client.get("/cierres")
    cierre = resumen_cierre(db_session, periodo=mes_actual())

    assert respuesta.status_code == 200
    assert cierre.total_cop == 3 * PRECIO_VENTA_COP
    assert cierre.licencias_n == 3
    # Las cifras pintadas son enteros de COP, con formato es-CO.
    assert "1.350.000" in respuesta.text


def test_los_pendientes_van_segregados_de_lo_cobrado(client, db_session):
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 1)

    db_session.add(
        Income(
            categoria="venta",
            monto_cop=PRECIO_VENTA_COP,
            estado_pago=PaymentStatus.PENDING.value,
            periodo=mes_actual(),
        )
    )
    db_session.commit()

    cierre = resumen_cierre(db_session, periodo=mes_actual())

    assert cierre.total_cop == PRECIO_VENTA_COP, "lo pendiente no es cobro del mes"
    assert cierre.pendientes_cop == PRECIO_VENTA_COP
    assert cierre.pendientes_n == 1


def test_un_reemplazo_a_cero_no_infla_el_total(client, db_session):
    _entrada_titular(client, db_session)

    db_session.add(
        Income(
            categoria="reemplazo",
            monto_cop=0,
            estado_pago=PaymentStatus.PAID.value,
            periodo=mes_actual(),
            nota="Hardware averiado",
        )
    )
    db_session.commit()

    cierre = resumen_cierre(db_session, periodo=mes_actual())

    assert cierre.total_cop == 0
    assert cierre.gratuitos_n == 1
    assert cierre.reemplazos_n == 1
    assert cierre.reemplazos_nota == "Hardware averiado"


def test_el_reparto_por_persona_atribuye_cada_cobro(client, db_session):
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 2)

    # Un segundo operador cobra con su propia sesion.
    crear_usuario_rol(
        db_session, Role.ADMIN.value, email="admin@frubber.co",
        password=CLAVE_ADMIN, nombre="Carlos Gomez",
    )
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)
    _ventas(db_session, client, cliente_id, 1, inicio=2)

    cierre = resumen_cierre(db_session, periodo=mes_actual())

    assert cierre.total_cop == 3 * PRECIO_VENTA_COP
    nombres = {fila.nombre: fila for fila in cierre.reparto}
    assert set(nombres) == {"Jhoan Rivera", "Carlos Gomez"}
    assert nombres["Jhoan Rivera"].monto_cop == 2 * PRECIO_VENTA_COP
    assert nombres["Carlos Gomez"].movimientos_n == 1


# --------------------------------------------------------------------------- #
# Selector de mes y comparativo semestral
# --------------------------------------------------------------------------- #


def test_el_mes_se_elige_con_query_string_y_no_con_fechas_digitadas(client, db_session):
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 1)
    _mover_ingresos_a(db_session, periodo_anterior(mes_actual()))

    mes_anterior = clave_periodo(periodo_anterior(mes_actual()))
    respuesta = client.get(f"/cierres?periodo={mes_anterior}")

    assert respuesta.status_code == 200
    cierre = resumen_cierre(db_session, periodo=periodo_anterior(mes_actual()))
    assert cierre.total_cop == PRECIO_VENTA_COP
    assert clave_periodo(cierre.periodo) == mes_anterior


def test_un_periodo_mal_digitado_redirige_con_error(client, db_session):
    _entrada_titular(client, db_session)

    respuesta = client.get("/cierres?periodo=2026-13", follow_redirects=False)

    assert respuesta.status_code == 303
    assert "no existe" in _ubicacion(respuesta)
    assert db_session.execute(select(AuditEvent).where(
        AuditEvent.accion == "cierre.exportado"
    )).scalar_one_or_none() is None


def test_el_comparativo_semestral_pinta_seis_meses(client, db_session):
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 2)

    cierre = resumen_cierre(db_session, periodo=mes_actual())

    assert len(cierre.barras) == 6
    assert cierre.barras[-1].activa, "el mes seleccionado es la ultima barra"
    assert cierre.barras[-1].monto_cop == 2 * PRECIO_VENTA_COP
    # Sin dinero en los meses previos no hay altura fingida.
    assert all(barra.alto_pct == 0 for barra in cierre.barras[:-1])


def test_un_mes_cerrado_conserva_su_historial(client, db_session):
    """Un cobro tardio no rompe el mes ya cerrado: pertenece al suyo."""
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 1)
    _mover_ingresos_a(db_session, date(2026, 8, 1))

    cierre_agosto = resumen_cierre(db_session, periodo=date(2026, 8, 1))
    cierre_octubre = resumen_cierre(db_session, periodo=date(2026, 10, 1))

    assert cierre_agosto.total_cop == PRECIO_VENTA_COP
    assert cierre_octubre.total_cop == 0


# --------------------------------------------------------------------------- #
# Exportacion del cierre
# --------------------------------------------------------------------------- #


def test_exportar_cierre_csv_con_bom_separador_y_auditoria(client, db_session):
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 1)

    periodo = clave_periodo(mes_actual())
    respuesta = client.get(f"/cierres/exportar?periodo={periodo}")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/csv")
    assert f'cierre-mensual-{periodo}.csv' in respuesta.headers["content-disposition"]

    contenido = respuesta.content
    assert contenido.startswith(b"\xef\xbb\xbf"), "falta el BOM UTF-8 de Excel"
    texto = contenido.decode("utf-8-sig")
    assert texto.splitlines()[0] == "sep=;"
    assert "Verduleria / Negocio" in texto
    assert NOMBRE_CLIENTE in texto
    assert str(PRECIO_VENTA_COP) in texto

    # Cada exportacion queda en la auditoria encadenada.
    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "cierre.exportado")
    ).scalar_one()
    detalle = json.loads(evento.detalle)
    assert detalle["periodo"] == periodo
    assert detalle["total_cop"] == PRECIO_VENTA_COP
    assert evento.usuario_email == "titular@frubber.co"
    assert verify_chain(db_session).ok


def test_exportar_un_periodo_invalido_no_descarga_nada(client, db_session):
    _entrada_titular(client, db_session)

    respuesta = client.get("/cierres/exportar?periodo=xxx", follow_redirects=False)

    assert respuesta.status_code == 303
    assert "AAAA-MM" in _ubicacion(respuesta)


# --------------------------------------------------------------------------- #
# Utilidades del modulo y reglas de presentacion
# --------------------------------------------------------------------------- #


def test_el_csv_es_reproducible_desde_el_servicio(client, db_session):
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 2)

    cierre = resumen_cierre(db_session, periodo=mes_actual())
    texto = exportar_cierre_csv(cierre)

    assert texto.startswith("\ufeffsep=;"), "Excel en espanol necesita BOM y sep=;"
    assert texto.count("\r\n") >= 3, "encabezado mas dos movimientos"
    assert "$" not in texto, "el CSV lleva enteros, no moneda formateada"


def test_format_millones_y_variacion_son_enteros():
    assert format_millones(1_800_000) == "$1,8M"
    assert format_millones(450_000) == "$450k"
    assert format_millones(0) == "$0"
    assert variacion_pct(1_350_000, 1_000_000) == 35
    assert variacion_pct(1_000_000, 1_350_000) == -26
    assert variacion_pct(100, 0) is None, "sin base no hay porcentaje"


def test_parse_periodo_solo_acepta_meses_reales():
    assert parse_periodo("2026-10") == date(2026, 10, 1)
    with pytest.raises(PeriodoError):
        parse_periodo("2026-00")
    with pytest.raises(PeriodoError):
        parse_periodo("octubre")
    with pytest.raises(PeriodoError):
        parse_periodo("1999-12")


# --------------------------------------------------------------------------- #
# CSP: sin scripts inline en la pantalla
# --------------------------------------------------------------------------- #


def test_la_pantalla_no_lleva_scripts_ni_eventos_inline(client, db_session):
    _entrada_titular(client, db_session)

    cuerpo = client.get("/cierres").text

    assert cuerpo.count("<script") == 1
    assert 'src="/static/lm.js"' in cuerpo
    assert "onclick=" not in cuerpo
    assert "onsubmit=" not in cuerpo
    assert "<script>" not in cuerpo
    assert "BEGIN" not in cuerpo
