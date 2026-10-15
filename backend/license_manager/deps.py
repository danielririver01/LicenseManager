"""Estado compartido de la aplicacion y sus dependencias de FastAPI.

Vive en un modulo aparte de ``app`` y de ``routes`` para evitar el circulo:
ambos importan de aqui, pero ninguno importa del otro a nivel de modulo.

Aqui se concentra la garantia de seguridad de JHO-97: la llave privada ECDSA
solo existe como atributo de este estado en memoria. Ninguna dependencia la
devuelve al llamador; ``get_signing_key`` es la unica puerta y solo la usan los
endpoints que firman.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException

from license_manager.config import Settings
from license_manager.crypto.keys import SigningKey, SigningKeyUnavailable, load_signing_key
from license_manager.db import build_engine as _build_engine
from license_manager.models import User
from license_manager.security.sessions import SessionManager


class ApplicationState:
    """Estado vivo del proceso: BD, sesiones y llave de firma."""

    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.engine = None
        self.session_factory: sessionmaker[Session] | None = None
        self.sessions: SessionManager | None = None
        self.signing_key: SigningKey | None = None
        self.signing_key_error: str | None = None


state = ApplicationState()


def build_engine(settings: Settings):
    """Delegado a ``license_manager.db``: un solo lugar decide el dialecto.

    SQLite necesita ``check_same_thread=False`` porque FastAPI ejecuta los
    endpoints sincronos en un threadpool. Duplicar esta fabrica aqui hizo que
    el servidor real (uvicorn) arrancara con un motor distinto al de la suite.
    """
    return _build_engine(settings)


def load_signing_key_safely(settings: Settings) -> tuple[SigningKey | None, str | None]:
    """Carga la llave sin tumbar el arranque.

    El sitio debe poder levantar para que el titular vea el panel y corrija la
    configuracion; lo que NO debe poder es firmar. La emision falla cerrado.
    """
    try:
        return load_signing_key(settings), None
    except SigningKeyUnavailable as exc:
        return None, str(exc)


def get_signing_key() -> SigningKey:
    """Dependencia para los endpoints que firman. Falla cerrado si no hay llave."""
    if state.signing_key is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "La llave de firma no esta disponible en el servidor. "
                "No es posible emitir licencias hasta configurarla."
            ),
        )
    return state.signing_key


def get_db_session() -> Iterator[Session]:
    """Sesion de base de datos por peticion."""
    if state.session_factory is None:  # pragma: no cover - solo con app mal montada
        raise RuntimeError("La aplicacion no esta inicializada.")
    session = state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_session_manager() -> SessionManager:
    if state.sessions is None:  # pragma: no cover
        raise RuntimeError("La aplicacion no esta inicializada.")
    return state.sessions


def get_current_user(request: Request, db: Session = Depends(get_db_session)) -> User | None:
    """Usuario autenticado a partir de la cookie, o None."""
    manager = get_session_manager()
    cookie = request.cookies.get(manager.cookie_name)
    return manager.resolve(db, cookie)
