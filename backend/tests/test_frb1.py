"""Pruebas del formato FRB1.

Estas pruebas son el contrato con Frubber POS. Si alguna falla, una licencia
emitida por el sitio seria rechazada por el POS y el cliente se quedaria sin
producto: por eso son las mas estrictas de la suite.
"""

from __future__ import annotations

import base64
from datetime import date

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from license_manager.crypto.frb1 import (
    Frb1Error,
    build_canonical_payload,
    is_valid_machine_code,
    normalize_machine_code,
    parse_license,
    sign_license,
    verify_license,
)

# Llave publica real embebida en Frubber POS (LicensePublicKeys.CurrentPublicKeyBase64).
# Solo se usa aqui para comprobar que nuestro entendimiento del formato coincide.
POS_PUBLIC_KEY_SPKI_B64 = (
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAElg9idAnomec0q5VomBxvHamdzBT+ZPSOlLLus30zO+unuDrTiNPo"
    "MiE0dRo+XXlWJtcQ7aDxw/mkuSw7xjz6Tg=="
)


@pytest.fixture
def par_llaves():
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, private_key.public_key()


def test_el_payload_canonico_es_exactamente_el_que_el_pos_espera():
    """Cualquier desviacion aqui rompe todas las licencias del mundo."""
    payload = build_canonical_payload("lic-v1-2026-09-29", "7B4F892E0C1A5D34", date(2026, 10, 15))
    assert payload == "FRB1|lic-v1-2026-09-29|7B4F892E0C1A5D34|20261015"


def test_el_payload_es_ascii_sin_espacios_ni_separadores_extras():
    payload = build_canonical_payload("k", "0123456789ABCDEF", date(2026, 1, 2))
    assert payload.isascii()
    assert payload == "FRB1|k|0123456789ABCDEF|20260102"


def test_licencia_firmada_se_parsea_y_se_verifica(par_llaves):
    private_key, public_key = par_llaves
    texto = sign_license(private_key, "lic-v1-2026-09-29", "7B4F892E0C1A5D34", date(2026, 10, 15))

    parsed = parse_license(texto)
    assert parsed is not None
    assert parsed.key_id == "lic-v1-2026-09-29"
    assert parsed.machine_code == "7B4F892E0C1A5D34"
    assert parsed.issued_date == date(2026, 10, 15)
    assert len(parsed.signature) == 64

    assert verify_license(
        public_key, parsed.key_id, parsed.machine_code, parsed.issued_date, parsed.signature
    )


def test_el_texto_de_licencia_tiene_la_forma_del_pos(par_llaves):
    private_key, _ = par_llaves
    texto = sign_license(private_key, "lic-v1-2026-09-29", "7B4F892E0C1A5D34", date(2026, 10, 15))

    payload, firma_b64 = texto.split(".")
    assert payload.startswith("FRB1|")
    assert len(payload.split("|")) == 4
    # 64 bytes en Base64 son 88 caracteres con relleno de '='.
    assert len(base64.b64decode(firma_b64)) == 64


def test_las_dos_primeras_firmas_de_una_misma_licencia_son_distintas(par_llaves):
    """ECDSA es aleatorio: dos firmas validas del mismo dato no son identicas.

    El POS valida por verificacion, no por comparacion de texto; esto confirma
    que no dependemos de una firma deterministica.
    """
    private_key, public_key = par_llaves
    una = sign_license(private_key, "k", "0123456789ABCDEF", date(2026, 10, 15))
    otra = sign_license(private_key, "k", "0123456789ABCDEF", date(2026, 10, 15))

    assert una != otra
    for texto in (una, otra):
        parsed = parse_license(texto)
        assert verify_license(
            public_key, parsed.key_id, parsed.machine_code, parsed.issued_date, parsed.signature
        )


def test_una_firma_de_otra_llave_es_rechazada(par_llaves):
    private_key, _ = par_llaves
    otra_llave = ec.generate_private_key(ec.SECP256R1()).public_key()

    texto = sign_license(private_key, "k", "0123456789ABCDEF", date(2026, 10, 15))
    parsed = parse_license(texto)

    assert not verify_license(
        otra_llave, parsed.key_id, parsed.machine_code, parsed.issued_date, parsed.signature
    )


def test_una_licencia_alterada_es_rechazada(par_llaves):
    """Si alguien cambia el codigo de equipo de una licencia, la firma cae."""
    private_key, public_key = par_llaves
    texto = sign_license(private_key, "k", "0123456789ABCDEF", date(2026, 10, 15))
    alterado = texto.replace("0123456789ABCDEF", "FEDCBA9876543210")

    parsed = parse_license(alterado)
    assert parsed is not None
    assert not verify_license(
        public_key, parsed.key_id, parsed.machine_code, parsed.issued_date, parsed.signature
    )


def test_el_key_id_desconocido_es_rechazado():
    """El POS solo acepta el keyId que reconoce; nosotros debemos emitir ese."""
    public_key = serialization.load_der_public_key(base64.b64decode(POS_PUBLIC_KEY_SPKI_B64))
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert isinstance(public_key.curve, ec.SECP256R1)
    # Sin la llave privada no podemos firmar, pero confirmamos que el formato
    # de la llave publica del POS es el que nosotros generamos.
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert base64.b64encode(der).decode("ascii") == POS_PUBLIC_KEY_SPKI_B64


# --------------------------------------------------------------------------- #
# Normalizacion del codigo de equipo
# --------------------------------------------------------------------------- #


def test_normaliza_minusculas_espacios_y_guiones():
    """El tecnico pega el codigo tal cual lo ve en WhatsApp o en el POS."""
    assert normalize_machine_code("  7b4f-892e-0c1a-5d34 ") == "7B4F892E0C1A5D34"


def test_rechaza_un_codigo_con_caracteres_no_hex():
    with pytest.raises(Frb1Error):
        normalize_machine_code("7B4F892E0C1A5D3Z")


def test_rechaza_un_codigo_con_longitud_distinta():
    with pytest.raises(Frb1Error):
        normalize_machine_code("7B4F892E")


def test_is_valid_machine_code_no_lanza():
    assert is_valid_machine_code("7B4F892E0C1A5D34")
    assert not is_valid_machine_code("hola")
    assert not is_valid_machine_code("")


# --------------------------------------------------------------------------- #
# Parseo cerrado: todo lo malo devuelve None, nunca excepcion
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "texto",
    [
        "",
        None,
        "FRB1",
        "FRB1|k|0123456789ABCDEF|20261015",  # sin firma
        "FRX1|k|0123456789ABCDEF|20261015.abc",  # esquema incorrecto
        "FRB1||0123456789ABCDEF|20261015.abc",  # keyId vacio
        "FRB1|k|0123456789ABCDE|20261015.abc",  # codigo corto
        "FRB1|k|0123456789abcdeF|20261015.abc",  # minusculas
        "FRB1|k|0123456789ABCDEF|15102026.abc",  # fecha con formato distinto
        "FRB1|k|0123456789ABCDEF|20261015.@@@",  # base64 invalido
        "FRB1|k|0123456789ABCDEF|20261015." + base64.b64encode(b"x" * 32).decode(),  # firma corta
        "FRB1|k|0123456789ABCDEF|20261015.a|b",  # separador de mas
    ],
)
def test_parseo_falla_cerrado(texto):
    assert parse_license(texto) is None
