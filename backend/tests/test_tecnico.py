"""Pruebas del rol Tecnico (JHO-107).

El tecnico balancero deja de depender de la sociedad: entra con su propia
cuenta, ve su saldo prepago y emite cuando quiere. Lo que estas pruebas
protegen es el AISLAMIENTO:

* Solo gasta SU paquete, aunque mande el id de otro tecnico.
* Solo ve su saldo: ni el de los demas ni el dinero de la sociedad.
* Sin 403 no llega a los modulos contables.
* La cortesia la autoriza la sociedad, no el.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from conftest import crear_titular, crear_usuario_rol, iniciar_sesion
from sqlalchemy import func, select

from license_manager.models import (
    Equipment,
    Income,
    License,
    Role,
    Technician,
    User,
    WholesalePackage,
)
from license_manager.services.audit import verify_chain
from license_manager.services.directory import create_customer, create_equipment
from license_manager.services.mayorista import saldo_tecnico
from license_manager.services.users import create_user

CLAVE_TITULAR = "clave-segura-123"
CLAVE_TECNICO = "clave-tecnico-larga"
CLIENTE = "Verduleria La Huerta Central"
CODIGOS = ("A4B9C102E8F37B1A", "5D2198AF44B10C3E", "77C1E40B9920A5D1")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ubicacion(response) -> str:
    return unquote(response.headers["location"])


def _entrada_titular(client, db):
    crear_titular(db, password=CLAVE_TITULAR)
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)


def _tecnico(db, nombre: str = "Carlos Balanzas") -> Technician:
    from license_manager.services.mayorista import crear_tecnico

    tecnico = crear_tecnico(db, actor_id=None, nombre=nombre)
    db.commit()
    return tecnico


def _acceso(db, tecnico: Technician, *, email: str = "carlos@balanzas.co") -> User:
    """Crea la cuenta del tecnico directamente (sin pasar por la ruta)."""
    titular = db.execute(select(User).where(User.rol == Role.OWNER.value)).scalar_one_or_none()
    if titular is None:
        titular = crear_titular(db)
    cuenta = create_user(
        db,
        actor=titular,
        nombre=tecnico.nombre,
        email=email,
        password=CLAVE_TECNICO,
        rol=Role.TECNICO.value,
        tecnico_id=tecnico.id,
    )
    db.commit()
    return cuenta


def _entrada_tecnico(client, db, tecnico: Technician, *, email: str = "carlos@balanzas.co"):
    cuenta = _acceso(db, tecnico, email=email)
    iniciar_sesion(client, email, CLAVE_TECNICO)
    return cuenta


def _paquete(db, tecnico: Technician, tipo: str = "inicial") -> WholesalePackage:
    from license_manager.services.mayorista import registrar_paquete

    paquete, _ = registrar_paquete(
        db, actor_id=None, tecnico_id=tecnico.id, tipo=tipo
    )
    db.commit()
    return paquete


def _cliente(db, de_usuario_id: str | None = None, nombre: str = CLIENTE) -> str:
    """Verduleria de prueba. ``de_usuario_id`` la deja a nombre de esa persona.

    Hace falta porque un tecnico solo ve y toca SUS verdulerias: una sin dueno
    (actor None) es invisible para el (JHO-107).
    """
    cliente = create_customer(db, actor_id=de_usuario_id, nombre=nombre)
    db.commit()
    return cliente.id


def _emitir(client, cliente_id: str, codigo: str, **extra):
    datos = {
        "cliente_id": cliente_id,
        "codigo": codigo,
        "tipo": extra.get("tipo", "comercial"),
        "importe": extra.get("importe", "450.000"),
        "tecnico_id": extra.get("tecnico_id", ""),
        "justificacion": extra.get("justificacion", ""),
    }
    return client.post("/emitir", data=datos, follow_redirects=False)


def _equipo(db, codigo: str, cliente_id: str) -> None:
    create_equipment(db, actor_id=None, hardware_code_raw=codigo, cliente_id=cliente_id)
    db.commit()


# --------------------------------------------------------------------------- #
# 1. El Titular le da acceso desde Mayorista
# --------------------------------------------------------------------------- #


def test_el_titular_da_acceso_al_tecnico_desde_mayorista(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)

    respuesta = client.post(
        f"/mayorista/tecnicos/{tecnico.id}/acceso",
        data={"email": "carlos@balanzas.co", "password": CLAVE_TECNICO},
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    assert "Acceso creado" in _ubicacion(respuesta)

    cuenta = db_session.execute(
        select(User).where(User.email == "carlos@balanzas.co")
    ).scalar_one()
    assert cuenta.rol == Role.TECNICO.value
    assert cuenta.tecnico_id == tecnico.id
    assert cuenta.nombre_completo == tecnico.nombre
    assert verify_chain(db_session).ok

    # Y con esa cuenta se puede entrar.
    client.post("/logout", follow_redirects=False)
    iniciar_sesion(client, "carlos@balanzas.co", CLAVE_TECNICO)
    assert client.get("/").status_code == 200


def test_no_se_da_acceso_dos_veces_al_mismo_tecnico(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    _acceso(db_session, tecnico)

    respuesta = client.post(
        f"/mayorista/tecnicos/{tecnico.id}/acceso",
        data={"email": "otro@balanzas.co", "password": CLAVE_TECNICO},
        follow_redirects=False,
    )

    assert "ya tiene un acceso" in _ubicacion(respuesta)


def test_no_se_da_acceso_a_una_ficha_inactiva(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)
    tecnico.activo = False
    db_session.commit()

    respuesta = client.post(
        f"/mayorista/tecnicos/{tecnico.id}/acceso",
        data={"email": "carlos@balanzas.co", "password": CLAVE_TECNICO},
        follow_redirects=False,
    )

    assert "desactivada" in _ubicacion(respuesta)


def test_un_administrador_no_da_acceso(client, db_session):
    crear_usuario_rol(
        db_session, Role.ADMIN.value, email="admin@frubber.co", password="clave-admin-larga-1"
    )
    tecnico = _tecnico(db_session)
    iniciar_sesion(client, "admin@frubber.co", "clave-admin-larga-1")

    respuesta = client.post(
        f"/mayorista/tecnicos/{tecnico.id}/acceso",
        data={"email": "carlos@balanzas.co", "password": CLAVE_TECNICO},
        follow_redirects=False,
    )

    assert respuesta.status_code == 403
    assert db_session.execute(
        select(func.count(User.id)).where(User.email == "carlos@balanzas.co")
    ).scalar_one() == 0


def test_la_pantalla_ofrece_dar_acceso_solo_si_falta(client, db_session):
    _entrada_titular(client, db_session)
    tecnico = _tecnico(db_session)

    cuerpo = client.get("/mayorista").text
    assert "data-abrir-acceso" in cuerpo
    assert "Dar acceso" in cuerpo

    _acceso(db_session, tecnico)
    cuerpo = client.get("/mayorista").text
    assert "data-abrir-acceso" not in cuerpo
    assert "Con acceso" in cuerpo


# --------------------------------------------------------------------------- #
# 2. Emite con SU saldo
# --------------------------------------------------------------------------- #


def test_el_tecnico_emite_con_su_propio_cupo(client, db_session):
    tecnico = _tecnico(db_session)
    _paquete(db_session, tecnico, "inicial")
    cuenta = _entrada_tecnico(client, db_session, tecnico)
    cliente_id = _cliente(db_session, cuenta.id)

    respuesta = _emitir(client, cliente_id, CODIGOS[0])

    assert respuesta.status_code == 303
    licencia = db_session.execute(select(License)).scalar_one()
    assert licencia.cupo_descontado is True
    assert licencia.tecnico_id == tecnico.id
    assert saldo_tecnico(db_session, tecnico.id) == 2

    # La auditoria deja claro quien emitio: el tecnico, no la sociedad.
    from license_manager.models import AuditEvent

    evento = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "licencia.emitida")
    ).scalar_one()
    assert evento.usuario_email == "carlos@balanzas.co"
    assert evento.usuario_rol == Role.TECNICO.value


def test_el_tecnico_no_elige_tecnico_en_la_pantalla(client, db_session):
    tecnico = _tecnico(db_session)
    _paquete(db_session, tecnico, "inicial")
    cuenta = _entrada_tecnico(client, db_session, tecnico)
    _cliente(db_session, cuenta.id)  # el formulario solo se pinta si hay verdulerias

    cuerpo = client.get("/emitir").text

    assert tecnico.nombre in cuerpo
    assert "Emites a tu nombre" in cuerpo
    # No hay selector: su nombre viene fijo.
    assert 'select id="tecnico_id"' not in cuerpo
    assert 'name="tecnico_id"' not in cuerpo
    # Y no se le ofrece la cortesia: la autoriza la sociedad.
    assert "Cortesia de la sociedad" not in cuerpo


def test_no_puede_gastar_el_cupo_de_otro_tecnico(client, db_session):
    """Aunque mande el id ajeno, el descuento va a SU paquete."""
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    _paquete(db_session, propio, "inicial")
    _paquete(db_session, ajeno, "master")
    cuenta = _entrada_tecnico(client, db_session, propio)
    # La verduleria es del tecnico que emite.
    cliente_id = _cliente(db_session, cuenta.id)

    _emitir(client, cliente_id, CODIGOS[0], tecnico_id=ajeno.id)

    licencia = db_session.execute(select(License)).scalar_one()
    assert licencia.tecnico_id == propio.id
    assert saldo_tecnico(db_session, propio.id) == 2
    assert saldo_tecnico(db_session, ajeno.id) == 10, "el saldo ajeno no se toca"


def test_sin_saldo_el_tecnico_no_puede_vender(client, db_session):
    tecnico = _tecnico(db_session)
    cuenta = _entrada_tecnico(client, db_session, tecnico)
    cliente_id = _cliente(db_session, cuenta.id)

    respuesta = _emitir(client, cliente_id, CODIGOS[0])

    assert "no tiene cupos disponibles" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(License.id))).scalar_one() == 0
    assert db_session.execute(select(func.count(Income.id))).scalar_one() == 0


def test_el_tecnico_puede_dar_prueba_y_reemplazo_sin_cupo(client, db_session):
    tecnico = _tecnico(db_session)
    _paquete(db_session, tecnico, "inicial")
    cuenta = _entrada_tecnico(client, db_session, tecnico)
    cliente_id = _cliente(db_session, cuenta.id)

    _emitir(client, cliente_id, CODIGOS[0], tipo="prueba", importe="0")
    _emitir(
        client,
        cliente_id,
        CODIGOS[1],
        tipo="reemplazo",
        importe="0",
        justificacion="Cambio de placa base por dano fisico",
    )

    licencias = db_session.execute(select(License).order_by(License.emitida_at)).scalars().all()
    assert [lic.tipo_emision for lic in licencias] == ["prueba", "reemplazo"]
    assert all(lic.cupo_descontado is False for lic in licencias)
    assert saldo_tecnico(db_session, tecnico.id) == 3, "ni la prueba ni el reemplazo gastan cupo"


def test_el_tecnico_no_puede_regalar_cortesias(client, db_session):
    tecnico = _tecnico(db_session)
    _paquete(db_session, tecnico, "inicial")
    cuenta = _entrada_tecnico(client, db_session, tecnico)
    cliente_id = _cliente(db_session, cuenta.id)

    respuesta = _emitir(client, cliente_id, CODIGOS[0], tipo="cortesia", importe="0")

    assert "cortesía la autoriza la sociedad" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(License.id))).scalar_one() == 0


def test_una_cuenta_de_tecnico_sin_ficha_no_puede_emitir(client, db_session):
    """Red de seguridad: cuenta tecnico sin vincular no firma nada.

    El servicio no deja crear una asi, por eso se inserta a mano: es la base
    heredada o manipulada que la pantalla debe rechazar igual.
    """
    from license_manager.security.passwords import hash_password

    crear_titular(db_session)
    cuenta_sin_ficha = User(
        nombre_completo="Sin ficha",
        email="sinficha@balanzas.co",
        password_hash=hash_password(CLAVE_TECNICO),
        rol=Role.TECNICO.value,
        activo=True,
    )
    db_session.add(cuenta_sin_ficha)
    db_session.commit()
    iniciar_sesion(client, "sinficha@balanzas.co", CLAVE_TECNICO)
    # Su propia verduleria: lo que falta es la ficha, no el permiso sobre ella.
    cliente_id = _cliente(db_session, cuenta_sin_ficha.id)

    respuesta = _emitir(client, cliente_id, CODIGOS[0])

    assert "no está vinculada a una ficha" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(License.id))).scalar_one() == 0


def test_el_servicio_no_deja_crear_un_tecnico_sin_ficha(client, db_session):
    from license_manager.services.users import UserManagementError

    crear_titular(db_session)
    titular = db_session.execute(select(User).where(User.rol == Role.OWNER.value)).scalar_one()

    with pytest.raises(UserManagementError, match="Elige el tecnico"):
        create_user(
            db_session,
            actor=titular,
            nombre="Sin ficha",
            email="sinficha@balanzas.co",
            password=CLAVE_TECNICO,
            rol=Role.TECNICO.value,
        )


# --------------------------------------------------------------------------- #
# 3. Los modulos de dinero le quedan cerrados
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ruta",
    [
        "/ingresos",
        "/cierres",
        "/cierres/exportar",
        "/historial",
        "/liquidaciones",
        "/administradores",
    ],
)
def test_los_modulos_de_la_sociedad_dan_403(client, db_session, ruta):
    tecnico = _tecnico(db_session)
    _entrada_tecnico(client, db_session, tecnico)

    respuesta = client.get(ruta)

    assert respuesta.status_code == 403
    assert "Acceso restringido" in respuesta.text


@pytest.mark.parametrize(
    ("metodo", "ruta", "datos"),
    [
        ("post", "/ingresos", {"licencia_id": "x", "categoria": "venta", "importe": "1.000"}),
        ("post", "/historial/verificar", {}),
        ("post", "/mayorista/paquetes", {"tecnico_id": "x", "tipo": "inicial"}),
        ("post", "/mayorista/tecnicos", {"nombre": "Otro tecnico"}),
        (
            "post",
            "/liquidaciones",
            {"administrador_id": "x", "licencias": "1", "tipo": "porcentaje", "valor": "30"},
        ),
    ],
)
def test_las_acciones_de_dinero_dan_403(client, db_session, metodo, ruta, datos):
    tecnico = _tecnico(db_session)
    _entrada_tecnico(client, db_session, tecnico)

    respuesta = getattr(client, metodo)(ruta, data=datos, follow_redirects=False)

    assert respuesta.status_code == 403


def test_el_tecnico_no_ve_contenido_de_los_modulos_prohibidos(client, db_session):
    tecnico = _tecnico(db_session)
    _entrada_tecnico(client, db_session, tecnico)

    cuerpo = client.get("/cierres").text

    assert "Acceso restringido" in cuerpo
    # Ni el resumen del cierre ni el detalle de cobros aparecen.
    assert "Cierre y liquidación mensual" not in cuerpo
    assert "Detalle de cobros del mes" not in cuerpo


# --------------------------------------------------------------------------- #
# 4. Solo ve lo suyo
# --------------------------------------------------------------------------- #


def test_en_mayorista_solo_ve_su_tarjeta(client, db_session):
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    _paquete(db_session, propio, "inicial")
    _paquete(db_session, ajeno, "master")
    _entrada_tecnico(client, db_session, propio)

    cuerpo = client.get("/mayorista").text

    assert "Mi saldo de licencias" in cuerpo
    assert propio.nombre in cuerpo
    assert ajeno.nombre not in cuerpo, "no debe ver el saldo de otro tecnico"
    # Nada de formularios de compra ni de alta de tecnicos.
    assert 'action="/mayorista/paquetes"' not in cuerpo
    assert 'action="/mayorista/tecnicos"' not in cuerpo


def test_en_mayorista_ve_sus_paquetes_y_su_saldo(client, db_session):
    tecnico = _tecnico(db_session)
    _paquete(db_session, tecnico, "profesional")
    _entrada_tecnico(client, db_session, tecnico)

    cuerpo = client.get("/mayorista").text

    assert "Paquete Profesional" in cuerpo
    assert "5 usados de 5 comprados" in cuerpo or "0 usados de 5 comprados" in cuerpo


def test_el_panel_del_tecnico_no_muestra_el_dinero_de_la_sociedad(client, db_session):
    tecnico = _tecnico(db_session)
    _paquete(db_session, tecnico, "master")
    _entrada_tecnico(client, db_session, tecnico)

    cuerpo = client.get("/").text

    assert "Tus cupos disponibles" in cuerpo
    assert "10" in cuerpo
    assert "Ingresos pagados" not in cuerpo
    assert "$" not in cuerpo.split("Tus cupos")[0], "sin cifras de dinero de la sociedad"


def test_el_tecnico_ve_sus_ultimas_emisiones_sin_dinero(client, db_session):
    tecnico = _tecnico(db_session)
    _paquete(db_session, tecnico, "inicial")
    cuenta = _entrada_tecnico(client, db_session, tecnico)
    cliente_id = _cliente(db_session, cuenta.id)
    _equipo(db_session, CODIGOS[0], cliente_id)
    _emitir(client, cliente_id, CODIGOS[0])

    cuerpo = client.get("/").text

    assert CLIENTE in cuerpo
    assert "A4B9-C102-E8F3-7B1A" in cuerpo
    assert "1 cupo" in cuerpo


def test_la_navegacion_del_tecnico_no_ofrece_lo_prohibido(client, db_session):
    tecnico = _tecnico(db_session)
    _entrada_tecnico(client, db_session, tecnico)

    nav = client.get("/").text

    assert ">Mayorista<" in nav
    assert ">Ingresos<" not in nav
    assert ">Cierres mensuales<" not in nav
    assert ">Historial<" not in nav
    assert ">Liquidación<" not in nav
    assert ">Administradores<" not in nav
    assert "Técnico" in nav  # su rol en el encabezado


def test_el_tecnico_si_puede_trabajar_en_la_calle(client, db_session):
    """Registrar verdulerias y equipos es su trabajo: eso si puede."""
    tecnico = _tecnico(db_session)
    _entrada_tecnico(client, db_session, tecnico)

    assert client.get("/clientes").status_code == 200
    assert client.get("/equipos").status_code == 200

    alta = client.post(
        "/clientes", data={"nombre": "Verduleria Nueva"}, follow_redirects=False
    )
    assert alta.status_code == 303
    assert (
        client.post(
            "/equipos",
            data={"hardware_code": CODIGOS[0], "cliente_id": _cliente(db_session)},
            follow_redirects=False,
        ).status_code
        == 303
    )


# --------------------------------------------------------------------------- #
# 5. Aislamiento: cada tecnico solo ve y toca LO SUYO
# --------------------------------------------------------------------------- #


def _con_equipo(db, de_usuario_id: str, codigo: str, nombre: str = CLIENTE) -> tuple[str, str]:
    """Verduleria de esa persona con un equipo suyo. Devuelve (cliente, equipo)."""
    cliente_id = _cliente(db, de_usuario_id, nombre)
    _equipo(db, codigo, cliente_id)
    equipo = db.execute(
        select(Equipment).where(Equipment.hardware_code == codigo)
    ).scalar_one()
    return cliente_id, equipo.id


def test_el_tecnico_solo_ve_sus_verdulerias(client, db_session):
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    cuenta_propio = _entrada_tecnico(client, db_session, propio)
    cuenta_ajeno = _acceso(db_session, ajeno, email="andrea@balanzas.co")
    _cliente(db_session, cuenta_propio.id)
    ajena = create_customer(db_session, actor_id=cuenta_ajeno.id, nombre="Verduleria De Otro")
    db_session.commit()

    cuerpo = client.get("/clientes").text

    assert CLIENTE in cuerpo
    assert ajena.nombre not in cuerpo, "no debe ver la verduleria de otro tecnico"
    assert "Solo aparecen las verdulerías que registraste tú" in cuerpo


def test_el_tecnico_no_puede_desactivar_la_verduleria_de_otro(client, db_session):
    """Ni siquiera deberia saber que existe; y si lo intenta, no pasa nada."""
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    _entrada_tecnico(client, db_session, propio)
    cuenta_ajeno = _acceso(db_session, ajeno, email="andrea@balanzas.co")
    ajena = create_customer(db_session, actor_id=cuenta_ajeno.id, nombre="Verduleria De Otro")
    db_session.commit()

    respuesta = client.post(
        f"/clientes/{ajena.id}/alternar", data={"activo": "0"}, follow_redirects=False
    )

    assert "no la registraste tú" in _ubicacion(respuesta)
    db_session.refresh(ajena)
    assert ajena.activo is True, "la verduleria de otro sigue intacta"


def test_el_tecnico_solo_ve_sus_equipos(client, db_session):
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    cuenta_propio = _entrada_tecnico(client, db_session, propio)
    cuenta_ajeno = _acceso(db_session, ajeno, email="andrea@balanzas.co")
    _con_equipo(db_session, cuenta_propio.id, CODIGOS[0], "Verduleria Mia")
    _con_equipo(db_session, cuenta_ajeno.id, CODIGOS[1], "Verduleria Ajena")

    cuerpo = client.get("/equipos").text

    assert "A4B9-C102-E8F3-7B1A" in cuerpo
    assert "5D21-98AF-44B1-0C3E" not in cuerpo, "no debe ver inventario de otro tecnico"
    assert "Solo aparecen los equipos que registraste tú" in cuerpo


def test_el_tecnico_no_abre_la_ficha_de_un_equipo_ajeno(client, db_session):
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    _entrada_tecnico(client, db_session, propio)
    cuenta_ajeno = _acceso(db_session, ajeno, email="andrea@balanzas.co")
    _, equipo_ajeno = _con_equipo(db_session, cuenta_ajeno.id, CODIGOS[1])

    respuesta = client.get(f"/equipos/{equipo_ajeno}", follow_redirects=False)

    assert "no lo registraste tú" in _ubicacion(respuesta)


def test_el_tecnico_no_emite_para_una_verduleria_ajena(client, db_session):
    """Aunque mande el id a mano, el servidor lo rechaza."""
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    _paquete(db_session, propio, "inicial")
    _entrada_tecnico(client, db_session, propio)
    cuenta_ajeno = _acceso(db_session, ajeno, email="andrea@balanzas.co")
    ajena = create_customer(db_session, actor_id=cuenta_ajeno.id, nombre="Verduleria De Otro")
    db_session.commit()

    respuesta = _emitir(client, ajena.id, CODIGOS[0])

    assert "no la registraste tú" in _ubicacion(respuesta)
    assert db_session.execute(select(func.count(License.id))).scalar_one() == 0
    assert saldo_tecnico(db_session, propio.id) == 3, "tampoco se le descuenta el cupo"


def test_el_tecnico_no_ve_el_dinero_de_otros_en_mayorista(client, db_session):
    """El KPI de Mayorista es SU dinero: nunca el total de la sociedad."""
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    _paquete(db_session, propio, "inicial")       # $960.000
    _paquete(db_session, ajeno, "master")         # $2.600.000
    _entrada_tecnico(client, db_session, propio)

    cuerpo = client.get("/mayorista").text

    assert "Lo que has pagado" in cuerpo
    assert "$960.000" in cuerpo
    assert "$2.600.000" not in cuerpo, "el dinero de otro tecnico no se muestra"
    # Y nada que suene al total de la sociedad.
    assert "Recaudado mayorista" not in cuerpo
    assert "Técnicos activos" not in cuerpo


def test_la_sociedad_sigue_viendo_todo(client, db_session):
    """Control: el Titular no queda limitado por el aislamiento."""
    propio = _tecnico(db_session, "Carlos Balanzas")
    ajeno = _tecnico(db_session, "Andrea Restrepo")
    cuenta_propio = _acceso(db_session, propio, email="carlos@balanzas.co")
    cuenta_ajeno = _acceso(db_session, ajeno, email="andrea@balanzas.co")
    _con_equipo(db_session, cuenta_propio.id, CODIGOS[0], "Verduleria Mia")
    _, equipo_ajeno = _con_equipo(db_session, cuenta_ajeno.id, CODIGOS[1], "Verduleria Ajena")

    # _acceso ya creo al titular (con la clave de las pruebas): solo entra.
    iniciar_sesion(client, "titular@frubber.co", CLAVE_TITULAR)

    clientes = client.get("/clientes").text
    equipos = client.get("/equipos").text

    assert "Verduleria Mia" in clientes
    assert "Verduleria Ajena" in clientes
    assert "5D21-98AF-44B1-0C3E" in equipos
    assert "A4B9-C102-E8F3-7B1A" in equipos
    assert client.get(f"/equipos/{equipo_ajeno}").status_code == 200


# --------------------------------------------------------------------------- #
# 6. La sociedad sigue igual
# --------------------------------------------------------------------------- #


def test_el_titular_sigue_viendo_todo(client, db_session):
    tecnico = _tecnico(db_session)
    _paquete(db_session, tecnico, "inicial")
    _entrada_titular(client, db_session)

    for ruta in ("/", "/ingresos", "/cierres", "/historial", "/mayorista"):
        assert client.get(ruta).status_code == 200, ruta

    # Y en Mayorista ve el directorio completo y puede registrar compras.
    cuerpo = client.get("/mayorista").text
    assert tecnico.nombre in cuerpo
    assert 'action="/mayorista/paquetes"' in cuerpo


def test_el_rol_tecnico_aparece_en_administradores(client, db_session):
    tecnico = _tecnico(db_session)
    _entrada_titular(client, db_session)  # el titular primero: _acceso lo reutiliza
    _acceso(db_session, tecnico)

    cuerpo = client.get("/administradores").text

    assert "carlos@balanzas.co" in cuerpo
    assert "Técnico" in cuerpo
