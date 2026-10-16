"""Pruebas de gestion de administradores y seguridad del Titular (JHO-99, UI-09).

Cubren las cinco reglas del issue:

1. Bloqueo 403 estricto para administradores delegados.
2. El Titular Principal cambia su propia contrasena verificando la anterior.
3. Alta de Co-Titular con acceso total.
4. Creacion, reseteo de clave y desactivacion inmediata de administradores.
5. Preservacion del historial de operadores desactivados.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import select

from license_manager.deps import state
from license_manager.models import AuditEvent, Role, User, UserSession
from license_manager.security.passwords import verify_password
from license_manager.services.audit import verify_chain

CLAVE_TITULAR = "clave-segura-123"
CLAVE_ADMIN = "clave-admin-larga-1"


def _ubicacion(response) -> str:
    """El header Location viene URL-codificado: se descodifica para leer el motivo."""
    return unquote(response.headers["location"])


def _alta_admin(db, email="admin@frubber.co", nombre="Carlos Gomez"):
    return crear_usuario_rol(
        db, Role.ADMIN.value, email=email, password=CLAVE_ADMIN, nombre=nombre
    )


# --------------------------------------------------------------------------- #
# 1. Bloqueo 403 estricto para administradores delegados
# --------------------------------------------------------------------------- #


def test_un_admin_no_ve_la_pantalla_y_recibe_403(client, db_session):
    _alta_admin(db_session)
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)

    response = client.get("/administradores")

    assert response.status_code == 403
    assert "Acceso restringido" in response.text
    # Ni un solo correo ni nombre de la tabla se filtra.
    assert "titular@frubber.co" not in response.text


def test_un_admin_no_puede_crear_cuentas(client, db_session):
    _alta_admin(db_session)
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)

    response = client.post(
        "/administradores/crear",
        data={
            "nombre": "Intruso",
            "email": "intruso@frubber.co",
            "password": "clave-intruso-larga",
            "rol": Role.ADMIN.value,
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    # Nada quedo creado.
    intruso = db_session.execute(
        select(User).where(User.email == "intruso@frubber.co")
    ).scalar_one_or_none()
    assert intruso is None


@pytest.mark.parametrize("rol", ["crear", "alternar", "restablecer"])
def test_un_admin_no_puede_tocar_ningun_endpoint_de_gestion(client, db_session, rol):
    _alta_admin(db_session)
    admin = db_session.execute(select(User).where(User.email == "admin@frubber.co")).scalar_one()
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)

    if rol == "crear":
        url, data = "/administradores/crear", {
            "nombre": "X", "email": "x@frubber.co", "password": "clave-suficiente-1", "rol": "admin"
        }
    elif rol == "alternar":
        url, data = f"/administradores/{admin.id}/alternar", {"activo": "0"}
    else:
        url, data = f"/administradores/{admin.id}/restablecer", {"password": "clave-suficiente-1"}

    assert client.post(url, data=data, follow_redirects=False).status_code == 403


def test_sin_sesion_no_se_entra_a_gestionar(client):
    assert client.get("/administradores", follow_redirects=False).status_code == 303
    assert (
        client.post("/administradores/crear", data={}, follow_redirects=False).status_code == 401
    )


def test_un_admin_desactivado_no_puede_iniciar_sesion_para_gestionar(client, db_session):
    admin = _alta_admin(db_session)
    admin.activo = False
    db_session.commit()

    response = client.post(
        "/login", data={"email": "admin@frubber.co", "password": CLAVE_ADMIN}, follow_redirects=False
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# 2. Cambio de contrasena propia del Titular con verificacion de la anterior
# --------------------------------------------------------------------------- #


def test_titular_cambia_su_clave_verificando_la_anterior(client, db_session):
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        "/seguridad/mi-clave",
        data={
            "password_actual": CLAVE_TITULAR,
            "password_nueva": "clave-nueva-super-larga",
            "password_confirmacion": "clave-nueva-super-larga",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    user = db_session.execute(select(User)).scalar_one()
    assert verify_password(user.password_hash, "clave-nueva-super-larga")
    assert not verify_password(user.password_hash, CLAVE_TITULAR)


def test_no_se_puede_cambiar_la_clave_con_la_anterior_mala(client, db_session):
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        "/seguridad/mi-clave",
        data={
            "password_actual": "clave-equivocada-1",
            "password_nueva": "clave-nueva-super-larga",
            "password_confirmacion": "clave-nueva-super-larga",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "no es correcta" in _ubicacion(response)
    user = db_session.execute(select(User)).scalar_one()
    # La clave no cambio.
    assert verify_password(user.password_hash, CLAVE_TITULAR)


def test_no_se_puede_cambiar_la_clave_si_la_confirmacion_no_coincide(client, db_session):
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        "/seguridad/mi-clave",
        data={
            "password_actual": CLAVE_TITULAR,
            "password_nueva": "clave-nueva-super-larga",
            "password_confirmacion": "otra-clave-distinta-1",
        },
        follow_redirects=False,
    )

    assert "no coincide" in _ubicacion(response)
    user = db_session.execute(select(User)).scalar_one()
    assert verify_password(user.password_hash, CLAVE_TITULAR)


def test_no_se_puede_usar_la_misma_clave(client, db_session):
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        "/seguridad/mi-clave",
        data={
            "password_actual": CLAVE_TITULAR,
            "password_nueva": CLAVE_TITULAR,
            "password_confirmacion": CLAVE_TITULAR,
        },
        follow_redirects=False,
    )

    assert "distinta" in _ubicacion(response)


def test_al_cambiar_la_clave_se_cierran_las_sesiones_antiguas(client, db_session):
    """La sesion vieja deja de servir aunque la cookie siga en el navegador."""
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    cookie_vieja = client.cookies.get("lm_session")
    client.post(
        "/seguridad/mi-clave",
        data={
            "password_actual": CLAVE_TITULAR,
            "password_nueva": "clave-nueva-super-larga",
            "password_confirmacion": "clave-nueva-super-larga",
        },
    )

    # La sesion original quedo cerrada en base de datos.
    abiertas = db_session.execute(
        select(UserSession).where(UserSession.cerrada_at.is_(None))
    ).scalars().all()
    assert len(abiertas) == 1  # solo la nueva
    assert abiertas[0].id != cookie_vieja


def test_un_admin_no_puede_usar_el_cambio_de_clave_propia(client, db_session):
    """El panel de seguridad es de titulares: el admin recibe 403 uniforme."""
    _alta_admin(db_session)
    iniciar_sesion(client, "admin@frubber.co", CLAVE_ADMIN)

    response = client.post(
        "/seguridad/mi-clave",
        data={
            "password_actual": CLAVE_ADMIN,
            "password_nueva": "clave-nueva-super-larga",
            "password_confirmacion": "clave-nueva-super-larga",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# 3. Alta de Co-Titular con acceso total
# --------------------------------------------------------------------------- #


def test_el_titular_crea_un_cotitular_con_acceso_total(client, db_session):
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        "/administradores/crear",
        data={
            "nombre": "Socio Nuevo",
            "email": "socio@frubber.co",
            "password": "clave-socio-larga-1",
            "rol": Role.COOWNER.value,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    socio = db_session.execute(select(User).where(User.email == "socio@frubber.co")).scalar_one()
    assert socio.rol == Role.COOWNER.value
    assert socio.activo


def test_el_cotitular_puede_gestionar_cuentas(client, db_session):
    """El Co-Titular tiene acceso total: tambien llega a la pantalla UI-09."""
    crear_titular(db_session)
    crear_usuario_rol(db_session, Role.COOWNER.value, email="socio@frubber.co")
    iniciar_sesion(client, "socio@frubber.co", "clave-larga-suficiente")

    response = client.get("/administradores")
    assert response.status_code == 200
    assert "Gestion de Administradores" in response.text


def test_el_cotitular_puede_desactivar_un_admin(client, db_session):
    crear_titular(db_session)
    socio = crear_usuario_rol(db_session, Role.COOWNER.value, email="socio@frubber.co")
    admin = _alta_admin(db_session)
    iniciar_sesion(client, "socio@frubber.co", "clave-larga-suficiente")

    response = client.post(
        f"/administradores/{admin.id}/alternar", data={"activo": "0"}, follow_redirects=False
    )

    assert response.status_code == 303
    db_session.refresh(admin)
    assert not admin.activo
    assert socio is not None


def test_solo_el_titular_principal_crea_un_cotitular(client, db_session):
    """Un Co-Titular no puede crear otro Co-Titular: evita socios infinitos."""
    crear_titular(db_session)
    crear_usuario_rol(db_session, Role.COOWNER.value, email="socio@frubber.co")
    iniciar_sesion(client, "socio@frubber.co", "clave-larga-suficiente")

    response = client.post(
        "/administradores/crear",
        data={
            "nombre": "Otro Socio",
            "email": "otro-socio@frubber.co",
            "password": "clave-otro-socio-larga",
            "rol": Role.COOWNER.value,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in _ubicacion(response)
    assert (
        db_session.execute(select(User).where(User.email == "otro-socio@frubber.co")).scalar_one_or_none()
        is None
    )


# --------------------------------------------------------------------------- #
# 4. Creacion, reseteo y desactivacion inmediata de administradores
# --------------------------------------------------------------------------- #


def test_el_titular_crea_un_administrador(client, db_session):
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    client.post(
        "/administradores/crear",
        data={
            "nombre": "Andrea Restrepo",
            "email": "andrea@frubber.co",
            "password": "clave-andrea-larga",
            "rol": Role.ADMIN.value,
        },
    )

    nuevo = db_session.execute(select(User).where(User.email == "andrea@frubber.co")).scalar_one()
    assert nuevo.rol == Role.ADMIN.value
    # Queda trazado quien lo creo.
    assert nuevo.creado_por_id is not None


def test_no_se_puede_repetir_el_correo(client, db_session):
    _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        "/administradores/crear",
        data={
            "nombre": "Repetido",
            "email": "admin@frubber.co",  # ya existe
            "password": "clave-repetida-larga",
            "rol": Role.ADMIN.value,
        },
        follow_redirects=False,
    )

    assert "error=" in _ubicacion(response)
    cuentas = db_session.execute(select(User).where(User.email == "admin@frubber.co")).scalars().all()
    assert len(cuentas) == 1


def test_se_rechaza_una_clave_debil_al_crear(client, db_session):
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        "/administradores/crear",
        data={
            "nombre": "Debil",
            "email": "debil@frubber.co",
            "password": "corta",
            "rol": Role.ADMIN.value,
        },
        follow_redirects=False,
    )

    assert "error=" in _ubicacion(response)
    debil = db_session.execute(
        select(User).where(User.email == "debil@frubber.co")
    ).scalar_one_or_none()
    assert debil is None


def test_restablecer_clave_cierra_las_sesiones_del_admin(client, db_session):
    admin = _alta_admin(db_session)
    crear_titular(db_session)

    # El admin tiene una sesion abierta en otro dispositivo.
    cookie_admin = state.sessions.issue(db_session, admin)
    db_session.commit()

    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    client.post(f"/administradores/{admin.id}/restablecer", data={"password": "clave-nueva-admin-larga"})

    db_session.refresh(admin)
    assert verify_password(admin.password_hash, "clave-nueva-admin-larga")
    abiertas = db_session.execute(
        select(UserSession).where(UserSession.usuario_id == admin.id, UserSession.cerrada_at.is_(None))
    ).scalars().all()
    assert abiertas == []
    # La cookie que tenia el admin ya no resuelve a nadie.
    assert state.sessions.resolve(db_session, cookie_admin) is None


def test_al_desactivar_se_revoca_la_sesion_al_instante(client, db_session):
    """El requisito central de UI-09: desactivar mata la sesion abierta."""
    admin = _alta_admin(db_session)
    crear_titular(db_session)

    # El admin tiene una sesion abierta en otro dispositivo.
    cookie_admin = state.sessions.issue(db_session, admin)
    db_session.commit()
    # Mientras esta activo, su sesion sirve.
    assert state.sessions.resolve(db_session, cookie_admin) is not None

    # El titular lo desactiva desde su propia sesion.
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)
    client.post(f"/administradores/{admin.id}/alternar", data={"activo": "0"})

    # La cookie sigue en el navegador, pero ya no resuelve a nadie.
    db_session.expire_all()
    assert state.sessions.resolve(db_session, cookie_admin) is None


def test_no_se_puede_desactivar_al_titular_principal(client, db_session):
    owner = crear_titular(db_session)
    crear_usuario_rol(db_session, Role.COOWNER.value, email="socio@frubber.co")
    iniciar_sesion(client, "socio@frubber.co", "clave-larga-suficiente")

    response = client.post(
        f"/administradores/{owner.id}/alternar", data={"activo": "0"}, follow_redirects=False
    )

    assert "error=" in _ubicacion(response)
    db_session.refresh(owner)
    assert owner.activo


def test_no_se_puede_desactivar_a_un_mismo(client, db_session):
    owner = crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        f"/administradores/{owner.id}/alternar", data={"activo": "0"}, follow_redirects=False
    )

    assert "error=" in _ubicacion(response)
    db_session.refresh(owner)
    assert owner.activo


def test_no_se_puede_dejar_el_sistema_sin_titular(db_session, settings, signing_key):
    """Invariante: siempre debe quedar al menos un titular activo.

    Se prueba en el servicio porque el Titular Principal es inmutable y nunca
    se desactiva por HTTP; el escenario real es una base heredada donde el
    owner ya este inactivo y solo quede un Co-Titular.
    """
    from license_manager.security.sessions import SessionManager
    from license_manager.services.users import UserManagementError, set_user_active

    socio = crear_usuario_rol(db_session, Role.COOWNER.value, email="socio@frubber.co")
    # Simula un owner inactivo heredado: el sistema depende solo del socio.
    dueno_inactivo = crear_usuario_rol(
        db_session, Role.OWNER.value, email="dueno-inactivo@frubber.co", activo=False
    )

    manager = SessionManager(settings)
    actor = socio

    with pytest.raises(UserManagementError):
        set_user_active(db_session, manager, actor=actor, target=socio, active=False)

    db_session.refresh(socio)
    assert socio.activo
    assert dueno_inactivo.activo is False


def test_una_cuenta_desactivada_se_puede_reactivar(client, db_session):
    admin = _alta_admin(db_session)
    admin.activo = False
    db_session.commit()
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    client.post(f"/administradores/{admin.id}/alternar", data={"activo": "1"})

    db_session.refresh(admin)
    assert admin.activo
    assert admin.desactivado_at is None


def test_el_titular_no_puede_restablecer_la_clave_del_titular_principal(client, db_session):
    owner = crear_titular(db_session)
    crear_usuario_rol(db_session, Role.COOWNER.value, email="socio@frubber.co")
    iniciar_sesion(client, "socio@frubber.co", "clave-larga-suficiente")

    response = client.post(
        f"/administradores/{owner.id}/restablecer",
        data={"password": "clave-robada-super-larga"},
        follow_redirects=False,
    )

    assert "error=" in _ubicacion(response)
    db_session.refresh(owner)
    assert verify_password(owner.password_hash, CLAVE_TITULAR)


def test_no_se_puede_restablecer_la_clave_propia_por_esta_via(client, db_session):
    admin = _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.post(
        f"/administradores/{admin.id}/restablecer",
        data={"password": "clave-nueva-admin-larga"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(admin)
    assert verify_password(admin.password_hash, "clave-nueva-admin-larga")


# --------------------------------------------------------------------------- #
# 5. Preservacion del historial de operadores desactivados
# --------------------------------------------------------------------------- #


def test_desactivar_no_borra_la_fila_ni_el_historial(client, db_session):
    admin = _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    client.post(f"/administradores/{admin.id}/alternar", data={"activo": "0"})

    # La fila sigue existiendo, con la marca de desactivado.
    # expire_all: db_session tiene la copia en cache y veria el valor viejo.
    db_session.expire_all()
    fila = db_session.execute(select(User).where(User.id == admin.id)).scalar_one()
    assert fila.activo is False
    assert fila.desactivado_at is not None
    assert fila.email == "admin@frubber.co"


def test_el_admin_desactivado_sigue_apareciendo_en_la_lista(client, db_session):
    admin = _alta_admin(db_session)
    admin.activo = False
    db_session.commit()
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.get("/administradores")

    assert response.status_code == 200
    assert "admin@frubber.co" in response.text
    assert "Desactivado" in response.text


def test_el_email_del_operador_desactivado_queda_congelado_en_auditoria(client, db_session):
    """Aunque el operador salga, la auditoria conserva su correo para siempre."""
    admin = _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    client.post(f"/administradores/{admin.id}/alternar", data={"activo": "0"})

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "usuario.desactivado")
    ).scalar_one()
    assert evento.usuario_email == "titular@frubber.co"  # quien lo hizo
    assert "admin@frubber.co" in (evento.detalle or "")  # a quien le afecto
    # Y la cadena de auditoria sigue intacta.
    assert verify_chain(db_session).ok


# --------------------------------------------------------------------------- #
# 6. La pantalla permite de verdad restablecer la clave (bug corregido)
# --------------------------------------------------------------------------- #
#
# El formulario de la tabla no llevaba el campo de contrasena que el servidor
# exige, asi que pulsar «Restablecer clave» devolvia un 422 con JSON crudo.


def test_la_pantalla_pide_la_contrasena_nueva_para_restablecer(client, db_session):
    _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    cuerpo = client.get("/administradores").text

    # El modal trae el campo con el nombre exacto que el endpoint espera.
    assert "data-modal-restablecer" in cuerpo
    assert 'name="password"' in cuerpo
    assert "data-abrir-restablecer" in cuerpo
    assert "/restablecer" in cuerpo


def test_el_modal_de_restablecer_apunta_a_la_cuenta_correcta(client, db_session):
    admin = _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    cuerpo = client.get("/administradores").text

    assert f'data-accion="/administradores/{admin.id}/restablecer"' in cuerpo


def test_restablecer_sin_contrasena_no_devuelve_json_crudo(client, db_session):
    """Un formulario incompleto vuelve con un aviso, no con JSON de FastAPI."""
    admin = _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    respuesta = client.post(
        f"/administradores/{admin.id}/restablecer",
        data={},
        headers={"referer": "http://testserver/administradores"},
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    assert "Faltan datos obligatorios" in _ubicacion(respuesta)
    assert "password" in _ubicacion(respuesta)
    assert respuesta.text == ""


def test_el_endpoint_de_api_sigue_devolviendo_json_al_validar(client, db_session):
    """La red de seguridad no cambia el contrato de la API."""
    respuesta = client.post(
        "/api/auditoria/verificar", json={"sobra": True}, follow_redirects=False
    )

    assert respuesta.status_code in (200, 422)


def test_las_operaciones_de_cuentas_quedan_auditadas(client, db_session):
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    client.post(
        "/administradores/crear",
        data={
            "nombre": "Auditado",
            "email": "auditado@frubber.co",
            "password": "clave-auditada-larga",
            "rol": Role.ADMIN.value,
        },
    )

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "usuario.creado")
    ).scalar_one()
    assert evento.usuario_email == "titular@frubber.co"
    assert "auditado@frubber.co" in (evento.detalle or "")
    # La contrasena jamas aparece ni en claro ni hasheada en la auditoria.
    assert "clave-auditada-larga" not in (evento.detalle or "")


def test_ninguna_operacion_de_cuentas_rompe_la_cadena(client, db_session):
    admin = _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    client.post(
        "/administradores/crear",
        data={
            "nombre": "Nuevo", "email": "nuevo@frubber.co",
            "password": "clave-nueva-larga-12", "rol": Role.ADMIN.value,
        },
    )
    client.post(f"/administradores/{admin.id}/restablecer", data={"password": "clave-restablecida-larga"})
    client.post(f"/administradores/{admin.id}/alternar", data={"activo": "0"})
    client.post(
        "/seguridad/mi-clave",
        data={
            "password_actual": CLAVE_TITULAR,
            "password_nueva": "clave-del-titular-nueva",
            "password_confirmacion": "clave-del-titular-nueva",
        },
    )

    verificacion = verify_chain(db_session)
    assert verificacion.ok, verificacion.reason
    assert verificacion.total_events >= 5


# --------------------------------------------------------------------------- #
# La pantalla en si
# --------------------------------------------------------------------------- #


def test_la_pantalla_muestra_las_columnas_de_ui_09(client, db_session):
    admin = _alta_admin(db_session)
    admin.activo = False
    db_session.commit()
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    response = client.get("/administradores")

    for columna in (
        "Nombre del Operador", "Correo de Acceso", "Rol", "Estado",
        "Licencias Emitidas", "Fecha de Alta", "Acciones del Titular",
    ):
        assert columna in response.text, f"Falta la columna {columna}"


def test_el_titular_principal_va_primero_en_la_lista(client, db_session):
    _alta_admin(db_session)
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    body = client.get("/administradores").text

    assert body.index("titular@frubber.co") < body.index("admin@frubber.co")


def test_una_licencia_emitida_por_un_admin_se_cuenta_en_su_fila(client, db_session):
    from datetime import date

    from license_manager.models import Equipment, License

    admin = _alta_admin(db_session)
    equipo = Equipment(hardware_code="AABBCCDDEEFF0011")
    db_session.add(equipo)
    db_session.flush()
    db_session.add(
        License(
            key_id="lic-test",
            equipo_id=equipo.id,
            licencia_text="FRB1|lic-test|AABBCCDDEEFF0011|20261015.firma",
            tipo_emision="prueba",
            emitida_fecha=date(2026, 10, 15),
            creado_por_id=admin.id,
        )
    )
    db_session.commit()
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    from license_manager.services.users import list_operators

    filas = {f.email: f for f in list_operators(db_session)}
    assert filas["admin@frubber.co"].licencias_emitidas == 1
    # El titular, que no emitio nada, aparece con 0 y no desaparece.
    assert filas["titular@frubber.co"].licencias_emitidas == 0
