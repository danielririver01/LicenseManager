"""Pruebas del motor de emision y la pantalla UI-05 (JHO-101).

Lo mas importante que se verifica aqui no es el formulario, es el contrato:

* La cadena que sale de License Manager se parsea y se verifica con el
  verificador de referencia ``verify_license``, el mismo algoritmo que corre
  Frubber POS con su llave publica embebida.
* El codigo que se firma es exactamente el de 16 hex que se pego, normalizado
  con la misma funcion del POS (doc 01 criterio 7, doc 02 seccion 5.2).
* Un codigo repetido no emite: se advierte y se lleva a la ficha existente.
* El dinero queda entero en COP y por tipo de emision.
* Un rechazo no deja nada a medias: ni licencia, ni ingreso, ni equipo.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import select

from license_manager.crypto.frb1 import parse_license, verify_license
from license_manager.deps import state
from license_manager.models import Customer, Equipment, Income, License
from license_manager.services.audit import verify_chain
from license_manager.services.directory import (
    create_customer,
    create_equipment,
    set_customer_active,
)

CLAVE_TITULAR = "clave-segura-123"
CODIGO = "A4B9C102E8F37B1A"
CODIGO_FORMATADO = "A4B9-C102-E8F3-7B1A"
NOMBRE_CLIENTE = "Verduleria La Huerta Central"


def _ubicacion(response) -> str:
    return unquote(response.headers["location"])


def _cliente(db, nombre: str = NOMBRE_CLIENTE) -> Customer:
    cliente = create_customer(db, actor_id=None, nombre=nombre)
    # El alta se hace en esta sesion de preparacion: sin commit, la peticion
    # del cliente (otra conexion) no veria el registro.
    db.commit()
    return cliente


def _equipo(db, codigo: str = CODIGO, cliente_id: str | None = None) -> Equipment:
    equipo = create_equipment(
        db, actor_id=None, hardware_code_raw=codigo, cliente_id=cliente_id
    )
    db.commit()
    return equipo


def _emitir(
    client,
    cliente_id: str,
    codigo: str = CODIGO,
    tipo: str = "comercial",
    importe: str = "450.000",
    justificacion: str | None = None,
    tecnico_id: str = "",
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
        # Doc 04 v2.1 seccion 6.2: toda reemision por reemplazo certifica el
        # cambio fisico; sin esa justificacion el servidor la rechaza.
        datos["justificacion"] = "Cambio de placa base por dano fisico"
    return client.post("/emitir", data=datos, follow_redirects=False)


# --------------------------------------------------------------------------- #
# Acceso y pantalla
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ruta", ["/emitir", f"/emitir?codigo={CODIGO}"])
def test_sin_sesion_no_se_emit(client, ruta):
    response = client.get(ruta, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_sin_sesion_no_se_puede_postear(client, db_session):
    cliente = _cliente(db_session)
    response = _emitir(client, cliente.id)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    # Ni la licencia ni el alta del equipo llegan a existir.
    assert db_session.execute(select(License)).scalars().all() == []
    assert db_session.execute(select(Equipment)).scalars().all() == []


def test_el_formulario_arranca_limpio(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _cliente(db_session)

    response = client.get("/emitir")

    assert response.status_code == 200
    # Nada preseleccionado ni prellenado: el operador decide todo.
    assert '<option value="" selected>' in response.text
    assert 'data-codigo-equipo' in response.text
    assert 'placeholder="XXXX-XXXX-XXXX-XXXX"' in response.text
    assert 'value=""' in response.text
    assert 'name="importe"' in response.text
    assert 'placeholder="450.000"' in response.text
    assert 'value="450' not in response.text
    # El resumen muestra los guiones vacios hasta que se pegue el codigo.
    assert "----------------" in response.text
    assert "Datos de la emision" in response.text


def test_sin_verdulerias_el_formulario_avisa_y_no_se_envia(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.get("/emitir")

    assert "Todavia no hay verdulerias registradas" in response.text
    assert '<form method="post" action="/emitir">' not in response.text


def test_sin_llave_de_firma_se_avisa_y_el_boton_se_deshabilita(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _cliente(db_session)

    llave_guardada = state.signing_key
    try:
        state.signing_key = None
        response = client.get("/emitir")
        assert "llave de firma no esta disponible" in response.text
        assert 'class="btn btn-primario" disabled' in response.text
    finally:
        state.signing_key = llave_guardada


def test_desde_la_ficha_se_prellena_cliente_y_codigo(client, db_session):
    """UI-06 ofrece «Emitir licencia para este equipo»: llega todo puesto."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    equipo = _equipo(db_session, cliente_id=cliente.id)

    response = client.get(f"/emitir?equipo={equipo.id}")

    assert response.status_code == 200
    assert f'value="{CODIGO_FORMATADO}"' in response.text
    assert f'value="{cliente.id}"' in response.text
    # El equipo elegido no tiene licencia, asi que no hay advertencia.
    assert "Codigo repetido" not in response.text


