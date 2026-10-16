"""Aplicacion web de License Manager (JHO-97 base + JHO-98 autenticacion).

Punto de entrada y composition root: aqui se conectan configuracion, base de
datos, sesiones, cabeceras de seguridad y el servicio de firma. Ninguna otra
capa accede al entorno directamente.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from license_manager.config import get_settings
from license_manager.db import Base, create_session_factory
from license_manager.deps import build_engine, load_signing_key_safely, state
from license_manager.security.headers import SecurityHeadersMiddleware
from license_manager.security.sessions import SessionManager
from license_manager.web.routes import api_router, pages_router, templates

registro = logging.getLogger("license_manager")


def create_app(settings=None) -> FastAPI:  # noqa: ANN001 - Settings opcional
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        state.settings = settings
        engine = build_engine(settings)
        state.engine = engine
        state.session_factory = create_session_factory(engine)
        state.sessions = SessionManager(settings)
        state.signing_key, state.signing_key_error = load_signing_key_safely(settings)
        initialize_schema(engine)
        yield
        engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    # Produccion: solo el dominio real puede pedir el sitio. Un Host ajeno
    # (ataque de cabecera Host contra cookies o enlaces) se rechaza antes de
    # llegar a cualquier ruta. En desarrollo queda "*" y no se monta nada.
    permitidos = [host.strip() for host in settings.trusted_hosts.split(",") if host.strip()]
    if permitidos and permitidos != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=permitidos)
    app.state.settings = settings

    _mount_static(app)

    app.include_router(pages_router)
    app.include_router(api_router, prefix="/api")

    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _formulario_incompleto)
    app.add_exception_handler(Exception, _error_interno)

    return app


def _mount_static(app: FastAPI) -> None:
    """Sirve el CSS propio. Sin CDNs ni recursos externos: el sitio es privado."""
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def initialize_schema(engine) -> None:
    """Crea el esquema si falta y aplica los refuerzos propios de PostgreSQL.

    ``create_all`` es idempotente: solo crea las tablas que no existen. En
    PostgreSQL se suma ademas el trigger que vuelve la auditoria fisicamente
    inmutable (ver ``migrations/sql/postgres_auditoria_trigger.sql``).
    """
    Base.metadata.create_all(engine)

    if engine.dialect.name == "postgresql":
        _apply_postgres_extras(engine)


def _apply_postgres_extras(engine) -> None:
    # El script vive en backend/migrations, fuera del paquete instalable.
    script = Path(__file__).resolve().parents[2] / "migrations" / "sql" / "postgres_auditoria_trigger.sql"
    if not script.is_file():
        return
    with engine.begin() as connection:
        connection.exec_driver_sql(script.read_text(encoding="utf-8"))


def _http_exception_handler(request: Request, exc: HTTPException):  # noqa: ANN001
    # Rechazo estricto y uniforme para roles no autorizados (UI-09, bloqueo 403).
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail or "No tiene permisos para realizar esta accion."},
    )


def _formulario_incompleto(request: Request, exc: RequestValidationError):  # noqa: ANN001
    """Un formulario al que le falta un campo no puede devolver JSON crudo.

    FastAPI responde 422 con el detalle de validacion, correcto para la API pero
    ilegible para quien acaba de pulsar un boton. En las paginas se vuelve al
    formulario con un aviso; en ``/api`` se mantiene el JSON.
    """
    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=422, content={"detail": jsonable_encoder(exc.errors())}
        )

    campos = sorted(
        {
            str(error.get("loc", ("", ""))[-1])
            for error in exc.errors()
            if error.get("loc")
        }
    )
    mensaje = "Faltan datos obligatorios en el formulario"
    if campos:
        mensaje = f"{mensaje}: {', '.join(campos)}."

    # Se vuelve al origen solo si es del propio sitio: nada de redirigir a un
    # dominio ajeno que venga en la cabecera Referer.
    destino = "/"
    referer = request.headers.get("referer") or ""
    if referer:
        partes = urlparse(referer)
        if not partes.netloc or partes.netloc == request.url.netloc:
            destino = partes.path or "/"

    separador = "&" if "?" in destino else "?"
    return RedirectResponse(f"{destino}{separador}error={quote(mensaje)}", status_code=303)


def _error_interno(request: Request, exc: Exception):  # noqa: ANN001, ARG001
    """Nunca mostrar un "Internal Server Error" pelado al Titular.

    El detalle tecnico va al registro del servidor (que es donde se depura) y la
    pantalla recibe una explicacion util y una salida. Un 500 suele significar
    que el proceso quedo con una version anterior del codigo: reiniciar lo
    resuelve, y la pantalla lo dice.
    """
    registro.exception("Error no controlado en %s %s", request.method, request.url.path)

    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=500,
            content={"detail": "Error interno del servidor.", "ruta": request.url.path},
        )

    try:
        return templates.TemplateResponse(
            request,
            "error_interno.html",
            {
                "request": request,
                "current_user": getattr(request.state, "user", None),
                "app_name": getattr(state.settings, "app_name", "License Manager Frubber"),
                "ruta": request.url.path,
                "seccion": None,
            },
            status_code=500,
        )
    except Exception:  # noqa: BLE001 - si hasta el error falla, se responde plano
        registro.exception("No se pudo pintar la pantalla de error")
        return PlainTextResponse("Error interno del servidor.", status_code=500)


app = create_app()
