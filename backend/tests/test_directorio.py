"""Pruebas del directorio de clientes y del inventario de equipos (JHO-100).

Cubre las tres pantallas del issue:

* UI-03: alta, busqueda, activacion y desactivacion de verdulerias.
* UI-04: alta de equipos con el MISMO normalizador de codigo que firma.
* UI-06: ficha del equipo con la licencia FRB1 copiable y su bitacora.

Ademas fija dos reglas transversales: el Administrador Delegado si puede
registrar clientes y equipos (doc 04 v2.0), y ninguna pagina con datos del
negocio puede contener script inline (CSP ``script-src 'self'``).
"""

from __future__ import annotations

from datetime import date
from urllib.parse import unquote

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import select

from license_manager.crypto.frb1 import normalize_machine_code
from license_manager.models import AuditEvent, Customer, Equipment, Income, License
from license_manager.services.audit import verify_chain

CLAVE_TITULAR = "clave-segura-123"
CLAVE_ADMIN = "clave-admin-larga-1"
CODIGO = "7B4F892E1C33AA01"


def _ubicacion(response) -> str:
    """El header Location viene URL-codificado: se descodifica para leer el motivo."""
    return unquote(response.headers["location"])


def _entrada_clientes(client, **datos):
    cuerpo = {
        "nombre": "Verduleria La Huerta Central",
        "propietario": "Pedro Gomez",
        "telefono": "+57 310 123 4567",
        "direccion": "Calle 10 # 45-20",
        "ciudad": "Medellin",
    }
    cuerpo.update(datos)
    return client.post("/clientes", data=cuerpo, follow_redirects=False)


def _entrada_equipos(client, **datos):
    cuerpo = {"hardware_code": CODIGO, "cliente_id": "", "notas": ""}
    cuerpo.update(datos)
    return client.post("/equipos", data=cuerpo, follow_redirects=False)