# --------------------------------------------------------------------------- #
# El contrato: lo firmado es exactamente lo que el POS verifica
# --------------------------------------------------------------------------- #


def test_la_licencia_emitida_lo_verifica_el_algoritmo_del_pos(client, db_session):
    titular = crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    response = _emitir(client, cliente.id)

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


def test_el_codigo_pegado_con_guiones_y_minusculas_se_normaliza(client, db_session):
    """El tecnico pega lo que vio en el TPV, no la forma canonica."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    response = _emitir(client, cliente.id, codigo="a4b9-c102-e8f3-7b1a")

    assert response.status_code == 303
    equipo = db_session.execute(select(Equipment)).scalar_one()
    assert equipo.hardware_code == CODIGO
    assert equipo.cliente_id == cliente.id
    licencia = db_session.execute(select(License)).scalar_one()
    assert f"|{CODIGO}|" in licencia.licencia_text


def test_el_equipo_alta_en_la_misma_emision(client, db_session):
    """La maquina no existia: se da de alta al firmar (doc 02, 5.2)."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    assert db_session.execute(select(Equipment)).scalars().all() == []
    response = _emitir(client, cliente.id)

    assert response.status_code == 303
    equipo = db_session.execute(select(Equipment)).scalar_one()
    assert equipo.hardware_code == CODIGO
    assert equipo.cliente_id == cliente.id


def test_la_venta_estandar_queda_registrada_con_su_ingreso(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    response = _emitir(client, cliente.id, importe="450.000")

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
    cliente = _cliente(db_session)

    # Aunque el operador escriba algo en el importe, el tipo manda.
    response = _emitir(client, cliente.id, tipo=tipo, importe="999.999")

    assert response.status_code == 303
    licencia = db_session.execute(select(License)).scalar_one()
    assert licencia.tipo_emision == tipo
    assert licencia.estado_cobro == estado_cobro
    ingreso = db_session.execute(select(Income)).scalar_one()
    assert ingreso.monto_cop == 0
    assert ingreso.categoria == categoria


# --------------------------------------------------------------------------- #
# Validacion estricta del hardware code (16 hex)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "codigo",
    [
        "",
        "abc",
        "A4B9C102E8F37B1Z",   # no hexadecimal
        "A4B9C102E8F37B1",    # 15 caracteres
        "A4B9C102E8F37B1A00", # 18 caracteres
        "9999",               # demasiado corto
        "FRUBBER-POS-2026!!",
    ],
)
def test_un_codigo_invalido_no_emite_nada_y_no_alta_equipo(client, db_session, codigo):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    response = _emitir(client, cliente.id, codigo=codigo)

    assert response.status_code == 303
    assert "error=" in _ubicacion(response)
    assert db_session.execute(select(License)).scalars().all() == []
    assert db_session.execute(select(Income)).scalars().all() == []
    assert db_session.execute(select(Equipment)).scalars().all() == []


