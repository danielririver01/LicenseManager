"""Custodia de la llave privada ECDSA P-256 (JHO-97).

Reglas de seguridad que este modulo materializa:

* La llave privada vive SOLO en el entorno del servidor (variable de entorno o
  archivo protegido del sistema operativo). Nunca en el codigo fuente, nunca en
  la base de datos, nunca en el navegador.
* Se carga una unica vez en memoria del proceso backend y no se expone en
  ninguna respuesta HTTP.
* Lo unico que sale de aqui hacia la UI es la huella SHA-256 de la llave PUBLICA,
  que es publica de por si y sirve para verificar la identidad de la clave.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ENV_PRIVATE_KEY_PEM = "LM_ECDSA_PRIVATE_KEY_PEM"
ENV_PRIVATE_KEY_FILE = "LM_ECDSA_PRIVATE_KEY_FILE"


class SigningKeyUnavailable(RuntimeError):
    """La llave privada de firma no esta disponible en el entorno."""


@dataclass(frozen=True)
class SigningKey:
    """Llave privada en memoria junto con su identidad publica verificable."""

    key_id: str
    _private_key: ec.EllipticCurvePrivateKey

    @property
    def private_key(self) -> ec.EllipticCurvePrivateKey:
        """Uso interno exclusivamente: nunca serializar ni loguear."""
        return self._private_key

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._private_key.public_key()

    @property
    def public_key_der_b64(self) -> str:
        """SubjectPublicKeyInfo en Base64. Es el mismo formato que embebe el POS."""
        der = self.public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(der).decode("ascii")

    @property
    def public_fingerprint_sha256(self) -> str:
        """Huella SHA-256 de la llave publica en hexadecimal, para mostrar en pantalla."""
        der = self.public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return hashlib.sha256(der).hexdigest()

    def __repr__(self) -> str:
        # Nunca volcar la llave privada en trazas, logs ni errores.
        return f"<SigningKey key_id={self.key_id!r} fingerprint={self.public_fingerprint_sha256[:16]}...>"


def _parse_pem(pem_text: str, key_id: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(pem_text.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise SigningKeyUnavailable(
            "LM_ECDSA_PRIVATE_KEY_PEM no contiene una llave privada ECDSA PEM valida."
        ) from exc

    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise SigningKeyUnavailable("La llave cargada no es una llave ECDSA.")
    if not isinstance(key.curve, ec.SECP256R1):
        raise SigningKeyUnavailable("La curva debe ser P-256 (SECP256R1 / prime256v1).")
    return key


def load_signing_key(settings) -> SigningKey:
    """Carga la llave privada desde el entorno. Falla cerrado si no existe."""
    key_id = settings.expected_key_id

    pem_text: str | None = None
    source = settings.ecdsa_private_key_pem
    if source:
        pem_text = source.replace("\\n", "\n")
    elif settings.ecdsa_private_key_file:
        path = Path(settings.ecdsa_private_key_file)
        if not path.is_file():
            raise SigningKeyUnavailable(f"LM_ECDSA_PRIVATE_KEY_FILE apunta a {path} y el archivo no existe.")
        pem_text = path.read_text(encoding="utf-8")

    if not pem_text:
        raise SigningKeyUnavailable(
            "No hay llave privada de firma. Define "
            f"{ENV_PRIVATE_KEY_PEM} o {ENV_PRIVATE_KEY_FILE} en el entorno del servidor. "
            "La llave privada correspondiente a la llave publica ya embebida en Frubber POS "
            "es material del titular y no se almacena en el repositorio."
        )

    private_key = _parse_pem(pem_text, key_id)
    return SigningKey(key_id=key_id, _private_key=private_key)


def generate_keypair_pem() -> tuple[str, str]:
    """Genera un par P-256 nuevo. Uso exclusivo del titular para rotar la llave."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem
