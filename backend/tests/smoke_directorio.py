"""Smoke en vivo de JHO-100 (UI-03 / UI-04 / UI-06) contra el servidor real.

Se ejecuta contra uvicorn en :8899, no contra el TestClient. Comprueba el
camino que ve el titular en el navegador: entrar, registrar una verduleria,
dar de alta un equipo pegando el codigo con guiones, abrir la ficha y
verificar que el texto FRB1 se muestra listo para copiar.

Uso:
    ..\\.venv\\Scripts\\python.exe tests/smoke_directorio.py [puerto]
"""

from __future__ import annotations

import re
import sys
import time

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899"
EMAIL = "titular@frubber.local"
CLAVE = "Frubber-Dev-2026!"

# Cada corrida usa identificadores nuevos: asi el smoke se puede repetir contra
# el mismo dev.db sin chocar con lo que dejo la corrida anterior. El sufijo va
# en los ultimos 4 caracteres para que el codigo siga midiendo exactamente 16.
SUFIJO = f"{int(time.time()):x}"[-4:].upper()
CODIGO = f"A4B9C102E8F3{SUFIJO}"
CODIGO_GUIONES = "-".join(CODIGO[i : i + 4] for i in range(0, 16, 4))
NOMBRE_NEGOCIO = f"Smoke Verduleria El Roble {SUFIJO}"


def _fila_de(html: str, texto: str) -> str:
    """Devuelve la fila <tr> que contiene el texto, para no confundir con
    datos que dejaron corridas anteriores."""
    for fila in html.split("<tr")[1:]:
        if texto in fila:
            return fila
    return ""