def test_un_importe_invalido_no_deja_el_equipo_a_medias(client, db_session):
    """El rollback es total: ni licencia, ni ingreso, ni alta de equipo."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    response = _emitir(client, cliente.id, importe="450.5")

    assert response.status_code == 303
    assert "error=" in _ubicacion(response)
    assert db_session.execute(select(License)).scalars().all() == []
    assert db_session.execute(select(Income)).scalars().all() == []
    assert db_session.execute(select(Equipment)).scalars().all() == []


@pytest.mark.parametrize("importe", ["", "abc", "-450000", "450.5", "450 000 COP", "1e6"])
def test_importes_rechazados_en_venta_estandar(client, db_session, importe):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    response = _emitir(client, cliente.id, importe=importe)

    assert "error=" in _ubicacion(response)
    assert db_session.execute(select(License)).scalars().all() == []


def test_el_importe_acepta_puntos_de_miles(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    _emitir(client, cliente.id, importe="450.000")

    ingreso = db_session.execute(select(Income)).scalar_one()
    assert ingreso.monto_cop == 450000


def test_sin_verduleria_no_se_emite(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = _emitir(client, "")

    assert "Elige la verduleria" in _ubicacion(response)
    assert db_session.execute(select(Equipment)).scalars().all() == []


def test_una_verduleria_inexistente_no_se_emite(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = _emitir(client, "00000000-0000-0000-0000-000000000000")

    assert "Elige la verduleria" in _ubicacion(response)
    assert db_session.execute(select(License)).scalars().all() == []


def test_una_verduleria_desactivada_no_recibe_licencias(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    set_customer_active(db_session, customer=cliente, active=False)
    db_session.commit()

    response = _emitir(client, cliente.id)

    assert "desactivada" in _ubicacion(response)
    assert db_session.execute(select(License)).scalars().all() == []


def test_un_tipo_desconocido_se_rechaza(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)

    response = _emitir(client, cliente.id, tipo="gratis-total")

    assert "Elige un tipo de licencia valido" in _ubicacion(response)
    assert db_session.execute(select(License)).scalars().all() == []


def test_un_administrador_delegado_si_puede_emitir(client, db_session):
    crear_usuario_rol(db_session, "admin", email="admin@frubber.co", password="clave-admin-larga-1")
    iniciar_sesion(client, "admin@frubber.co", "clave-admin-larga-1")
    cliente = _cliente(db_session)

    response = _emitir(client, cliente.id)

    assert response.status_code == 303
    assert len(db_session.execute(select(License)).scalars().all()) == 1


# --------------------------------------------------------------------------- #
# Codigo repetido: se advierte y se lleva a la ficha (doc 02, seccion 7)
# --------------------------------------------------------------------------- #


def test_un_codigo_repetido_no_emite_y_se_ofrece_la_ficha(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    _emitir(client, cliente.id)
    licencia = db_session.execute(select(License)).scalar_one()

    # Hasta un Reemplazo queda bloqueado: esta pantalla no decide esa politica.
    response = _emitir(client, cliente.id, tipo="reemplazo", importe="0")

    ubicacion = _ubicacion(response)
    assert ubicacion.startswith("/emitir?")
    assert "licencia=" not in ubicacion
    assert len(db_session.execute(select(License)).scalars().all()) == 1

    # El formulario vuelve con la advertencia y el enlace a la ficha.
    aviso = client.get(ubicacion)
    assert aviso.status_code == 200
    assert "Codigo repetido: este equipo ya tiene licencia" in aviso.text
    assert f"/equipos/{licencia.equipo_id}" in aviso.text
    assert CODIGO_FORMATADO in aviso.text


def test_el_codigo_repetido_no_duplica_al_equipo(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    _emitir(client, cliente.id)

    _emitir(client, cliente.id, tipo="cortesia", importe="0")

    assert len(db_session.execute(select(Equipment)).scalars().all()) == 1


def test_el_codigo_repetido_se_avisa_antes_que_el_importe(client, db_session):
    """Lo primero es decir «ya tiene licencia»; el dinero va despues."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    _emitir(client, cliente.id)

    response = _emitir(client, cliente.id, importe="")

    ubicacion = _ubicacion(response)
    assert "licencia=" not in ubicacion
    assert "error=" not in ubicacion

    aviso = client.get(ubicacion)
    assert "Codigo repetido: este equipo ya tiene licencia" in aviso.text
    assert "Escribe el importe" not in aviso.text


