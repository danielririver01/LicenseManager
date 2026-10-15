"""Prueba de interoperabilidad contra el C# real de Frubber POS.

No basta con verificar la firma en Python: eso solo prueba que firmamos y
verificamos con nuestra propia logica. La pregunta que importa es si **el POS,
con el codigo que ya esta compilado en la caja del cliente**, acepta la
licencia. Esta prueba ejecuta el verificador C# real de Frubber POS
(``EcdsaLicenseSignatureVerifier``) sobre una licencia firmada por este
servidor.

El arnés C# enlaza los archivos del POS sin copiarlos, asi que si alguien
cambia el formato alla, esta prueba se rompe de inmediato.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from license_manager.crypto.frb1 import sign_license

_INTEROP_DIR = Path(__file__).resolve().parent / "interop" / "Frb1Interop"
_DLL = _INTEROP_DIR / "bin" / "Release" / "net10.0" / "Frb1Interop.dll"
_DOTNET = shutil.which("dotnet")
_frubber_pos_src = Path(
    os.environ.get("FRUBBER_POS_SRC", r"C:\Users\danie\Desktop\01-Proyectos\Frutter POS\src")
)

pytestmark = [
    pytest.mark.skipif(_DOTNET is None, reason="dotnet no esta instalado en esta maquina"),
    pytest.mark.skipif(
        not _frubber_pos_src.is_dir(), reason=f"No se encuentra el codigo del POS en {_frubber_pos_src}"
    ),
]


def _spki_b64(public_key: ec.EllipticCurvePublicKey) -> str:
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode("ascii")


def _verificar_en_pos(licencia: str, key_id: str, public_key_b64: str) -> str:
    result = subprocess.run(
        [_DOTNET, str(_DLL), licencia, key_id, public_key_b64],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def arnes_listo() -> None:
    if _DLL.is_file():
        return
    subprocess.run(
        [
            _DOTNET,
            "build",
            str(_INTEROP_DIR / "Frb1Interop.csproj"),
            "-c",
            "Release",
            f"-p:FrubberPosSrc={_frubber_pos_src}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture
def par_llaves():
    """Par P-256 y su SPKI en Base64, el formato que embebe el POS."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, _spki_b64(private_key.public_key())


def test_el_pos_acepta_una_licencia_emitida_por_el_servidor(arnes_listo, par_llaves):
    """Caso feliz de principio a fin: Python firma, el POS verifica."""
    private_key, spki_b64 = par_llaves
    key_id = "lic-test-interop"

    licencia = sign_license(private_key, key_id, "7B4F892E0C1A5D34", date(2026, 10, 15))

    assert _verificar_en_pos(licencia, key_id, spki_b64) == "VERIFIED"


def test_el_pos_rechaza_una_licencia_firmada_con_otra_llave(arnes_listo, par_llaves):
    """La llave publica que lleva el POS no acepta firmas ajenas."""
    private_key, _ = par_llaves
    llave_ajena = _spki_b64(ec.generate_private_key(ec.SECP256R1()).public_key())

    licencia = sign_license(private_key, "lic-test-interop", "7B4F892E0C1A5D34", date(2026, 10, 15))

    assert _verificar_en_pos(licencia, "lic-test-interop", llave_ajena).startswith("REJECTED")


def test_el_pos_rechaza_una_licencia_con_el_codigo_de_equipo_alterado(arnes_listo, par_llaves):
    """Nadie puede cambiar el equipo de una licencia ajena y que siga valiendo."""
    private_key, spki_b64 = par_llaves
    key_id = "lic-test-interop"

    licencia = sign_license(private_key, key_id, "7B4F892E0C1A5D34", date(2026, 10, 15))
    alterada = licencia.replace("7B4F892E0C1A5D34", "FFFFFFFFFFFFFFFF")

    assert _verificar_en_pos(alterada, key_id, spki_b64).startswith("REJECTED")


def test_el_pos_rechaza_un_key_id_que_no_reconoce(arnes_listo, par_llaves):
    """El POS solo confia en su keyId; emitir con otro es inutil."""
    private_key, spki_b64 = par_llaves

    licencia = sign_license(private_key, "lic-inventado", "7B4F892E0C1A5D34", date(2026, 10, 15))

    assert _verificar_en_pos(licencia, "lic-v1-2026-09-29", spki_b64) == "REJECTED:keyId"


def test_el_pos_rechaza_una_licencia_con_fecha_alterada(arnes_listo, par_llaves):
    """La fecha esta dentro de lo firmado: cambiarla invalida la licencia."""
    private_key, spki_b64 = par_llaves
    key_id = "lic-test-interop"

    licencia = sign_license(private_key, key_id, "7B4F892E0C1A5D34", date(2026, 10, 15))
    alterada = licencia.replace("20261015", "20270101")

    assert _verificar_en_pos(alterada, key_id, spki_b64).startswith("REJECTED")


def test_el_pos_acepta_licencias_con_fechas_distintas(arnes_listo, par_llaves):
    """El formato de fecha YYYYMMDD que emitimos es el que espera el POS."""
    private_key, spki_b64 = par_llaves
    key_id = "lic-test-interop"

    for fecha in (date(2026, 1, 1), date(2026, 12, 31), date(2027, 6, 15)):
        licencia = sign_license(private_key, key_id, "0123456789ABCDEF", fecha)
        assert _verificar_en_pos(licencia, key_id, spki_b64) == "VERIFIED", f"falla con {fecha}"
