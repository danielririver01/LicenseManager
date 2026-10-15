"""Genera un par de llaves ECDSA P-256 para License Manager.

Uso::

    python -m license_manager.tools.genkey

Crea ``llave-privada.pem`` y ``llave-publica.pem`` en el directorio actual.

Antes de usar una llave nueva hay que comprobar dos cosas:

1. Que la llave publica se corresponda con la que Frubber POS trae embebida.
   Si se rota la llave sin actualizar el POS, todas las licencias emitidas
   seran rechazadas.
2. Que la privada quede solo en el servidor, con permisos restringidos.
"""

from __future__ import annotations

import base64
import contextlib
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from license_manager.crypto.keys import generate_keypair_pem

_ARCHIVO_PRIVADA = "llave-privada.pem"
_ARCHIVO_PUBLICA = "llave-publica.pem"


def main() -> int:
    privado, publico = generate_keypair_pem()

    destino_privada = Path(_ARCHIVO_PRIVADA)
    destino_publica = Path(_ARCHIVO_PUBLICA)

    if destino_privada.exists() and "--forzar" not in sys.argv:
        print(f"Ya existe {_ARCHIVO_PRIVADA}. Revisa su contenido antes de sobrescribirlo.")
        print("Usa --forzar si de verdad quieres generar otro par.")
        return 1

    destino_privada.write_text(privado, encoding="utf-8")
    destino_publica.write_text(publico, encoding="utf-8")

    with contextlib.suppress(OSError):
        # Windows usa ACLs distintas; el permiso lo aplica el titular en el servidor.
        destino_privada.chmod(0o600)

    spki_b64 = base64.b64encode(
        serialization.load_pem_public_key(publico.encode("utf-8")).public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode("ascii")

    print(f"Privada: {destino_privada.resolve()}")
    print(f"Publica: {destino_publica.resolve()}")
    print()
    print("SubjectPublicKeyInfo (Base64), mismo formato que LicensePublicKeys.cs del POS:")
    print(spki_b64)
    print()
    print("IMPORTANTE: esta publica debe ser la que Frubber POS verifica.")
    print("Si el POS ya tiene otra embebida, emitir con este par rompera el producto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
