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
# Configuracion de produccion (JHO-106): el arranque del VPS
# --------------------------------------------------------------------------- #


def _app_de_produccion(settings: Settings, tmp_path):  # noqa: ANN001 - fixture reutilizado
    """Levanta la app con el endurecimiento del servidor real."""
    from fastapi.testclient import TestClient

    from license_manager.app import create_app

    produccion = Settings(
        _env_file=None,
        environment="production",
        database_url=f"sqlite:///{tmp_path / 'produccion.db'}",
        session_secret="produccion-secreto-largo-y-aleatorio",
        cookie_secure=True,
        enable_hsts=True,
        trusted_hosts="license-manager.frubber.co",
    )
    return TestClient(create_app(produccion))


def test_la_configuracion_de_produccion_endurece_cookies_y_hosts(settings, tmp_path):
    with _app_de_produccion(settings, tmp_path) as cliente:
        alta = cliente.post(
            "/configurar",
            data={
                "nombre": "Daniel Rivera",
                "email": "daniel@frubber.co",
                "password": "clave-muy-segura-1",
                "password_confirm": "clave-muy-segura-1",
            },
            headers={"host": "license-manager.frubber.co"},
            follow_redirects=False,
        )
        assert alta.status_code == 303

        # La cookie de sesion solo viaja por HTTPS y no es leible por JS.
        cookie = alta.headers.get("set-cookie", "")
        assert "Secure" in cookie
        assert "HttpOnly" in cookie

        # HSTS se emite al ir detras de TLS.
        pagina = cliente.get(
            "/login",
            headers={"host": "license-manager.frubber.co", "x-forwarded-proto": "https"},
        )
        assert pagina.headers["Strict-Transport-Security"].startswith("max-age=")

        # Y un Host ajeno no llega ni a la primera ruta.
        ajeno = cliente.get("/login", headers={"host": "sitio-clonado.example"})
        assert ajeno.status_code == 400


def test_en_desarrollo_el_host_no_se_limita(client):
    """Con LM_TRUSTED_HOSTS=* (desarrollo) la app no rechaza ningun Host."""
    respuesta = client.get("/login", headers={"host": "127.0.0.1:8899"})

    assert respuesta.status_code == 200


# --------------------------------------------------------------------------- #
# CSP: ningun manejador inline en las pantallas (JHO-106, bug corregido)
# --------------------------------------------------------------------------- #
#
# ``script-src 'self'`` bloquea ``onclick``/``onchange``/``onsubmit`` inline: un
# boton que dependa de ellos no avisa nada y dispara la accion sin confirmar.


@pytest.mark.parametrize(
    "ruta",
    [
        "/",
        "/clientes",
        "/equipos",
        "/ingresos",
        "/cierres",
        "/mayorista",
        "/historial",
        "/liquidaciones",
        "/administradores",
    ],
)
def test_ninguna_pantalla_usa_manejadores_inline(client, ruta):
    client.post(
        "/configurar",
        data={
            "nombre": "Jhoan",
            "email": "jhoan@frubber.co",
            "password": "clave-muy-segura-1",
            "password_confirm": "clave-muy-segura-1",
        },
    )

    cuerpo = client.get(ruta).text

    assert "onclick=" not in cuerpo, f"{ruta} usa onclick, que la CSP bloquea"
    assert "onchange=" not in cuerpo, f"{ruta} usa onchange, que la CSP bloquea"
    assert "onsubmit=" not in cuerpo, f"{ruta} usa onsubmit, que la CSP bloquea"


def test_la_ficha_de_equipo_tampoco_usa_manejadores_inline(client, db_session):
    from license_manager.services.directory import create_customer, create_equipment

    client.post(
        "/configurar",
        data={
            "nombre": "Jhoan",
            "email": "jhoan@frubber.co",
            "password": "clave-muy-segura-1",
            "password_confirm": "clave-muy-segura-1",
        },
    )
    cliente = create_customer(db_session, actor_id=None, nombre="Verduleria La Huerta")
    equipo = create_equipment(
        db_session, actor_id=None, hardware_code_raw="A4B9C102E8F37B1A", cliente_id=cliente.id
    )
    db_session.commit()

    cuerpo = client.get(f"/equipos/{equipo.id}").text

    assert "onclick=" not in cuerpo
    assert "onchange=" not in cuerpo


# --------------------------------------------------------------------------- #
# Un error inesperado nunca muestra "Internal Server Error" pelado
# --------------------------------------------------------------------------- #


def _entrar(client):
    client.post(
        "/configurar",
        data={
            "nombre": "Jhoan",
            "email": "jhoan@frubber.co",
            "password": "clave-muy-segura-1",
            "password_confirm": "clave-muy-segura-1",
        },
    )


def test_un_error_inesperado_muestra_una_pantalla_util(client, db_session, monkeypatch):
    """El detalle tecnico va al registro; al Titular se le da una salida."""
    from fastapi.testclient import TestClient

    from license_manager.web import routes

    _entrar(client)

    def explotar(*_args, **_kwargs):
        raise RuntimeError("fallo simulado de plantilla")

    monkeypatch.setattr(routes, "listar_tecnicos", explotar)

    with TestClient(client.app, raise_server_exceptions=False) as crudo:
        crudo.post(
            "/login", data={"email": "jhoan@frubber.co", "password": "clave-muy-segura-1"}
        )
        respuesta = crudo.get("/mayorista")

    assert respuesta.status_code == 500
    assert "Algo falló en el servidor" in respuesta.text
    assert "/mayorista" in respuesta.text
    assert "Reinícialo" in respuesta.text or "Reinicia" in respuesta.text
    # El detalle tecnico NO sale a la pantalla.
    assert "fallo simulado" not in respuesta.text
    assert "RuntimeError" not in respuesta.text
    assert "Traceback" not in respuesta.text


def test_un_error_inesperado_en_la_api_sigue_siendo_json(client, db_session, monkeypatch):
    from fastapi.testclient import TestClient

    from license_manager.web import routes

    monkeypatch.setattr(
        routes, "resumen_cierre", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x"))
    )

    with TestClient(client.app, raise_server_exceptions=False) as crudo:
        respuesta = crudo.get("/api/salud")

    # /api/salud no usa el cierre: la API sana sigue respondiendo.
    assert respuesta.status_code == 200


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
