"""Smoke en vivo de JHO-101 (UI-05, emision de licencias) contra el servidor real.

Recorre lo que ve el operador en pantalla: entrar, abrir "Emitir licencia",
firmar una venta estandar, copiar el bloque FRB1 y comprobar que el equipo
queda registrado con su licencia vitalicia.

Uso:
    ..\\.venv\\Scripts\\python.exe tests/smoke_emision.py [puerto]
"""

from __future__ import annotations

import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899"
EMAIL = "titular@frubber.local"
CLAVE = "Frubber-Dev-2026!"

# Identificadores unicos por corrida para poder repetir el smoke.
SUFIJO = f"{int(time.time()):x}"[-4:].upper()
CODIGO = f"D1E2F3A4B5C6{SUFIJO}"
CODIGO_GUIONES = "-".join(CODIGO[i : i + 4] for i in range(0, 16, 4))
NOMBRE_NEGOCIO = f"Smoke Emision La Ceiba {SUFIJO}"


def main() -> int:
    sesion = requests.Session()

    r = sesion.post(
        f"{BASE}/login",
        data={"email": EMAIL, "password": CLAVE},
        allow_redirects=False,
        timeout=10,
    )
    assert r.status_code == 303, f"login rechazado: {r.status_code}"

    # --- Preparar un equipo: verduleria + codigo de hardware ---------------
    r = sesion.post(
        f"{BASE}/clientes",
        data={"nombre": NOMBRE_NEGOCIO, "propietario": "Prueba Automatica"},
        allow_redirects=False,
        timeout=10,
    )
    assert "error=" not in r.headers["location"], r.headers["location"]

    r = sesion.post(
        f"{BASE}/equipos",
        data={"hardware_code": CODIGO_GUIONES, "cliente_id": "", "notas": "smoke-emision"},
        allow_redirects=False,
        timeout=10,
    )
    assert "error=" not in r.headers["location"], r.headers["location"]
    ficha_url = r.headers["location"].split("?")[0]
    equipo_id = ficha_url.rsplit("/", 1)[-1]
    print(f"[1] Preparado el equipo {CODIGO_GUIONES}")

    # --- UI-05: el formulario arranca limpio -------------------------------
    r = sesion.get(f"{BASE}/emitir?equipo={equipo_id}", timeout=10)
    assert r.status_code == 200, f"/emitir no carga: {r.status_code}"
    assert "Datos de la emision" in r.text
    assert 'value="450' not in r.text, "el importe venia prellenado"
    assert CODIGO_GUIONES in r.text, "el equipo indicado no aparecio seleccionado"
    assert "Firmar licencia" in r.text
    print("[2] UI-05: formulario limpio con el equipo preseleccionado")

    # --- Emitir la venta estandar -----------------------------------------
    r = sesion.post(
        f"{BASE}/emitir",
        data={"equipo_id": equipo_id, "tipo": "comercial", "importe": "450.000"},
        allow_redirects=False,
        timeout=10,
    )
    assert r.status_code == 303, f"emision: {r.status_code}"
    assert "error=" not in r.headers["location"], r.headers["location"]

    query = parse_qs(urlparse(r.headers["location"]).query)
    licencia_id = query["licencia"][0]

    r = sesion.get(f"{BASE}/emitir?licencia={licencia_id}", timeout=10)
    assert r.status_code == 200
    m = re.search(r"<pre id=\"texto-licencia\"[^>]*>\s*(FRB1\|[^\s<]+)", r.text)
    assert m, "la pantalla no muestra el bloque de activacion"
    texto = m.group(1)
    assert texto.startswith("FRB1|"), "el bloque no arranca con el esquema FRB1"
    assert f"|{CODIGO}|" in texto, "el payload firmado no corresponde al equipo"
    assert 'data-copiar="texto-licencia"' in r.text, "falta el boton de copia"
    assert "$450.000 COP" in r.text, "el importe de la venta no se muestra"
    print("[3] Licencia firmada y visible, lista para copiar en un clic")
    print(f"    {texto[:64]}...")

    # --- La ficha del equipo refleja la emision ---------------------------
    r = sesion.get(f"{BASE}{ficha_url}", timeout=10)
    assert texto in r.text, "la ficha no muestra el texto FRB1"
    assert "Licencia activa / emitida" in r.text
    assert "Vitalicia (POS offline)" in r.text
    assert "Equipo registrado en el inventario" in r.text
    print("[4] UI-06: la ficha del equipo muestra la licencia y la bitacora")

    # --- Un equipo no acumula dos licencias vigentes -----------------------
    from urllib.parse import unquote

    r = sesion.post(
        f"{BASE}/emitir",
        data={"equipo_id": equipo_id, "tipo": "reemplazo", "importe": "0"},
        allow_redirects=False,
        timeout=10,
    )
    assert "ya tiene una licencia vigente" in unquote(r.headers["location"]), "se emitio dos veces"

    # En el selector el equipo queda deshabilitado.
    r = sesion.get(f"{BASE}/emitir", timeout=10)
    assert "ya tiene licencia vigente" in r.text
    print("[5] Regla cumplida: una sola licencia vitalicia por maquina")

    sesion.post(f"{BASE}/logout", allow_redirects=False, timeout=10)
    print("\nSMOKE JHO-101 OK - emision, firma, copia y regla de unicidad funcionaron.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
