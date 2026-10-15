"""Pruebas de seguridad del servidor (JHO-97).

Cubren los criterios de aceptacion del issue: cabeceras de seguridad (HSTS,
CSP), y sobre todo que la llave privada ECDSA jamas aparezca en ninguna
respuesta al navegador.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization

from license_manager.config import Settings
from license_manager.crypto.keys import SigningKey, SigningKeyUnavailable, load_signing_key


def _pem_privado(key: SigningKey) -> str:
    return key.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _settings_desde_entorno() -> Settings:
    """Reconstruye Settings para que pydantic relea el entorno ya modificado."""
    return Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# Cabeceras de seguridad
# --------------------------------------------------------------------------- #


def test_toda_respuesta_lleva_cabeceras_de_seguridad(client):
    response = client.get("/login")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "object-src 'none'" in response.headers["Content-Security-Policy"]


def test_las_paginas_del_negocio_no_se_cachean(client):
    client.post(
        "/configurar",
        data={
            "nombre": "Jhoan",
            "email": "jhoan@frubber.co",
            "password": "clave-muy-segura-1",
            "password_confirm": "clave-muy-segura-1",
        },
    )

    response = client.get("/")
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]


def test_no_hay_hsts_en_desarrollo_por_http_plano(settings, app, client):
    """Emitir HSTS en HTTP dejaria el sitio inaccesible hasta que expire."""
    response = client.get("/login")
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_se_emite_cuando_el_sitio_corre_tras_tls(settings, app, client):
    settings.enable_hsts = True
    response = client.get("/login", headers={"x-forwarded-proto": "https"})

    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")


def test_no_hay_documentacion_publica_de_la_api(client):
    """El sitio es privado: la referencia de endpoints no debe estar expuesta."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404


# --------------------------------------------------------------------------- #
# La llave privada jamas sale del servidor
# --------------------------------------------------------------------------- #


def test_el_estado_del_sistema_no_expone_la_llave_privada(client):
    response = client.get("/api/estado")
    body = response.text

    assert response.status_code == 200
    assert "BEGIN PRIVATE KEY" not in body
    assert "llave_privada" not in body


def test_el_estado_del_sistema_publica_solo_la_llave_publica(client, signing_key):
    response = client.get("/api/estado")
    data = response.json()

    assert data["firma_disponible"] is True
    assert data["key_id"] == signing_key.key_id
    assert data["llave_publica_sha256"] == signing_key.public_fingerprint_sha256
    assert data["llave_publica_spki_base64"] == signing_key.public_key_der_b64


def test_ninguna_respuesta_contiene_la_llave_privada(client, signing_key):
    """Barrido: ni el HTML de las paginas ni las respuestas JSON filtran la llave."""
    pem = _pem_privado(signing_key)
    clave_desnuda = (
        pem.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "").strip()
    )

    client.post(
        "/configurar",
        data={
            "nombre": "Jhoan",
            "email": "jhoan@frubber.co",
            "password": "clave-muy-segura-1",
            "password_confirm": "clave-muy-segura-1",
        },
    )

    for path in ("/", "/login", "/api/salud", "/api/estado"):
        body = client.get(path).text
        assert pem not in body, f"La llave privada aparece en {path}"
        assert clave_desnuda not in body, f"La llave privada aparece en {path}"


def test_las_variables_de_entorno_de_llave_no_llegan_a_las_respuestas(client, monkeypatch):
    """Aunque el entorno tenga la llave en texto plano, no se refleja."""
    import os

    monkeypatch.setenv("LM_ECDSA_PRIVATE_KEY_PEM", "MATERIAL-SECRETO-DE-PRUEBA")
    body = client.get("/api/estado").text
    assert "MATERIAL-SECRETO-DE-PRUEBA" not in body
    assert os.environ.get("LM_ECDSA_PRIVATE_KEY_PEM") is not None


# --------------------------------------------------------------------------- #
# Fallo cerrado sin llave
# --------------------------------------------------------------------------- #


def test_sin_llave_el_sitio_levanta_pero_no_afirma(client):
    """El titular debe poder entrar a corregir la configuracion, pero no firmar."""
    from license_manager.deps import state

    state.signing_key = None
    state.signing_key_error = "falta la llave"

    salud = client.get("/api/salud")
    assert salud.status_code == 200
    assert salud.json()["firma_disponible"] is False

    estado = client.get("/api/estado")
    assert estado.status_code == 503
    assert estado.json()["firma_disponible"] is False


def test_cargar_llave_desde_entorno_falla_cerrado(monkeypatch):
    monkeypatch.delenv("LM_ECDSA_PRIVATE_KEY_PEM", raising=False)
    monkeypatch.delenv("LM_ECDSA_PRIVATE_KEY_FILE", raising=False)

    with pytest.raises(SigningKeyUnavailable, match="LM_ECDSA_PRIVATE_KEY_PEM"):
        load_signing_key(_settings_desde_entorno())


def test_cargar_llave_mala_falla_cerrado(monkeypatch):
    monkeypatch.setenv("LM_ECDSA_PRIVATE_KEY_PEM", "esto no es una llave")

    with pytest.raises(SigningKeyUnavailable, match="PEM valida"):
        load_signing_key(_settings_desde_entorno())


def test_cargar_llave_de_otra_curva_es_rechazada(monkeypatch):
    """P-384 no sirve: el POS solo verifica P-256."""
    from cryptography.hazmat.primitives.asymmetric import ec

    llave_p384 = ec.generate_private_key(ec.SECP384R1())
    pem = llave_p384.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    monkeypatch.setenv("LM_ECDSA_PRIVATE_KEY_PEM", pem)

    with pytest.raises(SigningKeyUnavailable, match="P-256"):
        load_signing_key(_settings_desde_entorno())


def test_cargar_llave_desde_archivo(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec

    monkeypatch.delenv("LM_ECDSA_PRIVATE_KEY_PEM", raising=False)
    archivo = tmp_path / "llave.pem"
    archivo.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv("LM_ECDSA_PRIVATE_KEY_FILE", str(archivo))

    settings = _settings_desde_entorno()
    key = load_signing_key(settings)

    assert key.key_id == settings.expected_key_id
    # El repr nunca debe volcar la llave.
    assert "PRIVATE KEY" not in repr(key)


def test_el_endpoint_de_salud_no_delata_si_hay_llave_real(client):
    """La salud informa disponibilidad, no identidad criptografica."""
    body = client.get("/api/salud").json()
    assert set(body.keys()) == {"estado", "firma_disponible"}


def test_el_spki_publicado_coincide_con_el_que_embebe_el_pos(client):
    """La llave publica que publicamos debe ser parseable como P-256 SPKI."""
    from cryptography.hazmat.primitives.asymmetric import ec

    spki = client.get("/api/estado").json()["llave_publica_spki_base64"]
    key = serialization.load_der_public_key(base64.b64decode(spki))

    assert isinstance(key, ec.EllipticCurvePublicKey)
    assert isinstance(key.curve, ec.SECP256R1)
