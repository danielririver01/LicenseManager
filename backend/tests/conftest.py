"""Ajustes comunes de la suite.

Cada test crea su propia base SQLite en memoria y su propio par ECDSA, de modo
que las pruebas son independientes y ninguna puede firmar con una llave de
otro test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from license_manager.app import create_app
from license_manager.config import Settings, reset_settings_cache
from license_manager.crypto.keys import SigningKey
from license_manager.deps import state
from license_manager.models import User


@pytest.fixture
def settings(tmp_path) -> Settings:
    reset_settings_cache()
    return Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        session_secret="test-secret-not-used-in-production",
        cookie_secure=False,
        enable_hsts=False,
    )


@pytest.fixture
def signing_key() -> SigningKey:
    """Par ECDSA P-256 efimero, solo para la prueba en curso."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    return SigningKey(key_id="lic-test-2026-10-15", _private_key=private_key)


@pytest.fixture
def app(settings, signing_key):
    application = create_app(settings)
    yield application


@pytest.fixture
def client(app, signing_key) -> Iterator[TestClient]:
    # El lifespan de la app ya crea el esquema y carga la configuracion.
    with TestClient(app) as test_client:
        # La llave de firma se inyecta aqui: las pruebas nunca tocan la real.
        state.signing_key = signing_key
        state.signing_key_error = None
        test_client.cookies.clear()
        yield test_client


@pytest.fixture
def db_session(client) -> Iterator[Session]:
    """Sesion directa a la MISMA base que usa el cliente, para preparar datos."""
    factory: sessionmaker[Session] = state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def titular(db_session) -> User:
    """Titular Principal ya creado, para tests que necesitan una cuenta."""
    return crear_titular(db_session)


def crear_titular(db: Session, email: str = "titular@frubber.co", password: str = "clave-segura-123"):
    """Helper: crea un Titular Principal ya hasheado."""
    from license_manager.models import User
    from license_manager.security.passwords import hash_password

    user = User(
        nombre_completo="Jhoan Rivera",
        email=email,
        password_hash=hash_password(password),
        rol="owner",
        activo=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def iniciar_sesion(client: TestClient, email: str, password: str) -> None:
    response = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert response.status_code == 303, f"Login inesperado: {response.status_code} {response.text}"
