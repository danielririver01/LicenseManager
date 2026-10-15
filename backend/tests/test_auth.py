"""Pruebas de autenticacion, roles y sesiones (JHO-98 / JHO-99)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from license_manager.models import AuditEvent, Role, User, UserSession
from license_manager.security.passwords import hash_password, verify_password
from license_manager.services.audit import assert_can_manage_users, verify_chain

TITULAR = {
    "nombre": "Jhoan Rivera",
    "email": "jhoan@frubber.co",
    "password": "clave-muy-segura-1",
    "password_confirm": "clave-muy-segura-1",
}


# --------------------------------------------------------------------------- #
# Setup inicial del Titular Principal
# --------------------------------------------------------------------------- #


def test_primer_inicio_redirige_a_configuracion(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/configurar"


def test_se_puede_crear_el_primer_titular(client):
    response = client.post("/configurar", data=TITULAR, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "lm_session" in response.cookies


def test_el_titular_creado_es_owner(client, db_session):
    client.post("/configurar", data=TITULAR)

    user = db_session.execute(select(User)).scalar_one()
    assert user.rol == Role.OWNER.value
    assert user.activo
    # El hash nunca guarda la contrasena en claro.
    assert user.password_hash != TITULAR["password"]
    assert user.password_hash.startswith("$argon2")


def test_no_se_puede_configurar_dos_veces(client):
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()

    repetido = client.post("/configurar", data=TITULAR, follow_redirects=False)
    assert repetido.status_code == 403


def test_configuracion_rechaza_contrasenas_cortas(client):
    debil = {**TITULAR, "password": "corta", "password_confirm": "corta"}
    response = client.post("/configurar", data=debil, follow_redirects=False)

    assert response.status_code == 400
    assert "10 caracteres" in response.text


def test_configuracion_rechaza_contrasenas_que_no_coinciden(client):
    distinto = {**TITULAR, "password_confirm": "otra-clave-12345"}
    response = client.post("/configurar", data=distinto, follow_redirects=False)

    assert response.status_code == 400
    assert "no coinciden" in response.text


# --------------------------------------------------------------------------- #
# Inicio de sesion
# --------------------------------------------------------------------------- #


def test_login_correcto_abre_el_panel(client):
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()

    response = client.post(
        "/login", data={"email": TITULAR["email"], "password": TITULAR["password"]}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "lm_session" in response.cookies


def test_login_con_contrasena_mala_es_rechazado(client):
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()

    response = client.post(
        "/login", data={"email": TITULAR["email"], "password": "contrasena-mala"}, follow_redirects=False
    )

    assert response.status_code == 401
    assert "incorrectos" in response.text


def test_login_con_correo_inexistente_no_revela_que_la_cuenta_no_existe(client):
    """La respuesta debe ser identica a la de una contrasena mala."""
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()

    response = client.post(
        "/login", data={"email": "nadie@frubber.co", "password": "cualquier-cosa"}, follow_redirects=False
    )

    assert response.status_code == 401
    assert "incorrectos" in response.text


def test_sin_sesion_no_se_entra_al_panel(client):
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_logout_cerra_la_sesion(client):
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()
    client.post("/login", data={"email": TITULAR["email"], "password": TITULAR["password"]})

    client.post("/logout", follow_redirects=False)
    response = client.get("/", follow_redirects=False)

    assert response.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# Cuentas desactivadas
# --------------------------------------------------------------------------- #


def test_una_cuenta_desactivada_no_puede_iniciar_sesion(client, db_session):
    client.post("/configurar", data=TITULAR)
    user = db_session.execute(select(User)).scalar_one()
    user.activo = False
    db_session.commit()
    client.cookies.clear()

    response = client.post(
        "/login", data={"email": TITULAR["email"], "password": TITULAR["password"]}, follow_redirects=False
    )

    assert response.status_code == 403
    assert "desactivada" in response.text


def test_desactivar_una_cuenta_deja_sin_efecto_sus_sesiones_abiertas(client, db_session):
    """JHO-99: desactivar debe cortar el acceso de inmediato, no al expirar."""
    client.post("/configurar", data=TITULAR)
    assert client.get("/", follow_redirects=False).status_code == 200

    user = db_session.execute(select(User)).scalar_one()
    user.activo = False
    db_session.commit()

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_al_desactivar_quedan_sesiones_abiertas_registradas(client, db_session):
    client.post("/configurar", data=TITULAR)

    abiertas = db_session.execute(select(UserSession).where(UserSession.cerrada_at.is_(None))).scalars().all()
    assert len(abiertas) == 1


# --------------------------------------------------------------------------- #
# Politica de contrasenas
# --------------------------------------------------------------------------- #


def test_el_hash_es_verificable_sin_guardar_la_clave():
    hashed = hash_password("otra-clave-segura-99")
    assert verify_password(hashed, "otra-clave-segura-99")
    assert not verify_password(hashed, "otra-clave-segura-98")


def test_un_hash_corrompido_no_explota():
    assert not verify_password("no-es-un-hash", "cualquiera")
    assert not verify_password("", "cualquiera")


# --------------------------------------------------------------------------- #
# Permisos por rol (UI-09)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rol,puede_gestionar",
    [
        (Role.OWNER.value, True),
        (Role.COOWNER.value, True),
        (Role.ADMIN.value, False),
    ],
)
def test_solo_los_titulares_gestionan_cuentas(db_session, rol, puede_gestionar):
    from license_manager.models import utcnow

    user = User(
        nombre_completo="Persona",
        email=f"{rol}@frubber.co",
        password_hash=hash_password("clave-larga-suficiente"),
        rol=rol,
        activo=True,
        creado_at=utcnow(),
    )

    if puede_gestionar:
        assert_can_manage_users(user)  # no debe lanzar
    else:
        with pytest.raises(PermissionError):
            assert_can_manage_users(user)


# --------------------------------------------------------------------------- #
# Auditoria de autenticacion
# --------------------------------------------------------------------------- #


def test_el_inicio_de_sesion_exitoso_queda_auditado(client, db_session):
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()
    client.post("/login", data={"email": TITULAR["email"], "password": TITULAR["password"]})

    acciones = [e.accion for e in db_session.execute(select(AuditEvent)).scalars().all()]
    assert "login.exitoso" in acciones


def test_el_inicio_de_sesion_fallido_queda_auditado(client, db_session):
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()
    client.post("/login", data={"email": TITULAR["email"], "password": "mala"})

    eventos = (
        db_session.execute(select(AuditEvent).where(AuditEvent.accion == "login.fallido")).scalars().all()
    )
    assert len(eventos) == 1
    # La contrasena intentada jamas se guarda.
    assert "mala" not in (eventos[0].detalle or "")


def test_los_eventos_de_autenticacion_no_rompen_la_cadena(client, db_session):
    client.post("/configurar", data=TITULAR)
    client.cookies.clear()
    client.post("/login", data={"email": TITULAR["email"], "password": TITULAR["password"]})
    client.post("/logout")

    result = verify_chain(db_session)
    assert result.ok, result.reason
    assert result.total_events >= 3
