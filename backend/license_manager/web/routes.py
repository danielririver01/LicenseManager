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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from license_manager.deps import get_current_user, get_db_session, get_session_manager, state
from license_manager.models import Equipment, Income, License, User
from license_manager.security.passwords import (
    PasswordTooWeak,
    hash_password,
    validate_password_strength,
    verify_password,
)
from license_manager.services.audit import record_event

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
