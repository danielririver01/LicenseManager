"""Pruebas integrales de punta a punta (JHO-106).

A diferencia de las suites por modulo, aqui se recorre el negocio COMPLETO por
HTTP, como lo haria el Titular en el servidor: primer arranque, directorio,
emision firmada, paquetes mayoristas, cobros, cierre contable, liquidacion de
utilidades, auditoria y respaldo.

Lo que se comprueba al final de cada recorrido:

* La licencia que sale del sistema se verifica con el verificador de referencia
  (el mismo algoritmo que corre Frubber POS con su llave publica embebida).
* Las cifras cuadran entre modulos: el cierre es la suma de lo cobrado, la
  liquidacion no infla los ingresos y un cupo prepago no se cuenta dos veces.
* La cadena de auditoria queda integra y cubre cada paso.
* El respaldo de la base se puede restaurar y conserva la cadena.
* Un rechazo a mitad de camino no deja basura en ninguna tabla.
"""

from __future__ import annotations

from urllib.parse import unquote

from sqlalchemy import func, select

from license_manager.crypto.frb1 import parse_license, verify_license
from license_manager.deps import state
from license_manager.models import (
    AuditEvent,
    Customer,
    Equipment,
    Income,
    License,
    Role,
    Settlement,
    Technician,
    User,
    WholesalePackage,
)
from license_manager.services.audit import verify_chain
from license_manager.services.cierres import resumen_cierre
from license_manager.services.licencias import PRECIO_VENTA_COP
from license_manager.services.mayorista import saldo_tecnico
from license_manager.services.periodos import mes_actual
from license_manager.tools import backup

CLAVE_TITULAR = "clave-segura-123"
CLAVE_DELEGADO = "clave-admin-larga-1"
CLIENTE = "Verduleria La Huerta Central"
CODIGOS = (
    "A4B9C102E8F37B1A",
    "5D2198AF44B10C3E",
    "77C1E40B9920A5D1",
    "F110B88201CA77E4",
)


# --------------------------------------------------------------------------- #
# Recorrido
# --------------------------------------------------------------------------- #


def _primer_arranque(client, db) -> None:
    """Crea al Titular Principal desde el asistente y entra con el."""
    client.post(
        "/configurar",
        data={
            "nombre": "Daniel Rivera",
            "email": "daniel@frubber.co",
            "password": CLAVE_TITULAR,
            "password_confirm": CLAVE_TITULAR,
        },
        follow_redirects=False,
    )
    client.post(
        "/login",
        data={"email": "daniel@frubber.co", "password": CLAVE_TITULAR},
        follow_redirects=False,
    )


def _cliente(client, db) -> Customer:
    client.post(
        "/clientes",
        data={"nombre": CLIENTE, "propietario": "Marta", "ciudad": "Medellin"},
        follow_redirects=False,
    )
    return db.execute(select(Customer).where(Customer.nombre == CLIENTE)).scalar_one()


def _equipo(client, db, codigo: str, cliente_id: str) -> Equipment:
    client.post(
        "/equipos",
        data={"hardware_code": codigo, "cliente_id": cliente_id},
        follow_redirects=False,
    )
    return db.execute(
        select(Equipment).where(Equipment.hardware_code == codigo)
    ).scalar_one()


def _emitir(
    client,
    cliente_id: str,
    codigo: str,
    *,
    tipo: str = "comercial",
    importe: str = "450.000",
    tecnico_id: str = "",
    justificacion: str = "",
):
    return client.post(
        "/emitir",
        data={
            "cliente_id": cliente_id,
            "codigo": codigo,
            "tipo": tipo,
            "importe": importe,
            "tecnico_id": tecnico_id,
            "justificacion": justificacion,
        },
        follow_redirects=False,
    )


def _ultima_licencia(db) -> License:
    return db.execute(select(License).order_by(License.emitida_at.desc())).scalars().first()


def _contar(db, modelo) -> int:
    return int(db.execute(select(func.count(modelo.id))).scalar_one())


def _acciones(db) -> list[str]:
    return list(db.execute(select(AuditEvent.accion).order_by(AuditEvent.id)).scalars())


# --------------------------------------------------------------------------- #
# 1. Camino dorado: de la verduleria al cierre contable
# --------------------------------------------------------------------------- #


