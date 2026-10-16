"""Pruebas del chequeo de llave contra el POS (JHO-106).

El caso que estas pruebas protegen es el peor de todos: un servidor con la
llave privada equivocada firma licencias que se ven correctas y que el POS
rechaza todas. El ``keyId`` no lo detecta (es solo una cadena); la huella de la
llave publica si.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import serialization

from license_manager.config import Settings
from license_manager.crypto.keys import generate_keypair_pem
from license_manager.tools import verificar_llave

_CS = """namespace FrubberPOS.Infrastructure.Licensing;

public static class LicensePublicKeys
{{
    public const string CurrentKeyId = "{key_id}";

    public const string CurrentAlgorithm = "ECDSA-P256-SHA256";

    /// <summary>
    /// SubjectPublicKeyInfo (DER) Base64 of the owner's offline public key.
    /// </summary>
    public const string CurrentPublicKeyBase64 =
        "{publica}";
}}
"""


def _spki_b64(pem_privado: str) -> str:
    privada = serialization.load_pem_private_key(pem_privado.encode("utf-8"), password=None)
    der = privada.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode("ascii")


def _escribir_pos(tmp_path, key_id: str, publica_b64: str):
    carpeta = tmp_path / "src" / "FrubberPOS.Infrastructure" / "Licensing"
    carpeta.mkdir(parents=True)
    (carpeta / "LicensePublicKeys.cs").write_text(
        _CS.format(key_id=key_id, publica=publica_b64), encoding="utf-8"
    )
    return tmp_path / "src"


def _settings(tmp_path, privado_pem: str, key_id: str = "lic-v1-2026-09-29") -> Settings:
    archivo = tmp_path / "servidor.pem"
    archivo.write_text(privado_pem, encoding="utf-8")
    return Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'x.db'}",
        session_secret="prueba",
        ecdsa_private_key_file=str(archivo),
        expected_key_id=key_id,
    )


# --------------------------------------------------------------------------- #
# Lectura del POS
# --------------------------------------------------------------------------- #


def test_lee_las_constantes_del_pos(tmp_path):
    privado, _ = generate_keypair_pem()
    publica = _spki_b64(privado)
    src = _escribir_pos(tmp_path, "lic-v1-2026-09-29", publica)

    key_id, leida = verificar_llave.leer_llave_del_pos(src)

    assert key_id == "lic-v1-2026-09-29"
    assert leida == publica, "la constante partida en dos lineas debe leerse completa"


def test_falla_con_un_mensaje_claro_si_no_esta_el_pos(tmp_path):
    with pytest.raises(verificar_llave.VerificacionError):
        verificar_llave.leer_llave_del_pos(tmp_path / "no-existe")


# --------------------------------------------------------------------------- #
# La comprobacion que evita emitir con la llave equivocada
# --------------------------------------------------------------------------- #


def test_detecta_la_llave_correcta(tmp_path, monkeypatch):
    privado, _ = generate_keypair_pem()
    src = _escribir_pos(tmp_path, "lic-v1-2026-09-29", _spki_b64(privado))
    monkeypatch.setattr(verificar_llave, "get_settings", lambda: _settings(tmp_path, privado))

    assert verificar_llave.main(["--pos", str(src), "--quiet"]) == 0


def test_detecta_la_llave_equivocada(tmp_path, monkeypatch, capsys):
    """El caso peligroso: firmaria bien y el POS lo rechazaria todo."""
    privado_pos, _ = generate_keypair_pem()
    privado_servidor, _ = generate_keypair_pem()
    src = _escribir_pos(tmp_path, "lic-v1-2026-09-29", _spki_b64(privado_pos))
    monkeypatch.setattr(
        verificar_llave, "get_settings", lambda: _settings(tmp_path, privado_servidor)
    )

    codigo = verificar_llave.main(["--pos", str(src)])

    salida = capsys.readouterr().out
    assert codigo == 1
    assert "NO COINCIDE" in salida
    assert "RECHAZARIA todas las licencias" in salida


def test_sin_llave_en_el_servidor_avisa(tmp_path, monkeypatch, capsys):
    sin_llave = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'x.db'}",
        session_secret="prueba",
    )
    monkeypatch.setattr(verificar_llave, "get_settings", lambda: sin_llave)

    assert verificar_llave.main(["--esperada", "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE"]) == 1
    assert "No hay llave privada de firma" in capsys.readouterr().err


def test_tambien_acepta_la_llave_esperada_a_mano(tmp_path, monkeypatch):
    privado, _ = generate_keypair_pem()
    monkeypatch.setattr(verificar_llave, "get_settings", lambda: _settings(tmp_path, privado))

    assert verificar_llave.main(["--esperada", _spki_b64(privado), "--quiet"]) == 0


def test_la_huella_es_la_del_der():
    privado, _ = generate_keypair_pem()
    spki = _spki_b64(privado)
    assert verificar_llave.huella_spki_b64(spki) == hashlib.sha256(
        base64.b64decode(spki)
    ).hexdigest()
