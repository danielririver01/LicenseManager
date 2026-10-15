"""Prueba del flujo HTTP completo contra el servidor real (uvicorn).

No usa TestClient: habla por TCP contra http://127.0.0.1:8899 para verificar
que lo que ve el dueno es lo que la suite dice que funciona.
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8899"
PASSWORD = "Frubber-Dev-2026!"


def esperar_servidor(client: httpx.Client, timeout: float = 30.0) -> bool:
    limite = time.time() + timeout
    while time.time() < limite:
        try:
            if client.get(f"{BASE}/api/salud").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    fallos: list[str] = []
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        if not esperar_servidor(client):
            print("FALLO: el servidor no respondio en /api/salud")
            return 1

        # 1. Estado general: la llave de desarrollo debe estar cargada.
        estado = client.get(f"{BASE}/api/estado")
        print(f"/api/estado -> {estado.status_code}")
        if estado.status_code != 200:
            fallos.append(f"/api/estado devolvio {estado.status_code}: {estado.text[:300]}")

        # 2. Raiz sin sesion: debe mandar a /configurar si no hay usuarios.
        raiz = client.get(f"{BASE}/")
        print(f"/ -> {raiz.status_code} {raiz.headers.get('location', '')}")
        if raiz.status_code not in (200, 303):
            fallos.append(f"/ devolvio {raiz.status_code}")

        destino = raiz.headers.get("location", "/configurar")
        configurar = client.get(BASE + destino)
        print(f"{destino} -> {configurar.status_code}")
        if configurar.status_code != 200:
            fallos.append(f"{destino} devolvio {configurar.status_code}")
        elif "Titular Principal" not in configurar.text:
            fallos.append("La pagina de configuracion no muestra el formulario del Titular.")
        else:
            print("   -> formulario del Titular Principal visible")

        # 3. Crear el Titular.
        creado = client.post(
            f"{BASE}/configurar",
            data={
                "nombre": "Jhoan Rivera",
                "email": "titular@frubber.local",
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            },
        )
        print(f"POST /configurar -> {creado.status_code} {creado.headers.get('location', '')}")
        if creado.status_code not in (303, 200):
            fallos.append(f"POST /configurar devolvio {creado.status_code}: {creado.text[:300]}")

        # 4. Con la sesion ya emitida, el panel debe renderizar.
        panel = client.get(f"{BASE}/")
        print(f"/ (con sesion) -> {panel.status_code}")
        if panel.status_code != 200:
            fallos.append(f"El panel devolvio {panel.status_code}: {panel.text[:300]}")
        else:
            for marcador in ("Panel", "Licencias", "Equipos"):
                if marcador not in panel.text:
                    fallos.append(f"El panel no contiene '{marcador}'.")
            print(f"   -> panel renderizado ({len(panel.text)} bytes)")

        # 5. Cerrar sesion y confirmar que vuelve a pedir login.
        cerrar = client.post(f"{BASE}/logout")
        print(f"POST /logout -> {cerrar.status_code} {cerrar.headers.get('location', '')}")
        tras_logout = client.get(f"{BASE}/")
        print(f"/ tras logout -> {tras_logout.status_code} {tras_logout.headers.get('location', '')}")
        if tras_logout.status_code != 303:
            fallos.append("Tras cerrar sesion la raiz no redirige a /login.")

        # 6. Cabeceras de seguridad en una respuesta HTML real.
        login = client.get(f"{BASE}/login")
        print(f"/login -> {login.status_code}")
        for cabecera in ("content-security-policy", "x-content-type-options", "x-frame-options"):
            if cabecera not in {k.lower() for k in login.headers}:
                fallos.append(f"Falta la cabecera de seguridad {cabecera}.")
        if "no-store" not in login.headers.get("cache-control", "").lower():
            fallos.append("La pagina de login no prohibe cache (no-store).")

    print()
    if fallos:
        print("FALLAS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("FLUJO HTTP COMPLETO OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
