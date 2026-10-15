"""Prueba en vivo de UI-09 contra el servidor real (uvicorn, puerto 8899).

Habla por TCP, no con TestClient: verifica que lo que el dueno ve en el
navegador es exactamente lo que la suite dice que funciona.
"""

from __future__ import annotations

import re
import sys
import time

import httpx

BASE = "http://127.0.0.1:8899"
TITULAR = ("titular@frubber.local", "Frubber-Dev-2026!")
ADMIN = ("prueba.admin@frubber.local", "Clave-Admin-Prueba-1")


def main() -> int:
    fallos: list[str] = []
    admin_id: str | None = None
    with httpx.Client(timeout=15.0, follow_redirects=False) as c:
        limite = time.time() + 30
        while time.time() < limite:
            try:
                if c.get(f"{BASE}/api/salud").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            print("FALLO: el servidor no arranco.")
            return 1

        # --- El titular entra a la pantalla UI-09 -------------------------------
        c.post(f"{BASE}/login", data={"email": TITULAR[0], "password": TITULAR[1]})
        pantalla = c.get(f"{BASE}/administradores")
        print(f"GET /administradores (titular) -> {pantalla.status_code}")
        if pantalla.status_code != 200:
            fallos.append(f"La pantalla devolvio {pantalla.status_code}")
        else:
            for texto in (
                "Gestion de Administradores",
                "Nombre del Operador",
                "Correo de Acceso",
                "Licencias Emitidas",
                "Fecha de Alta",
                "Acciones del Titular",
                "Cambiar mi contrasena",
                "Crear nuevo usuario",
                "Reglas de control del Titular",
            ):
                if texto not in pantalla.text:
                    fallos.append(f"Falta en la pantalla: {texto!r}")
            print("   -> columnas, panel de seguridad y formulario visibles")

        # --- El titular crea un administrador ----------------------------------
        creado = c.post(
            f"{BASE}/administradores/crear",
            data={
                "nombre": "Admin de Prueba",
                "email": ADMIN[0],
                "password": ADMIN[1],
                "rol": "admin",
            },
        )
        print(f"POST /administradores/crear -> {creado.status_code} {creado.headers.get('location','')[:60]}")
        if creado.status_code != 303:
            fallos.append(f"El alta devolvio {creado.status_code}")

        # --- El admin recibe 403 estricto --------------------------------------
        with httpx.Client(timeout=15.0, follow_redirects=False) as admin:
            admin.post(f"{BASE}/login", data={"email": ADMIN[0], "password": ADMIN[1]})
            restringido = admin.get(f"{BASE}/administradores")
            print(f"GET /administradores (admin) -> {restringido.status_code}")
            if restringido.status_code != 403:
                fallos.append(f"El admin debia recibir 403 y recibio {restringido.status_code}")
            elif "Acceso restringido" not in restringido.text:
                fallos.append("El 403 no muestra la tarjeta de acceso restringido.")
            else:
                print("   -> 403 estricto con tarjeta de acceso restringido")

            # El admin tampoco puede crear cuentas por la via directa.
            intento = admin.post(
                f"{BASE}/administradores/crear",
                data={
                    "nombre": " Intruso",
                    "email": "intruso@frubber.local",
                    "password": "Clave-Intruso-Larga-1",
                    "rol": "admin",
                },
            )
            print(f"POST /administradores/crear (admin) -> {intento.status_code}")
            if intento.status_code != 403:
                fallos.append(f"El admin debia recibir 403 al crear y recibio {intento.status_code}")

        # --- El titular la ve en la lista con su fila --------------------------
        lista = c.get(f"{BASE}/administradores")
        if ADMIN[0] not in lista.text:
            fallos.append("El administrador creado no aparece en la tabla.")

        # --- La pantalla es movil-friendly y sin JS inline (CSP script-src self)
        if "<script" in lista.text.lower():
            fallos.append("La pagina trae <script> inline, que la CSP bloquea.")
        else:
            print("   -> sin JS inline: la CSP no se rompe")

        csp = lista.headers.get("content-security-policy", "")
        if "script-src 'self'" not in csp:
            fallos.append("Falta la cabecera CSP script-src 'self'.")
        if "no-store" not in lista.headers.get("cache-control", "").lower():
            fallos.append("La pagina de administradores se cachea (debe ser no-store).")

        # --- El titular cambia su propia clave ---------------------------------
        cambio = c.post(
            f"{BASE}/seguridad/mi-clave",
            data={
                "password_actual": TITULAR[1],
                "password_nueva": "Frubber-Dev-2026-v2",
                "password_confirmacion": "Frubber-Dev-2026-v2",
            },
        )
        print(f"POST /seguridad/mi-clave -> {cambio.status_code} {cambio.headers.get('location','')[:60]}")
        if cambio.status_code != 303:
            fallos.append(f"El cambio de clave devolvio {cambio.status_code}")

        # La clave vieja ya no entra.
        vieja = httpx.post(
            f"{BASE}/login",
            data={"email": TITULAR[0], "password": TITULAR[1]},
            timeout=15.0,
            follow_redirects=False,
        )
        print(f"login con la clave vieja -> {vieja.status_code}")
        if vieja.status_code == 303:
            fallos.append("La clave vieja sigue abriendo sesion tras el cambio.")

        # --- Se desactiva al administrador y pierde el acceso ------------------
        # Se reactiva el titular con la clave nueva para poder desactivar.
        c.post(f"{BASE}/login", data={"email": TITULAR[0], "password": "Frubber-Dev-2026-v2"})
        pagina = c.get(f"{BASE}/administradores").text

        m = re.search(r' action="/administradores/([0-9a-f-]+)/alternar"', pagina)
        if not m:
            fallos.append("No se encontro el formulario de desactivacion en la tabla.")
        else:
            admin_id = m.group(1)
            apagado = c.post(
                f"{BASE}/administradores/{admin_id}/alternar", data={"activo": "0"}
            )
            print(f"POST alternar (desactivar) -> {apagado.status_code}")

            # El admin ya no puede iniciar sesion.
            fuera = httpx.post(
                f"{BASE}/login",
                data={"email": ADMIN[0], "password": ADMIN[1]},
                timeout=15.0,
                follow_redirects=False,
            )
            print(f"login del admin desactivado -> {fuera.status_code}")
            if fuera.status_code == 303:
                fallos.append("El administrador desactivado sigue pudiendo entrar.")

            # Sigue visible en la tabla, marcado como desactivado.
            tras = c.get(f"{BASE}/administradores").text
            if ADMIN[0] not in tras:
                fallos.append("El administrador desactivado desaparecio de la tabla.")
            elif "Desactivado" not in tras:
                fallos.append("El administrador desactivado no muestra el estado Desactivado.")
            else:
                print("   -> sigue en la tabla, marcado Desactivado (historial preservado)")

        # --- Restaurar el entorno de desarrollo --------------------------------
        if admin_id is not None:
            c.post(f"{BASE}/administradores/{admin_id}/alternar", data={"activo": "1"})
            c.post(
                f"{BASE}/administradores/{admin_id}/restablecer",
                data={"password": ADMIN[1]},
            )
        c.post(
            f"{BASE}/seguridad/mi-clave",
            data={
                "password_actual": "Frubber-Dev-2026-v2",
                "password_nueva": TITULAR[1],
                "password_confirmacion": TITULAR[1],
            },
        )
        print("   -> entorno de desarrollo restaurado")

    print()
    if fallos:
        print("FALLAS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("UI-09 EN VIVO: TODO OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
