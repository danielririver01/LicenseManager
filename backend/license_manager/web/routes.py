"""Rutas web y API de License Manager.

Separacion deliberada:

* ``pages_router`` sirve HTML a los administradores autenticados.
* ``api_router`` expone los puntos de integracion y salud del sistema.

Regla inmutable de JHO-97: ningun punto de este archivo devuelve la llave
privada. El unico dato criptografico publicable es la huella de la llave
publica, que de por si es publica.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
from license_manager.models import Equipment, Income, License, Role, User
from license_manager.security.passwords import (
    PasswordTooWeak,
    hash_password,
    validate_password_strength,
    verify_password,
)
from license_manager.security.sessions import SessionManager
from license_manager.services.audit import record_event
from license_manager.services.users import (
    UserManagementError,
    change_own_password,
    create_user,
    list_operators,
    reset_password,
    rol_etiqueta,
    set_user_active,
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


@pages_router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db_session)):
    if _count_users(db) == 0:
        return RedirectResponse("/configurar", status_code=303)

    user = get_current_user(request, db)
    request.state.user = user
    if user is None:
        return RedirectResponse("/login", status_code=303)

    total_equipos = db.execute(select(func.count(Equipment.id))).scalar_one() or 0
    total_licencias = db.execute(select(func.count(License.id))).scalar_one() or 0
    ingresos_pagados = (
        db.execute(
            select(func.coalesce(func.sum(Income.monto_cop), 0)).where(Income.estado_pago == "pagado")
        ).scalar_one()
        or 0
    )

    return _render(
        request,
        "panel.html",
        {
            "total_equipos": total_equipos,
            "total_licencias": total_licencias,
            "ingresos_pagados_cop": ingresos_pagados,
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
