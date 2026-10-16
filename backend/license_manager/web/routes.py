"""Rutas web y API de License Manager.

Separacion deliberada:

* ``pages_router`` sirve HTML a los administradores autenticados.
* ``api_router`` expone los puntos de integracion y salud del sistema.

Regla inmutable de JHO-97: ningun punto de este archivo devuelve la llave
privada. El unico dato criptografico publicable es la huella de la llave
publica, que de por si es publica.
"""

from __future__ import annotations

from datetime import UTC, date
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from license_manager.deps import (
    get_current_user,
    get_db_session,
    get_session_manager,
    require_titular,
    state,
)
from license_manager.models import (
    Customer,
    EmissionType,
    Equipment,
    Income,
    License,
    Role,
    Technician,
    User,
    utcnow,
)
from license_manager.security.passwords import (
    PasswordTooWeak,
    hash_password,
    validate_password_strength,
    verify_password,
)
from license_manager.security.sessions import SessionManager
from license_manager.services.audit import record_event, verify_chain
from license_manager.services.cierres import (
    exportar_cierre_csv,
    format_millones,
    resumen_cierre,
)
from license_manager.services.directory import (
    DirectoryError,
    create_customer,
    create_equipment,
    format_cop,
    format_hardware_code,
    link_equipment_to_customer,
    list_customers,
    list_equipment,
    load_equipment_file,
    set_customer_active,
)
from license_manager.services.historial import (
    GRUPOS,
    LIMITE_EVENTOS,
    listar_eventos,
    total_eventos,
    usuarios_con_eventos,
)
from license_manager.services.ingresos import (
    ETIQUETAS_CATEGORIA,
    ETIQUETAS_ESTADO,
    IngresoError,
    cambiar_estado_cuenta,
    licencia_de,
    listar_ingresos,
    listar_operaciones,
    parse_categoria,
    parse_estado_pago,
    parse_metodo,
    parse_monto,
    registrar_ingreso,
    resumen_ingresos,
    sincronizar_ciclo,
)
from license_manager.services.licencias import (
    MOTIVO_INVALIDO,
    MOTIVO_VIGENTE,
    PRECIO_VENTA_COP,
    TIPOS_EMISION,
    TIPOS_EMISION_TECNICO,
    EmissionError,
    diagnosticar_codigo,
    emit_license,
    licencia_resultado,
    listar_clientes_activos,
    parse_importe_cop,
    preparar_equipo_para_emision,
    tipo_emision_por,
)
from license_manager.services.liquidaciones import (
    CANALES_PAGO_ETIQUETA,
    TIPOS_COMISION,
    LiquidacionError,
    confirmar_pago,
    etiqueta_comision,
    liquidacion_de,
    liquidacion_etiqueta,
    liquidacion_fila,
    listar_liquidaciones,
    registrar_liquidacion,
    ventas_del_periodo,
)
from license_manager.services.mayorista import (
    TIPOS_PAQUETE,
    MayoristaError,
    consumir_cupo,
    crear_tecnico,
    etiqueta_paquete,
    listar_paquetes,
    listar_tecnicos,
    registrar_paquete,
    resumen_mayorista,
    saldo_tecnico,
    set_tecnico_activo,
    tecnico_de,
    tecnicos_para_emision,
)
from license_manager.services.periodos import (
    PeriodoError,
    clave_periodo,
    etiqueta_periodo,
    mes_actual,
    parse_periodo,
    periodo_anterior,
    periodo_siguiente,
)
from license_manager.services.users import (
    UserManagementError,
    change_own_password,
    create_user,
    list_operators,
    reset_password,
    rol_etiqueta,
    set_user_active,
    tecnico_de_usuario,
    tecnicos_con_acceso,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

pages_router = APIRouter(tags=["paginas"])
api_router = APIRouter(tags=["api"])


def _set_session_cookie(response: RedirectResponse, value: str) -> None:
    settings = state.settings
    manager = get_session_manager()
    response.set_cookie(
        key=manager.cookie_name,
        value=value,
        max_age=manager.max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


def _clear_session_cookie(response: RedirectResponse) -> None:
    manager = get_session_manager()
    response.delete_cookie(manager.cookie_name, path="/")


def _render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    user = getattr(request.state, "user", None)
    payload = {"request": request, "current_user": user, "app_name": state.settings.app_name}
    payload.update(context or {})
    return templates.TemplateResponse(request, name, payload, status_code=status_code)


# --------------------------------------------------------------------------- #
# Setup inicial del Titular Principal (UI-01B)
# --------------------------------------------------------------------------- #


def _count_users(db: Session) -> int:
    return db.execute(select(func.count(User.id))).scalar_one()


@pages_router.get("/configurar", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db_session)):
    if _count_users(db) > 0:
        return RedirectResponse("/", status_code=303)
    return _render(request, "setup.html", {"error": None})


@pages_router.post("/configurar")
def setup_submit(
    request: Request,
    nombre: str = Form(...),
    email: str = Form(...),
    password: str = Form(..., alias="password"),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db_session),
):
    if _count_users(db) > 0:
        # El sistema ya fue inicializado: nadie se puede autocrear cuenta.
        raise HTTPException(status_code=403, detail="El sistema ya tiene un Titular configurado.")

    email = email.strip().lower()
    error = None
    if password != password_confirm:
        error = "Las contrasenas no coinciden."
    else:
        try:
            validate_password_strength(password)
        except PasswordTooWeak as exc:
            error = str(exc)

    if error:
        return _render(
            request, "setup.html", {"error": error, "nombre": nombre, "email": email}, status_code=400
        )

    user = User(
        nombre_completo=nombre.strip(),
        email=email,
        password_hash=hash_password(password),
        rol="owner",
        activo=True,
    )
    db.add(user)
    db.flush()
    record_event(
        db,
        action="usuario.configuracion_inicial",
        user=user,
        entity_type="usuario",
        entity_id=user.id,
        detail={"rol": user.rol},
    )
    # La sesion se crea y se confirma en la misma transaccion: si el commit
    # falla, no puede quedar una cookie que apunta a una sesion inexistente.
    cookie = get_session_manager().issue(db, user, request.headers.get("user-agent"))
    db.commit()
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, cookie)
    return response


# --------------------------------------------------------------------------- #
# Inicio de sesion (UI-01)
# --------------------------------------------------------------------------- #


@pages_router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db_session)):
    if _count_users(db) == 0:
        return RedirectResponse("/configurar", status_code=303)
    if get_current_user(request, db) is not None:
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", {"error": None})


@pages_router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db_session),
):
    email = email.strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()

    # Verificacion siempre, con hash valido o no, para no revelar si la cuenta existe.
    stored = user.password_hash if user else hash_password("no-real-password-placeholder")
    if not verify_password(stored, password) or user is None:
        record_event(db, action="login.fallido", user=user, detail={"email": email})
        db.commit()
        return _render(request, "login.html", {"error": "Correo o contrasena incorrectos."}, status_code=401)

    if not user.activo:
        record_event(db, action="login.rechazado_cuenta_inactiva", user=user)
        db.commit()
        return _render(
            request,
            "login.html",
            {"error": "Esta cuenta esta desactivada. Contacte al Titular."},
            status_code=403,
        )

    from license_manager.models import utcnow

    user.ultimo_acceso_at = utcnow()
    record_event(db, action="login.exitoso", user=user)
    cookie = get_session_manager().issue(db, user, request.headers.get("user-agent"))
    db.commit()

    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, cookie)
    return response


@pages_router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db_session)):
    manager = get_session_manager()
    cookie = request.cookies.get(manager.cookie_name)
    user = manager.resolve(db, cookie)
    if user is not None:
        record_event(db, action="login.sesion_cerrada", user=user)
    manager.revoke(db, cookie)
    db.commit()

    response = RedirectResponse("/login", status_code=303)
    _clear_session_cookie(response)
    return response


# --------------------------------------------------------------------------- #
# Panel (UI-02) - requiere sesion
# --------------------------------------------------------------------------- #


def _ultimas_licencias_del_tecnico(db: Session, user_id: str, limite: int = 8) -> list[dict]:
    """Ultimas licencias emitidas por esta cuenta, con su equipo y verduleria."""
    filas = db.execute(
        select(License, Equipment.hardware_code, Customer.nombre)
        .join(Equipment, Equipment.id == License.equipo_id, isouter=True)
        .join(Customer, Customer.id == Equipment.cliente_id, isouter=True)
        .where(License.creado_por_id == user_id)
        .order_by(License.emitida_at.desc())
        .limit(limite)
    ).all()
    return [
        {"licencia": licencia, "codigo": codigo, "cliente": nombre}
        for licencia, codigo, nombre in filas
    ]


