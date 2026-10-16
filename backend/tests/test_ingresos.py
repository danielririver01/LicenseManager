"""Pruebas del modulo de Ingresos y del ciclo de cobro (JHO-102, UI-07).

Lo que se verifica aqui:

* El ciclo automatico de la prueba: dia 1-30 gratis, dia 31 cobro pendiente con
  gracia, dia 34 vencida con el cronometro CONGELADO en 34, y ``pagado`` como
  congelamiento definitivo que consolida la venta (LM-RF-008).
* Que una transicion deja su evento de auditoria y su fila de ingreso, sin
  duplicar cobros.
* El alta manual de pagos en COP: importe entero, metodo de pago y categoria,
  con rollback completo ante cualquier rechazo.
* Filtros por estado de pago / categoria y el resumen de recaudacion.
* La pantalla UI-07 sin scripts inline (CSP ``script-src 'self'``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import crear_titular, iniciar_sesion
from sqlalchemy import select

from license_manager.models import (
    AuditEvent,
    Customer,
    Equipment,
    Income,
    License,
)
from license_manager.services.audit import verify_chain
from license_manager.services.directory import create_customer, create_equipment
from license_manager.services.ingresos import (
    DIA_COBRO_PENDIENTE,
    DIA_VENCIMIENTO,
    calcular_ciclo,
    listar_ingresos,
    resumen_ingresos,
    sincronizar_ciclo,
)

CLAVE = "clave-segura-123"
CODIGO = "A4B9C102E8F37B1A"
NOMBRE_CLIENTE = "Verduleria La Huerta Central"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _hoy():
    return datetime.now(UTC).date()


def _cliente(db, nombre: str = NOMBRE_CLIENTE) -> Customer:
    cliente = create_customer(db, actor_id=None, nombre=nombre)
    db.commit()
    return cliente


def _equipo(db, codigo: str = CODIGO, cliente_id: str | None = None) -> Equipment:
    equipo = create_equipment(
        db, actor_id=None, hardware_code_raw=codigo, cliente_id=cliente_id
    )
    db.commit()
    return equipo


def _emitir(client, cliente_id: str, *, tipo: str, codigo: str = CODIGO):
    return client.post(
        "/emitir",
        data={
            "cliente_id": cliente_id,
            "codigo": codigo,
            "tipo": tipo,
            "importe": "450.000",
        },
        follow_redirects=False,
    )


def _licencia(db) -> License:
    licencia = db.execute(select(License).order_by(License.emitida_at.desc())).scalars().first()
    assert licencia is not None, "la prueba necesita una licencia emitida"
    return licencia


def _avanzar_a_dia(db, licencia: License, dias: int) -> None:
    """Retrasa la fecha de emision para que el cronometro marque ``dias``.

    El dia de emision es el dia 1, asi que un dia N es hoy menos (N-1) dias.
    """
    licencia.emitida_fecha = _hoy() - timedelta(days=dias - 1)
    db.commit()


def _ingresos_de(db, licencia_id: str) -> list[Income]:
    return list(
        db.execute(
            select(Income)
            .where(Income.licencia_id == licencia_id)
            .order_by(Income.creado_at.asc())
        ).scalars()
    )


def _entrada_sesion(client, db):
    crear_titular(db, password=CLAVE)
    iniciar_sesion(client, "titular@frubber.co", CLAVE)


def _filas_html(texto: str) -> str:
    """Solo el cuerpo de la tabla de movimientos.

    Los KPI siempre imprimen montos y los selects siempre listan todas las
    etiquetas: para probar los filtros hay que mirar unicamente las filas.
    """
    inicio = texto.find("<tbody")
    fin = texto.find("</tbody>", inicio)
    return texto[inicio:fin] if inicio >= 0 and fin > inicio else ""


def _ubicacion(response) -> str:
    from urllib.parse import unquote

    return unquote(response.headers["location"])


# --------------------------------------------------------------------------- #
# Acceso
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("metodo", ["get", "post"])
def test_sin_sesion_no_se_toca_ingresos(client, metodo):
    respuesta = getattr(client, metodo)("/ingresos", follow_redirects=False)
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"


def test_sin_sesion_no_se_cambia_la_cuenta(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba", codigo=CODIGO)
    licencia = _licencia(db_session)

    # Se cierra la sesion: lo que sigue debe rebotar en /login sin tocar nada.
    client.cookies.clear()
    respuesta = client.post(
        f"/ingresos/{licencia.id}/cuenta",
        data={"estado": "pagado"},
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"
    db_session.refresh(licencia)
    assert licencia.estado_cobro == "prueba_activa"
    assert licencia.cronometro_congelado_at is None


# --------------------------------------------------------------------------- #
# Ciclo automatico de la prueba (dia 1 -> 31 -> 34)
# --------------------------------------------------------------------------- #


def test_dia_1_es_prueba_activa_a_cero(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")

    licencia = _licencia(db_session)
    assert licencia.estado_cobro == "prueba_activa"
    assert licencia.cronometro_congelado_at is None

    ingresos = _ingresos_de(db_session, licencia.id)
    assert len(ingresos) == 1
    assert ingresos[0].monto_cop == 0
    assert ingresos[0].categoria == "gratuito"
    assert ingresos[0].estado_pago == "pagado"


def test_dia_31_pasa_a_cobro_pendiente_y_crea_el_ingreso(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)
    _avanzar_a_dia(db_session, licencia, DIA_COBRO_PENDIENTE)

    respuesta = client.get("/ingresos")
    assert respuesta.status_code == 200
    assert "periodo de prueba finalizado" in respuesta.text

    db_session.refresh(licencia)
    assert licencia.estado_cobro == "cobro_pendiente"
    # El congelamiento NO ocurre en el 31: todavia hay gracia.
    assert licencia.cronometro_congelado_at is None

    cobros = [
        i
        for i in _ingresos_de(db_session, licencia.id)
        if i.categoria == "cobro_prueba"
    ]
    assert len(cobros) == 1
    assert cobros[0].monto_cop == 450_000
    assert cobros[0].estado_pago == "pendiente"

    eventos = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "ingreso.ciclo_prueba")
    ).scalars().all()
    assert len(eventos) == 1
    assert "cobro_pendiente" in (eventos[0].detalle or "")


def test_dia_34_vence_congela_y_no_vuelve_a_correr(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)
    _avanzar_a_dia(db_session, licencia, DIA_VENCIMIENTO)

    respuesta = client.get("/ingresos")
    assert "cuenta vencida" in respuesta.text.lower()
    assert "Detenido al Día 34" in respuesta.text

    db_session.refresh(licencia)
    assert licencia.estado_cobro == "vencida"
    assert licencia.cronometro_congelado_at is not None
    congelado = licencia.cronometro_congelado_at

    # Unos dias despues el sitio sigue mostrando la cuenta detenida en el 34:
    # vencida es un estado congelado, no se recalcula.
    _avanzar_a_dia(db_session, licencia, DIA_VENCIMIENTO + 9)
    client.get("/ingresos")
    db_session.refresh(licencia)
    assert licencia.estado_cobro == "vencida"
    assert licencia.cronometro_congelado_at == congelado


def test_el_ciclo_no_toca_licencias_comerciales_ni_de_cortesia(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    comercial = _licencia(db_session)

    _avanzar_a_dia(db_session, comercial, DIA_VENCIMIENTO + 20)
    cambios = sincronizar_ciclo(db_session)
    assert cambios == []
    db_session.refresh(comercial)
    assert comercial.estado_cobro == "pagado"


def test_la_sincronizacion_es_idempotente(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)
    _avanzar_a_dia(db_session, licencia, DIA_COBRO_PENDIENTE)

    primera = sincronizar_ciclo(db_session)
    db_session.commit()
    segunda = sincronizar_ciclo(db_session)

    assert len(primera) == 1
    assert segunda == []
    cobros = [i for i in _ingresos_de(db_session, licencia.id) if i.categoria == "cobro_prueba"]
    assert len(cobros) == 1


def test_calcular_ciclo_no_escribe_en_la_base(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)

    assert calcular_ciclo(licencia, _hoy()) == "prueba_activa"
    _avanzar_a_dia(db_session, licencia, DIA_COBRO_PENDIENTE)
    db_session.refresh(licencia)
    # La funcion calcula, pero el estado guardado sigue intacto.
    assert licencia.estado_cobro == "prueba_activa"
    assert calcular_ciclo(licencia, _hoy()) == "cobro_pendiente"


# --------------------------------------------------------------------------- #
# Marcar Pagado: congelamiento y consolidacion
# --------------------------------------------------------------------------- #


def test_marcar_pagado_durante_la_prueba_congela_y_consolida(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)

    respuesta = client.post(
        f"/ingresos/{licencia.id}/cuenta",
        data={"estado": "pagado", "metodo": "nequi", "nota": "Pago antes del dia 10"},
        follow_redirects=False,
    )
    assert respuesta.status_code == 303
    assert "Cuenta actualizada" in _ubicacion(respuesta)

    db_session.refresh(licencia)
    assert licencia.estado_cobro == "pagado"
    assert licencia.cronometro_congelado_at is not None

    ventas = [i for i in _ingresos_de(db_session, licencia.id) if i.categoria == "venta"]
    assert len(ventas) == 1
    assert ventas[0].monto_cop == 450_000
    assert ventas[0].estado_pago == "pagado"
    assert ventas[0].metodo_pago == "nequi"
    assert ventas[0].nota and "Pago antes del dia 10" in ventas[0].nota

    # Congelado: ni el 31 ni el 34 le hacen nada despues.
    _avanzar_a_dia(db_session, licencia, DIA_VENCIMIENTO + 5)
    client.get("/ingresos")
    db_session.refresh(licencia)
    assert licencia.estado_cobro == "pagado"


def test_marcar_pagado_reutiliza_el_ingreso_pendiente_del_dia_31(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)
    _avanzar_a_dia(db_session, licencia, DIA_COBRO_PENDIENTE)
    client.get("/ingresos")

    client.post(
        f"/ingresos/{licencia.id}/cuenta",
        data={"estado": "pagado", "importe": "450.000", "metodo": "bancolombia"},
        follow_redirects=False,
    )

    cobros = [i for i in _ingresos_de(db_session, licencia.id) if i.categoria == "cobro_prueba"]
    assert len(cobros) == 1
    assert cobros[0].estado_pago == "pagado"
    assert cobros[0].metodo_pago == "bancolombia"
    assert int(cobros[0].monto_cop) == 450_000
    # No se crea una segunda fila de venta por el mismo cobro.
    assert [i for i in _ingresos_de(db_session, licencia.id) if i.categoria == "venta"] == []


def test_una_cuenta_pagada_no_se_vuelve_a_abrir(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)

    respuesta = client.post(
        f"/ingresos/{licencia.id}/cuenta",
        data={"estado": "cobro_pendiente"},
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    assert "no se puede volver a abrir" in _ubicacion(respuesta)
    db_session.refresh(licencia)
    assert licencia.estado_cobro == "pagado"


def test_estado_de_cuenta_invalido_no_cambia_nada(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)

    respuesta = client.post(
        f"/ingresos/{licencia.id}/cuenta",
        data={"estado": "reiniciar"},
        follow_redirects=False,
    )

    assert "Estado de cuenta no valido" in _ubicacion(respuesta)
    db_session.refresh(licencia)
    assert licencia.estado_cobro == "prueba_activa"


def test_la_licencia_inexistente_no_se_puede_editar(client, db_session):
    _entrada_sesion(client, db_session)
    respuesta = client.post(
        "/ingresos/00000000-0000-0000-0000-000000000000/cuenta",
        data={"estado": "pagado"},
        follow_redirects=False,
    )
    assert "no existe" in _ubicacion(respuesta)


# --------------------------------------------------------------------------- #
# Alta manual de ingresos (doc 02, seccion 5.3)
# --------------------------------------------------------------------------- #


def test_registrar_ingreso_manual_pagado(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)

    respuesta = client.post(
        "/ingresos",
        data={
            "licencia_id": licencia.id,
            "categoria": "venta",
            "importe": "450.000",
            "estado": "pagado",
            "metodo": "efectivo",
            "nota": "Entrega en efectivo",
        },
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    assert "Ingreso de $450.000 COP registrado" in _ubicacion(respuesta)

    nuevo = [i for i in _ingresos_de(db_session, licencia.id) if i.nota == "Entrega en efectivo"]
    assert len(nuevo) == 1
    assert nuevo[0].monto_cop == 450_000
    assert nuevo[0].moneda == "COP"
    assert nuevo[0].estado_pago == "pagado"
    assert nuevo[0].metodo_pago == "efectivo"

    acciones = db_session.execute(
        select(AuditEvent).where(AuditEvent.accion == "ingreso.registrado")
    ).scalars().all()
    assert len(acciones) == 1
    assert verify_chain(db_session).ok


def test_importe_con_decimales_no_registra_nada(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)
    antes = len(_ingresos_de(db_session, licencia.id))

    respuesta = client.post(
        "/ingresos",
        data={
            "licencia_id": licencia.id,
            "categoria": "venta",
            "importe": "450.5",
            "estado": "pagado",
        },
        follow_redirects=False,
    )

    assert "sin centavos" in _ubicacion(respuesta)
    assert len(_ingresos_de(db_session, licencia.id)) == antes


def test_categoria_gratuito_no_admite_importe(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)

    respuesta = client.post(
        "/ingresos",
        data={
            "licencia_id": licencia.id,
            "categoria": "gratuito",
            "importe": "450.000",
            "estado": "pagado",
        },
        follow_redirects=False,
    )

    assert "categoria gratuita" in _ubicacion(respuesta)


def test_metodo_de_pago_invalido_se_rechaza(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)

    respuesta = client.post(
        "/ingresos",
        data={
            "licencia_id": licencia.id,
            "categoria": "venta",
            "importe": "450.000",
            "estado": "pagado",
            "metodo": "tarjeta-magica",
        },
        follow_redirects=False,
    )

    assert "metodo de pago valido" in _ubicacion(respuesta)


def test_sin_licencia_no_se_registra_el_ingreso(client, db_session):
    _entrada_sesion(client, db_session)
    _cliente(db_session)

    respuesta = client.post(
        "/ingresos",
        data={
            "licencia_id": "",
            "categoria": "venta",
            "importe": "450.000",
            "estado": "pendiente",
        },
        follow_redirects=False,
    )

    assert "Elige la licencia" in _ubicacion(respuesta)
    assert db_session.execute(select(Income)).scalars().all() == []


def test_el_importe_vacio_se_rechaza_cuando_la_categoria_cobra(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)

    respuesta = client.post(
        "/ingresos",
        data={
            "licencia_id": licencia.id,
            "categoria": "venta",
            "importe": "",
            "estado": "pendiente",
        },
        follow_redirects=False,
    )

    assert "Escribe el importe" in _ubicacion(respuesta)


def test_un_pago_manual_sobre_cuenta_en_prueba_la_congela(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)
    _avanzar_a_dia(db_session, licencia, DIA_COBRO_PENDIENTE)
    client.get("/ingresos")

    client.post(
        "/ingresos",
        data={
            "licencia_id": licencia.id,
            "categoria": "cobro_prueba",
            "importe": "450.000",
            "estado": "pagado",
            "metodo": "daviplata",
        },
        follow_redirects=False,
    )

    db_session.refresh(licencia)
    assert licencia.estado_cobro == "pagado"
    assert licencia.cronometro_congelado_at is not None
    cobrados = [
        i
        for i in _ingresos_de(db_session, licencia.id)
        if i.estado_pago == "pagado" and i.monto_cop > 0
    ]
    assert len(cobrados) == 1


# --------------------------------------------------------------------------- #
# Filtros y resumen de recaudacion
# --------------------------------------------------------------------------- #


def test_filtro_por_estado_y_categoria(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)
    client.post(
        "/ingresos",
        data={
            "licencia_id": licencia.id,
            "categoria": "reemplazo",
            "importe": "0",
            "estado": "pendiente",
        },
        follow_redirects=False,
    )

    pagados = client.get("/ingresos?estado=pagado")
    assert "Venta" in _filas_html(pagados.text)
    assert "Reemplazo" not in _filas_html(pagados.text)

    pendientes = client.get("/ingresos?estado=pendiente")
    filas_pendientes = _filas_html(pendientes.text)
    assert "Reemplazo" in filas_pendientes
    # Los KPI siempre imprimen montos: el filtro se verifica en las filas.
    assert "$450.000 COP" not in filas_pendientes

    categoria = client.get("/ingresos?categoria=reemplazo")
    assert "Reemplazo" in _filas_html(categoria.text)

    invalido = client.get("/ingresos?estado=cualquiera", follow_redirects=False)
    assert invalido.status_code == 303
    assert "no es valido" in _ubicacion(invalido)


def test_busqueda_por_verduleria_o_codigo(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")

    con_codigo = client.get("/ingresos?q=A4B9-C102")
    assert "Verduleria La Huerta Central" in con_codigo.text
    assert "$450.000 COP" in con_codigo.text

    sin_resultados = client.get("/ingresos?q=zzz-no-existe")
    assert "No hay movimientos" in sin_resultados.text


def test_resumen_de_recaudacion(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    _equipo(db_session, codigo="5D2198AF44B102C9", cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba", codigo="5D2198AF44B102C9")

    resumen = resumen_ingresos(db_session)
    assert resumen.cobradas_cop == 450_000
    assert resumen.cobradas_n == 1
    assert resumen.pendientes_n == 0
    assert resumen.vencidas_n == 0
    assert resumen.pruebas_activas_n == 1

    # La prueba del dia 31 suma pendiente y baja las pruebas en curso.
    prueba = db_session.execute(
        select(License).where(License.tipo_emision == "prueba")
    ).scalars().one()
    _avanzar_a_dia(db_session, prueba, DIA_VENCIMIENTO)
    sincronizar_ciclo(db_session)
    db_session.commit()

    resumen = resumen_ingresos(db_session)
    assert resumen.pruebas_activas_n == 0
    assert resumen.vencidas_n == 1
    assert resumen.pendientes_n == 0  # la vencida sale en su propia tarjeta
    assert resumen.deuda_vencida_cop == 450_000


def test_listar_ingresos_ordena_y_trae_el_ciclo(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)
    _avanzar_a_dia(db_session, licencia, 7)

    filas = listar_ingresos(db_session)
    assert filas
    fila = filas[0]
    assert fila.cliente_nombre == NOMBRE_CLIENTE
    assert fila.ciclo is not None
    assert fila.ciclo.texto == "Día 7 de 30"
    assert fila.estado_cobro == "prueba_activa"
    assert fila.monto_cop == 0
    assert fila.puede_cobrar is True


def test_la_columna_de_cuenta_informa_el_ciclo_no_el_movimiento(client, db_session):
    """UI-07, columna "Estado de la cuenta".

    La fila automatica de $ 0 de una prueba esta "pagada" por definicion, pero
    eso no debe tapar el estado real de la cuenta: el mockup pide "En periodo
    de prueba" el dia 1 y "Cobro Pendiente" desde el 31.
    """
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)

    dia1 = _filas_html(client.get("/ingresos").text)
    assert 'badge-azul">En periodo de prueba<' in dia1
    assert 'badge-verde">Pagado<' not in dia1
    # El movimiento de $ 0 no dice nada de pagos: no lleva nota al lado.
    assert "Pago:" not in dia1

    _avanzar_a_dia(db_session, licencia, DIA_COBRO_PENDIENTE)
    en_gracia = _filas_html(client.get("/ingresos").text)
    assert 'badge-ambar">Cobro Pendiente<' in en_gracia
    # La fila de $ 0 no contradice a la cuenta: la deuda es la del dia 31.
    assert "Pago:" not in en_gracia

    _avanzar_a_dia(db_session, licencia, DIA_VENCIMIENTO)
    vencida = _filas_html(client.get("/ingresos").text)
    assert 'badge-rojo">Cuenta Vencida<' in vencida


def test_un_movimiento_que_contradice_a_la_cuenta_lo_dice_al_lado(client, db_session):
    """El filtro es por estado de PAGO: si ese pago no cuadra con la cuenta,
    la fila lo anota para que el operador entienda por que salio en el filtro."""
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)

    # La cuenta comercial ya esta pagada; se anota un cobro pendiente sobre ella.
    client.post(
        "/ingresos",
        data={
            "licencia_id": licencia.id,
            "categoria": "venta",
            "importe": "450.000",
            "estado": "pendiente",
            "metodo": "nequi",
        },
        follow_redirects=False,
    )

    filas = listar_ingresos(db_session)
    pendiente = next(f for f in filas if f.estado_pago == "pendiente")
    assert pendiente.cuenta_etiqueta == "Pagado"
    assert pendiente.pago_nota == "Pago: Pendiente"


# --------------------------------------------------------------------------- #
# Pantalla UI-07
# --------------------------------------------------------------------------- #


def test_pantalla_muestra_tarjetas_cronometro_y_filtros(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)
    _avanzar_a_dia(db_session, licencia, DIA_COBRO_PENDIENTE)

    respuesta = client.get("/ingresos")

    assert respuesta.status_code == 200
    assert "Ingresos y Estado de Cuentas (COP)" in respuesta.text
    for tarjeta in ("Ventas cobradas", "Cobros pendientes", "Cuentas vencidas", "Pruebas en curso"):
        assert tarjeta in respuesta.text
    assert "Día 31 (Gracia)" in respuesta.text
    assert "$450.000 COP" in respuesta.text
    assert 'name="estado"' in respuesta.text
    assert 'name="categoria"' in respuesta.text
    assert "Marcar Pagado" in respuesta.text
    assert "Actualizar estado de cuenta" in respuesta.text


def test_la_pantalla_no_lleva_scripts_ni_eventos_inline(client, db_session):
    _entrada_sesion(client, db_session)
    respuesta = client.get("/ingresos")
    cuerpo = respuesta.text

    # CSP script-src 'self': un solo archivo externo y ni un atributo inline.
    assert cuerpo.count("<script") == 1
    assert 'src="/static/lm.js"' in cuerpo
    assert "onclick=" not in cuerpo
    assert "onsubmit=" not in cuerpo
    assert "<script>" not in cuerpo
    # Tampoco se filtra algo que parezca una llave privada.
    assert "BEGIN" not in cuerpo


def test_el_panel_deja_alerta_de_cuentas_pendientes(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="prueba")
    licencia = _licencia(db_session)
    _avanzar_a_dia(db_session, licencia, DIA_VENCIMIENTO)

    panel = client.get("/")

    assert panel.status_code == 200
    assert "cuentas vencidas" in panel.text.lower()
    assert 'href="/ingresos"' in panel.text
    db_session.refresh(licencia)
    assert licencia.estado_cobro == "vencida"


def test_el_enlace_del_menu_de_ingresos_funciona(client, db_session):
    _entrada_sesion(client, db_session)
    panel = client.get("/")
    assert '<a href="/ingresos"' in panel.text

    ingresos = client.get("/ingresos")
    assert ingresos.status_code == 200
    assert 'class="activo"' in ingresos.text


def test_tras_un_rechazo_el_modal_se_vuelve_a_abrir(client, db_session):
    _entrada_sesion(client, db_session)
    cliente = _cliente(db_session)
    _equipo(db_session, cliente_id=cliente.id)
    _emitir(client, cliente.id, tipo="comercial")
    licencia = _licencia(db_session)

    respuesta = client.post(
        f"/ingresos/{licencia.id}/cuenta",
        data={"estado": "pagado", "importe": "mil pesos"},
        follow_redirects=False,
    )

    destino = _ubicacion(respuesta)
    assert f"cuenta={licencia.id}" in destino
    assert "error=" in destino

    pagina = client.get(destino)
    assert pagina.status_code == 200
    # El modal queda visible con la cuenta elegida para corregir y reenviar.
    assert 'class="modal-cuenta oculto"' not in pagina.text
    assert 'data-reabrir="1"' in pagina.text
    assert f'action="/ingresos/{licencia.id}/cuenta"' in pagina.text
