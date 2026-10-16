"""Comprueba que la llave del servidor sea la que Frubber POS espera (JHO-106).

Esta herramienta existe por un motivo concreto: el ``keyId`` del texto de la
licencia es solo una CADENA de configuracion. Un servidor con la llave privada
equivocada firma licencias que se ven perfectas y que el POS rechaza TODAS,
sin que nada avise. Este chequeo compara la llave PUBLICA derivada de la llave
privada del servidor contra la que el POS trae embebida.

Uso::

    python -m license_manager.tools.verificar_llave --pos "C:\\ruta\\a\\Frutter POS\\src"
    python -m license_manager.tools.verificar_llave --esperada "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE..."

Sale con codigo 0 si coinciden y 1 si no: sirve para el runbook y para CI.
Nunca lee ni imprime material privado mas alla de derivar la publica.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
from pathlib import Path

from license_manager.config import Settings, get_settings
from license_manager.crypto.keys import SigningKey, SigningKeyUnavailable, load_signing_key

ARCHIVO_POS = "LicensePublicKeys.cs"
RUTA_POS_POR_DEFECTO = Path(r"C:\Users\danie\Desktop\01-Proyectos\Frutter POS\src")


class VerificacionError(RuntimeError):
    """No se pudo leer la llave esperada o no hay llave en el servidor."""


def huella_spki_b64(spki_b64: str) -> str:
    """Huella SHA-256 de una llave publica SPKI en Base64."""
    return hashlib.sha256(base64.b64decode(spki_b64)).hexdigest()


def leer_llave_del_pos(ruta_src: Path | str | None = None) -> tuple[str, str]:
    """Devuelve (keyId, publicaBase64) de ``LicensePublicKeys.cs``."""
    base = Path(ruta_src) if ruta_src else RUTA_POS_POR_DEFECTO
    archivo = base if base.is_file() else base / ARCHIVO_POS
    candidatos = [archivo] if archivo.is_file() else list(base.rglob(ARCHIVO_POS))
    if not candidatos:
        raise VerificacionError(
            f"No encontre {ARCHIVO_POS} en {base}. Indica la carpeta src del POS con --pos."
        )

    texto = candidatos[0].read_text(encoding="utf-8")
    # La constante puede estar repartida en dos lineas: el patron cruza saltos.
    key_id = re.search(r'CurrentKeyId\s*=\s*"([^"]+)"', texto)
    publica = re.search(r'CurrentPublicKeyBase64\s*=\s*\s*"([^"]+)"', texto, re.DOTALL)
    if not key_id or not publica:
        raise VerificacionError(f"No pude leer las constantes de llave en {candidatos[0]}.")
    return key_id.group(1), publica.group(1).replace("\n", "").replace(" ", "")


def comparar(esperada_b64: str, actual_b64: str) -> bool:
    return esperada_b64.strip() == actual_b64.strip()


def _clave_del_servidor(settings: Settings | None = None) -> SigningKey:
    ajustes = settings or get_settings()
    try:
        return load_signing_key(ajustes)
    except SigningKeyUnavailable as exc:
        raise VerificacionError(str(exc)) from exc


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="license_manager.tools.verificar_llave",
        description="Comprueba que la llave de firma corresponda al POS.",
    )
    parser.add_argument("--pos", default=None, help="Carpeta src de Frubber POS.")
    parser.add_argument(
        "--esperada", default=None, help="Llave publica SPKI en Base64 (en vez de leer el POS)."
    )
    parser.add_argument("--quiet", action="store_true", help="Solo imprime el veredicto.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)

    try:
        clave = _clave_del_servidor()
        if args.esperada:
            key_id_esperado, esperada = get_settings().expected_key_id, args.esperada
            origen = "--esperada"
        else:
            key_id_esperado, esperada = leer_llave_del_pos(args.pos)
            origen = str(args.pos or RUTA_POS_POR_DEFECTO)
    except VerificacionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    actual = clave.public_key_der_b64
    coincide = comparar(esperada, actual)

    if not args.quiet:
        print(f"llave esperada     : {origen}")
        print(f"  keyId            : {key_id_esperado}")
        print(f"  huella SHA-256   : {huella_spki_b64(esperada)}")
        print(f"llave del servidor : keyId {clave.key_id}")
        print(f"  huella SHA-256   : {huella_spki_b64(actual)}")
        if key_id_esperado != clave.key_id:
            print()
            print(f"AVISO: el keyId del POS ({key_id_esperado}) no es el del servidor ({clave.key_id}).")
        print()

    if coincide:
        print("OK: la llave del servidor es la que Frubber POS verifica.")
        return 0

    print("NO COINCIDE: el POS RECHAZARIA todas las licencias emitidas con esta llave.")
    print("No emitas en produccion hasta corregirlo (revisa LM_ECDSA_PRIVATE_KEY_FILE).")
    return 1


if __name__ == "__main__":  # pragma: no cover - entrada de consola
    raise SystemExit(main())
