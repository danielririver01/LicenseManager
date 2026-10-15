"""Conexion a la base de datos.

Produccion corre sobre PostgreSQL. El desarrollo y la suite usan SQLite con el
mismo esquema, para que nadie necesite un servidor de base de datos para
trabajar. Las migraciones son la fuente de verdad del esquema en ambos casos.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from license_manager.config import Settings


class Base(DeclarativeBase):
    """Base declarativa unica de todo el modelo."""


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def build_engine(settings: Settings) -> Engine:
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if _is_sqlite(settings.database_url):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(settings.database_url, **kwargs)


def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    # SQLite no respeta FOREIGN KEY sin esto; el resto de la suite depende de ello.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def attach_sqlite_pragmas(engine: Engine) -> None:
    if not _is_sqlite(engine.url.render_as_string(hide_password=False)):
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - disparado por el driver
        _enable_sqlite_foreign_keys(dbapi_connection, _record)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def iter_sessions(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
