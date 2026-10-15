"""Aplicacion web de License Manager (JHO-97 base + JHO-98 autenticacion).

Punto de entrada y composition root: aqui se conectan configuracion, base de
datos, sesiones, cabeceras de seguridad y el servicio de firma. Ninguna otra
capa accede al entorno directamente.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from license_manager.config import get_settings
from license_manager.db import Base, create_session_factory
from license_manager.deps import build_engine, load_signing_key_safely, state
from license_manager.security.headers import SecurityHeadersMiddleware
from license_manager.security.sessions import SessionManager
from license_manager.web.routes import api_router, pages_router


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
    app.state.settings = settings

    _mount_static(app)

    app.include_router(pages_router)
    app.include_router(api_router, prefix="/api")

    app.add_exception_handler(HTTPException, _http_exception_handler)

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


app = create_app()