def test_de_la_verduleria_al_cierre_contable(client, db_session):
    _primer_arranque(client, db_session)
    cliente = _cliente(client, db_session)
    _equipo(client, db_session, CODIGOS[0], cliente.id)

    emision = _emitir(client, cliente.id, CODIGOS[0], importe="450.000")

    assert emision.status_code == 303
    licencia = _ultima_licencia(db_session)
    assert licencia.tipo_emision == "comercial"

    # El contrato con el POS: la cadena firmada se verifica con la llave publica.
    parsed = parse_license(licencia.licencia_text)
    assert parsed is not None
    assert parsed.machine_code == CODIGOS[0]
    assert verify_license(
        state.signing_key.public_key,
        parsed.key_id,
        parsed.machine_code,
        parsed.issued_date,
        parsed.signature,
    )

    # El dinero es entero en COP y el cierre lo refleja.
    ingreso = db_session.execute(
        select(Income).where(Income.licencia_id == licencia.id)
    ).scalar_one()
    assert ingreso.monto_cop == PRECIO_VENTA_COP
    assert ingreso.categoria == "venta"

    cierre = resumen_cierre(db_session, periodo=mes_actual())
    assert cierre.total_cop == PRECIO_VENTA_COP
    assert cierre.licencias_n == 1
    assert "1.350.000" not in str(cierre.total_cop)  # sin inflado

    # Y la pantalla del cierre responde para el mes en curso.
    assert client.get("/cierres").status_code == 200


# --------------------------------------------------------------------------- #
# 2. Mayorista: cupo prepago + liquidacion sin doble conteo
# --------------------------------------------------------------------------- #


def test_mayorista_y_liquidacion_cuadran_entre_si(client, db_session):
    _primer_arranque(client, db_session)
    cliente = _cliente(client, db_session)

    # Tecnico + paquete Master (10 cupos, $ 2.600.000).
    client.post(
        "/mayorista/tecnicos",
        data={"nombre": "Carlos Balanzas", "telefono": "300 555 1122"},
        follow_redirects=False,
    )
    tecnico = db_session.execute(select(Technician)).scalar_one()
    client.post(
        "/mayorista/paquetes",
        data={"tecnico_id": tecnico.id, "tipo": "master", "metodo": "nequi"},
        follow_redirects=False,
    )
    assert saldo_tecnico(db_session, tecnico.id) == 10

    _equipo(client, db_session, CODIGOS[1], cliente.id)
    ingresos_antes = _contar(db_session, Income)

    _emitir(client, cliente.id, CODIGOS[1], tecnico_id=tecnico.id)

    licencia = _ultima_licencia(db_session)
    assert licencia.cupo_descontado is True
    assert licencia.tecnico_id == tecnico.id
    assert saldo_tecnico(db_session, tecnico.id) == 9

    # La licencia del cupo movio $ 0: el paquete ya se cobro.
    ingreso_licencia = db_session.execute(
        select(Income).where(Income.licencia_id == licencia.id)
    ).scalar_one()
    assert ingreso_licencia.monto_cop == 0
    assert ingreso_licencia.categoria == "paquete_mayorista"

    cierre = resumen_cierre(db_session, periodo=mes_actual())
    assert cierre.total_cop == 2_600_000, "solo cuenta el paquete"
    assert cierre.licencias_n == 0, "el verdulero pago al tecnico, no a la sociedad"

    # La liquidacion paga comisiones a un OPERADOR (usuario), no al tecnico.
    client.post(
        "/administradores/crear",
        data={
            "nombre": "Operador Delegado",
            "email": "operador@frubber.co",
            "password": CLAVE_DELEGADO,
            "rol": Role.ADMIN.value,
        },
        follow_redirects=False,
    )
    operador = db_session.execute(select(User).where(User.email == "operador@frubber.co")).scalar_one()

    # La liquidacion es salida de dinero: no crea ingresos.
    client.post(
        "/liquidaciones",
        data={
            "periodo": f"{mes_actual():%Y-%m}",
            "administrador_id": operador.id,
            "licencias": "3",
            "tipo": "porcentaje",
            "valor": "30",
        },
        follow_redirects=False,
    )
    assert _contar(db_session, Income) == ingresos_antes + 1  # solo el $0 del cupo
    liquidacion = db_session.execute(select(Settlement)).scalar_one()
    assert liquidacion.comision_cop == 405_000

    client.post(
        f"/liquidaciones/{liquidacion.id}/pago",
        data={"metodo": "nequi", "comprobante": "Nequi #849201"},
        follow_redirects=False,
    )
    db_session.refresh(liquidacion)
    assert liquidacion.pagado_at is not None
    assert _contar(db_session, Income) == ingresos_antes + 1

    # El cierre del mes no cambio por liquidar comisiones.
    assert resumen_cierre(db_session, periodo=mes_actual()).total_cop == 2_600_000


