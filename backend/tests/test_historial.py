"""Pruebas del historial de auditoria inmutable (JHO-105, UI-08).

Lo que se verifica aqui:

* Los tres roles consultan el historial; el administrador delegado con
  "lectura basica" (doc 04 v2.1, seccion 7) y sin la accion de verificar.
* Cada fila muestra la trazabilidad completa: hora UTC del servidor, operador
  con el rol congelado en el evento, entidad y el hash que la encadena.
* La pantalla recalcula la cadena al abrir y delata una manipulacion hecha por
  fuera de la aplicacion (SQL crudo).
* Verificar la cadena sella el resultado en el propio log y es cosa de
  Titulares (403 para el delegado).
* El detalle guardado se traduce a una frase legible, con respaldo en JSON.
* La pantalla no lleva scripts inline (CSP ``script-src 'self'``).
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import select, text

from license_manager.models import AuditEvent, Role
from license_manager.services.audit import verify_chain
from license_manager.services.directory import create_customer, create_equipment
from license_manager.services.historial import (
    etiqueta_accion,
    resumen_detalle,
    tono_accion,
)

CLAVE_TITULAR = "clave-segura-123"
CLAVE_ADMIN = "clave-admin-larga-1"
CODIGO = "A4B9C102E8F37B1A"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ubicacion(response) -> str:
    return unquote(response.headers["location"])


def _entrada_titular(client, db):
    crear_titular(db, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)


def _entrada_admin(client, db):
    crear_usuario_rol(
        db, Role.ADMIN.value, email="admin@frubber.co", password=CLAVE_ADMIN,
        nombre="Operador Delegado",
    )
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)


def _emitir_una(client, db):
    """Deja un evento de emision real en el historial."""
    cliente = create_customer(db, actor_id=None, nombre="Verduleria La Huerta Central")
    create_equipment(db, actor_id=None, hardware_code_raw=CODIGO, cliente_id=cliente.id)
    db.commit()
    client.post(
        "/emitir",
        data={
            "cliente_id": cliente.id,
            "codigo": CODIGO,
            "tipo": "comercial",
            "importe": "450.000",
        },
        follow_redirects=False,
    )


def _eventos(db) -> list[AuditEvent]:
    return list(db.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars())


# --------------------------------------------------------------------------- #
# Acceso: consulta para los tres roles
# --------------------------------------------------------------------------- #


def test_sin_sesion_no_se_entra(client):
    respuesta = client.get("/historial", follow_redirects=False)

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"


def test_el_titular_ve_la_pantalla_completa(client, db_session):
    _entrada_titular(client, db_session)

    respuesta = client.get("/historial")

    assert respuesta.status_code == 200
    assert "Historial de Auditoría del Sistema" in respuesta.text
    assert "Verificar cadena" in respuesta.text
    assert "lectura básica" not in respuesta.text


def test_el_delegado_tiene_lectura_basica(client, db_session):
    _entrada_admin(client, db_session)

    respuesta = client.get("/historial")

    assert respuesta.status_code == 200
    assert "Historial de Auditoría del Sistema" in respuesta.text
    assert "lectura básica" in respuesta.text
    # Sin la accion de custodia: solo el Titular verifica la cadena.
    assert "Verificar cadena" not in respuesta.text


# --------------------------------------------------------------------------- #
# Trazabilidad de las filas
# --------------------------------------------------------------------------- #


def test_cada_fila_trae_operador_rol_fecha_utc_y_hash(client, db_session):
    _entrada_titular(client, db_session)
    _emitir_una(client, db_session)

    cuerpo = client.get("/historial").text

    # Hora del servidor con marca UTC.
    assert "UTC" in cuerpo
    # Nombre y rol congelado del operador que ejecuto la accion.
    assert "Jhoan Rivera" in cuerpo
    assert "Titular Principal" in cuerpo
    # Traduccion del evento de emision.
    assert "Emision de licencia FRB1" in cuerpo
    assert "Verduleria La Huerta Central" in cuerpo
    assert "A4B9-C102-E8F3-7B1A" in cuerpo
    # La evidencia de la cadena: el hash corto de la fila.
    evento = _eventos(db_session)[-1]
    assert evento.hash[:12] in cuerpo


def test_la_pantalla_reporta_la_cadena_integra(client, db_session):
    _entrada_titular(client, db_session)
    _emitir_una(client, db_session)

    cuerpo = client.get("/historial").text

    assert "&#10004; Íntegra" in cuerpo
    assert "Hash encadenado correcto" in cuerpo
    assert verify_chain(db_session).ok


def test_el_detalle_se_traduce_y_cae_al_json_crudo():
    texto = resumen_detalle(
        "licencia.emitida",
        {
            "tipo_emision": "comercial",
            "cliente_nombre": "La Huerta",
            "hardware_code": CODIGO,
            "cupo_descontado": True,
            "tecnico_nombre": "Carlos Balanzas",
        },
    )
    assert "Tipo comercial" in texto
    assert "A4B9-C102-E8F3-7B1A" in texto
    assert "cupo prepago descontado" in texto
    assert "Carlos Balanzas" in texto

    cobro = resumen_detalle(
        "ingreso.registrado",
        {"categoria": "venta", "monto_cop": 450000, "estado_pago": "pagado"},
    )
    assert "$450.000 COP" in cobro

    assert etiqueta_accion("login.fallido") == "Intento de sesion fallido"
    assert tono_accion("login.fallido") == "rojo"
    assert tono_accion("licencia.emitida") == "azul"

    # Sin traductor propio se muestra el detalle tal como quedo en el log.
    assert resumen_detalle("accion.desconocida", {"z": 1, "a": 2}) == '{"a": 2, "z": 1}'
    assert resumen_detalle("accion.desconocida", None) == ""


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #


def test_el_filtro_por_grupo_acota_los_eventos(client, db_session):
    _entrada_titular(client, db_session)
    _emitir_una(client, db_session)

    usuarios = client.get("/historial?grupo=usuarios").text
    licencias = client.get("/historial?grupo=licencias").text

    assert "Inicio de sesion" in usuarios
    assert "Emision de licencia FRB1" not in usuarios
    assert "Emision de licencia FRB1" in licencias
    assert "Inicio de sesion" not in licencias


def test_el_filtro_por_operador(client, db_session):
    _entrada_titular(client, db_session)
    _emitir_una(client, db_session)
    _entrada_admin(client, db_session)

    solo_admin = client.get("/historial?usuario=admin@frubber.co").text

    assert "Operador Delegado" in solo_admin
    assert "Jhoan Rivera" not in solo_admin


def test_la_busqueda_libre_encuentra_por_detalle(client, db_session):
    _entrada_titular(client, db_session)
    _emitir_una(client, db_session)

    encontrado = client.get("/historial?q=La Huerta").text

    assert "La Huerta" in encontrado
    assert "No hay eventos que coincidan" not in encontrado


# --------------------------------------------------------------------------- #
# Verificacion de la cadena
# --------------------------------------------------------------------------- #


def test_el_titular_verifica_y_sella_la_verificacion(client, db_session):
    _entrada_titular(client, db_session)
    antes = len(_eventos(db_session))

    respuesta = client.post("/historial/verificar", follow_redirects=False)

    assert respuesta.status_code == 303
    assert "Cadena integra" in _ubicacion(respuesta)

    eventos = _eventos(db_session)
    assert len(eventos) == antes + 1
    ultimo = eventos[-1]
    assert ultimo.accion == "auditoria.verificada"
    assert '"ok":true' in ultimo.detalle
    assert ultimo.usuario_email == "titular@frubber.co"
    # El propio sello encadena: el log sigue siendo verificable.
    assert verify_chain(db_session).ok


def test_el_delegado_no_verifica_la_cadena(client, db_session):
    _entrada_admin(client, db_session)

    respuesta = client.post("/historial/verificar", follow_redirects=False)

    assert respuesta.status_code == 403
    assert not any(e.accion == "auditoria.verificada" for e in _eventos(db_session))


def test_una_manipulacion_por_sql_crudo_se_detecta_en_la_pantalla(client, db_session):
    """Aunque alguien edite la base por fuera, UI-08 lo delata."""
    _entrada_titular(client, db_session)
    _emitir_una(client, db_session)

    evento = _eventos(db_session)[0]
    db_session.execute(
        text("UPDATE auditoria SET detalle = :nuevo WHERE id = :id"),
        {"nuevo": '{"alterado":true}', "id": evento.id},
    )
    db_session.commit()

    cuerpo = client.get("/historial").text
    assert "&#10006; Rota" in cuerpo
    assert f"Ruptura en el evento #{evento.id}" in cuerpo

    # Y el sello de verificacion lo deja escrito en el log.
    respuesta = client.post("/historial/verificar", follow_redirects=False)
    assert "cadena esta rota" in _ubicacion(respuesta)
    ultimo = _eventos(db_session)[-1]
    assert ultimo.accion == "auditoria.verificada"
    assert '"ok":false' in ultimo.detalle


def test_la_api_de_verificacion_sigue_respondiendo(client, db_session):
    _entrada_titular(client, db_session)

    respuesta = client.post("/api/auditoria/verificar")

    assert respuesta.status_code == 200
    assert respuesta.json()["ok"] is True


# --------------------------------------------------------------------------- #
# Read-only y CSP
# --------------------------------------------------------------------------- #


def test_la_pantalla_no_ofrece_borrar_ni_editar(client, db_session):
    _entrada_titular(client, db_session)

    cuerpo = client.get("/historial").text

    # El unico formulario propio de la pantalla es la verificacion.
    assert 'action="/historial/verificar"' in cuerpo
    # Y ninguna ruta de la app muta la auditoria: solo se verifica.
    mutaciones = [
        (getattr(r, "path", ""), sorted(getattr(r, "methods", []) or []))
        for r in client.app.routes
        if "historial" in getattr(r, "path", "")
        and "POST" in (getattr(r, "methods", []) or [])
        and "verificar" not in getattr(r, "path", "")
    ]
    assert mutaciones == []


@pytest.mark.parametrize("ruta", ["/historial"])
def test_la_pantalla_no_lleva_scripts_inline(client, db_session, ruta):
    _entrada_titular(client, db_session)

    cuerpo = client.get(ruta).text

    assert cuerpo.count("<script") == 1
    assert 'src="/static/lm.js"' in cuerpo
    assert "onclick=" not in cuerpo
    assert "onsubmit=" not in cuerpo
    assert "<script>" not in cuerpo
    assert "BEGIN" not in cuerpo