# --------------------------------------------------------------------------- #
# Acceso
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ruta", ["/clientes", "/equipos"])
def test_sin_sesion_no_se_ve_nada_y_se_va_a_login(client, ruta):
    response = client.get(ruta, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_sin_sesion_no_se_puede_registrar_un_cliente(client, db_session):
    response = _entrada_clientes(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert db_session.execute(select(Customer)).scalars().all() == []


def test_el_titular_ve_el_directorio_vacio_y_su_guia(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.get("/clientes")

    assert response.status_code == 200
    assert "Directorio de Verdulerias y Clientes" in response.text
    assert "Todavia no hay verdulerias registradas" in response.text


# --------------------------------------------------------------------------- #
# UI-03 · Verdulerias
# --------------------------------------------------------------------------- #


def test_alta_de_verduleria_y_aparicion_en_el_listado(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = _entrada_clientes(client)

    assert response.status_code == 303
    assert _ubicacion(response).startswith("/clientes?aviso=")
    assert "registrada" in _ubicacion(response)

    listado = client.get("/clientes")
    assert "Verduleria La Huerta Central" in listado.text
    assert "Pedro Gomez" in listado.text
    assert "+57 310 123 4567" in listado.text
    assert "Sin licencia" in listado.text


def test_alta_de_verduleria_sin_nombre_es_rechazada(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = _entrada_clientes(client, nombre="   ")

    assert "El nombre del negocio es obligatorio." in _ubicacion(response)
    assert db_session.execute(select(Customer)).scalars().all() == []


def test_no_se_pueden_dos_negocios_activos_con_el_mismo_nombre(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_clientes(client)

    # El segundo intento cambia mayusculas: la regla no depende del teclado.
    respuesta = _entrada_clientes(client, nombre="VERDULERIA LA HUERTA CENTRAL")

    assert "Ya existe un negocio activo" in _ubicacion(respuesta)
    assert len(db_session.execute(select(Customer)).scalars().all()) == 1


def test_desactivar_no_borra_nada_y_los_equipos_quedan_conservados(client, db_session):
    titular = crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_clientes(client)
    cliente = db_session.execute(select(Customer)).scalar_one()
    _entrada_equipos(client, cliente_id=cliente.id)

    response = client.post(
        f"/clientes/{cliente.id}/alternar", data={"activo": "0"}, follow_redirects=False
    )

    assert response.status_code == 303
    db_session.expire_all()
    assert db_session.get(Customer, cliente.id) is not None
    assert db_session.get(Equipment, db_session.execute(select(Equipment)).scalar_one().id) is not None

    listado = client.get("/clientes")
    assert "Verduleria La Huerta Central" in listado.text
    assert "Negocio desactivado" in listado.text
    assert "conservados" in _ubicacion(response)
    assert titular is not None


def test_reactivar_un_negocio(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_clientes(client)
    cliente = db_session.execute(select(Customer)).scalar_one()

    client.post(f"/clientes/{cliente.id}/alternar", data={"activo": "0"}, follow_redirects=False)
    client.post(f"/clientes/{cliente.id}/alternar", data={"activo": "1"}, follow_redirects=False)

    db_session.expire_all()
    assert db_session.get(Customer, cliente.id).activo is True
    assert "vuelve a estar activa" in _ubicacion(
        client.post(f"/clientes/{cliente.id}/alternar", data={"activo": "1"}, follow_redirects=False)
    )


def test_el_buscador_de_clientes_filtra_por_nombre_propietario_y_telefono(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_clientes(client)
    _entrada_clientes(
        client,
        nombre="Frutas El Manantial",
        propietario="Maria Soto",
        telefono="+57 312 987 6543",
        ciudad="Bogota",
        direccion="",
    )

    por_nombre = client.get("/clientes", params={"q": "manantial"})
    assert "Frutas El Manantial" in por_nombre.text
    assert "La Huerta Central" not in por_nombre.text

    por_propietario = client.get("/clientes", params={"q": "soto"})
    assert "Frutas El Manantial" in por_propietario.text
    assert "La Huerta Central" not in por_propietario.text

    por_telefono = client.get("/clientes", params={"q": "987"})
    assert "Frutas El Manantial" in por_telefono.text

    sin_resultados = client.get("/clientes", params={"q": "no-existe-ninguno"})
    assert "No hay verdulerias que coincidan" in sin_resultados.text


def test_un_administrador_delegado_si_puede_registrar_clientes(client, db_session):
    """Doc 04 v2.0: el Administrador Delegado registra clientes y emite licencias."""
    crear_usuario_rol(
        db_session, "admin", email="admin@frubber.co", password=CLAVE_ADMIN
    )
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)

    assert client.get("/clientes").status_code == 200
    response = _entrada_clientes(client)
    assert response.status_code == 303
    assert "registrada" in _ubicacion(response)


# --------------------------------------------------------------------------- #
# UI-04 · Equipos
# --------------------------------------------------------------------------- #


def test_el_codigo_con_guiones_se_normaliza_igual_que_al_firmar(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = _entrada_equipos(client, hardware_code="7b4f-892e-1c33-aa01")

    assert response.status_code == 303
    db_session.expire_all()
    equipo = db_session.execute(select(Equipment)).scalar_one()
    # Lo que se guarda es exactamente lo que Frubber POS va a pedir verificar.
    assert equipo.hardware_code == normalize_machine_code("7b4f-892e-1c33-aa01") == CODIGO

    listado = client.get("/equipos")
    assert "7B4F-892E-1C33-AA01" in listado.text
    assert "Sin verduleria asignada" in listado.text


@pytest.mark.parametrize(
    "codigo",
    ["", "7B4F892E1C33AA0", "7B4F892E1C33AA011", "7B4F892E1C33AA0Z", "ABC-DEF-GHI-JKL"],
)
def test_un_codigo_invalido_no_da_de_alta_nada(client, db_session, codigo):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = _entrada_equipos(client, hardware_code=codigo)

    assert response.status_code == 303
    assert "error=" in str(response.headers["location"])
    assert db_session.execute(select(Equipment)).scalars().all() == []


def test_no_se_puede_registrar_el_mismo_codigo_dos_veces(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_equipos(client)

    response = _entrada_equipos(client)

    assert "ya esta registrado" in _ubicacion(response)
    assert len(db_session.execute(select(Equipment)).scalars().all()) == 1


def test_el_buscador_de_equipos_encuentra_con_guiones_y_sin_ellos(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_equipos(client)

    assert "7B4F-892E-1C33-AA01" in client.get("/equipos", params={"q": "7B4F-892E"}).text
    assert "7B4F-892E-1C33-AA01" in client.get("/equipos", params={"q": "1c33aa01"}).text
    assert "No hay equipos que coincidan" in client.get("/equipos", params={"q": "zzzz"}).text


def test_una_verduleria_desactivada_no_aparece_para_nuevos_equipos(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_clientes(client)
    cliente = db_session.execute(select(Customer)).scalar_one()
    client.post(f"/clientes/{cliente.id}/alternar", data={"activo": "0"}, follow_redirects=False)

    listado = client.get("/equipos")

    assert "La Huerta Central" not in listado.text
    assert "Todavia no hay verdulerias registradas" in listado.text


def test_alta_de_equipo_a_una_verduleria_inexistente(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = _entrada_equipos(client, cliente_id="00000000-0000-0000-0000-000000000000")

    assert "La verduleria indicada no existe." in _ubicacion(response)
    assert db_session.execute(select(Equipment)).scalars().all() == []


# --------------------------------------------------------------------------- #
# UI-06 · Ficha del equipo
# --------------------------------------------------------------------------- #


def _equipo_con_licencia(db, licencia_text: str | None = None) -> Equipment:
    """Equipo ya registrado con su licencia, para probar la ficha directamente."""
    from license_manager.services.directory import create_customer, create_equipment

    cliente = create_customer(db, actor_id=None, nombre="Verduleria La Huerta Central")
    equipo = create_equipment(
        db, actor_id=None, hardware_code_raw="7B4F-892E-1C33-AA01", cliente_id=cliente.id
    )
    if licencia_text is not None:
        db.add(
            License(
                key_id="lic-test-2026-10-15",
                equipo_id=equipo.id,
                licencia_text=licencia_text,
                tipo_emision="comercial",
                estado_cobro="pagado",
                emitida_fecha=date(2026, 10, 15),
                creado_por_id=None,
            )
        )
    db.commit()
    return equipo


def test_la_ficha_muestra_el_codigo_sin_guiones_internos_y_su_bitacora(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo_con_licencia(db_session)

    response = client.get(f"/equipos/{equipo.id}")

    assert response.status_code == 200
    assert "7B4F-892E-1C33-AA01" in response.text
    assert "Equipo registrado en el inventario" in response.text
    assert "Verduleria La Huerta Central" in response.text


def test_sin_licencia_la_ficha_no_ofrece_boton_de_copiar(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo_con_licencia(db_session)

    response = client.get(f"/equipos/{equipo.id}")

    assert "Todavia no hay licencia para este equipo" in response.text
    assert 'data-copiar' not in response.text


def test_con_licencia_el_texto_frb1_se_muestra_para_copiar(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    texto = "FRB1|lic-test-2026-10-15|7B4F892E1C33AA01|20261015.MEQCIQCabNoSample"
    equipo = _equipo_con_licencia(db_session, licencia_text=texto)

    response = client.get(f"/equipos/{equipo.id}")

    assert texto in response.text
    assert 'data-copiar="texto-licencia"' in response.text
    assert "Licencia activa / emitida" in response.text
    assert "Vitalicia (POS offline)" in response.text
    assert "ECDSA P-256 / SHA-256" in response.text


def test_la_ficha_no_contiene_script_inline_por_la_csp(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo_con_licencia(db_session, licencia_text="FRB1|k|7B4F892E1C33AA01|20261015.x")

    for ruta in ("/clientes", "/equipos", f"/equipos/{equipo.id}"):
        html = client.get(ruta).text
        bloques = [b for b in html.split("<script")[1:]]
        assert len(bloques) == 1, f"{ruta}: se esperaba un unico bloque <script>"
        assert bloques[0].lstrip().startswith('src="/static/lm.js"'), ruta


def test_un_equipo_inexistente_regresa_al_inventario_con_error(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.get("/equipos/00000000-0000-0000-0000-000000000000", follow_redirects=False)

    assert response.status_code == 303
    assert "El equipo solicitado no existe." in _ubicacion(response)


def test_se_puede_cambiar_la_verduleria_desde_la_ficha_y_el_historial_queda(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo_con_licencia(
        db_session, licencia_text="FRB1|k|7B4F892E1C33AA01|20261015.x"
    )
    _entrada_clientes(client, nombre="Frutas El Manantial", ciudad="Bogota")
    otra = db_session.execute(
        select(Customer).where(Customer.nombre == "Frutas El Manantial")
    ).scalar_one()

    asignar = client.post(
        f"/equipos/{equipo.id}/cliente", data={"cliente_id": otra.id}, follow_redirects=False
    )
    assert asignar.status_code == 303
    assert "asignado a Frutas El Manantial" in _ubicacion(asignar)

    quitar = client.post(
        f"/equipos/{equipo.id}/cliente", data={"cliente_id": ""}, follow_redirects=False
    )
    assert "sin verduleria asignada" in _ubicacion(quitar).lower()

    db_session.expire_all()
    ficha = client.get(f"/equipos/{equipo.id}", follow_redirects=False)
    assert ficha.status_code == 200, ficha.headers.get("location")
    assert "Sin verduleria asignada" in ficha.text
    # La licencia emitida con la verduleria anterior no desaparece.
    assert "Licencia activa / emitida" in ficha.text


def test_el_estado_financiero_se_muestra_en_pesos_enteros(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    equipo = _equipo_con_licencia(db_session, licencia_text="FRB1|k|7B4F892E1C33AA01|20261015.x")
    licencia = db_session.execute(select(License)).scalar_one()
    db_session.add(
        Income(
            licencia_id=licencia.id,
            categoria="venta",
            monto_cop=450000,
            estado_pago="pagado",
            metodo_pago="bancolombia",
            periodo=date(2026, 10, 15),
        )
    )
    db_session.commit()

    response = client.get(f"/equipos/{equipo.id}")

    assert "$450.000 COP" in response.text
    assert "Pagado" in response.text


# --------------------------------------------------------------------------- #
# Auditoria
# --------------------------------------------------------------------------- #


def test_cada_alta_y_cada_cambio_quedan_en_la_auditoria(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_clientes(client)
    cliente = db_session.execute(select(Customer)).scalar_one()
    _entrada_equipos(client, cliente_id=cliente.id)
    client.post(f"/clientes/{cliente.id}/alternar", data={"activo": "0"}, follow_redirects=False)

    acciones = [
        e.accion
        for e in db_session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars()
    ]

    assert "cliente.creado" in acciones
    assert "equipo.creado" in acciones
    assert "cliente.desactivado" in acciones
    # La cadena no se rompe por mas que se haya escrito mucho despues del genesis.
    assert verify_chain(db_session).ok is True


def test_la_auditoria_del_equipo_registra_el_codigo_y_el_negocio(client, db_session):
    crear_titular(db_session, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    _entrada_clientes(client)
    cliente = db_session.execute(select(Customer)).scalar_one()
    _entrada_equipos(client, cliente_id=cliente.id)

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "equipo.creado")
    ).scalar_one()

    assert evento.entidad_tipo == "equipo"
    assert CODIGO in (evento.detalle or "")
    assert cliente.id in (evento.detalle or "")
