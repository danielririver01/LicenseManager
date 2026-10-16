"""Pruebas del motor de emision y la pantalla UI-05 (JHO-101).

Lo mas importante que se verifica aqui no es el formulario, es el contrato:

* La cadena que sale de License Manager se parsea y se verifica con el
  verificador de referencia ``verify_license``, el mismo algoritmo que corre
  Frubber POS con su llave publica embebida.
* Un equipo no acumula licencias vigentes.
* El dinero queda entero en COP y por tipo de emision.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import select

from license_manager.crypto.frb1 import parse_license, verify_license
from license_manager.deps import state
from license_manager.models import Equipment, Income, License
from license_manager.services.audit import verify_chain
from license_manager.services.directory import create_equipment

CLAVE_TITULAR = "clave-segura-123"
CODIGO = "A4B9C102E8F37B1A"


def _ubicacion(response) -> str:
    return unquote(response.headers["location"])


def _equipo(db, codigo: str = CODIGO, notas: str | None = None) -> Equipment:
    equipo = create_equipment(db, actor_id=None, hardware_code_raw=codigo, notas=notas)
    # El alta se hace en esta sesion de preparacion: sin commit, la peticion
    # del cliente (otra conexion) no veria el equipo.
    db.commit()
    return equipo


def _emitir(client, equipo_id: str, tipo: str = "comercial", importe: str = "450.000"):
    return client.post(
        "/emitir",
        data={"equipo_id": equipo_id, "tipo": tipo, "importe": importe},
        follow_redirects=False,
    )


# --------------------------------------------------------------------------- #
# Acceso y pantalla
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ruta", ["/emitir", "/emitir?equipo=x"])
def test_sin_sesion_no_se_emit(client, ruta):
    response = client.get(ruta, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_sin_sesion_no_se_puede_postear(client, db_session):
    equipo = _equipo(db_session)
    response = _emitir(client, equipo.id)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert db_session.execute(select(License)).scalars().all() == []


def test_el_formulario_arranca_limpio(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _equipo(db_session)

    response = client.get("/emitir")

    assert response.status_code == 200
    # Nada preseleccionado ni prellenado: el operador decide todo.
    assert '<option value="" selected>&mdash; Elige el equipo &mdash;</option>' in response.text
    assert 'name="importe"' in response.text
    assert 'placeholder="450.000"' in response.text
    assert 'value="450' not in response.text
    assert "Datos de la emision" in response.text


def test_sin_equipos_el_formulario_avisa_y_no_se_envia(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.get("/emitir")

    assert "Todavia no hay equipos en el inventario" in response.text
    assert '<form method="post" action="/emitir">' not in response.text


def test_sin_llave_de_firma_se_avisa_y_el_boton_se_deshabilita(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _equipo(db_session)

    llave_guardada = state.signing_key
    try:
        state.signing_key = None
        response = client.get("/emitir")
        assert "llave de firma no esta disponible" in response.text
        assert 'btn btn-primario" disabled' in response.text
    finally:
        state.signing_key = llave_guardada


# --------------------------------------------------------------------------- #
# El contrato: lo firmado es exactamente lo que el POS verifica
# --------------------------------------------------------------------------- #


def test_la_licencia_emitida_lo_verifica_el_algoritmo_del_pos(client, db_session):
    titular = crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)

    response = _emitir(client, equipo.id)

    assert response.status_code == 303
    assert "/emitir?licencia=" in response.headers["location"]

    licencia = db_session.execute(select(License)).scalar_one()
    assert licencia.creado_por_id == titular.id
    assert licencia.licencia_text.startswith("FRB1|")
    assert f"|{CODIGO}|" in licencia.licencia_text

    parsed = parse_license(licencia.licencia_text)
    assert parsed is not None
    assert parsed.machine_code == CODIGO
    assert parsed.key_id == licencia.key_id
    assert parsed.issued_date == licencia.emitida_fecha
    # El verificador de referencia es el mismo algoritmo del POS.
    assert verify_license(
        state.signing_key.public_key, parsed.key_id, parsed.machine_code,
        parsed.issued_date, parsed.signature,
    )


def test_la_venta_estandar_queda_registrada_con_su_ingreso(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)

    response = _emitir(client, equipo.id, importe="450.000")

    assert response.status_code == 303
    licencia = db_session.execute(select(License)).scalar_one()
    assert licencia.tipo_emision == "comercial"
    assert licencia.estado_registro == "vigente"
    assert licencia.estado_cobro == "pagado"
    assert licencia.key_id == state.signing_key.key_id

    ingreso = db_session.execute(select(Income)).scalar_one()
    assert ingreso.licencia_id == licencia.id
    assert ingreso.monto_cop == 450000 and isinstance(ingreso.monto_cop, int)
    assert ingreso.categoria == "venta"
    assert ingreso.estado_pago == "pagado"


@pytest.mark.parametrize(
    ("tipo", "categoria", "estado_cobro"),
    [
        ("prueba", "gratuito", "prueba_activa"),
        ("reemplazo", "reemplazo", "no_aplica"),
        ("cortesia", "gratuito", "no_aplica"),
    ],
)
def test_los_tipos_sin_cobro_quedan_en_cero_pesos(
    client, db_session, tipo, categoria, estado_cobro
):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)

    # Aunque el operador escriba algo en el importe, el tipo manda.
    response = _emitir(client, equipo.id, tipo=tipo, importe="999.999")

    assert response.status_code == 303
    licencia = db_session.execute(select(License)).scalar_one()
    assert licencia.tipo_emision == tipo
    assert licencia.estado_cobro == estado_cobro
    ingreso = db_session.execute(select(Income)).scalar_one()
    assert ingreso.monto_cop == 0
    assert ingreso.categoria == categoria


# --------------------------------------------------------------------------- #
# Reglas de negocio
# --------------------------------------------------------------------------- #


def test_un_equipo_no_acumula_dos_licencias_vigentes(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)
    _emitir(client, equipo.id)

    response = _emitir(client, equipo.id, tipo="reemplazo", importe="0")

    assert "ya tiene una licencia vigente" in _ubicacion(response)
    assert "Reemplazo" in _ubicacion(response)
    assert len(db_session.execute(select(License)).scalars().all()) == 1


def test_el_reemplazo_va_al_equipo_nuevo(client, db_session):
    """Doc 04 v2.1 seccion 6: el reemplazo se emite para la maquina nueva."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    viejo = _equipo(db_session, "B1C2D3E4F5061728")
    _emitir(client, viejo.id)

    nuevo = _equipo(db_session, "C9D8E7F6051423AB")
    response = _emitir(client, nuevo.id, tipo="reemplazo", importe="0")

    assert response.status_code == 303
    assert len(db_session.execute(select(License)).scalars().all()) == 2


