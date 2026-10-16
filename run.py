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
import os
import sys
import threading
import webbrowser
from pathlib import Path

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
