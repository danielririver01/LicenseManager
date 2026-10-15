"""Hash de contraseñas con Argon2id (JHO-98).

Argon2id es el ganador del Password Hashing Competition y lo pide el issue
expresamente. Los hashes guardan sus parametros junto al digest, de modo que se
pueda endurecer el coste en el futuro sin invalidar las cuentas existentes.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Parametros por defecto de argon2-cffi (RFC 9106, perfil recomendado).
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

MIN_PASSWORD_LENGTH = 10


class PasswordTooWeak(ValueError):
    """La contraseña no cumple la politica minima."""


def hash_password(plain_password: str) -> str:
    return _hasher.hash(plain_password)


def verify_password(stored_hash: str, plain_password: str) -> bool:
    """Devuelve False ante cualquier problema, sin lanzar al llamador."""
    if not stored_hash or not plain_password:
        return False
    try:
        return _hasher.verify(stored_hash, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True si el hash usa parametros mas debiles que los actuales."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def validate_password_strength(plain_password: str) -> None:
    """Politica minima de contrasena. Lanza PasswordTooWeak si no cumple."""
    if len(plain_password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooWeak(f"La contrasena debe tener al menos {MIN_PASSWORD_LENGTH} caracteres.")
    if plain_password.lower() in {"contrasena", "password", "frubber", "1234567890"}:
        raise PasswordTooWeak("La contrasena es demasiado comun. Elige otra.")
