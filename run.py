"""Arranca License Manager en local, sin PowerShell ni variables a mano.

Uso (Windows):

    .\\.venv\\Scripts\\python.exe run.py

Uso (Linux / macOS):

    ./.venv/bin/python run.py

Hace lo mismo que ``scripts/dev.ps1`` pero en Python, para que funcione igual
en cualquier sistema. Las variables de entorno que ya esten puestas se
respetan: este archivo solo rellena lo que falte.

    --puerto 8899     puerto (por defecto 8899)
    --sin-recarga     no reiniciar al guardar cambios
    --sin-navegador   no abrir el navegador
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

RAIZ = Path(__file__).resolve().parent
BACKEND = RAIZ / "backend"
sys.path.insert(0, str(BACKEND))

try:
    import uvicorn
except ModuleNotFoundError:  # pragma: no cover - ayuda para el primer arranque
    print("No encuentro las dependencias de la aplicacion.")
    print("Usa el Python del entorno virtual:")
    print(r"  .\.venv\Scripts\python.exe run.py")
    print(r"  (si no existe el .venv:  python -m venv .venv  y luego")
    print(r"   .\.venv\Scripts\python.exe -m pip install -e backend[dev])")
    raise SystemExit(1) from None


def _llave_de_firma() -> tuple[Path, bool]:
    """Devuelve (ruta de la llave, es_la_real). Genera una de desarrollo si falta."""
    real = BACKEND / "llave-pos.pem"
    if real.is_file():
        return real, True

    dev = BACKEND / ".dev-private.pem"
    if not dev.is_file():
        from license_manager.crypto.keys import generate_keypair_pem

        dev.write_text(generate_keypair_pem()[0], encoding="utf-8")
        print("Llave de desarrollo creada en backend\\.dev-private.pem")
    return dev, False


def _abrir_navegador(url: str) -> None:
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()


def _pid_escuchando(puerto: int) -> int | None:
    """PID que tiene el puerto en escucha, si hay alguno."""
    if sys.platform.startswith("win"):
        salida = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], check=False, capture_output=True, text=True
        ).stdout
        for linea in salida.splitlines():
            partes = linea.split()
            if (
                len(partes) >= 5
                and partes[0].upper() == "TCP"
                and partes[3] == "LISTENING"
                and partes[1].endswith(f":{puerto}")
            ):
                try:
                    return int(partes[4])
                except ValueError:
                    return None
        return None

    resultado = subprocess.run(
        ["lsof", "-ti", f"tcp:{puerto}"], check=False, capture_output=True, text=True
    )
    linea = resultado.stdout.strip().splitlines()
    return int(linea[0]) if linea and linea[0].isdigit() else None


def _es_nuestro_servidor(puerto: int) -> bool:
    """Pregunta al puerto si quien responde es esta misma aplicacion.

    Es mas fiable que mirar la linea de comandos: el proceso que arranca
    ``run.py`` puede no llevar la ruta del proyecto en sus argumentos. La
    respuesta de ``/api/salud`` es inconfundible (``estado`` + ``firma_disponible``).
    """
    try:
        with urlopen(f"http://127.0.0.1:{puerto}/api/salud", timeout=2) as respuesta:
            datos = json.loads(respuesta.read().decode("utf-8"))
        return "estado" in datos and "firma_disponible" in datos
    except (OSError, ValueError):
        return False


def _linea_de_comando(pid: int) -> str:
    if sys.platform.startswith("win"):
        resultado = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return resultado.stdout.strip()
    try:
        crudo = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return crudo.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _liberar_puerto(puerto: int) -> None:
    """Cierra un servidor ANTERIOR de este proyecto que tenga el puerto.

    Sin esto, en Windows el segundo arranque no puede tomar el puerto y el viejo
    sigue respondiendo con codigo ya desactualizado: la pantalla falla por algo
    que en el codigo nuevo ya esta arreglado, y parece un fallo fantasma.
    """
    pid = _pid_escuchando(puerto)
    if pid is None:
        return

    linea = _linea_de_comando(pid)
    nuestro = _es_nuestro_servidor(puerto) or any(
        marca in linea for marca in ("LicenseManager", "license_manager", "run.py")
    )
    if not nuestro:
        print(f"El puerto {puerto} lo ocupa otro programa (PID {pid}):")
        print(f"  {linea[:140] or 'sin detalle'}")
        print(f"Cierralo, o arranca en otro puerto con  --puerto {puerto + 1}")
        raise SystemExit(1)

    print(f"Cerrando el servidor anterior de License Manager (PID {pid})...")
    if sys.platform.startswith("win"):
        # /T mata tambien a los hijos del recargador: si no, quedan huerfanos.
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True, text=True
        )
    else:
        subprocess.run(["kill", "-TERM", str(pid)], check=False, capture_output=True, text=True)
    time.sleep(1.2)

    if _pid_escuchando(puerto) is not None:
        print(f"El puerto {puerto} sigue ocupado. Cierra la otra ventana y vuelve a intentar.")
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Arranca License Manager en local.")
    parser.add_argument("--puerto", type=int, default=int(os.environ.get("LM_PUERTO", "8899")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--sin-recarga",
        action="store_true",
        help="desactiva el reinicio automatico al guardar cambios",
    )
    parser.add_argument("--sin-navegador", action="store_true")
    args = parser.parse_args()

    llave, es_real = _llave_de_firma()

    # Un servidor anterior en el mismo puerto seguiria respondiendo con codigo
    # viejo: se cierra antes de arrancar.
    _liberar_puerto(args.puerto)

    # Solo rellena lo que falte: si el entorno ya lo define, manda el entorno.
    os.environ.setdefault("LM_ECDSA_PRIVATE_KEY_FILE", str(llave))
    os.environ.setdefault("LM_DATABASE_URL", f"sqlite:///{BACKEND / 'dev.db'}")
    os.environ.setdefault("LM_COOKIE_SECURE", "false")
    os.environ.setdefault("LM_SESSION_SECRET", "dev-secret-cambiar")
    os.environ.setdefault("LM_ENVIRONMENT", "development")
    os.environ.setdefault("LM_EXPECTED_KEY_ID", "lic-v1-2026-09-29")

    if es_real:
        print("Llave de firma : llave-pos.pem  (la REAL de Frubber POS: las licencias sirven)")
    else:
        print("Llave de firma : .dev-private.pem  (PRUEBAS: el POS rechazaria estas licencias)")
    print(f"Base de datos  : {os.environ['LM_DATABASE_URL']}")

    url = f"http://{args.host}:{args.puerto}"
    print()
    print(f"Abre en el navegador:  {url}")
    print("Para parar:  Ctrl + C")
    print()

    if not args.sin_navegador:
        _abrir_navegador(url)

    uvicorn.run(
        "license_manager.app:create_app",
        factory=True,
        host=args.host,
        port=args.puerto,
        app_dir=str(BACKEND),
        # Por defecto se reinicia al guardar: evita quedarse con una version
        # anterior del codigo mientras las plantillas ya cambiaron.
        reload=not args.sin_recarga,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