@pages_router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db_session)):
    if _count_users(db) == 0:
        return RedirectResponse("/configurar", status_code=303)

    user = get_current_user(request, db)
    request.state.user = user
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # El tecnico tiene su propio resumen: su saldo y lo que el mismo emitio.
    # El dinero de la sociedad no se le muestra (JHO-107).
    if Role(user.rol).is_tecnico:
        tecnico = tecnico_de_usuario(db, user)
        return _render(
            request,
            "panel_tecnico.html",
            {
                "tecnico": tecnico,
                "saldo": saldo_tecnico(db, tecnico.id) if tecnico else 0,
                "paquetes": listar_paquetes(db, tecnico_id=tecnico.id) if tecnico else [],
                "emitidas_n": db.execute(
                    select(func.count(License.id)).where(License.creado_por_id == user.id)
                ).scalar_one()
                or 0,
                "ultimas": _ultimas_licencias_del_tecnico(db, user.id),
                "format_cop": format_cop,
                "format_hardware_code": format_hardware_code,
            },
        )

    total_equipos = db.execute(select(func.count(Equipment.id))).scalar_one() or 0
    total_licencias = db.execute(select(func.count(License.id))).scalar_one() or 0
    ingresos_pagados = (
        db.execute(
            select(func.coalesce(func.sum(Income.monto_cop), 0)).where(Income.estado_pago == "pagado")
        ).scalar_one()
        or 0
    )

    # El ciclo de prueba tambien se evalua al abrir el panel: un dia 31 o un
    # dia 34 no pueden esperar a que alguien entre a la pantalla de Ingresos.
    try:
        sincronizar_ciclo(db, actor_id=user.id)
        resumen_cobros = resumen_ingresos(db)
    except IngresoError:  # pragma: no cover - filtro interno, nunca con defaults
        db.rollback()
        resumen_cobros = resumen_ingresos(db)
    else:
        db.commit()

    return _render(
        request,
        "panel.html",
        {
            "total_equipos": total_equipos,
            "total_licencias": total_licencias,
            "ingresos_pagados_cop": ingresos_pagados,
            "cuentas_pendientes": resumen_cobros.pendientes_n,
            "cuentas_vencidas": resumen_cobros.vencidas_n,
            "deuda_vencida_cop": resumen_cobros.deuda_vencida_cop,
            "format_cop": format_cop,
        },
    )


# --------------------------------------------------------------------------- #
# API de estado
# --------------------------------------------------------------------------- #


@api_router.get("/salud")
def salud():
    """Salud del servicio. No expone datos del negocio ni de la llave."""
    return {
        "estado": "ok",
        "firma_disponible": state.signing_key is not None,
    }


@api_router.get("/estado")
def estado_sistema():
    """Identidad criptografica publica. NUNCA la llave privada."""
    key = state.signing_key
    settings = state.settings
    if key is None:
        return JSONResponse(
            status_code=503,
            content={
                "firma_disponible": False,
                "motivo": state.signing_key_error,
                "key_id": settings.expected_key_id,
            },
        )
    return {
        "firma_disponible": True,
        "key_id": key.key_id,
        # La huella es de la llave PUBLICA, que ya va embebida en Frubber POS.
        "llave_publica_sha256": key.public_fingerprint_sha256,
        "llave_publica_spki_base64": key.public_key_der_b64,
    }


@api_router.post("/auditoria/verificar")
def verificar_auditoria(db: Session = Depends(get_db_session)):
    """Recomputa la cadena de hash de la auditoria."""
    from license_manager.services.audit import verify_chain

    result = verify_chain(db)
    return {"ok": result.ok, "total_eventos": result.total_events, "detalle": result.reason}


# --------------------------------------------------------------------------- #
# Gestion de administradores (UI-09, JHO-99) - exclusivo de Titulares
# --------------------------------------------------------------------------- #


@pages_router.get("/administradores", response_class=HTMLResponse)
def administradores_page(
    request: Request,
    db: Session = Depends(get_db_session),
    aviso: str | None = None,
    error: str | None = None,
):
    user = get_current_user(request, db)
    request.state.user = user
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Bloqueo 403 estricto y visible: el administrador delegado ve la tarjeta
    # de acceso restringido con estado 403, no una lista vacia ni un redirect
    # que oculte que la pantalla existe.
    if not Role(user.rol).is_titular:
        return _render(
            request,
            "administradores.html",
            {"acceso_denegado": True, "operadores": [], "aviso": None, "error": None},
            status_code=403,
        )

    return _render(
        request,
        "administradores.html",
        {
            "acceso_denegado": False,
            "operadores": list_operators(db),
            "aviso": aviso,
            "error": error,
            "rol_etiqueta": rol_etiqueta,
            "seccion": "administradores",
        },
    )


def _admin_redirect(aviso: str | None = None, error: str | None = None) -> RedirectResponse:
    destino = "/administradores"
    if aviso:
        destino += f"?aviso={quote(aviso)}"
    elif error:
        destino += f"?error={quote(error)}"
    return RedirectResponse(destino, status_code=303)


@pages_router.post("/administradores/crear")
def crear_usuario(
    request: Request,
    nombre: str = Form(...),
    email: str = Form(...),
    password: str = Form(..., alias="password"),
    rol: str = Form(...),
    user: User = Depends(require_titular),
    db: Session = Depends(get_db_session),
):
    try:
        nuevo = create_user(
            db, actor=user, nombre=nombre, email=email, password=password, rol=rol
        )
    except UserManagementError as exc:
        db.rollback()
        return _admin_redirect(error=str(exc))

    record_event(
        db,
        action="usuario.creado",
        user=user,
        entity_type="usuario",
        entity_id=nuevo.id,
        # Nunca se registra la contrasena, ni hasheada ni en claro.
        detail={"rol": nuevo.rol, "email_creado": nuevo.email},
    )
    db.commit()
    etiqueta = rol_etiqueta(nuevo.rol)
    return _admin_redirect(aviso=f"Cuenta de {etiqueta} creada: {nuevo.nombre_completo}.")


@pages_router.post("/administradores/{user_id}/alternar")
def alternar_usuario(
    request: Request,
    user_id: str,
    activo: str = Form(...),
    user: User = Depends(require_titular),
    db: Session = Depends(get_db_session),
    sessions: SessionManager = Depends(get_session_manager),
):
    target = db.get(User, user_id)
    if target is None:
        return _admin_redirect(error="La cuenta solicitada no existe.")

    deseado = activo in ("1", "true", "True", "on")
    try:
        revocadas = set_user_active(
            db, sessions, actor=user, target=target, active=deseado
        )
    except UserManagementError as exc:
        db.rollback()
        return _admin_redirect(error=str(exc))

    record_event(
        db,
        action="usuario.activado" if deseado else "usuario.desactivado",
        user=user,
        entity_type="usuario",
        entity_id=target.id,
        # El email del operador queda congelado en la auditoria aunque despues
        # se desactive o salga de la empresa: eso es lo que exige UI-09.
        detail={"email_afectado": target.email, "rol": target.rol, "sesiones_revocadas": revocadas},
    )
    db.commit()

    if deseado:
        return _admin_redirect(aviso=f"Cuenta de {target.nombre_completo} activada.")
    return _admin_redirect(
        aviso=(
            f"Cuenta de {target.nombre_completo} desactivada. "
            f"Se cerraron {revocadas} sesion(es); su historial se conserva."
        )
    )


@pages_router.post("/administradores/{user_id}/restablecer")
def restablecer_clave(
    request: Request,
    user_id: str,
    password: str = Form(..., alias="password"),
    user: User = Depends(require_titular),
    db: Session = Depends(get_db_session),
    sessions: SessionManager = Depends(get_session_manager),
):
    target = db.get(User, user_id)
    if target is None:
        return _admin_redirect(error="La cuenta solicitada no existe.")

    try:
        revocadas = reset_password(
            db, sessions, actor=user, target=target, new_password=password
        )
    except UserManagementError as exc:
        db.rollback()
        return _admin_redirect(error=str(exc))

    record_event(
        db,
        action="usuario.clave_restablecida",
        user=user,
        entity_type="usuario",
        entity_id=target.id,
        # Solo el conteo: la clave y su hash jamas salen de aqui.
        detail={"email_afectado": target.email, "sesiones_revocadas": revocadas},
    )
    db.commit()
    return _admin_redirect(
        aviso=(
            f"Clave de {target.nombre_completo} restablecida. "
            f"Se cerraron {revocadas} sesion(es); debe usar la clave nueva."
        )
    )