@pytest.mark.parametrize("importe", ["", "abc", "-450000", "450.5", "450 000 COP", "1e6"])
def test_un_importe_invalido_no_emite_nada(client, db_session, importe):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)

    response = _emitir(client, equipo.id, importe=importe)

    assert response.status_code == 303
    assert "error=" in str(response.headers["location"])
    assert db_session.execute(select(License)).scalars().all() == []
    assert db_session.execute(select(Income)).scalars().all() == []


def test_el_importe_acepta_puntos_de_miles(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)

    _emitir(client, equipo.id, importe="450.000")

    ingreso = db_session.execute(select(Income)).scalar_one()
    assert ingreso.monto_cop == 450000


def test_sin_elegir_equipo_se_pide_de_nuevo(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = _emitir(client, "")

    assert "Selecciona el equipo" in _ubicacion(response)


def test_un_tipo_desconocido_se_rechaza(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)

    response = _emitir(client, equipo.id, tipo="gratis-total")

    assert "Elige un tipo de licencia valido" in _ubicacion(response)
    assert db_session.execute(select(License)).scalars().all() == []


def test_un_administrador_delegado_si_puede_emitir(client, db_session):
    crear_usuario_rol(db_session, "admin", email="admin@frubber.co", password="clave-admin-larga-1")
    iniciar_sesion(client, "admin@frubber.co", "clave-admin-larga-1")
    equipo = _equipo(db_session)

    response = _emitir(client, equipo.id)

    assert response.status_code == 303
    assert len(db_session.execute(select(License)).scalars().all()) == 1


# --------------------------------------------------------------------------- #
# Como se ve el resultado
# --------------------------------------------------------------------------- #


def test_el_resultado_muestra_el_bloque_para_copiar(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)
    response = _emitir(client, equipo.id)
    query = parse_qs(urlparse(response.headers["location"]).query)
    licencia_id = query["licencia"][0]

    resultado = client.get(f"/emitir?licencia={licencia_id}")

    assert resultado.status_code == 200
    licencia = db_session.execute(select(License)).scalar_one()
    assert licencia.licencia_text in resultado.text
    assert 'data-copiar="texto-licencia"' in resultado.text
    assert "$450.000 COP" in resultado.text


def test_una_licencia_inexistente_no_se_muestra(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.get(
        "/emitir?licencia=00000000-0000-0000-0000-000000000000", follow_redirects=False
    )

    assert response.status_code == 303
    assert "La licencia indicada no existe." in _ubicacion(response)


def test_la_ficha_y_el_inventario_reflejan_la_emision(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)
    _emitir(client, equipo.id)
    licencia = db_session.execute(select(License)).scalar_one()

    ficha = client.get(f"/equipos/{equipo.id}")
    assert licencia.licencia_text in ficha.text
    assert "Licencia activa / emitida" in ficha.text
    assert "ECDSA P-256 / SHA-256" in ficha.text

    inventario = client.get("/equipos")
    assert "Venta" in inventario.text
    assert "ECDSA P-256" in inventario.text

    # En /emitir la opcion queda marcada como usada.
    selector = client.get("/emitir")
    assert "ya tiene licencia vigente" in selector.text
    assert 'disabled' in selector.text


# --------------------------------------------------------------------------- #
# Auditoria
# --------------------------------------------------------------------------- #


def test_la_auditoria_registra_la_emision_sin_el_texto_frb1(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo(db_session)
    _emitir(client, equipo.id)
    licencia = db_session.execute(select(License)).scalar_one()

    from license_manager.models import AuditEvent

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "licencia.emitida")
    ).scalar_one()

    assert evento.entidad_id == licencia.id
    detalle = evento.detalle or ""
    assert '"tipo_emision":"comercial"' in detalle
    assert CODIGO in detalle
    # El texto firmado no se copia a la bitacora.
    assert licencia.licencia_text not in detalle
    assert verify_chain(db_session).ok is True
