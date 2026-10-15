"""Formato y firma de licencias FRB1.

Este modulo es el contrato que Frubber POS ya implementa y NO puede cambiar sin
romper la compatibilidad. Referencia normativa en el POS:

* ``FrubberPOS.Domain.Licensing.LicenseKeyFormat``  -> texto y payload canonico.
* ``FrubberPOS.Infrastructure.Licensing.EcdsaLicenseSignatureVerifier`` -> verificacion.

Contrato exacto (copiado del codigo del POS, no de la memoria):

    canonico  = "FRB1|<key_id>|<machine_code>|<issued_date:YYYYMMDD>"   (bytes UTF-8)
    firma     = ECDSA P-256 / SHA-256 sobre ``canonico``, formato IEEE P1363 (r||s),
                exactamente 64 bytes.
    licencia  = f"{canonico}.{base64(firma)}"

El POS valida ademas que ``machine_code`` sean 16 caracteres hexadecimales en
MAYUSCULAS y que la firma pese 64 bytes; cualquier fallo es rechazo cerrado.

Nota sobre P1363: ``cryptography`` entrega firmas DER por defecto. El POS espera
r||s concatenados en 32 bytes cada uno, hay que convertirlo explicitamente.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

SCHEME = "FRB1"
MACHINE_CODE_LENGTH = 16
SIGNATURE_LENGTH_BYTES = 64
_EDITION_DATE_FORMAT = "%Y%m%d"

_MACHINE_CODE_RE = re.compile(r"^[0-9A-F]{16}$")


class Frb1Error(ValueError):
    """Entrada invalida: la licencia no se genera ni se guarda."""


@dataclass(frozen=True)
class ParsedLicense:
    """Licencia FRB1 ya partida en sus partes (espejo del record del POS)."""

    key_id: str
    machine_code: str
    issued_date: date
    signature: bytes


def normalize_machine_code(raw: str) -> str:
    """Normaliza el codigo de equipo a la forma que el POS espera firmar.

    Acepta lo que un tecnico puede pegar desde WhatsApp o desde la pantalla del
    POS: minusculas, espacios y el guion del formato presentado (``7B4F-892E-...``).
    Devuelve los 16 hex en mayusculas. Falla cerrado si el contenido no es hex
    valido o si tiene una longitud distinta de 16.
    """
    if raw is None:
        raise Frb1Error("El codigo de equipo esta vacio.")

    cleaned = "".join(ch for ch in raw.strip().upper() if not ch.isspace() and ch != "-")

    if len(cleaned) != MACHINE_CODE_LENGTH:
        raise Frb1Error(
            f"El codigo de equipo debe tener {MACHINE_CODE_LENGTH} caracteres; se recibieron {len(cleaned)}."
        )
    if not _MACHINE_CODE_RE.match(cleaned):
        raise Frb1Error("El codigo de equipo solo puede contener caracteres hexadecimales (0-9, A-F).")

    return cleaned


def is_valid_machine_code(raw: str) -> bool:
    try:
        normalize_machine_code(raw)
    except Frb1Error:
        return False
    return True


def build_canonical_payload(key_id: str, machine_code: str, issued_date: date) -> str:
    """Payload canónico cubierto por la firma. Debe coincidir byte a byte con el POS."""
    if not key_id:
        raise Frb1Error("El identificador de clave no puede estar vacio.")
    if issued_date is None:
        raise Frb1Error("La fecha de emision no puede estar vacia.")
    return f"{SCHEME}|{key_id}|{machine_code}|{issued_date:{_EDITION_DATE_FORMAT}}"


def _der_to_p1363(der_signature: bytes) -> bytes:
    """Convierte firma DER de OpenSSL al r||s de 64 bytes que espera el POS."""
    r, s = decode_dss_signature(der_signature)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _p1363_to_der(signature_p1363: bytes) -> bytes:
    """Inversa de :func:`_der_to_p1363`.

    ``cryptography`` habla DER de forma nativa, asi que para verificar una
    firma que ya esta en r||s hay que reconstruir el DER equivalente.
    """
    if len(signature_p1363) != SIGNATURE_LENGTH_BYTES:
        raise Frb1Error("La firma debe pesar exactamente 64 bytes.")
    r = int.from_bytes(signature_p1363[:32], "big")
    s = int.from_bytes(signature_p1363[32:], "big")
    return encode_dss_signature(r, s)


def sign_license(
    private_key: ec.EllipticCurvePrivateKey, key_id: str, machine_code: str, issued_date: date
) -> str:
    """Firma y devuelve el texto completo de licencia listo para entregar al cliente."""
    if not isinstance(private_key.curve, ec.SECP256R1):
        raise Frb1Error("La llave de firma debe ser ECDSA P-256 (SECP256R1).")

    normalized_code = normalize_machine_code(machine_code)
    canonical = build_canonical_payload(key_id, normalized_code, issued_date)
    der_signature = private_key.sign(canonical.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    signature_p1363 = _der_to_p1363(der_signature)
    return f"{canonical}.{base64.b64encode(signature_p1363).decode('ascii')}"


def parse_license(text: str) -> ParsedLicense | None:
    """Parte una licencia sin lanzar. Devuelve ``None`` ante cualquier problema de forma.

    Es la contraparte exacta de ``LicenseKeyFormat.TryParse`` del POS: si esta
    funcion la acepta, el POS debe poder aceptarla tambien.
    """
    if not text or not text.strip():
        return None

    parts = text.strip().split(".")
    if len(parts) != 2:
        return None

    payload_parts = parts[0].split("|")
    if len(payload_parts) != 4 or payload_parts[0] != SCHEME:
        return None

    key_id = payload_parts[1]
    machine_code = payload_parts[2]
    if not key_id or not _MACHINE_CODE_RE.match(machine_code):
        return None

    from datetime import datetime

    try:
        issued_date = datetime.strptime(payload_parts[3], _EDITION_DATE_FORMAT).date()
    except ValueError:
        return None

    try:
        signature = base64.b64decode(parts[1], validate=True)
    except (ValueError, base64.binascii.Error):
        return None

    if len(signature) != SIGNATURE_LENGTH_BYTES:
        return None

    return ParsedLicense(
        key_id=key_id, machine_code=machine_code, issued_date=issued_date, signature=signature
    )


def verify_license(
    public_key: ec.EllipticCurvePublicKey, key_id: str, machine_code: str, issued_date: date, signature: bytes
) -> bool:
    """Verificacion de referencia. Se usa en pruebas y para auditar licencias emitidas.

    El POS hace exactamente esto con la llave publica que ya lleva embebida.
    """
    if not key_id or not signature or len(signature) != SIGNATURE_LENGTH_BYTES:
        return False
    try:
        canonical = build_canonical_payload(key_id, machine_code, issued_date)
        public_key.verify(_p1363_to_der(signature), canonical.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, Frb1Error):
        return False
    return True