@pages_router.post("/seguridad/mi-clave")
def cambiar_mi_clave(
    request: Request,
    password_actual: str = Form(..., alias="password_actual"),
    password_nueva: str = Form(..., alias="password_nueva"),
    password_confirmacion: str = Form(..., alias="password_confirmacion"),
    user: User = Depends(require_titular),
    db: Session = Depends(get_db_session),
    sessions: SessionManager = Depends(get_session_manager),
):
    try:
        nueva_cookie = change_own_password(
            db,
            sessions,
            user=user,
            current_password=password_actual,
            new_password=password_nueva,
            new_password_confirm=password_confirmacion,
        )
    except UserManagementError as exc:
        db.rollback()
        # Cambio de clave propio: se vuelve a la pantalla con el error, nunca
        # se confirma un cambio que no se efectuo.
        return RedirectResponse(
            f"/administradores?error={quote(str(exc))}", status_code=303
        )

    record_event(
        db,
        action="usuario.clave_propia_cambiada",
        user=user,
        entity_type="usuario",
        entity_id=user.id,
        detail={"sesiones_cerradas_anteriores": True},
    )
    db.commit()

    aviso_clave = quote("Su clave fue actualizada.")
    response = RedirectResponse(f"/administradores?aviso={aviso_clave}", status_code=303)
    # Se borra la cookie vieja antes de emitir la nueva: si algo falla en el
    # medio, no queda una sesion a medias.
    _clear_session_cookie(response)
    _set_session_cookie(response, nueva_cookie)
    return response


# --------------------------------------------------------------------------- #
# Directorio de clientes e inventario de equipos (UI-03 / UI-04 / JHO-100)
# --------------------------------------------------------------------------- #
#
# A diferencia de /administradores (UI-09), estas pantallas estan disponibles
# para los tres roles: el documento 04 v2.0 dice que el Administrador Delegado
# "registra clientes y emite licencias". Lo que NO puede es gestionar cuentas.


def _sesion_o_login(request: Request, db: Session):
    """Devuelve (usuario, None) o (None, redirect a /login)."""
    user = get_current_user(request, db)
    request.state.user = user
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


def _volver(destino: str, aviso: str | None = None, error: str | None = None) -> RedirectResponse:
    params: list[str] = []
    if aviso:
        params.append(f"aviso={quote(aviso)}")
    if error:
        params.append(f"error={quote(error)}")
    if not params:
        return RedirectResponse(destino, status_code=303)
    # El destino puede traer sus propios parametros (/emitir?licencia=...):
    # ahi se concatena con & para no romper la cadena de consulta.
    separador = "&" if "?" in destino else "?"
    return RedirectResponse(f"{destino}{separador}{'&'.join(params)}", status_code=303)


def _denegar_modulo(request: Request, *, modulo: str, motivo: str):
    """403 visible y uniforme para un modulo fuera del alcance del rol."""
    return _render(
        request,
        "acceso_denegado.html",
        {"modulo": modulo, "motivo": motivo},
        status_code=403,
    )


def _sin_acceso_a_dinero(request: Request, user: User, modulo: str):
    """El rol tecnico emite con su saldo; el dinero de la sociedad no es suyo."""
    if Role(user.rol).is_tecnico:
        return _denegar_modulo(
            request,
            modulo=modulo,
            motivo=(
                "Tu cuenta de Técnico sirve para emitir licencias con tu saldo "
                "prepago. La parte contable de la sociedad (cobros, cierres, "
                "liquidaciones) no forma parte de tu acceso."
            ),
        )
    return None


@pages_router.get("/clientes", response_class=HTMLResponse)
def clientes_page(request: Request, db: Session = Depends(get_db_session), q: str = ""):
    """UI-03 · listado de verdulerias con buscador y estado comercial."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    clientes = list_customers(db, busqueda=q)
    return _render(
        request,
        "clientes.html",
        {
            "seccion": "clientes",
            "clientes": clientes,
            "busqueda": q.strip(),
            "total": len(clientes),
            "aviso": request.query_params.get("aviso"),
            "error": request.query_params.get("error"),
        },
    )


@pages_router.post("/clientes")
def crear_cliente(
    request: Request,
    nombre: str = Form(""),
    propietario: str = Form(""),
    telefono: str = Form(""),
    direccion: str = Form(""),
    ciudad: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-03 · alta de verduleria."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    try:
        customer = create_customer(
            db,
            actor_id=user.id,
            nombre=nombre,
            propietario=propietario,
            telefono=telefono,
            direccion=direccion,
            ciudad=ciudad,
        )
    except DirectoryError as exc:
        db.rollback()
        return _volver("/clientes", error=str(exc))

    record_event(
        db,
        action="cliente.creado",
        user=user,
        entity_type="verduleria",
        entity_id=customer.id,
        detail={"nombre": customer.nombre, "ciudad": customer.ciudad},
    )
    db.commit()
    return _volver("/clientes", aviso=f"Verduleria {customer.nombre} registrada.")


@pages_router.post("/clientes/{customer_id}/alternar")
def alternar_cliente(
    request: Request,
    customer_id: str,
    activo: str = Form(...),
    db: Session = Depends(get_db_session),
):
    """UI-03 · activar/desactivar sin borrar historial."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    customer = db.get(Customer, customer_id)
    if customer is None:
        return _volver("/clientes", error="La verduleria solicitada no existe.")

    deseado = activo in ("1", "true", "True", "on")
    set_customer_active(db, customer=customer, active=deseado)
    record_event(
        db,
        action="cliente.activado" if deseado else "cliente.desactivado",
        user=user,
        entity_type="verduleria",
        entity_id=customer.id,
        detail={"nombre": customer.nombre},
    )
    db.commit()

    if deseado:
        return _volver("/clientes", aviso=f"{customer.nombre} vuelve a estar activa.")
    return _volver(
        "/clientes",
        aviso=(
            f"{customer.nombre} desactivada. Sus equipos y licencias quedan "
            f"conservados; ya no se pueden firmar licencias nuevas para ella."
        ),
    )


@pages_router.get("/equipos", response_class=HTMLResponse)
def equipos_page(request: Request, db: Session = Depends(get_db_session), q: str = ""):
    """UI-04 · inventario de maquinas con su licencia vigente."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    equipos = list_equipment(db, busqueda=q)
    clientes_activos = [
        c for c in list_customers(db) if c.activo
    ]
    return _render(
        request,
        "equipos.html",
        {
            "seccion": "equipos",
            "equipos": equipos,
            "clientes": clientes_activos,
            "busqueda": q.strip(),
            "total": len(equipos),
            "format_hardware_code": format_hardware_code,
            "aviso": request.query_params.get("aviso"),
            "error": request.query_params.get("error"),
        },
    )


@pages_router.post("/equipos")
def crear_equipo(
    request: Request,
    hardware_code: str = Form(""),
    cliente_id: str = Form(""),
    notas: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-04 · alta de equipo. El codigo se normaliza igual que al firmar."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    try:
        equipo = create_equipment(
            db,
            actor_id=user.id,
            hardware_code_raw=hardware_code,
            cliente_id=cliente_id.strip() or None,
            notas=notas,
        )
    except DirectoryError as exc:
        db.rollback()
        return _volver("/equipos", error=str(exc))

    record_event(
        db,
        action="equipo.creado",
        user=user,
        entity_type="equipo",
        entity_id=equipo.id,
        # El codigo de hardware no es secreto: es la identidad publica de la
        # maquina y aparece ademas dentro del propio texto FRB1.
        detail={"hardware_code": equipo.hardware_code, "cliente_id": equipo.cliente_id},
    )
    db.commit()
    return _volver(
        f"/equipos/{equipo.id}",
        aviso=f"Equipo {format_hardware_code(equipo.hardware_code)} registrado.",
    )


@pages_router.get("/equipos/{equipment_id}", response_class=HTMLResponse)
def ficha_equipo_page(request: Request, equipment_id: str, db: Session = Depends(get_db_session)):
    """UI-06 · ficha del equipo: licencia FRB1, estado COP y bitacora."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    ficha = load_equipment_file(db, equipment_id)
    if ficha is None:
        return _volver("/equipos", error="El equipo solicitado no existe.")

    return _render(
        request,
        "ficha_equipo.html",
        {
            "seccion": "equipos",
            "ficha": ficha,
            "codigo_formateado": format_hardware_code(ficha.equipo.hardware_code),
            "format_cop": format_cop,
            "clientes": [c for c in list_customers(db) if c.activo],
            "aviso": request.query_params.get("aviso"),
            "error": request.query_params.get("error"),
        },
    )


