"""Pruebas de la liquidacion y reparto de utilidades (JHO-103, UI-11).

Lo que se verifica aqui:

* El bloqueo 403 estricto para el administrador delegado: la pantalla es de
  los Titulares (Titular Principal y Co-Titular), igual que UI-09.
* El calculo del reparto: bruto = licencias x $ 450.000 COP, comision por
  porcentaje o valor fijo, siempre en enteros de pesos.
* Cualquier rechazo deja la base intacta (rollback total): sin filas, sin
  eventos de auditoria a medias.
* Una sola liquidacion por (periodo, beneficiario): el segundo intento
  recalcula la pendiente en vez de abrir una copia.
* El pago exige canal y comprobante y SELLA la liquidacion: sus importes no
  se tocan mas.
* La liquidacion es una salida de dinero: no crea filas en ``ingresos``.
* La pantalla se rinde sin scripts inline (CSP ``script-src 'self'``).
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import func, select

from license_manager.models import AuditEvent, Income, PaymentMethod, Role, Settlement
from license_manager.services.audit import verify_chain
from license_manager.services.directory import create_customer, create_equipment
from license_manager.services.licencias import PRECIO_VENTA_COP
from license_manager.services.liquidaciones import (
    LiquidacionError,
    calcular_comision,
    parse_licencias,
    parse_valor_comision,
)
from license_manager.services.periodos import clave_periodo, mes_actual

CLAVE_TITULAR = "clave-segura-123"
CLAVE_ADMIN = "clave-admin-larga-1"
NOMBRE_CLIENTE = "Verduleria La Huerta Central"
ADMIN_EMAIL = "admin@frubber.co"

CODIGOS = (
    "A4B9C102E8F37B1A",
    "5D2198AF44B10C3E",
    "77C1E40B9920A5D1",
    "F110B88201CA77E4",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ubicacion(response) -> str:
    return unquote(response.headers["location"])


def _cliente(db) -> str:
    cliente = create_customer(db, actor_id=None, nombre=NOMBRE_CLIENTE)
    db.commit()
    return cliente.id


def _ventas(db, client, cliente_id: str, cantidad: int) -> None:
    for indice in range(cantidad):
        create_equipment(
            db, actor_id=None, hardware_code_raw=CODIGOS[indice], cliente_id=cliente_id
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


def _admin(db, *, activo: bool = True):
    return crear_usuario_rol(
        db,
        Role.ADMIN.value,
        email=ADMIN_EMAIL,
        password=CLAVE_ADMIN,
        nombre="Carlos Gomez",
        activo=activo,
    )


def _entrada_titular(client, db):
    crear_titular(db, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)


def _entrada_admin(client, db):
    admin = _admin(db)
    iniciar_sesion(client, ADMIN_EMAIL, CLAVE_ADMIN)
    return admin


def _registrar(client, *, admin_id: str, licencias="3", tipo="porcentaje", valor="30", periodo=None):
    return client.post(
        "/liquidaciones",
        data={
            "periodo": periodo or clave_periodo(mes_actual()),
            "administrador_id": admin_id,
            "licencias": licencias,
            "tipo": tipo,
            "valor": valor,
        },
        follow_redirects=False,
    )


def _liquidacion(db) -> Settlement | None:
    return db.execute(select(Settlement)).scalars().first()


def _eventos(db, prefijo: str) -> list[AuditEvent]:
    return list(
        db.execute(
            select(AuditEvent).where(AuditEvent.accion.like(f"{prefijo}%"))
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# Acceso: 403 estricto para el administrador delegado
# --------------------------------------------------------------------------- #


def test_sin_sesion_no_se_entra(client):
    respuesta = client.get("/liquidaciones", follow_redirects=False)

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"


def test_un_admin_no_ve_la_pantalla_y_recibe_403(client, db_session):
    _entrada_admin(client, db_session)

    respuesta = client.get("/liquidaciones")

    assert respuesta.status_code == 403
    assert "Acceso restringido" in respuesta.text
    # Ni la calculadora ni la tabla de comisiones se filtran.
    assert "Formulario de cálculo" not in respuesta.text
    assert "Registrar pago" not in respuesta.text


@pytest.mark.parametrize("ruta", ["pago", "soporte"])
def test_un_admin_no_toca_ninguna_accion_de_liquidacion(client, db_session, ruta):
    _entrada_admin(client, db_session)

    if ruta == "pago":
        respuesta = client.post(
            "/liquidaciones/00000000-0000-0000-0000-000000000000/pago",
            data={"metodo": "nequi", "comprobante": "x"},
            follow_redirects=False,
        )
    else:
        respuesta = client.get(
            "/liquidaciones/00000000-0000-0000-0000-000000000000/soporte"
        )

    assert respuesta.status_code == 403


def test_un_admin_no_registra_liquidaciones(client, db_session):
    admin = _entrada_admin(client, db_session)

    respuesta = _registrar(client, admin_id=admin.id)

    assert respuesta.status_code == 403
    assert db_session.execute(select(func.count(Settlement.id))).scalar_one() == 0


def test_un_admin_no_puede_revisar_el_soporte_de_otro(client, db_session):
    """Aunque la liquidacion exista, el delegado ni la lee."""
    _entrada_titular(client, db_session)
    admin = _admin(db_session)
    _registrar(client, admin_id=admin.id)
    liquidacion_id = _liquidacion(db_session).id

    # Cambia la sesion a la del delegado.
    client.post("/logout", follow_redirects=False)
    iniciar_sesion(client, ADMIN_EMAIL, CLAVE_ADMIN)
    respuesta = client.get(f"/liquidaciones/{liquidacion_id}/soporte")

    assert respuesta.status_code == 403


# --------------------------------------------------------------------------- #
# Calculo del reparto
# --------------------------------------------------------------------------- #


def test_el_titular_ve_la_calculadora_con_los_candidatos_del_mes(client, db_session):
    _entrada_admin(client, db_session)
    cliente_id = _cliente(db_session)
    _ventas(db_session, client, cliente_id, 3)

    client.post("/logout", follow_redirects=False)
    _entrada_titular(client, db_session)
    respuesta = client.get("/liquidaciones")

    assert respuesta.status_code == 200
    assert "Formulario de cálculo y reparto de ventas" in respuesta.text
    assert "Carlos Gomez" in respuesta.text
    assert "3 ventas registradas" in respuesta.text


def test_porcentaje_de_tres_licencias(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)

    respuesta = _registrar(client, admin_id=admin.id, licencias="3", valor="30")

    assert respuesta.status_code == 303
    liquidacion = _liquidacion(db_session)
    assert liquidacion.monto_bruto_cop == 3 * PRECIO_VENTA_COP == 1_350_000
    assert liquidacion.comision_cop == 405_000
    assert liquidacion.ganancia_neta_cop == 945_000
    assert liquidacion.tipo_comision == "porcentaje"
    assert liquidacion.pagado_at is None

    evento = _eventos(db_session, "liquidacion.")
    assert [e.accion for e in evento] == ["liquidacion.creada"]
    assert json.loads(evento[0].detalle)["comision_cop"] == 405_000
    assert verify_chain(db_session).ok


def test_valor_fijo_por_licencia(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)

    _registrar(client, admin_id=admin.id, licencias="2", tipo="fijo", valor="100.000")

    liquidacion = _liquidacion(db_session)
    assert liquidacion.monto_bruto_cop == 900_000
    assert liquidacion.comision_cop == 200_000
    assert liquidacion.ganancia_neta_cop == 700_000


def test_el_porcentaje_se_redondea_al_peso_sin_centavos():
    # 33% de 1.350.000 = 445.500 exacto; un tercero impar nunca da decimal.
    bruto, comision, neta = calcular_comision(licencias=3, tipo="porcentaje", valor=33)
    assert (bruto, comision, neta) == (1_350_000, 445_500, 904_500)
    assert isinstance(comision, int)

    bruto, comision, _ = calcular_comision(licencias=1, tipo="porcentaje", valor=33)
    # 450.000 * 33 = 14.850.000 / 100 = 148.500 exacto.
    assert comision == 148_500


# --------------------------------------------------------------------------- #
# Rechazos: rollback total
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("licencias", "tipo", "valor"),
    [
        ("0", "porcentaje", "30"),          # se liquida desde una licencia
        ("-1", "porcentaje", "30"),         # cantidad negativa
        ("dos", "porcentaje", "30"),        # no es entero
        ("3", "porcentaje", "0"),           # porcentaje fuera de rango
        ("3", "porcentaje", "101"),         # porcentaje fuera de rango
        ("3", "porcentaje", "30.5"),        # decimal
        ("1", "fijo", "500.000"),           # la comision supera el bruto
        ("3", "fijo", "mil"),               # valor no numerico
        ("3", "mitad", "30"),               # tipo inexistente
    ],
)
def test_un_dato_invalido_revierte_todo(client, db_session, licencias, tipo, valor):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)

    respuesta = _registrar(
        client, admin_id=admin.id, licencias=licencias, tipo=tipo, valor=valor
    )

    assert respuesta.status_code == 303
    assert "error=" in respuesta.headers["location"]
    assert db_session.execute(select(func.count(Settlement.id))).scalar_one() == 0
    assert _eventos(db_session, "liquidacion.") == []


def test_no_se_puede_liquidar_al_propio_titular(client, db_session):
    _entrada_titular(client, db_session)

    from license_manager.models import User

    propia = db_session.execute(select(User).where(User.rol == "owner")).scalar_one()
    respuesta = _registrar(client, admin_id=propia.id)

    assert "liquidarte a ti mismo" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(Settlement.id))).scalar_one() == 0


def test_no_se_liquida_a_una_cuenta_desactivada(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session, activo=False)

    respuesta = _registrar(client, admin_id=admin.id)

    assert "desactivada" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(Settlement.id))).scalar_one() == 0


def test_no_se_liquida_a_una_cuenta_inexistente(client, db_session):
    _entrada_titular(client, db_session)

    respuesta = _registrar(client, admin_id="00000000-0000-0000-0000-000000000000")

    assert "Elige a quien va dirigida" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(Settlement.id))).scalar_one() == 0


def test_los_parseos_rechazan_lo_que_no_es_entero():
    assert parse_licencias("3") == 3
    with pytest.raises(LiquidacionError):
        parse_licencias("3.5")
    assert parse_valor_comision("30", tipo="porcentaje") == 30
    with pytest.raises(LiquidacionError):
        parse_valor_comision("101", tipo="porcentaje")
    with pytest.raises(LiquidacionError):
        parse_valor_comision("0", tipo="fijo")


# --------------------------------------------------------------------------- #
# Una sola liquidacion por (periodo, beneficiario) y recalculo
# --------------------------------------------------------------------------- #


def test_el_segundo_registro_recalcula_la_misma_fila(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)

    _registrar(client, admin_id=admin.id, licencias="3", valor="30")
    _registrar(client, admin_id=admin.id, licencias="4", valor="50")

    assert db_session.execute(select(func.count(Settlement.id))).scalar_one() == 1
    liquidacion = _liquidacion(db_session)
    assert liquidacion.licencias_vendidas == 4
    assert liquidacion.comision_cop == 900_000  # 50% de 1.800.000
    acciones = [e.accion for e in _eventos(db_session, "liquidacion.")]
    assert acciones == ["liquidacion.creada", "liquidacion.recalculada"]


# --------------------------------------------------------------------------- #
# Pago: canal + comprobante y sellado inmutable
# --------------------------------------------------------------------------- #


def _pagar(client, liquidacion_id: str, *, metodo="nequi", comprobante="Nequi #849201"):
    return client.post(
        f"/liquidaciones/{liquidacion_id}/pago",
        data={"metodo": metodo, "comprobante": comprobante},
        follow_redirects=False,
    )


def test_el_pago_exige_canal_y_comprobante(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)
    _registrar(client, admin_id=admin.id)
    liquidacion_id = _liquidacion(db_session).id

    sin_comprobante = _pagar(client, liquidacion_id, comprobante="")
    sin_canal = _pagar(client, liquidacion_id, metodo="")

    assert "comprobante" in _ubicacion(sin_comprobante)
    assert "canal de pago" in _ubicacion(sin_canal)
    assert _liquidacion(db_session).pagado_at is None


def test_el_pago_sella_la_liquidacion_y_deja_auditoria(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)
    _registrar(client, admin_id=admin.id, licencias="3", valor="30")
    liquidacion_id = _liquidacion(db_session).id

    respuesta = _pagar(client, liquidacion_id)

    assert respuesta.status_code == 303
    liquidacion = _liquidacion(db_session)
    assert liquidacion.pagado_at is not None
    assert liquidacion.metodo_pago == PaymentMethod.NEQUI.value
    assert liquidacion.nota == "Nequi #849201"
    assert liquidacion.confirmado_por_id is not None

    acciones = [e.accion for e in _eventos(db_session, "liquidacion.")]
    assert acciones == ["liquidacion.creada", "liquidacion.pagada"]
    assert verify_chain(db_session).ok


def test_una_liquidacion_pagada_no_se_vuelve_a_pagar(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)
    _registrar(client, admin_id=admin.id)
    liquidacion_id = _liquidacion(db_session).id
    _pagar(client, liquidacion_id)

    respuesta = _pagar(client, liquidacion_id)

    assert "ya esta pagada" in _ubicacion(respuesta)


def test_una_liquidacion_pagada_es_inmutable(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)
    _registrar(client, admin_id=admin.id, licencias="3", valor="30")
    liquidacion_id = _liquidacion(db_session).id
    _pagar(client, liquidacion_id)
    sellada = _liquidacion(db_session)
    comision_original = sellada.comision_cop

    respuesta = _registrar(client, admin_id=admin.id, licencias="9", valor="90")

    assert "sellada" in _ubicacion(respuesta)
    assert _liquidacion(db_session).comision_cop == comision_original
    assert db_session.execute(select(func.count(Settlement.id))).scalar_one() == 1


def test_aplicar_y_pagar_reabre_el_modal(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)

    respuesta = _registrar(client, admin_id=admin.id)
    liquidacion_id = _liquidacion(db_session).id

    assert f"pago={liquidacion_id}" in respuesta.headers["location"]
    pagina = client.get(f"/liquidaciones?pago={liquidacion_id}")
    assert 'data-reabrir="1"' in pagina.text


# --------------------------------------------------------------------------- #
# La liquidacion no infla los ingresos (UI-07 / UI-07B)
# --------------------------------------------------------------------------- #


def test_la_liquidacion_no_crea_filas_en_ingresos(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)
    antes = db_session.execute(select(func.count(Income.id))).scalar_one()

    _registrar(client, admin_id=admin.id, licencias="3", valor="30")

    despues = db_session.execute(select(func.count(Income.id))).scalar_one()
    assert despues == antes, "una salida de dinero no es un ingreso"


# --------------------------------------------------------------------------- #
# Soporte imprimible
# --------------------------------------------------------------------------- #


def test_el_soporte_muestra_los_importes_y_permite_imprimir(client, db_session):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)
    _registrar(client, admin_id=admin.id, licencias="3", valor="30")
    liquidacion_id = _liquidacion(db_session).id
    _pagar(client, liquidacion_id)

    respuesta = client.get(f"/liquidaciones/{liquidacion_id}/soporte")

    assert respuesta.status_code == 200
    assert "Soporte de Liquidación de Comisiones" in respuesta.text
    assert "1.350.000" in respuesta.text
    assert "405.000" in respuesta.text
    assert "Nequi #849201" in respuesta.text
    assert "data-imprimir" in respuesta.text


def test_el_soporte_de_una_liquidacion_inexistente_redirige(client, db_session):
    _entrada_titular(client, db_session)

    respuesta = client.get(
        "/liquidaciones/00000000-0000-0000-0000-000000000000/soporte",
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    assert "no existe" in _ubicacion(respuesta)


# --------------------------------------------------------------------------- #
# CSP: sin scripts inline
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ruta", ["liquidaciones", "soporte"])
def test_las_pantallas_no_llevan_scripts_inline(client, db_session, ruta):
    _entrada_titular(client, db_session)
    admin = _admin(db_session)
    _registrar(client, admin_id=admin.id)
    liquidacion_id = _liquidacion(db_session).id

    url = "/liquidaciones" if ruta == "liquidaciones" else f"/liquidaciones/{liquidacion_id}/soporte"
    cuerpo = client.get(url).text

    assert cuerpo.count("<script") == 1
    assert 'src="/static/lm.js"' in cuerpo
    assert "onclick=" not in cuerpo
    assert "onsubmit=" not in cuerpo
    assert "BEGIN" not in cuerpo
