"""Pruebas del modelo mayorista: paquetes prepago y saldo de cupos (JHO-104).

Lo que se verifica aqui:

* El catalogo de paquetes es el del doc 04 v2.1 seccion 3 (3/5/10 licencias por
  $ 960.000 / $ 1.450.000 / $ 2.600.000), y la sociedad cobra el paquete
  COMPLETO al registrarlo, no en cada emision.
* El saldo de un tecnico es ``comprado - consumido`` y se calcula de los
  paquetes reales.
* Una emision comercial a nombre de un tecnico descuenta UN cupo del paquete
  mas antiguo con saldo (FIFO); sin saldo la emision se rechaza entera.
* Una reemision por reemplazo fisico justificado va a $ 0 COP y NO descuenta
  cupo: la licencia original ya estaba pagada.
* La compra de paquetes la registran solo los Titulares (403 para el delegado)
  y queda auditada en la cadena.
* La pantalla se rinde sin scripts inline (CSP ``script-src 'self'``).
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import func, select

from license_manager.models import (
    AuditEvent,
    Equipment,
    Income,
    License,
    Role,
    Technician,
    WholesalePackage,
)
from license_manager.services.audit import verify_chain
from license_manager.services.cierres import resumen_cierre
from license_manager.services.directory import create_customer, create_equipment
from license_manager.services.ingresos import hoy_utc
from license_manager.services.licencias import PRECIO_VENTA_COP
from license_manager.services.mayorista import (
    TIPOS_PAQUETE,
    MayoristaError,
    consumir_cupo,
    crear_tecnico,
    registrar_paquete,
    saldo_tecnico,
)
from license_manager.services.periodos import mes_actual

CLAVE_TITULAR = "clave-segura-123"
CLAVE_ADMIN = "clave-admin-larga-1"
NOMBRE_CLIENTE = "Verduleria La Huerta Central"
JUSTIFICACION = "Cambio de placa base por dano fisico comprobado"

CODIGOS = (
    "A4B9C102E8F37B1A",
    "5D2198AF44B10C3E",
    "77C1E40B9920A5D1",
    "F110B88201CA77E4",
    "3B1044F281D0C9B5",
    "99A211C077E5B6C7",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ubicacion(response) -> str:
    return unquote(response.headers["location"])


def _entrada_titular(client, db):
    crear_titular(db, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)


def _entrada_admin(client, db):
    admin = crear_usuario_rol(
        db, Role.ADMIN.value, email="admin@frubber.co", password=CLAVE_ADMIN,
        nombre="Operador Delegado",
    )
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)
    return admin


def _cliente(db) -> str:
    cliente = create_customer(db, actor_id=None, nombre=NOMBRE_CLIENTE)
    db.commit()
    return cliente.id


def _tecnico(db, nombre: str = "Carlos Balanzas") -> Technician:
    tecnico = crear_tecnico(db, actor_id=None, nombre=nombre, telefono="300 123 4567")
    db.commit()
    return tecnico


def _registrar_paquete(client, tecnico_id: str, tipo: str = "inicial", **extra):
    datos = {
        "tecnico_id": tecnico_id,
        "tipo": tipo,
        "metodo": extra.get("metodo", "nequi"),
        "comprobante": extra.get("comprobante", "Transferencia #77120"),
    }
    return client.post("/mayorista/paquetes", data=datos, follow_redirects=False)


def _emitir(
    client,
    cliente_id: str,
    codigo: str,
    *,
    tipo: str = "comercial",
    importe: str = "450.000",
    tecnico_id: str = "",
    justificacion: str | None = None,
):
    datos = {
        "cliente_id": cliente_id,
        "codigo": codigo,
        "tipo": tipo,
        "importe": importe,
        "tecnico_id": tecnico_id,
    }
    if justificacion is not None:
        datos["justificacion"] = justificacion
    elif tipo == "reemplazo":
        datos["justificacion"] = JUSTIFICACION
    return client.post("/emitir", data=datos, follow_redirects=False)


def _paquete(db, tipo: str = "inicial") -> WholesalePackage:
    return db.execute(
        select(WholesalePackage).where(WholesalePackage.tipo_paquete == tipo)
    ).scalars().first()


def _licencia(db) -> License | None:
    return db.execute(select(License).order_by(License.emitida_at.desc())).scalars().first()


def _consumidos(db, tecnico_id: str) -> int:
    total = db.execute(
        select(func.coalesce(func.sum(WholesalePackage.licencias_usadas), 0)).where(
            WholesalePackage.tecnico_id == tecnico_id
        )
    ).scalar_one()
    return int(total)


# --------------------------------------------------------------------------- #
# Acceso y pantalla
# --------------------------------------------------------------------------- #


def test_sin_sesion_no_se_entra(client):
    respuesta = client.get("/mayorista", follow_redirects=False)

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"


def test_los_tres_roles_consultan_la_pantalla(client, db_session):
    _entrada_admin(client, db_session)

    respuesta = client.get("/mayorista")

    assert respuesta.status_code == 200
    assert "Técnicos y Paquetes Prepago" in respuesta.text
    assert "Cupos disponibles" in respuesta.text
    # El delegado no recibe el formulario de compra, pero si la explicacion.
    assert 'action="/mayorista/paquetes"' not in respuesta.text
    assert "reservado a los Titulares" in respuesta.text


def test_el_titular_ve_el_formulario_de_paquete(client, db_session):
    _entrada_titular(client, db_session)
    _tecnico(db_session)

    respuesta = client.get("/mayorista")

    assert respuesta.status_code == 200
    assert 'action="/mayorista/paquetes"' in respuesta.text
    assert "Carlos Balanzas" in respuesta.text


def test_alta_de_tecnico_auditada(client, db_session):
    _entrada_titular(client, db_session)

    respuesta = client.post(
        "/mayorista/tecnicos",
        data={"nombre": "  Carlos Balanzas  ", "telefono": "300 123 4567", "email": "c@bal.co"},
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    tecnico = db_session.execute(select(Technician)).scalar_one()
    assert tecnico.nombre == "Carlos Balanzas"  # sin espacios sobrantes
    assert tecnico.activo is True

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "tecnico.creado")
    ).scalar_one()
    assert json.loads(evento.detalle)["nombre"] == "Carlos Balanzas"
    assert verify_chain(db_session).ok


@pytest.mark.parametrize("nombre", ["", "   "])
def test_el_nombre_del_tecnico_es_obligatorio(client, db_session, nombre):
    _entrada_titular(client, db_session)

    respuesta = client.post(
        "/mayorista/tecnicos", data={"nombre": nombre}, follow_redirects=False
    )

    assert "obligatorio" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(Technician.id))).scalar_one() == 0


def test_no_se_repite_un_tecnico_activo(client, db_session):
    _entrada_titular(client, db_session)
    _tecnico(db_session, "Carlos Balanzas")

    respuesta = client.post(
        "/mayorista/tecnicos", data={"nombre": "carlos balanzas"}, follow_redirects=False
    )

    assert "Ya existe" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(Technician.id))).scalar_one() == 1


def test_un_admin_no_registra_paquetes(client, db_session):
    _entrada_admin(client, db_session)
    tecnico = _tecnico(db_session)

    respuesta = _registrar_paquete(client, tecnico.id)

    assert respuesta.status_code == 403
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0
    assert db_session.execute(select(func.count(Income.id))).scalar_one() == 0


def test_un_admin_no_activa_ni_desactiva_tecnicos(client, db_session):
    _entrada_admin(client, db_session)
    tecnico = _tecnico(db_session)

    respuesta = client.post(
        f"/mayorista/tecnicos/{tecnico.id}/alternar",
        data={"activo": "0"},
        follow_redirects=False,
    )

    assert respuesta.status_code == 403
    db_session.refresh(tecnico)
    assert tecnico.activo is True


# --------------------------------------------------------------------------- #
# Catalogo y registro de paquetes
# --------------------------------------------------------------------------- #


def test_los_precios_son_los_del_doc_04():
    tabla = {
        tipo.valor: (tipo.licencias, tipo.valor_total_cop, tipo.costo_unitario_cop)
        for tipo in TIPOS_PAQUETE
    }
    assert tabla == {
        "inicial": (3, 960_000, 320_000),
        "profesional": (5, 1_450_000, 290_000),
        "master": (10, 2_600_000, 260_000),
    }


def test_registrar_paquete_crea_el_ingreso_mayorista(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)

    respuesta = _registrar_paquete(client, tecnico.id, "profesional")

    assert respuesta.status_code == 303
    paquete = _paquete(db_session, "profesional")
    assert paquete.licencias_total == 5
    assert paquete.licencias_usadas == 0
    assert paquete.valor_total_cop == 1_450_000
    assert paquete.costo_unitario_cop == 290_000

    ingreso = db_session.execute(select(Income)).scalar_one()
    assert ingreso.categoria == "paquete_mayorista"
    assert ingreso.monto_cop == 1_450_000
    assert ingreso.estado_pago == "pagado"
    assert ingreso.metodo_pago == "nequi"
    assert ingreso.periodo == hoy_utc()
    assert saldo_tecnico(db_session, tecnico.id) == 5

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "paquete.registrado")
    ).scalar_one()
    detalle = json.loads(evento.detalle)
    assert detalle["licencias"] == 5
    assert detalle["valor_total_cop"] == 1_450_000
    assert verify_chain(db_session).ok


@pytest.mark.parametrize(
    ("tipo", "metodo"),
    [("gigante", "nequi"), ("inicial", "bitcoin")],
)
def test_un_dato_invalido_no_registra_nada(client, db_session, tipo, metodo):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)

    respuesta = _registrar_paquete(client, tecnico.id, tipo, metodo=metodo)

    assert "error=" in respuesta.headers["location"]
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0
    assert db_session.execute(select(func.count(Income.id))).scalar_one() == 0


def test_no_se_registra_paquete_a_un_tecnico_desactivado(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    tecnico.activo = False
    db_session.commit()

    respuesta = _registrar_paquete(client, tecnico.id)

    assert "desactivada" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0


def test_un_tecnico_desactivado_conserva_su_saldo(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id)

    client.post(
        f"/mayorista/tecnicos/{tecnico.id}/alternar",
        data={"activo": "0"},
        follow_redirects=False,
    )

    db_session.refresh(tecnico)
    assert tecnico.activo is False
    assert saldo_tecnico(db_session, tecnico.id) == 3
    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "tecnico.actualizado")
    ).scalar_one()
    assert json.loads(evento.detalle)["activo"] is False


# --------------------------------------------------------------------------- #
# Descuento automatico de cupo en UI-05
# --------------------------------------------------------------------------- #


def test_emitir_a_nombre_de_un_tecnico_descuenta_un_cupo(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id, "inicial")
    cliente_id = _cliente(db_session)
    create_equipment(db_session, actor_id=None, hardware_code_raw=CODIGOS[0], cliente_id=cliente_id)
    db_session.commit()

    respuesta = _emitir(client, cliente_id, CODIGOS[0], tecnico_id=tecnico.id)

    assert respuesta.status_code == 303
    licencia = _licencia(db_session)
    assert licencia.tipo_emision == "comercial"
    assert licencia.cupo_descontado is True
    assert licencia.tecnico_id == tecnico.id
    assert licencia.paquete_id is not None
    assert saldo_tecnico(db_session, tecnico.id) == 2

    # El cupo ya estaba pagado: el movimiento queda en $ 0 y no vuelve a cobrar.
    ingreso = db_session.execute(
        select(Income).where(Income.licencia_id == licencia.id)
    ).scalar_one()
    assert ingreso.categoria == "paquete_mayorista"
    assert ingreso.monto_cop == 0

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "licencia.emitida")
    ).scalar_one()
    detalle = json.loads(evento.detalle)
    assert detalle["cupo_descontado"] is True
    assert detalle["tecnico_id"] == tecnico.id
    assert verify_chain(db_session).ok


def test_con_cupo_no_se_exige_el_importe(client, db_session):
    """El cupo ya esta pagado: la sociedad no vuelve a cobrar ni lo pide."""
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id)
    cliente_id = _cliente(db_session)

    respuesta = _emitir(client, cliente_id, CODIGOS[0], importe="", tecnico_id=tecnico.id)

    assert respuesta.status_code == 303
    licencia = _licencia(db_session)
    assert licencia is not None
    ingreso = db_session.execute(
        select(Income).where(Income.licencia_id == licencia.id)
    ).scalar_one()
    assert ingreso.monto_cop == 0
    assert saldo_tecnico(db_session, tecnico.id) == 2


def test_una_venta_directa_no_toca_ningun_paquete(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id)
    cliente_id = _cliente(db_session)
    create_equipment(db_session, actor_id=None, hardware_code_raw=CODIGOS[0], cliente_id=cliente_id)
    db_session.commit()

    _emitir(client, cliente_id, CODIGOS[0])  # sin tecnico

    ingreso = db_session.execute(
        select(Income).where(Income.categoria == "venta")
    ).scalar_one()
    assert ingreso.categoria == "venta"
    assert ingreso.monto_cop == PRECIO_VENTA_COP
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_sin_saldo_la_emision_se_rechaza_entera(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    cliente_id = _cliente(db_session)

    respuesta = _emitir(client, cliente_id, CODIGOS[0], tecnico_id=tecnico.id)

    assert "no tiene cupos disponibles" in _ubicacion(respuesta)
    # Rollback total: ni licencia, ni equipo, ni ingreso, ni auditoria a medias.
    assert db_session.execute(select(func.count(License.id))).scalar_one() == 0
    assert db_session.execute(select(func.count(Equipment.id))).scalar_one() == 0
    assert db_session.execute(select(func.count(Income.id))).scalar_one() == 0
    assert _consumidos(db_session, tecnico.id) == 0
    assert verify_chain(db_session).ok


def test_el_descuento_es_fifo_del_paquete_mas_antiguo(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id, "inicial")       # 3 cupos, el mas viejo
    _registrar_paquete(client, tecnico.id, "profesional")   # 5 cupos, despues
    cliente_id = _cliente(db_session)

    for indice in range(4):
        create_equipment(
            db_session, actor_id=None, hardware_code_raw=CODIGOS[indice], cliente_id=cliente_id
        )
        db_session.commit()
        _emitir(client, cliente_id, CODIGOS[indice], tecnico_id=tecnico.id)

    inicial = _paquete(db_session, "inicial")
    profesional = _paquete(db_session, "profesional")
    assert inicial.licencias_usadas == 3, "el paquete viejo se agota primero"
    assert profesional.licencias_usadas == 1
    assert saldo_tecnico(db_session, tecnico.id) == 4


def test_el_consumo_de_cupo_se_devuelve_al_revertir(db_session):
    """El descuento vive dentro de la transaccion de la emision."""
    tecnico = _tecnico(db_session)
    registrar_paquete(db_session, actor_id=None, tecnico_id=tecnico.id, tipo="inicial")
    db_session.commit()

    consumir_cupo(db_session, tecnico)
    assert _consumidos(db_session, tecnico.id) == 1

    db_session.rollback()
    assert _consumidos(db_session, tecnico.id) == 0


def test_una_venta_con_cupo_no_infla_el_cierre(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id, "inicial")
    cliente_id = _cliente(db_session)
    create_equipment(db_session, actor_id=None, hardware_code_raw=CODIGOS[0], cliente_id=cliente_id)
    db_session.commit()

    _emitir(client, cliente_id, CODIGOS[0], tecnico_id=tecnico.id)

    cierre = resumen_cierre(db_session, periodo=mes_actual())
    # Lo unico cobrado en el mes es el paquete prepago, no una venta suelta.
    assert cierre.total_cop == 960_000
    assert cierre.licencias_n == 0, "la licencia la pago el tecnico, no el verdulero"


def test_una_cortesia_al_tecnico_no_consume_cupo(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id)
    cliente_id = _cliente(db_session)

    _emitir(client, cliente_id, CODIGOS[0], tipo="cortesia", importe="0", tecnico_id=tecnico.id)

    licencia = _licencia(db_session)
    assert licencia.cupo_descontado is False
    assert licencia.tecnico_id == tecnico.id
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_un_tecnico_inactivo_no_recibe_emisiones(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id)
    tecnico.activo = False
    db_session.commit()
    cliente_id = _cliente(db_session)

    respuesta = _emitir(client, cliente_id, CODIGOS[0], tecnico_id=tecnico.id)

    assert "desactivada" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(License.id))).scalar_one() == 0
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_un_tecnico_inexistente_no_emite(client, db_session):
    _entrada_titular(client, db_session)
    cliente_id = _cliente(db_session)

    respuesta = _emitir(
        client, cliente_id, CODIGOS[0], tecnico_id="00000000-0000-0000-0000-000000000000"
    )

    assert "no existe" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(License.id))).scalar_one() == 0


# --------------------------------------------------------------------------- #
# Reemision por reemplazo fisico justificado
# --------------------------------------------------------------------------- #


def test_el_reemplazo_exige_justificacion(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id)
    cliente_id = _cliente(db_session)

    respuesta = _emitir(
        client, cliente_id, CODIGOS[0], tipo="reemplazo", importe="0", justificacion="roto"
    )

    assert "certificar el cambio fisico" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(License.id))).scalar_one() == 0
    assert db_session.execute(select(func.count(Equipment.id))).scalar_one() == 0
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_el_reemplazo_va_a_cero_y_no_descuenta_cupo(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _registrar_paquete(client, tecnico.id)
    cliente_id = _cliente(db_session)

    respuesta = _emitir(
        client,
        cliente_id,
        CODIGOS[0],
        tipo="reemplazo",
        importe="999.999",  # el tipo manda: un reemplazo siempre es $ 0
        tecnico_id=tecnico.id,
        justificacion=JUSTIFICACION,
    )

    assert respuesta.status_code == 303
    licencia = _licencia(db_session)
    assert licencia.tipo_emision == "reemplazo"
    assert licencia.cupo_descontado is False
    assert licencia.tecnico_id == tecnico.id
    assert licencia.paquete_id is None
    # La licencia original ya estaba pagada: el cupo no se toca.
    assert saldo_tecnico(db_session, tecnico.id) == 3

    ingreso = db_session.execute(
        select(Income).where(Income.licencia_id == licencia.id)
    ).scalar_one()
    assert ingreso.categoria == "reemplazo"
    assert ingreso.monto_cop == 0
    assert ingreso.nota == f"Reemplazo justificado: {JUSTIFICACION}"


# --------------------------------------------------------------------------- #
# Servicio: saldo y errores
# --------------------------------------------------------------------------- #


def test_el_saldo_suma_todos_los_paquetes(db_session):
    tecnico = _tecnico(db_session)
    registrar_paquete(db_session, actor_id=None, tecnico_id=tecnico.id, tipo="inicial")
    registrar_paquete(db_session, actor_id=None, tecnico_id=tecnico.id, tipo="master")
    db_session.commit()

    assert saldo_tecnico(db_session, tecnico.id) == 13


def test_consumir_cupo_sin_paquetes_falla(db_session):
    tecnico = _tecnico(db_session)

    with pytest.raises(MayoristaError):
        consumir_cupo(db_session, tecnico)


# --------------------------------------------------------------------------- #
# CSP: sin scripts inline
# --------------------------------------------------------------------------- #


def test_la_pantalla_no_lleva_scripts_inline(client, db_session):
    _entrada_titular(client, db_session)

    cuerpo = client.get("/mayorista").text

    assert cuerpo.count("<script") == 1
    assert 'src="/static/lm.js"' in cuerpo
    assert "onclick=" not in cuerpo
    assert "onsubmit=" not in cuerpo
    assert "<script>" not in cuerpo
    assert "BEGIN" not in cuerpo