def test_el_equipo_registrado_sin_licencia_se_reutiliza(client, db_session):
    """Ya estaba en UI-04 sin licencia: no se crea una segunda fila."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    _equipo(db_session)  # alta previa, sin cliente ni licencia

    response = _emitir(client, cliente.id)

    assert response.status_code == 303
    assert len(db_session.execute(select(Equipment)).scalars().all()) == 1
    equipo = db_session.execute(select(Equipment)).scalar_one()
    assert equipo.cliente_id == cliente.id
    assert len(db_session.execute(select(License)).scalars().all()) == 1


def test_un_equipo_de_otra_verduleria_no_se_toma(client, db_session):
    """El codigo coincide pero pertenece a otro negocio: se pide revisar."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    duena = _cliente(db_session, "Verduleria La Huerta Central")
    otra = _cliente(db_session, "Frutas El Manantial")
    _equipo(db_session, cliente_id=duena.id)

    response = _emitir(client, otra.id)

    ubicacion = _ubicacion(response)
    assert "ya esta registrado a nombre de Verduleria La Huerta Central" in ubicacion
    assert db_session.execute(select(License)).scalars().all() == []
    equipo = db_session.execute(select(Equipment)).scalar_one()
    assert equipo.cliente_id == duena.id


def test_el_reemplazo_va_a_una_maquina_nueva(client, db_session):
    """Doc 04 v2.1 seccion 6: el reemplazo es para el equipo fisico nuevo."""
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    _emitir(client, cliente.id)

    response = _emitir(
        client, cliente.id, codigo="C9D8E7F6051423AB", tipo="reemplazo", importe="0"
    )

    assert response.status_code == 303
    assert len(db_session.execute(select(License)).scalars().all()) == 2
    assert len(db_session.execute(select(Equipment)).scalars().all()) == 2


# --------------------------------------------------------------------------- #
# Como se ve el resultado
# --------------------------------------------------------------------------- #


def test_el_resultado_muestra_el_bloque_para_copiar(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    response = _emitir(client, cliente.id)
    query = parse_qs(urlparse(response.headers["location"]).query)
    licencia_id = query["licencia"][0]

    resultado = client.get(f"/emitir?licencia={licencia_id}")

    assert resultado.status_code == 200
    licencia = db_session.execute(select(License)).scalar_one()
    assert licencia.licencia_text in resultado.text
    assert 'data-copiar="texto-licencia"' in resultado.text
    assert "$450.000 COP" in resultado.text
    assert NOMBRE_CLIENTE in resultado.text
    assert CODIGO_FORMATADO in resultado.text


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
    cliente = _cliente(db_session)
    _emitir(client, cliente.id)
    licencia = db_session.execute(select(License)).scalar_one()

    ficha = client.get(f"/equipos/{licencia.equipo_id}")
    assert licencia.licencia_text in ficha.text
    assert "Licencia activa / emitida" in ficha.text
    assert "ECDSA P-256 / SHA-256" in ficha.text

    inventario = client.get("/equipos")
    assert "Venta" in inventario.text
    assert "ECDSA P-256" in inventario.text


# --------------------------------------------------------------------------- #
# Auditoria
# --------------------------------------------------------------------------- #


def test_la_auditoria_registra_la_emision_sin_el_texto_frb1(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    cliente = _cliente(db_session)
    _emitir(client, cliente.id)
    licencia = db_session.execute(select(License)).scalar_one()

    from license_manager.models import AuditEvent

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "licencia.emitida")
    ).scalar_one()

    assert evento.entidad_id == licencia.id
    detalle = evento.detalle or ""
    assert '"tipo_emision":"comercial"' in detalle
    assert CODIGO in detalle
    assert NOMBRE_CLIENTE in detalle
    # El texto firmado no se copia a la bitacora.
    assert licencia.licencia_text not in detalle
    assert verify_chain(db_session).ok is True
