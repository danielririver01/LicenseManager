"""Pruebas del cobro online con pasarela (JHO-108).

Lo que protegen estas pruebas es la plata: que nadie pueda acreditarse cupos sin
haber pagado y que un pago no se acredite dos veces. Se usa un SIMULADOR de la
pasarela: nada de red ni de credenciales reales.

Reglas cubiertas:

* La firma del aviso (HMAC-SHA256 con la plantilla oficial) y su rechazo.
* La notificacion no dice cuanto se pago: el importe se consulta y se compara.
* Idempotencia: reintentos del aviso y pagos repetidos.
* Modo pruebas contra produccion.
* Un pago aprobado acredita exactamente un paquete y su ingreso.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from sqlalchemy import func, select

from license_manager.config import Settings
from license_manager.models import EstadoPago, Income, PagoMayorista, WholesalePackage
from license_manager.services import pagos
from license_manager.services.mayorista import TIPOS_PAQUETE, crear_tecnico, saldo_tecnico
from license_manager.services.pagos import PagoError, RespuestaPasarela

SECRETO = "secreto-de-prueba-webhook"
TOKEN = "token-de-prueba"
REFERENCIA = "ref-orden-1"


# --------------------------------------------------------------------------- #
# Dobles de prueba
# --------------------------------------------------------------------------- #


class PasarelaFalsa:
    """Simulador de Mercado Pago controlado por la prueba."""

    def __init__(self, *, pagos_por_id: dict | None = None, falla: bool = False):
        self.pagos = pagos_por_id or {}
        self.falla = falla
        self.preferencias: list[dict] = []

    def crear_preferencia(self, payload: dict) -> RespuestaPasarela:
        if self.falla:
            return RespuestaPasarela(False, {}, "sin respuesta de la pasarela (Timeout)")
        self.preferencias.append(payload)
        return RespuestaPasarela(
            True, {"id": "pref-1", "init_point": "https://mp.example/checkout/pref-1"}
        )

    def obtener_pago(self, payment_id: str) -> RespuestaPasarela:
        if self.falla:
            return RespuestaPasarela(False, {}, "sin respuesta de la pasarela (Timeout)")
        pago = self.pagos.get(str(payment_id))
        if pago is None:
            return RespuestaPasarela(False, {}, "HTTP 404: not found")
        return RespuestaPasarela(True, pago)

    def buscar_pagos(self, referencia: str) -> RespuestaPasarela:
        if self.falla:
            return RespuestaPasarela(False, {}, "sin respuesta de la pasarela (Timeout)")
        return RespuestaPasarela(
            True,
            {"results": [p for p in self.pagos.values() if p.get("external_reference") == referencia]},
        )


def _ajustes(**extra) -> Settings:
    base = {
        "_env_file": None,
        "environment": "test",
        "database_url": "sqlite://",
        "mp_access_token": TOKEN,
        "mp_webhook_secret": SECRETO,
        "mp_production": True,
        "mp_base_url": "https://licencias.frubber.co",
    }
    base.update(extra)
    return Settings(**base)


def _firma(data_id: str, *, ts: str = "1704908010", request_id: str = "req-1", secreto: str = SECRETO) -> str:
    firma = pagos.calcular_firma(
        data_id=data_id, x_request_id=request_id, ts=ts, secret=secreto
    )
    return f"ts={ts},v1={firma}"


def _pago(
    *, payment_id: str = "999", referencia: str = REFERENCIA, monto: float = 960_000.0,
    estado: str = "approved", live: bool = True, detalle: str = "accredited",
) -> dict:
    return {
        "id": payment_id,
        "status": estado,
        "status_detail": detalle,
        "transaction_amount": monto,
        "external_reference": referencia,
        "live_mode": live,
        "currency_id": "COP",
    }


def _tecnico(db, nombre: str = "Carlos Balanzas"):
    tecnico = crear_tecnico(db, actor_id=None, nombre=nombre)
    db.commit()
    return tecnico


def _orden(db, tecnico, tipo: str = "inicial") -> PagoMayorista:
    orden = pagos.crear_orden(db, tecnico=tecnico, tipo=tipo)
    db.commit()
    return orden


# --------------------------------------------------------------------------- #
# Firma del aviso
# --------------------------------------------------------------------------- #


def test_la_firma_sigue_la_plantilla_oficial():
    esperada = hmac.new(
        SECRETO.encode(),
        b"id:999;request-id:req-1;ts:1704908010;",
        hashlib.sha256,
    ).hexdigest()

    assert (
        pagos.calcular_firma(data_id="999", x_request_id="req-1", ts="1704908010", secret=SECRETO)
        == esperada
    )


def test_los_valores_ausentes_se_omiten_en_la_plantilla():
    """La documentacion pide quitar los campos que no vienen."""
    esperada = hmac.new(SECRETO.encode(), b"id:999;ts:1;", hashlib.sha256).hexdigest()

    assert pagos.calcular_firma(data_id="999", x_request_id="", ts="1", secret=SECRETO) == esperada


def test_un_id_alfanumerico_va_en_minusculas():
    mayusculas = pagos.calcular_firma(
        data_id="ABC-123", x_request_id="r", ts="1", secret=SECRETO
    )
    minusculas = pagos.calcular_firma(
        data_id="abc-123", x_request_id="r", ts="1", secret=SECRETO
    )

    assert mayusculas == minusculas


def test_la_firma_valida_da_verdadero():
    assert pagos.firma_valida(
        x_signature=_firma("999"), x_request_id="req-1", data_id="999", secret=SECRETO
    )


@pytest.mark.parametrize(
    ("firma", "request_id", "data_id", "secreto"),
    [
        ("ts=1,v1=deadbeef", "req-1", "999", SECRETO),      # firma inventada
        (_firma("999"), "req-1", "999", "otro-secreto"),    # secreto distinto
        (_firma("999", ts=""), "req-1", "999", SECRETO),    # sin ts
        ("basura", "req-1", "999", SECRETO),                # encabezado malformado
        (_firma("999"), "req-1", "", SECRETO),              # sin data.id
        (_firma("999"), "req-1", "999", None),              # sin secreto configurado
        (_firma("111"), "req-1", "999", SECRETO),           # firma de otro id
    ],
)
def test_cualquier_firma_que_no_corresponde_se_rechaza(firma, request_id, data_id, secreto):
    assert not pagos.firma_valida(
        x_signature=firma, x_request_id=request_id, data_id=data_id, secret=secreto
    )


# --------------------------------------------------------------------------- #
# Orden y preferencia
# --------------------------------------------------------------------------- #


def test_la_orden_nace_pendiente_con_el_precio_del_paquete(db_session):
    tecnico = _tecnico(db_session)

    orden = _orden(db_session, tecnico, "profesional")

    assert orden.estado == EstadoPago.PENDING.value
    assert orden.monto_cop == 1_450_000
    assert orden.referencia and orden.mp_payment_id is None


def test_no_se_crea_orden_de_un_paquete_inventado(db_session):
    tecnico = _tecnico(db_session)

    with pytest.raises(PagoError, match="paquete valido"):
        pagos.crear_orden(db_session, tecnico=tecnico, tipo="gigante")


def test_no_se_crea_orden_para_una_ficha_desactivada(db_session):
    tecnico = _tecnico(db_session)
    tecnico.activo = False
    db_session.commit()

    with pytest.raises(PagoError, match="desactivada"):
        pagos.crear_orden(db_session, tecnico=tecnico, tipo="inicial")


def test_la_preferencia_lleva_la_referencia_y_la_notificacion(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico)

    payload = pagos.payload_preferencia(orden, base_url="https://licencias.frubber.co/")

    assert payload["external_reference"] == orden.referencia
    assert payload["items"][0]["currency_id"] == "COP"
    assert payload["items"][0]["unit_price"] == float(orden.monto_cop)
    # El retorno lleva al tecnico a SU orden, no a una pantalla generica.
    assert payload["back_urls"]["success"].endswith(f"/mayorista/recarga/{orden.id}")
    # Solo webhooks, y la notificacion apunta a nuestra ruta publica.
    assert payload["notification_url"].startswith("https://licencias.frubber.co/webhooks/mercadopago")
    assert "source_news=webhooks" in payload["notification_url"]


def test_los_tres_paquetes_tienen_su_precio():
    techo = {t.valor: t.valor_total_cop for t in TIPOS_PAQUETE}

    assert techo == {"inicial": 960_000, "profesional": 1_450_000, "master": 2_600_000}


# --------------------------------------------------------------------------- #
# Acreditacion
# --------------------------------------------------------------------------- #


def test_un_pago_aprobado_acredita_el_paquete_y_su_ingreso(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")

    pagos.confirmar_pago(
        db_session, orden=orden, pago=_pago(monto=960_000.0), produccion=True
    )
    db_session.commit()

    assert orden.estado == EstadoPago.APPROVED.value
    assert orden.pagado_at is not None
    assert orden.paquete_id is not None
    assert saldo_tecnico(db_session, tecnico.id) == 3

    paquete = db_session.execute(select(WholesalePackage)).scalar_one()
    assert paquete.valor_total_cop == 960_000
    ingreso = db_session.execute(select(Income)).scalar_one()
    assert ingreso.categoria == "paquete_mayorista"
    assert ingreso.monto_cop == 960_000
    assert "Mercado Pago" in (ingreso.nota or "")


def test_un_pago_no_se_acredita_dos_veces(db_session):
    """Los reintentos de la pasarela no pueden regalar cupos."""
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")
    pago = _pago(monto=960_000.0)

    for _ in range(3):
        pagos.confirmar_pago(db_session, orden=orden, pago=pago, produccion=True)
    db_session.commit()

    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 1
    assert db_session.execute(select(func.count(Income.id))).scalar_one() == 1
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_un_importe_distinto_no_acredita_nada(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")

    with pytest.raises(PagoError, match="no coincide"):
        pagos.confirmar_pago(
            db_session, orden=orden, pago=_pago(monto=1.0), produccion=True
        )

    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0
    assert orden.estado == EstadoPago.PENDING.value


def test_un_pago_de_pruebas_no_acredita_en_produccion(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")

    with pytest.raises(PagoError, match="no es del modo configurado"):
        pagos.confirmar_pago(
            db_session, orden=orden, pago=_pago(monto=960_000.0, live=False), produccion=True
        )

    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0


def test_un_pago_productivo_no_acredita_en_pruebas(db_session):
    """El control va en los dos sentidos."""
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")

    with pytest.raises(PagoError, match="no es del modo configurado"):
        pagos.confirmar_pago(
            db_session, orden=orden, pago=_pago(monto=960_000.0, live=True), produccion=False
        )


def test_un_pago_rechazado_deja_la_orden_rechazada(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")

    pagos.confirmar_pago(
        db_session,
        orden=orden,
        pago=_pago(estado="rejected", detalle="cc_rejected_insufficient_amount"),
        produccion=True,
    )
    db_session.commit()

    assert orden.estado == EstadoPago.REJECTED.value
    assert orden.detalle_pasarela == "cc_rejected_insufficient_amount"
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0


def test_un_pago_devuelto_queda_cancelado(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")

    pagos.confirmar_pago(
        db_session, orden=orden, pago=_pago(estado="refunded"), produccion=True
    )

    assert orden.estado == EstadoPago.CANCELED.value


def test_un_pago_en_proceso_deja_la_orden_pendiente(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")

    pagos.confirmar_pago(
        db_session, orden=orden, pago=_pago(estado="in_process"), produccion=True
    )

    assert orden.estado == EstadoPago.PENDING.value
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0


def test_si_la_ficha_se_desactivo_despues_de_pagar_queda_por_revisar(db_session):
    """El dinero no se pierde: queda marcado para que el Titular lo resuelva."""
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")
    tecnico.activo = False
    db_session.commit()

    pagos.confirmar_pago(
        db_session, orden=orden, pago=_pago(monto=960_000.0), produccion=True
    )
    db_session.commit()

    assert orden.estado == EstadoPago.REVIEW.value
    assert "no acreditado" in (orden.nota or "")
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0
    assert orden.mp_payment_id == "999", "el pago queda identificado para revisarlo"


# --------------------------------------------------------------------------- #
# Webhook
# --------------------------------------------------------------------------- #


def test_sin_configurar_la_funcion_queda_apagada(db_session):
    assert not pagos.pasarela_configurada(_ajustes(mp_access_token=None))
    assert not pagos.pasarela_configurada(_ajustes(mp_webhook_secret=None))
    assert pagos.pasarela_configurada(_ajustes())


def test_el_webhook_con_firma_invalida_no_hace_nada(db_session):
    tecnico = _tecnico(db_session)
    _orden(db_session, tecnico)
    cliente = PasarelaFalsa(pagos_por_id={"999": _pago(monto=960_000.0)})

    resultado = pagos.procesar_webhook(
        db_session,
        data_id="999",
        x_signature="ts=1,v1=inventada",
        x_request_id="req-1",
        cliente=cliente,
        settings=_ajustes(),
    )

    assert not resultado.aceptado
    assert resultado.etiqueta == "firma_invalida"
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0


def test_el_webhook_bien_firmado_acredita(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")
    pago = _pago(payment_id="999", referencia=orden.referencia, monto=960_000.0)
    cliente = PasarelaFalsa(pagos_por_id={"999": pago})

    resultado = pagos.procesar_webhook(
        db_session,
        data_id="999",
        x_signature=_firma("999"),
        x_request_id="req-1",
        cliente=cliente,
        settings=_ajustes(),
    )
    db_session.commit()

    assert resultado.aceptado
    assert resultado.etiqueta == "orden_aprobado"
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_el_webhook_repetido_no_duplica_cupos(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")
    pago = _pago(payment_id="999", referencia=orden.referencia, monto=960_000.0)
    cliente = PasarelaFalsa(pagos_por_id={"999": pago})

    for _ in range(3):
        pagos.procesar_webhook(
            db_session,
            data_id="999",
            x_signature=_firma("999"),
            x_request_id="req-1",
            cliente=cliente,
            settings=_ajustes(),
        )
    db_session.commit()

    assert saldo_tecnico(db_session, tecnico.id) == 3
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 1


def test_un_pago_ya_usado_no_acredita_otra_orden(db_session):
    tecnico = _tecnico(db_session)
    primera = _orden(db_session, tecnico, "inicial")
    segunda = _orden(db_session, tecnico, "inicial")
    # La pasarela devuelve el MISMO pago, pero con la referencia de la segunda.
    cliente = PasarelaFalsa(
        pagos_por_id={"999": _pago(payment_id="999", referencia=segunda.referencia, monto=960_000.0)}
    )
    pagos.confirmar_pago(
        db_session,
        orden=primera,
        pago=_pago(payment_id="999", referencia=primera.referencia, monto=960_000.0),
        produccion=True,
    )
    db_session.commit()

    resultado = pagos.procesar_webhook(
        db_session,
        data_id="999",
        x_signature=_firma("999"),
        x_request_id="req-1",
        cliente=cliente,
        settings=_ajustes(),
    )
    db_session.commit()

    assert resultado.etiqueta == "pago_repetido"
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 1
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_una_notificacion_de_una_orden_desconocida_no_rompe(db_session):
    cliente = PasarelaFalsa(
        pagos_por_id={"999": _pago(referencia="referencia-que-no-existe", monto=1.0)}
    )

    resultado = pagos.procesar_webhook(
        db_session,
        data_id="999",
        x_signature=_firma("999"),
        x_request_id="req-1",
        cliente=cliente,
        settings=_ajustes(),
    )

    assert resultado.aceptado
    assert resultado.etiqueta == "orden_desconocida"


def test_si_la_pasarela_no_responde_se_pide_reintento(db_session):
    tecnico = _tecnico(db_session)
    _orden(db_session, tecnico)
    cliente = PasarelaFalsa(falla=True)

    resultado = pagos.procesar_webhook(
        db_session,
        data_id="999",
        x_signature=_firma("999"),
        x_request_id="req-1",
        cliente=cliente,
        settings=_ajustes(),
    )

    assert not resultado.aceptado
    assert resultado.etiqueta == "consulta_fallida"
    assert "token" not in resultado.detalle.lower()


# --------------------------------------------------------------------------- #
# Conciliacion (red de seguridad si el aviso no llega)
# --------------------------------------------------------------------------- #


def test_la_conciliacion_acredita_si_el_aviso_no_llego(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")
    cliente = PasarelaFalsa(
        pagos_por_id={"777": _pago(payment_id="777", referencia=orden.referencia, monto=960_000.0)}
    )

    resultado = pagos.conciliar_orden(
        db_session, orden=orden, cliente=cliente, settings=_ajustes()
    )
    db_session.commit()

    assert resultado.etiqueta == "orden_aprobado"
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_la_conciliacion_no_hace_nada_si_no_hay_pago_aprobado(db_session):
    tecnico = _tecnico(db_session)
    orden = _orden(db_session, tecnico, "inicial")
    cliente = PasarelaFalsa(
        pagos_por_id={"777": _pago(payment_id="777", referencia=orden.referencia, estado="pending")}
    )

    resultado = pagos.conciliar_orden(
        db_session, orden=orden, cliente=cliente, settings=_ajustes()
    )

    assert resultado.etiqueta == "sin_pago_aprobado"
    assert orden.estado == EstadoPago.PENDING.value


# --------------------------------------------------------------------------- #
# Presentacion
# --------------------------------------------------------------------------- #


def test_el_monto_entero_redondea_al_peso():
    assert pagos.monto_entero(960000.0) == 960_000
    assert pagos.monto_entero("960000.4") == 960_000
    assert pagos.monto_entero("no-es-numero") == -1


def test_las_etiquetas_de_estado_son_legibles():
    assert pagos.etiqueta_estado(EstadoPago.APPROVED.value)[0].startswith("Pagado")
    assert pagos.etiqueta_estado(EstadoPago.REVIEW.value)[1] == "rojo"
    assert pagos.etiqueta_estado(EstadoPago.REJECTED.value)[0] == "Rechazado"


# --------------------------------------------------------------------------- #
# Rutas: recarga, retorno y webhook
# --------------------------------------------------------------------------- #


def _entrada_tecnico(client, db, *, activo: bool = True):
    """Tecnico con cuenta y ficha, listo para recargar."""
    from license_manager.models import Role, User
    from license_manager.security.passwords import hash_password

    tecnico = crear_tecnico(db, actor_id=None, nombre="Carlos Balanzas")
    tecnico.activo = activo
    db.add(
        User(
            nombre_completo=tecnico.nombre,
            email="carlos@balanzas.co",
            password_hash=hash_password("clave-tecnico-larga"),
            rol=Role.TECNICO.value,
            activo=True,
            tecnico_id=tecnico.id,
        )
    )
    db.commit()
    client.post(
        "/login",
        data={"email": "carlos@balanzas.co", "password": "clave-tecnico-larga"},
        follow_redirects=False,
    )
    return tecnico


def _activar_pasarela(settings, monkeypatch, *, pagos_por_id=None, falla=False):
    settings.mp_access_token = TOKEN
    settings.mp_webhook_secret = SECRETO
    settings.mp_base_url = "https://licencias.frubber.co"
    settings.mp_production = True
    falsa = PasarelaFalsa(pagos_por_id=pagos_por_id or {}, falla=falla)
    monkeypatch.setattr("license_manager.web.routes._cliente_pasarela", lambda _s: falsa)
    return falsa


def test_la_recarga_esta_oculta_si_no_hay_pasarela(client, db_session):
    """Sin configurar, el tecnico no ve el boton y la ruta no existe."""
    from urllib.parse import unquote

    _entrada_tecnico(client, db_session)

    cuerpo = client.get("/mayorista").text
    assert "Recargar mis cupos" not in cuerpo
    assert "El saldo lo carga la sociedad" in cuerpo

    respuesta = client.post(
        "/mayorista/recargar", data={"tipo": "inicial"}, follow_redirects=False
    )
    assert "cobro en línea no está configurado" in unquote(respuesta.headers["location"])


def test_la_recarga_lleva_a_la_pasarela(client, db_session, settings, monkeypatch):
    _entrada_tecnico(client, db_session)
    falsa = _activar_pasarela(settings, monkeypatch)

    cuerpo = client.get("/mayorista").text
    assert "Recargar mis cupos" in cuerpo
    assert "Pagarte ahora" in cuerpo or "Pagar ahora" in cuerpo

    respuesta = client.post(
        "/mayorista/recargar", data={"tipo": "profesional"}, follow_redirects=False
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].startswith("https://mp.example/checkout/")
    # La preferencia salio con la referencia de la orden recien creada.
    assert len(falsa.preferencias) == 1
    orden = db_session.execute(select(PagoMayorista)).scalar_one()
    assert falsa.preferencias[0]["external_reference"] == orden.referencia
    assert orden.mp_preference_id == "pref-1"
    assert orden.estado == EstadoPago.PENDING.value


def test_si_la_pasarela_no_responde_la_orden_queda_cancelada(
    client, db_session, settings, monkeypatch
):
    from urllib.parse import unquote

    _entrada_tecnico(client, db_session)
    _activar_pasarela(settings, monkeypatch, falla=True)

    respuesta = client.post(
        "/mayorista/recargar", data={"tipo": "inicial"}, follow_redirects=False
    )

    assert "no respondió" in unquote(respuesta.headers["location"])
    orden = db_session.execute(select(PagoMayorista)).scalar_one()
    assert orden.estado == EstadoPago.CANCELED.value, "no queda una orden colgada"


def test_el_webhook_acredita_y_responde_200(client, db_session, settings, monkeypatch):
    tecnico = _entrada_tecnico(client, db_session)
    orden = pagos.crear_orden(db_session, tecnico=tecnico, tipo="inicial")
    db_session.commit()
    pago = _pago(payment_id="500", referencia=orden.referencia, monto=960_000.0)
    _activar_pasarela(settings, monkeypatch, pagos_por_id={"500": pago})

    respuesta = client.post(
        "/webhooks/mercadopago?data.id=500&type=payment",
        headers={"x-signature": _firma("500"), "x-request-id": "req-1"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["resultado"] == "orden_aprobado"
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_el_webhook_sin_firma_valida_responde_401(client, db_session, settings, monkeypatch):
    tecnico = _entrada_tecnico(client, db_session)
    orden = pagos.crear_orden(db_session, tecnico=tecnico, tipo="inicial")
    db_session.commit()
    _activar_pasarela(
        settings, monkeypatch, pagos_por_id={"500": _pago(payment_id="500", referencia=orden.referencia)}
    )

    respuesta = client.post(
        "/webhooks/mercadopago?data.id=500&type=payment",
        headers={"x-signature": "ts=1,v1=inventada", "x-request-id": "req-1"},
    )

    assert respuesta.status_code == 401
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0


def test_el_webhook_ignora_los_eventos_que_no_son_pagos(client, db_session, settings, monkeypatch):
    _entrada_tecnico(client, db_session)
    _activar_pasarela(settings, monkeypatch)

    respuesta = client.post("/webhooks/mercadopago?type=subscription_preapproval&data.id=1")

    assert respuesta.status_code == 200
    assert respuesta.json()["ignorado"] == "subscription_preapproval"


def test_el_webhook_no_existe_sin_configurar(client, db_session):
    _entrada_tecnico(client, db_session)

    respuesta = client.post("/webhooks/mercadopago?data.id=1&type=payment")

    assert respuesta.status_code == 404


def test_el_webhook_queda_en_la_auditoria(client, db_session, settings, monkeypatch):
    from license_manager.models import AuditEvent
    from license_manager.services.audit import verify_chain

    tecnico = _entrada_tecnico(client, db_session)
    orden = pagos.crear_orden(db_session, tecnico=tecnico, tipo="inicial")
    db_session.commit()
    _activar_pasarela(
        settings,
        monkeypatch,
        pagos_por_id={"500": _pago(payment_id="500", referencia=orden.referencia, monto=960_000.0)},
    )

    client.post(
        "/webhooks/mercadopago?data.id=500&type=payment",
        headers={"x-signature": _firma("500"), "x-request-id": "req-1"},
    )

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "pago.webhook")
    ).scalar_one()
    assert evento.usuario_id is None, "el aviso viene de la pasarela, no de una persona"
    assert "orden_aprobado" in (evento.detalle or "")
    # El token nunca se registra.
    assert TOKEN not in (evento.detalle or "")
    assert verify_chain(db_session).ok


def test_la_pantalla_de_retorno_concilia_si_falto_el_aviso(
    client, db_session, settings, monkeypatch
):
    """Red de seguridad: si el webhook se pierde, el tecnico igual queda acreditado."""
    tecnico = _entrada_tecnico(client, db_session)
    orden = pagos.crear_orden(db_session, tecnico=tecnico, tipo="inicial")
    db_session.commit()
    _activar_pasarela(
        settings,
        monkeypatch,
        pagos_por_id={"777": _pago(payment_id="777", referencia=orden.referencia, monto=960_000.0)},
    )

    respuesta = client.get(f"/mayorista/recarga/{orden.id}")

    assert respuesta.status_code == 200
    assert "¡Pago confirmado!" in respuesta.text
    assert saldo_tecnico(db_session, tecnico.id) == 3


def test_no_se_puede_ver_la_recarga_de_otro_tecnico(client, db_session, settings, monkeypatch):
    from urllib.parse import unquote

    otro = crear_tecnico(db_session, actor_id=None, nombre="Andrea Restrepo")
    db_session.commit()
    orden_ajena = pagos.crear_orden(db_session, tecnico=otro, tipo="inicial")
    db_session.commit()
    _entrada_tecnico(client, db_session)
    _activar_pasarela(settings, monkeypatch)

    respuesta = client.get(f"/mayorista/recarga/{orden_ajena.id}", follow_redirects=False)

    assert "no existe" in unquote(respuesta.headers["location"])


def test_un_titular_no_se_recarga_a_si_mismo(client, db_session, settings, monkeypatch):
    """La recarga en linea es del tecnico; la sociedad registra compras a mano."""
    from conftest import crear_titular, iniciar_sesion

    crear_titular(db_session, password="clave-segura-123")
    iniciar_sesion(client, "titular@frubber.co", "clave-segura-123")
    _activar_pasarela(settings, monkeypatch)

    respuesta = client.post(
        "/mayorista/recargar", data={"tipo": "inicial"}, follow_redirects=False
    )

    assert respuesta.status_code == 403
    assert "es para el acceso del Técnico" in respuesta.text