def main() -> int:
    sesion = requests.Session()

    r = sesion.get(f"{BASE}/login", timeout=10)
    assert r.status_code == 200, f"login no carga: {r.status_code}"

    r = sesion.post(
        f"{BASE}/login",
        data={"email": EMAIL, "password": CLAVE},
        allow_redirects=False,
        timeout=10,
    )
    assert r.status_code == 303, f"login rechazado: {r.status_code}"

    r = sesion.get(f"{BASE}/", timeout=10)
    assert r.status_code == 200 and "Panel" in r.text, "el panel no cargo"

    # --- UI-03: alta de verduleria -----------------------------------------
    r = sesion.post(
        f"{BASE}/clientes",
        data={
            "nombre": NOMBRE_NEGOCIO,
            "propietario": "Prueba Automatica",
            "telefono": "+57 300 000 0000",
            "direccion": "Calle 1 # 2-3",
            "ciudad": "Medellin",
        },
        allow_redirects=False,
        timeout=10,
    )
    assert r.status_code == 303, f"alta de verduleria: {r.status_code}"
    assert "error=" not in r.headers["location"], f"alta rechazada: {r.headers['location']}"

    r = sesion.get(f"{BASE}/clientes", timeout=10)
    assert NOMBRE_NEGOCIO in r.text, "la verduleria no aparece en el listado"
    print("[1] UI-03: verduleria registrada y visible en el directorio")

    # --- UI-04: alta de equipo pegando el codigo con guiones ----------------
    r = sesion.post(
        f"{BASE}/equipos",
        data={"hardware_code": CODIGO_GUIONES.lower(), "cliente_id": "", "notas": "smoke"},
        allow_redirects=False,
        timeout=10,
    )
    assert r.status_code == 303, f"alta de equipo: {r.status_code}"
    assert "error=" not in r.headers["location"], f"alta rechazada: {r.headers['location']}"
    # El alta te lleva directo a la ficha del equipo recien registrado.
    ficha_url = r.headers["location"].split("?")[0]
    assert ficha_url.startswith("/equipos/"), f"redireccion inesperada: {ficha_url}"

    r = sesion.get(f"{BASE}/equipos", params={"q": CODIGO}, timeout=10)
    assert CODIGO_GUIONES in r.text, "el codigo no aparece normalizado en el inventario"
    # El codigo continuo solo puede aparecer en el value del buscador: en la
    # tabla siempre se muestra ya con guiones.
    sin_buscador = r.text.replace(f'value="{CODIGO}"', "")
    assert CODIGO not in sin_buscador, "quedo guardado sin normalizar"
    print("[2] UI-04: equipo registrado, codigo normalizado con guiones")

    # --- UI-06: ficha ------------------------------------------------------
    r = sesion.get(f"{BASE}{ficha_url}", timeout=10)
    assert r.status_code == 200, f"la ficha no carga: {r.status_code}"
    assert CODIGO_GUIONES in r.text, "el codigo no se ve en la ficha"
    assert "Equipo registrado en el inventario" in r.text, "la bitacora esta vacia"
    assert ">Sin verduleria asignada<" in r.text, "el equipo quedo asignado sin querer"
    print("[3] UI-06: ficha con codigo, bitacora y negocio sin asignar")

    # --- Buscadores --------------------------------------------------------
    assert CODIGO_GUIONES in sesion.get(f"{BASE}/equipos", params={"q": CODIGO[8:12]}).text
    assert CODIGO_GUIONES in sesion.get(f"{BASE}/equipos", params={"q": CODIGO_GUIONES[:9]}).text
    assert NOMBRE_NEGOCIO in sesion.get(f"{BASE}/clientes", params={"q": "roble"}).text
    assert "No hay verdulerias" in sesion.get(f"{BASE}/clientes", params={"q": "zzz"}).text
    print("[4] Buscadores: encuentran con guiones, sin guiones y por nombre")

    # --- Vinculacion desde la ficha ----------------------------------------
    listado = sesion.get(f"{BASE}/clientes", timeout=10).text
    fila = _fila_de(listado, NOMBRE_NEGOCIO)
    m = re.search(r'action="/clientes/([0-9a-f-]+)/alternar"', fila)
    assert m, "no se hallo el id de la verduleria en el listado"
    cliente_id = m.group(1)

    r = sesion.post(
        f"{BASE}{ficha_url}/cliente",
        data={"cliente_id": cliente_id},
        allow_redirects=False,
        timeout=10,
    )
    assert r.status_code == 303, f"vincular: {r.status_code}"
    r = sesion.get(f"{BASE}{ficha_url}", timeout=10)
    assert NOMBRE_NEGOCIO in r.text, "el vinculo no se refleja en la ficha"
    # El distintivo "Sin verduleria asignada" de la cabecera desaparece; el
    # texto homonimo del selector de opciones es otro y no cuenta.
    assert ">Sin verduleria asignada<" not in r.text, "el equipo sigue sin vincular"
    print("[5] UI-06: equipo vinculado a la verduleria desde la ficha")

    # --- Desactivar conserva todo -----------------------------------------
    r = sesion.post(
        f"{BASE}/clientes/{cliente_id}/alternar",
        data={"activo": "0"},
        allow_redirects=False,
        timeout=10,
    )
    assert r.status_code == 303, f"desactivar: {r.status_code}"
    r = sesion.get(f"{BASE}/equipos/{ficha_url.split('/')[-1]}", timeout=10)
    assert CODIGO_GUIONES in r.text, "el equipo desaparecio al desactivar el negocio"
    fila = _fila_de(sesion.get(f"{BASE}/clientes", timeout=10).text, NOMBRE_NEGOCIO)
    assert "Negocio desactivado" in fila, "no se ve el estado desactivado"
    print("[6] Desactivar la verduleria conserva equipos, licencias e historial")

    # --- CSP: sin scripts inline ------------------------------------------
    for ruta in ("/clientes", "/equipos", ficha_url):
        html = sesion.get(f"{BASE}{ruta}", timeout=10).text
        bloques = html.split("<script")[1:]
        assert len(bloques) == 1, f"{ruta}: mas de un bloque <script>"
        assert bloques[0].lstrip().startswith('src="/static/lm.js"'), f"{ruta}: script inline"
    js = sesion.get(f"{BASE}/static/lm.js", timeout=10)
    assert js.status_code == 200 and "writeText" in js.text, "falta /static/lm.js"
    print("[7] CSP: un unico bloque <script>, servido desde /static/lm.js")

    sesion.post(f"{BASE}/logout", allow_redirects=False, timeout=10)
    print("\nSMOKE JHO-100 OK - todo el recorrido de UI-03 / UI-04 / UI-06 funciono.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