# --------------------------------------------------------------------------- #
# 3. Roles: el delegado opera, no administra
# --------------------------------------------------------------------------- #


def test_el_delegado_trabaja_pero_no_administra(client, db_session):
    _primer_arranque(client, db_session)
    cliente = _cliente(client, db_session)

    client.post(
        "/administradores/crear",
        data={
            "nombre": "Operador Delegado",
            "email": "operador@frubber.co",
            "password": CLAVE_DELEGADO,
            "rol": Role.ADMIN.value,
        },
        follow_redirects=False,
    )
    client.post("/logout", follow_redirects=False)
    client.post(
        "/login",
        data={"email": "operador@frubber.co", "password": CLAVE_DELEGADO},
        follow_redirects=False,
    )

    # Puede emitir y consultar.
    _equipo(client, db_session, CODIGOS[2], cliente.id)
    assert _emitir(client, cliente.id, CODIGOS[2]).status_code == 303
    assert _ultima_licencia(db_session).creado_por_id is not None
    assert client.get("/historial").status_code == 200

    # No puede tocar cuentas, paquetes ni liquidaciones.
    assert client.get("/administradores").status_code == 403
    assert client.get("/liquidaciones").status_code == 403
    assert (
        client.post(
            "/mayorista/paquetes",
            data={"tecnico_id": "x", "tipo": "inicial"},
            follow_redirects=False,
        ).status_code
        == 403
    )
    assert client.post("/historial/verificar", follow_redirects=False).status_code == 403


# --------------------------------------------------------------------------- #
# 4. Auditoria: cubre el flujo, queda integra y se puede respaldar
# --------------------------------------------------------------------------- #


def test_el_flujo_queda_auditado_y_el_respaldo_se_restaura(
    client, db_session, settings, tmp_path
):
    _primer_arranque(client, db_session)
    cliente = _cliente(client, db_session)
    _equipo(client, db_session, CODIGOS[3], cliente.id)
    _emitir(client, cliente.id, CODIGOS[3])

    acciones = _acciones(db_session)
    for esperada in (
        "usuario.configuracion_inicial",
        "login.exitoso",
        "cliente.creado",
        "equipo.creado",
        "licencia.emitida",
    ):
        assert esperada in acciones, f"falta el evento {esperada}"

    # La cadena esta integra y el Titular puede sellar la verificacion.
    assert verify_chain(db_session).ok
    client.post("/historial/verificar", follow_redirects=False)
    assert "auditoria.verificada" in _acciones(db_session)
    assert verify_chain(db_session).ok

    # El respaldo de ESA base se restaura y conserva la cadena intacta.
    archivo = backup.respaldar(settings, tmp_path / "respaldos")
    ok, detalle = backup.verificar(archivo)
    assert ok, detalle
    assert f"cadena con {_contar(db_session, AuditEvent)} evento(s)" in detalle


# --------------------------------------------------------------------------- #
# 5. Un rechazo no deja basura
# --------------------------------------------------------------------------- #


def test_un_rechazo_a_mitad_de_camino_no_deja_basura(client, db_session):
    _primer_arranque(client, db_session)
    cliente = _cliente(client, db_session)
    client.post(
        "/mayorista/tecnicos",
        data={"nombre": "Carlos Balanzas"},
        follow_redirects=False,
    )
    tecnico = db_session.execute(select(Technician)).scalar_one()

    licencias_antes = _contar(db_session, License)
    equipos_antes = _contar(db_session, Equipment)
    ingresos_antes = _contar(db_session, Income)

    # Sin cupos: la emision se rechaza entera.
    respuesta = _emitir(client, cliente.id, "99A211C077E5B6C7", tecnico_id=tecnico.id)

    assert respuesta.status_code == 303
    assert "no tiene cupos disponibles" in unquote(respuesta.headers["location"])
    assert _contar(db_session, License) == licencias_antes
    assert _contar(db_session, Equipment) == equipos_antes
    assert _contar(db_session, Income) == ingresos_antes
    assert db_session.execute(select(func.count(WholesalePackage.id))).scalar_one() == 0
    assert verify_chain(db_session).ok