@pages_router.post("/equipos/{equipment_id}/cliente")
def vincular_equipo(
    request: Request,
    equipment_id: str,
    cliente_id: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-06 · asignar o quitar la verduleria de un equipo."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    equipo = db.get(Equipment, equipment_id)
    if equipo is None:
        return _volver("/equipos", error="El equipo solicitado no existe.")

    destino_id = cliente_id.strip() or None
    try:
        link_equipment_to_customer(db, equipo=equipo, cliente_id=destino_id)
    except DirectoryError as exc:
        db.rollback()
        return _volver(f"/equipos/{equipment_id}", error=str(exc))

    cliente = db.get(Customer, destino_id) if destino_id else None
    record_event(
        db,
        action="equipo.vinculado" if cliente else "equipo.desvinculado",
        user=user,
        entity_type="equipo",
        entity_id=equipo.id,
        detail={
            "hardware_code": equipo.hardware_code,
            "cliente_id": destino_id,
            "cliente_nombre": cliente.nombre if cliente else None,
        },
    )
    db.commit()

    if cliente:
        return _volver(
            f"/equipos/{equipment_id}",
            aviso=f"Equipo asignado a {cliente.nombre}.",
        )
    return _volver(
        f"/equipos/{equipment_id}",
        aviso="Equipo sin verduleria asignada. Su historial se conserva completo.",
    )


# --------------------------------------------------------------------------- #
# Emision de licencias FRB1 (UI-05, JHO-101)
# --------------------------------------------------------------------------- #
#
# Los tres roles pueden emitir (doc 04 v2.1, seccion 7). La llave privada vive
# solo en el proceso del backend: la pantalla recibe el texto firmado, nunca
# la llave.


def _cliente_o_error(db: Session, cliente_id: str) -> Customer | None:
    """Cliente elegido en UI-05. None si no existe o esta desactivado."""
    if not cliente_id:
        return None
    cliente = db.get(Customer, cliente_id)
    if cliente is None:
        return None
    return cliente if cliente.activo else None


@pages_router.get("/emitir", response_class=HTMLResponse)
def emitir_page(
    request: Request,
    db: Session = Depends(get_db_session),
    equipo: str = "",
    codigo: str = "",
    cliente: str = "",
    licencia: str = "",
    tipo: str = "",
    tecnico: str = "",
    justificacion: str = "",
):
    """UI-05 · formulario de emision (doc 02, seccion 5.2).

    Arranca siempre limpio. Los parametros existen para las entradas reales del
    flujo: ``equipo`` viene de la ficha UI-06 («Emitir licencia para este
    equipo») y ``codigo``/``cliente``/``tipo``/``tecnico`` conservan lo que se
    escribio cuando un rechazo devuelve el formulario. ``tecnicos`` (JHO-104)
    alimenta el selector de paquete prepago.
    """
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    # Un tecnico no elige tecnico: emite SIEMPRE con el suyo (JHO-107).
    tecnico_propio = tecnico_de_usuario(db, user) if Role(user.rol).is_tecnico else None
    es_tecnico = Role(user.rol).is_tecnico

    resultado = None
    tecnico_resultado = None
    saldo_resultado = None
    if licencia:
        resultado = licencia_resultado(db, licencia)
        if resultado is None:
            return _volver("/emitir", error="La licencia indicada no existe.")
        if resultado.tecnico_id:
            tecnico_resultado = db.get(Technician, resultado.tecnico_id)
            saldo_resultado = saldo_tecnico(db, resultado.tecnico_id)

    equipo_previo = db.get(Equipment, equipo) if equipo else None
    if equipo and equipo_previo is None:
        return _volver("/emitir", error="El equipo indicado no existe.")

    cliente_previo = _cliente_o_error(db, cliente)
    if cliente and cliente_previo is None:
        return _volver("/emitir", error="La verduleria indicada no existe o esta desactivada.")

    # La ficha trae el codigo ya registrado y, si lo tiene, su verduleria.
    codigo_sugerido = codigo
    if equipo_previo is not None:
        codigo_sugerido = format_hardware_code(equipo_previo.hardware_code)
        if cliente_previo is None and equipo_previo.cliente_id:
            cliente_previo = db.get(Customer, equipo_previo.cliente_id)

    error = request.query_params.get("error")
    diagnostico = diagnosticar_codigo(db, codigo_sugerido) if codigo_sugerido else None
    if diagnostico is not None and diagnostico.motivo == MOTIVO_INVALIDO and not error:
        # Un codigo ilegible no se descarta en silencio: se explica al operador.
        error = diagnostico.texto

    hoy = utcnow().astimezone(UTC).date()
    return _render(
        request,
        "emitir.html",
        {
            "seccion": "equipos",
            "tipos": TIPOS_EMISION,
            "clientes": listar_clientes_activos(db),
            "cliente_previo": cliente_previo,
            "codigo_sugerido": codigo_sugerido,
            "diagnostico": diagnostico,
            "resultado": resultado,
            "tecnico_resultado": tecnico_resultado,
            "saldo_resultado": saldo_resultado,
            # Preservado cuando un rechazo devuelve el formulario.
            "tipo_previo": tipo,
            "tecnico_previo": tecnico,
            "justificacion_previa": justificacion,
            # Selector de paquete prepago (JHO-104, UI-10). Un tecnico no lo ve:
            # su nombre viene fijo y su saldo es lo unico que puede gastar.
            "tecnicos": tecnicos_para_emision(db),
            "es_tecnico": es_tecnico,
            "tecnico_propio": tecnico_propio,
            "saldo_propio": (
                saldo_tecnico(db, tecnico_propio.id) if tecnico_propio else 0
            ),
            "tipos_tecnico": TIPOS_EMISION_TECNICO,
            "precio_venta": PRECIO_VENTA_COP,
            "hoy": hoy.strftime("%d/%m/%Y"),
            "format_cop": format_cop,
            "format_hardware_code": format_hardware_code,
            "firma_disponible": state.signing_key is not None,
            "aviso": request.query_params.get("aviso"),
            "error": error,
        },
    )


@pages_router.post("/emitir")
def emitir_submit(
    request: Request,
    cliente_id: str = Form(""),
    codigo: str = Form(""),
    tipo: str = Form(""),
    importe: str = Form(""),
    tecnico_id: str = Form(""),
    justificacion: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-05 · valida el codigo pegado, firma y registra el ingreso.

    Con ``tecnico_id`` (JHO-104) la venta descuenta un cupo prepago del tecnico
    y no vuelve a cobrar: el paquete ya se pago al comprarlo.
    """
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    # Todo rechazo devuelve el formulario con lo que se habia escrito.
    destino = (
        f"/emitir?cliente={quote(cliente_id, safe='')}"
        f"&codigo={quote(codigo, safe='')}"
        f"&tipo={quote(tipo, safe='')}"
        f"&tecnico={quote(tecnico_id, safe='')}"
        f"&justificacion={quote(justificacion, safe='')}"
    )

    if tipo_emision_por(tipo) is None:
        return _volver(destino, error="Elige un tipo de licencia valido.")

    if state.signing_key is None:
        return _volver(
            destino,
            error=(
                "La llave de firma no esta disponible en el servidor. "
                "No es posible emitir licencias hasta configurarla."
            ),
        )

    # Tecnico balancero (opcional): identifica la operacion antes del dinero.
    # Si quien firma es un tecnico, el tecnico es SIEMPRE el de su cuenta: el
    # campo del formulario se ignora para que no pueda gastar cupos ajenos.
    if Role(user.rol).is_tecnico:
        tecnico = tecnico_de_usuario(db, user)
        if tecnico is None:
            return _volver(
                destino,
                error=(
                    "Tu cuenta de Técnico no está vinculada a una ficha de técnico. "
                    "Pide al Titular que revise tu acceso."
                ),
            )
        if tipo == EmissionType.COURTESY.value:
            return _volver(
                destino,
                error=(
                    "Una cortesía la autoriza la sociedad, no el técnico. "
                    "Elige Venta estándar, Prueba o Reemplazo."
                ),
            )
    else:
        tecnico = tecnico_de(db, tecnico_id)
        if tecnico_id and tecnico is None:
            return _volver(destino, error="El tecnico indicado no existe.")

    if tecnico is not None and not tecnico.activo:
        return _volver(
            destino,
            error=(
                f"La cuenta de {tecnico.nombre} esta desactivada: no se emiten "
                "licencias a su nombre."
            ),
        )

    try:
        # Orden de los rechazos: primero lo que identifica la operacion
        # (codigo y verduleria) y al final el dinero, que es la ultima
        # confirmacion del operador.
        diagnostico = diagnosticar_codigo(db, codigo)
        if diagnostico.motivo == MOTIVO_INVALIDO:
            raise EmissionError(diagnostico.texto)
        if diagnostico.motivo == MOTIVO_VIGENTE:
            # Sin 'error': la pantalla muestra la advertencia con el enlace a la
            # ficha. Aqui no se decide una reemision ni un reemplazo.
            return _volver(destino)

        equipo = preparar_equipo_para_emision(
            db, codigo_raw=codigo, cliente_id=cliente_id, actor_id=user.id
        )

        seleccion = tipo_emision_por(tipo)

        # Cupo prepago: solo una venta comercial a nombre de un tecnico lo
        # descuenta. Se hace dentro de la transaccion de la emision para que un
        # rechazo posterior devuelva tambien el cupo (doc 04, secciones 5 y 6).
        paquete = None
        if tecnico is not None and seleccion.es_pagable:
            paquete = consumir_cupo(db, tecnico)

        # Si el cupo cubre la venta no se vuelve a cobrar: el importe no se
        # exige ni se usa. Solo una venta directa de la sociedad lo necesita.
        importe_cop = 0
        if seleccion.es_pagable and paquete is None:
            importe_cop = parse_importe_cop(importe)

        licencia = emit_license(
            db,
            actor_id=user.id,
            signing_key=state.signing_key,
            equipo=equipo,
            tipo=tipo,
            importe_cop=importe_cop,
            tecnico=tecnico,
            paquete=paquete,
            justificacion=justificacion,
        )
    except (EmissionError, MayoristaError) as exc:
        # Se revierte TODO: si el codigo dio de alta un equipo, tampoco se queda.
        db.rollback()
        return _volver(destino, error=str(exc))

    cliente = db.get(Customer, equipo.cliente_id) if equipo.cliente_id else None
    record_event(
        db,
        action="licencia.emitida",
        user=user,
        entity_type="licencia",
        entity_id=licencia.id,
        # En la auditoria NO va el texto FRB1: van los datos contables del
        # evento. La cadena firmada vive en la tabla de licencias.
        detail={
            "tipo_emision": licencia.tipo_emision,
            "key_id": licencia.key_id,
            "hardware_code": equipo.hardware_code,
            "cliente_id": equipo.cliente_id,
            "cliente_nombre": cliente.nombre if cliente else None,
            # Trazabilidad del modelo mayorista (JHO-104).
            "tecnico_id": tecnico.id if tecnico else None,
            "tecnico_nombre": tecnico.nombre if tecnico else None,
            "paquete_id": licencia.paquete_id,
            "cupo_descontado": licencia.cupo_descontado,
            "justificacion": (
                justificacion.strip() or None
                if licencia.tipo_emision == EmissionType.REPLACEMENT.value
                else None
            ),
        },
    )
    db.commit()

    return _volver(
        f"/emitir?licencia={licencia.id}",
        aviso="Licencia firmada. Copia el bloque de activacion y entregalo al tecnico.",
    )


# --------------------------------------------------------------------------- #
# Ingresos manuales y ciclo de cobro (UI-07, JHO-102)
# --------------------------------------------------------------------------- #
#
# Los tres roles consultan y registran ingresos (doc 01, LM-RF-002). El sitio
# no simula conciliacion bancaria: el pago lo confirma una persona y queda con
# su usuario, su fecha y su metodo de pago.


METODOS_PAGO = (
    ("efectivo", "Efectivo"),
    ("nequi", "Nequi"),
    ("daviplata", "Daviplata"),
    ("bancolombia", "Bancolombia"),
    ("otro", "Otro"),
)

# Estados de cuenta que admite el modal de UI-07 (doc 02, seccion 5.3).
ESTADOS_CUENTA = (
    ("pagado", "Pagado — detiene el cronometro y consolida la venta"),
    ("cobro_pendiente", "Cobro pendiente ($ 450.000 COP) — periodo de gracia"),
    ("vencida", "Vencida / sin cobro — detiene el cronometro"),
)


def _ingresos_destino(params: dict[str, str] | None = None, **extras: str) -> str:
    """URL de vuelta a /ingresos conservando lo que el operador escribio."""
    valores = {k: (v or "") for k, v in (params or {}).items()}
    valores.update(extras)
    pares = [f"{clave}={quote(valor, safe='')}" for clave, valor in valores.items() if valor]
    return "/ingresos" + (f"?{'&'.join(pares)}" if pares else "")


@pages_router.get("/ingresos", response_class=HTMLResponse)
def ingresos_page(
    request: Request,
    db: Session = Depends(get_db_session),
    q: str = "",
    estado: str = "",
    categoria: str = "",
    registrar: str = "",
    cuenta: str = "",
    # Campos que un rechazo devuelve al formulario de alta manual.
    f_categoria: str = "",
    f_importe: str = "",
    f_metodo: str = "",
    f_estado: str = "",
    f_nota: str = "",
):
    """UI-07 · cobros manuales, filtros y ciclo de prueba."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    sin_acceso = _sin_acceso_a_dinero(request, user, "Ingresos y cobros")
    if sin_acceso:
        return sin_acceso

    # Primero el ciclo automatico: lo que paso el dia 31 o el 34 se aplica
    # antes de pintar cualquier cifra de la pantalla.
    try:
        cambios = sincronizar_ciclo(db, actor_id=user.id)
        filas = listar_ingresos(db, busqueda=q, estado=estado, categoria=categoria)
        resumen = resumen_ingresos(db)
    except IngresoError as exc:
        db.rollback()
        return _volver("/ingresos", error=str(exc))

    if cambios:
        db.commit()

    licencia_modal = licencia_de(db, cuenta) if cuenta else None
    cuenta_datos = None
    if licencia_modal is not None:
        equipo_modal = db.get(Equipment, licencia_modal.equipo_id)
        cliente_modal = (
            db.get(Customer, equipo_modal.cliente_id)
            if equipo_modal and equipo_modal.cliente_id
            else None
        )
        cuenta_datos = {
            "id": licencia_modal.id,
            "cliente": cliente_modal.nombre if cliente_modal else "Sin verduleria",
            "codigo": (
                format_hardware_code(equipo_modal.hardware_code)
                if equipo_modal
                else "—"
            ),
            "estado": licencia_modal.estado_cobro,
        }

    return _render(
        request,
        "ingresos.html",
        {
            "seccion": "ingresos",
            "filas": filas,
            "resumen": resumen,
            "cambios": cambios,
            "operaciones": listar_operaciones(db),
            "filtros_estado": list(ETIQUETAS_ESTADO.items()),
            "filtros_categoria": list(ETIQUETAS_CATEGORIA.items()),
            "metodos": METODOS_PAGO,
            "estados_cuenta": ESTADOS_CUENTA,
            "categorias_alta": list(ETIQUETAS_CATEGORIA.items()),
            "busqueda": q.strip(),
            "filtro_estado": estado,
            "filtro_categoria": categoria,
            "total": len(filas),
            # Preservado tras un rechazo del alta manual.
            "alta_licencia": registrar,
            "alta_categoria": f_categoria or "venta",
            "alta_importe": f_importe,
            "alta_metodo": f_metodo,
            "alta_estado": f_estado or "pagado",
            "alta_nota": f_nota,
            # Cuenta cuyo modal hay que reabrir tras un rechazo.
            "cuenta_modal": cuenta_datos,
            "precio_venta": PRECIO_VENTA_COP,
            "format_cop": format_cop,
            "format_hardware_code": format_hardware_code,
            "aviso": request.query_params.get("aviso"),
            "error": request.query_params.get("error"),
        },
    )


@pages_router.post("/ingresos")
def ingresos_registrar(
    request: Request,
    licencia_id: str = Form(""),
    categoria: str = Form(""),
    importe: str = Form(""),
    metodo: str = Form(""),
    estado: str = Form(""),
    nota: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-07 · alta manual de un pago contra una licencia/operacion."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    sin_acceso = _sin_acceso_a_dinero(request, user, "Ingresos y cobros")
    if sin_acceso:
        return sin_acceso

    preservado = {
        "registrar": licencia_id,
        "f_categoria": categoria,
        "f_importe": importe,
        "f_metodo": metodo,
        "f_estado": estado,
        "f_nota": nota,
    }

    try:
        cat = parse_categoria(categoria)
        est = parse_estado_pago(estado)
        metodo_pago = parse_metodo(metodo)
        # Para un cobro el importe es obligatorio; las categorias a $0 pueden
        # enviarse vacias y se registran como 0.
        monto = parse_monto(
            importe, requerido=cat not in ("gratuito", "reemplazo")
        )
        if monto is None:
            monto = 0

        ingreso = registrar_ingreso(
            db,
            actor_id=user.id,
            licencia_id=licencia_id,
            categoria=cat,
            importe_cop=monto,
            estado_pago=est,
            metodo_pago=metodo_pago,
            nota=nota,
        )
    except IngresoError as exc:
        db.rollback()
        return _volver(_ingresos_destino(preservado), error=str(exc))

    licencia = db.get(License, ingreso.licencia_id)
    record_event(
        db,
        action="ingreso.registrado",
        user=user,
        entity_type="ingreso",
        entity_id=ingreso.id,
        # En la auditoria van los datos contables, nunca algo sensible.
        detail={
            "licencia_id": ingreso.licencia_id,
            "categoria": ingreso.categoria,
            "monto_cop": int(ingreso.monto_cop),
            "estado_pago": ingreso.estado_pago,
            "metodo_pago": ingreso.metodo_pago,
            "estado_cobro": licencia.estado_cobro if licencia else None,
        },
    )
    db.commit()

    return _volver(
        "/ingresos",
        aviso=(
            f"Ingreso de {format_cop(ingreso.monto_cop)} registrado "
            f"({ingreso.categoria} · {ingreso.estado_pago})."
        ),
    )


@pages_router.post("/ingresos/{licencia_id}/cuenta")
def ingresos_cambiar_cuenta(
    request: Request,
    licencia_id: str,
    estado: str = Form(""),
    importe: str = Form(""),
    metodo: str = Form(""),
    nota: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-07 · actualizar la cuenta de una licencia (modal de la tabla)."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    sin_acceso = _sin_acceso_a_dinero(request, user, "Ingresos y cobros")
    if sin_acceso:
        return sin_acceso

    destino = _ingresos_destino({"cuenta": licencia_id})
    licencia = licencia_de(db, licencia_id)
    if licencia is None:
        return _volver("/ingresos", error="La licencia indicada no existe.")

    try:
        metodo_pago = parse_metodo(metodo)
        monto = parse_monto(importe, requerido=False)
        aplicado = cambiar_estado_cuenta(
            db,
            licencia=licencia,
            estado_deseado=estado,
            actor_id=user.id,
            importe_cop=monto,
            metodo_pago=metodo_pago,
            nota=nota,
        )
    except IngresoError as exc:
        db.rollback()
        return _volver(destino, error=str(exc))

    record_event(
        db,
        action="ingreso.cuenta_actualizada",
        user=user,
        entity_type="licencia",
        entity_id=licencia.id,
        detail={
            "estado_cobro": aplicado,
            "metodo_pago": metodo_pago,
            "monto_cop": int(monto) if monto else None,
        },
    )
    db.commit()

    etiqueta = dict(ESTADOS_CUENTA).get(aplicado, aplicado)
    return _volver(
        "/ingresos",
        aviso=f"Cuenta actualizada: {etiqueta.split(' — ')[0]}. "
        "El cronometro quedo congelado donde corresponde.",
    )


# --------------------------------------------------------------------------- #
# Cierres mensuales (UI-07B, JHO-103)
# --------------------------------------------------------------------------- #
#
# UI-07B es la lectura consolidada de lo que ya paso en UI-07: los tres roles
# la consultan (doc 01, LM-RF-002), pero nadie digita fechas: el mes llega en
# ``?periodo=AAAA-MM`` o se toma el mes en curso. La exportacion deja su
# evento de auditoria como cualquier otra accion de negocio.


def _periodo_valido(texto: str | None, destino: str) -> tuple[date | None, RedirectResponse | None]:
    """Mes elegido por el operador, con el mes en curso como respaldo."""
    if not texto:
        return mes_actual(), None
    try:
        return parse_periodo(texto), None
    except PeriodoError as exc:
        return None, _volver(destino, error=str(exc))


@pages_router.get("/cierres", response_class=HTMLResponse)
def cierres_page(request: Request, db: Session = Depends(get_db_session), periodo: str = ""):
    """UI-07B · desglose mensual, comparativo semestral y cierre contable."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    sin_acceso = _sin_acceso_a_dinero(request, user, "Cierres mensuales")
    if sin_acceso:
        return sin_acceso

    mes, rechazo = _periodo_valido(periodo, "/cierres")
    if rechazo:
        return rechazo

    cierre = resumen_cierre(db, periodo=mes)
    return _render(
        request,
        "cierres.html",
        {
            "seccion": "cierres",
            "cierre": cierre,
            "etiqueta": etiqueta_periodo(mes),
            "periodo_actual": clave_periodo(mes),
            "periodo_anterior": clave_periodo(periodo_anterior(mes)),
            "periodo_siguiente": clave_periodo(periodo_siguiente(mes)),
            "aviso": request.query_params.get("aviso"),
            "error": request.query_params.get("error"),
            "format_cop": format_cop,
            "format_hardware_code": format_hardware_code,
            "format_millones": format_millones,
            "precio_venta": PRECIO_VENTA_COP,
        },
    )


@pages_router.get("/cierres/exportar")
def cierres_exportar(request: Request, db: Session = Depends(get_db_session), periodo: str = ""):
    """UI-07B · descarga del cierre en CSV con BOM UTF-8 (Excel es-CO)."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    sin_acceso = _sin_acceso_a_dinero(request, user, "Cierres mensuales")
    if sin_acceso:
        return sin_acceso

    mes, rechazo = _periodo_valido(periodo, "/cierres")
    if rechazo:
        return rechazo

    cierre = resumen_cierre(db, periodo=mes)
    record_event(
        db,
        action="cierre.exportado",
        user=user,
        entity_type="periodo",
        entity_id=cierre.clave,
        # Nada sensible: solo las cifras del mes que salen del sistema.
        detail={
            "periodo": cierre.clave,
            "total_cop": cierre.total_cop,
            "licencias_n": cierre.licencias_n,
            "movimientos_n": len(cierre.movimientos),
        },
    )
    db.commit()

    nombre = f"cierre-mensual-{cierre.clave}.csv"
    return Response(
        content=exportar_cierre_csv(cierre),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# --------------------------------------------------------------------------- #
# Liquidacion y reparto de utilidades (UI-11, JHO-103) - exclusivo de Titulares
# --------------------------------------------------------------------------- #
#
# Igual que en UI-09, el bloqueo es estricto y visible: el administrador
# delegado ve la tarjeta de acceso restringido con estado 403, no una lista
# vacia ni un redirect que oculte la pantalla.


def _liquidaciones_denegado(request: Request):
    return _render(
        request,
        "liquidaciones.html",
        {"acceso_denegado": True, "filas": [], "aviso": None, "error": None},
        status_code=403,
    )


def _liquidaciones_destino(periodo: date, **extras: str) -> str:
    """URL de vuelta a /liquidaciones conservando el mes y los avisos."""
    destino = f"/liquidaciones?periodo={clave_periodo(periodo)}"
    pares = [f"{clave}={quote(valor, safe='')}" for clave, valor in extras.items() if valor]
    return f"{destino}&{'&'.join(pares)}" if pares else destino


@pages_router.get("/liquidaciones", response_class=HTMLResponse)
def liquidaciones_page(
    request: Request,
    db: Session = Depends(get_db_session),
    periodo: str = "",
    pago: str = "",
):
    """UI-11 · calculadora, estado del periodo y modal de pago de comisiones."""
    user = get_current_user(request, db)
    request.state.user = user
    if user is None:
        return RedirectResponse("/login", status_code=303)

    if not Role(user.rol).is_titular:
        return _liquidaciones_denegado(request)

    mes, rechazo = _periodo_valido(periodo, "/liquidaciones")
    if rechazo:
        return rechazo

    # Ventas del mes por persona: alimentan el prellenado de la calculadora.
    ventas = ventas_del_periodo(db, periodo=mes)
    candidatos = []
    for fila in ventas:
        cuenta = db.get(User, fila.usuario_id)
        candidatos.append(
            {
                "id": fila.usuario_id,
                "nombre": cuenta.nombre_completo if cuenta else "Cuenta eliminada",
                "licencias": fila.licencias_n,
                "monto_cop": fila.monto_cop,
            }
        )

    filas = listar_liquidaciones(db, periodo=mes)
    modal_pago = next((fila for fila in filas if fila.id == pago), None)

    return _render(
        request,
        "liquidaciones.html",
        {
            "seccion": "liquidaciones",
            "acceso_denegado": False,
            "filas": filas,
            "candidatos": candidatos,
            "tipos_comision": TIPOS_COMISION,
            "canales_pago": CANALES_PAGO_ETIQUETA,
            "etiqueta": etiqueta_periodo(mes),
            "periodo_actual": clave_periodo(mes),
            "periodo_anterior": clave_periodo(periodo_anterior(mes)),
            "periodo_siguiente": clave_periodo(periodo_siguiente(mes)),
            # Liquidacion cuyo modal de pago hay que reabrir tras un rechazo
            # o tras el alta ("Aplicar y Pagar").
            "modal_pago": modal_pago,
            "aviso": request.query_params.get("aviso"),
            "error": request.query_params.get("error"),
            "format_cop": format_cop,
            "etiqueta_comision": etiqueta_comision,
            "precio_venta": PRECIO_VENTA_COP,
        },
    )


@pages_router.post("/liquidaciones")
def liquidaciones_registrar(
    request: Request,
    periodo: str = Form(""),
    administrador_id: str = Form(""),
    licencias: str = Form(""),
    tipo: str = Form(""),
    valor: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-11 · crea o recalcula la liquidacion pendiente del mes."""
    user = get_current_user(request, db)
    request.state.user = user
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not Role(user.rol).is_titular:
        return _liquidaciones_denegado(request)

    mes, rechazo = _periodo_valido(periodo, "/liquidaciones")
    if rechazo:
        return rechazo
    destino = _liquidaciones_destino(mes)

    try:
        liquidacion, accion = registrar_liquidacion(
            db,
            actor_id=user.id,
            periodo=mes,
            administrador_id=administrador_id,
            licencias=licencias,
            tipo=tipo,
            valor=valor,
        )
    except LiquidacionError as exc:
        db.rollback()
        return _volver(destino, error=str(exc))

    record_event(
        db,
        action=f"liquidacion.{accion}",
        user=user,
        entity_type="liquidacion",
        entity_id=liquidacion.id,
        # Datos contables puros: ni el canal ni el comprobante existen aun.
        detail={
            "periodo": clave_periodo(liquidacion.periodo),
            "beneficiario_id": liquidacion.administrador_id,
            "licencias": int(liquidacion.licencias_vendidas),
            "tipo_comision": liquidacion.tipo_comision,
            "valor_comision": int(liquidacion.valor_comision),
            "monto_bruto_cop": int(liquidacion.monto_bruto_cop),
            "comision_cop": int(liquidacion.comision_cop),
            "ganancia_neta_cop": int(liquidacion.ganancia_neta_cop),
        },
    )
    db.commit()

    etiqueta = liquidacion_etiqueta(db, liquidacion)
    return _volver(
        _liquidaciones_destino(mes, pago=liquidacion.id),
        aviso=(
            f"Liquidacion creada: {etiqueta}."
            if accion == "creada"
            else f"Liquidacion recalculada: {etiqueta}."
        ),
    )


@pages_router.post("/liquidaciones/{liquidacion_id}/pago")
def liquidaciones_pagar(
    request: Request,
    liquidacion_id: str,
    metodo: str = Form(""),
    comprobante: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-11 · confirma el pago y sella la liquidacion para siempre."""
    user = get_current_user(request, db)
    request.state.user = user
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not Role(user.rol).is_titular:
        return _liquidaciones_denegado(request)

    liquidacion = liquidacion_de(db, liquidacion_id)
    if liquidacion is None:
        return _volver("/liquidaciones", error="La liquidacion indicada no existe.")

    destino = _liquidaciones_destino(liquidacion.periodo)
    try:
        confirmar_pago(
            db,
            liquidacion=liquidacion,
            actor_id=user.id,
            metodo=metodo,
            comprobante=comprobante,
        )
    except LiquidacionError as exc:
        db.rollback()
        # El modal se reabre con la fila pendiente para corregir el dato.
        return _volver(_liquidaciones_destino(liquidacion.periodo, pago=liquidacion.id), error=str(exc))

    record_event(
        db,
        action="liquidacion.pagada",
        user=user,
        entity_type="liquidacion",
        entity_id=liquidacion.id,
        detail={
            "periodo": clave_periodo(liquidacion.periodo),
            "beneficiario_id": liquidacion.administrador_id,
            "comision_cop": int(liquidacion.comision_cop),
            "metodo_pago": liquidacion.metodo_pago,
            "comprobante": liquidacion.nota,
        },
    )
    db.commit()

    etiqueta = liquidacion_etiqueta(db, liquidacion)
    return _volver(
        destino,
        aviso=f"Pago de comision registrado y liquidacion sellada: {etiqueta}.",
    )


@pages_router.get("/liquidaciones/{liquidacion_id}/soporte", response_class=HTMLResponse)
def liquidaciones_soporte(
    request: Request, liquidacion_id: str, db: Session = Depends(get_db_session)
):
    """UI-11 · soporte imprimible de una liquidacion (sellada o pendiente)."""
    user = get_current_user(request, db)
    request.state.user = user
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not Role(user.rol).is_titular:
        return _liquidaciones_denegado(request)

    fila = liquidacion_fila(db, liquidacion_id)
    if fila is None:
        return _volver("/liquidaciones", error="La liquidacion indicada no existe.")

    return _render(
        request,
        "soporte_liquidacion.html",
        {
            "seccion": "liquidaciones",
            "fila": fila,
            "etiqueta": etiqueta_periodo(fila.periodo),
            "format_cop": format_cop,
            "etiqueta_comision": etiqueta_comision,
        },
    )


# --------------------------------------------------------------------------- #
# Modelo mayorista: tecnicos y paquetes prepago (UI-10, JHO-104)
# --------------------------------------------------------------------------- #
#
# Los tres roles consultan el saldo y registran tecnicos (igual que registran
# verdulerias: doc 04 v2.1, seccion 7). La COMPRA de cupos la registran solo
# los Titulares: la seccion 5 les atribuye la recarga de cupos, y es una
# entrada de dinero de la sociedad.


def _mayorista_titular(request: Request, db: Session) -> User | None:
    """Usuario autenticado si es Titular; 403 uniforme si es delegado."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return None
    if not Role(user.rol).is_titular:
        raise HTTPException(
            status_code=403,
            detail=(
                "Solo el Titular Principal y el Co-Titular registran compras de "
                "paquetes mayoristas."
            ),
        )
    return user


@pages_router.get("/mayorista", response_class=HTMLResponse)
def mayorista_page(request: Request, db: Session = Depends(get_db_session), q: str = ""):
    """UI-10 · tecnicos balanceros, paquetes prepago y saldo de cupos."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    # El tecnico ve SOLO su tarjeta y sus paquetes: el directorio completo y los
    # saldos de los demas son informacion de la sociedad (JHO-107).
    if Role(user.rol).is_tecnico:
        propio = tecnico_de_usuario(db, user)
        filas = [fila for fila in listar_tecnicos(db) if propio and fila.id == propio.id]
        return _render(
            request,
            "mayorista.html",
            {
                "seccion": "mayorista",
                "es_tecnico": True,
                "tecnico_propio": propio,
                "tecnicos": filas,
                "paquetes": listar_paquetes(db, tecnico_id=propio.id if propio else None),
                "tipos_paquete": TIPOS_PAQUETE,
                "resumen": resumen_mayorista(db, solo_tecnico_id=propio.id if propio else None),
                "metodos": METODOS_PAGO,
                "busqueda": "",
                "total": len(filas),
                "puede_registrar": False,
                "con_acceso": set(),
                "precio_venta": PRECIO_VENTA_COP,
                "format_cop": format_cop,
                "format_hardware_code": format_hardware_code,
                "aviso": request.query_params.get("aviso"),
                "error": request.query_params.get("error"),
            },
        )

    filas = listar_tecnicos(db, q)
    return _render(
        request,
        "mayorista.html",
        {
            "seccion": "mayorista",
            "es_tecnico": False,
            "tecnico_propio": None,
            "tecnicos": filas,
            "paquetes": listar_paquetes(db),
            "tipos_paquete": TIPOS_PAQUETE,
            "resumen": resumen_mayorista(db),
            "metodos": METODOS_PAGO,
            "busqueda": q.strip(),
            "total": len(filas),
            "puede_registrar": Role(user.rol).is_titular,
            # Tecnicos que ya tienen acceso creado, para ofrecer "Dar acceso".
            "con_acceso": tecnicos_con_acceso(db),
            "precio_venta": PRECIO_VENTA_COP,
            "format_cop": format_cop,
            "format_hardware_code": format_hardware_code,
            "aviso": request.query_params.get("aviso"),
            "error": request.query_params.get("error"),
        },
    )


@pages_router.post("/mayorista/tecnicos")
def mayorista_crear_tecnico(
    request: Request,
    nombre: str = Form(""),
    telefono: str = Form(""),
    email: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-10 · alta de un tecnico balancero."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    # Un tecnico no da de alta a otros tecnicos: eso es de la sociedad.
    sin_acceso = _sin_acceso_a_dinero(request, user, "Mayorista")
    if sin_acceso:
        return sin_acceso

    try:
        tecnico = crear_tecnico(
            db,
            actor_id=user.id,
            nombre=nombre,
            telefono=telefono,
            email=email,
        )
    except MayoristaError as exc:
        db.rollback()
        return _volver("/mayorista", error=str(exc))

    record_event(
        db,
        action="tecnico.creado",
        user=user,
        entity_type="tecnico",
        entity_id=tecnico.id,
        detail={"nombre": tecnico.nombre},
    )
    db.commit()

    return _volver("/mayorista", aviso=f"Tecnico {tecnico.nombre} registrado.")


@pages_router.post("/mayorista/tecnicos/{tecnico_id}/acceso")
def mayorista_dar_acceso(
    request: Request,
    tecnico_id: str,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-10 · da acceso propio a un tecnico, atado a su saldo (Titulares)."""
    user = _mayorista_titular(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    tecnico = tecnico_de(db, tecnico_id)
    if tecnico is None:
        return _volver("/mayorista", error="El tecnico indicado no existe.")

    try:
        nuevo = create_user(
            db,
            actor=user,
            nombre=tecnico.nombre,
            email=email,
            password=password,
            rol=Role.TECNICO.value,
            tecnico_id=tecnico.id,
        )
    except UserManagementError as exc:
        db.rollback()
        return _volver("/mayorista", error=str(exc))

    record_event(
        db,
        action="usuario.creado",
        user=user,
        entity_type="usuario",
        entity_id=nuevo.id,
        # Nunca se registra la contrasena, ni hasheada ni en claro.
        detail={
            "rol": nuevo.rol,
            "email_creado": nuevo.email,
            "tecnico_id": tecnico.id,
            "tecnico_nombre": tecnico.nombre,
        },
    )
    db.commit()

    return _volver(
        "/mayorista",
        aviso=(
            f"Acceso creado para {tecnico.nombre} ({nuevo.email}). Ya puede emitir "
            "licencias con su saldo sin depender de nadie."
        ),
    )


@pages_router.post("/mayorista/tecnicos/{tecnico_id}/alternar")
def mayorista_alternar_tecnico(
    request: Request,
    tecnico_id: str,
    activo: str = Form("0"),
    db: Session = Depends(get_db_session),
):
    """UI-10 · activa/desactiva un tecnico sin borrar su saldo ni su historial."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied
    if not Role(user.rol).is_titular:
        raise HTTPException(
            status_code=403,
            detail="Solo el Titular Principal y el Co-Titular gestionan tecnicos.",
        )

    tecnico = tecnico_de(db, tecnico_id)
    if tecnico is None:
        return _volver("/mayorista", error="El tecnico indicado no existe.")

    set_tecnico_activo(db, tecnico=tecnico, activo=activo == "1")
    record_event(
        db,
        action="tecnico.actualizado",
        user=user,
        entity_type="tecnico",
        entity_id=tecnico.id,
        detail={"nombre": tecnico.nombre, "activo": tecnico.activo},
    )
    db.commit()

    estado = "activado" if tecnico.activo else "desactivado"
    return _volver("/mayorista", aviso=f"Tecnico {tecnico.nombre} {estado}.")


@pages_router.post("/mayorista/paquetes")
def mayorista_registrar_paquete(
    request: Request,
    tecnico_id: str = Form(""),
    tipo: str = Form(""),
    metodo: str = Form(""),
    comprobante: str = Form(""),
    db: Session = Depends(get_db_session),
):
    """UI-10 · registro de la compra de un paquete prepago (Titulares)."""
    user = _mayorista_titular(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    try:
        paquete, ingreso = registrar_paquete(
            db,
            actor_id=user.id,
            tecnico_id=tecnico_id,
            tipo=tipo,
            metodo_pago=metodo,
            comprobante=comprobante,
        )
    except MayoristaError as exc:
        db.rollback()
        return _volver("/mayorista", error=str(exc))

    tecnico = tecnico_de(db, paquete.tecnico_id)
    record_event(
        db,
        action="paquete.registrado",
        user=user,
        entity_type="paquete",
        entity_id=paquete.id,
        detail={
            "tecnico_id": paquete.tecnico_id,
            "tecnico_nombre": tecnico.nombre if tecnico else None,
            "tipo_paquete": paquete.tipo_paquete,
            "licencias": int(paquete.licencias_total),
            "valor_total_cop": int(paquete.valor_total_cop),
            "ingreso_id": ingreso.id,
            "metodo_pago": ingreso.metodo_pago,
        },
    )
    db.commit()

    return _volver(
        "/mayorista",
        aviso=(
            f"{etiqueta_paquete(paquete.tipo_paquete)} registrado para "
            f"{tecnico.nombre if tecnico else 'el tecnico'}: "
            f"{saldo_tecnico(db, paquete.tecnico_id)} cupos disponibles."
        ),
    )


# --------------------------------------------------------------------------- #
# Historial de auditoria inmutable (UI-08, JHO-105)
# --------------------------------------------------------------------------- #
#
# La pantalla NO edita ni borra: el log es append-only, el modelo rechaza toda
# mutacion (``AuditEventInmutable``) y PostgreSQL lo bloquea con un trigger.
# Los tres roles consultan (doc 04 v2.1, seccion 7: el administrador delegado
# tiene "solo lectura basica"); verificar la cadena y dejar esa verificacion
# sellada en el propio log es una accion de custodia de los Titulares.


@pages_router.get("/historial", response_class=HTMLResponse)
def historial_page(
    request: Request,
    db: Session = Depends(get_db_session),
    q: str = "",
    grupo: str = "",
    usuario: str = "",
):
    """UI-08 · pista inmutable de eventos con su verificacion de cadena."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied

    sin_acceso = _sin_acceso_a_dinero(request, user, "Historial de auditoría")
    if sin_acceso:
        return sin_acceso

    eventos = listar_eventos(db, busqueda=q, grupo=grupo, usuario=usuario)
    total = total_eventos(db)
    verificacion = verify_chain(db)

    return _render(
        request,
        "historial.html",
        {
            "seccion": "historial",
            "eventos": eventos,
            "total": total,
            "mostrados": len(eventos),
            "truncado": total > len(eventos),
            "limite": LIMITE_EVENTOS,
            # Verificacion de la cadena de hash calculada en vivo al abrir.
            "verificacion": verificacion,
            "grupos": GRUPOS,
            "operadores": usuarios_con_eventos(db),
            "busqueda": q.strip(),
            "filtro_grupo": grupo,
            "filtro_usuario": usuario,
            "puede_verificar": Role(user.rol).is_titular,
            "aviso": request.query_params.get("aviso"),
            "error": request.query_params.get("error"),
        },
    )


@pages_router.post("/historial/verificar")
def historial_verificar(request: Request, db: Session = Depends(get_db_session)):
    """Recomputa la cadena y sella el resultado en el propio historial."""
    user, denied = _sesion_o_login(request, db)
    if denied:
        return denied
    if not Role(user.rol).is_titular:
        raise HTTPException(
            status_code=403,
            detail="Solo el Titular Principal y el Co-Titular verifican la cadena de auditoria.",
        )

    resultado = verify_chain(db)
    record_event(
        db,
        action="auditoria.verificada",
        user=user,
        entity_type="auditoria",
        entity_id=str(resultado.broken_at) if resultado.broken_at else None,
        detail={
            "ok": resultado.ok,
            "total_events": resultado.total_events,
            "broken_at": resultado.broken_at,
        },
    )
    db.commit()

    if resultado.ok:
        return _volver(
            "/historial",
            aviso=f"Cadena integra: {resultado.total_events} evento(s) verificados.",
        )
    return _volver(
        "/historial",
        error=(
            f"ALERTA: la cadena esta rota en el evento #{resultado.broken_at}. "
            f"{resultado.reason}"
        ),
    )
