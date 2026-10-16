# Arranca License Manager en local para revisar la interfaz.
# Uso:  .\scripts\dev.ps1
#
# Usa SQLite y una llave de desarrollo generada en .dev-private.pem.
# En el servidor real las variables se configuran en el entorno del servicio.

$ErrorActionPreference = "Stop"
$raiz = Split-Path $PSScriptRoot -Parent
$python = Join-Path $raiz ".venv\Scripts\python.exe"
Set-Location (Join-Path $raiz "backend")

$devKey = Join-Path $raiz "backend\.dev-private.pem"
if (-not (Test-Path $devKey)) {
    & $python -c "from license_manager.crypto.keys import generate_keypair_pem; open(r'$devKey','w').write(generate_keypair_pem()[0]); print('Llave de desarrollo creada en backend\.dev-private.pem')"
}

$env:LM_ECDSA_PRIVATE_KEY_FILE = Join-Path $raiz "backend\.dev-private.pem"
$env:LM_DATABASE_URL = "sqlite:///" + (Join-Path $raiz "backend\dev.db")
$env:LM_COOKIE_SECURE = "false"
$env:LM_SESSION_SECRET = "dev-secret-cambiar"
$env:LM_ENVIRONMENT = "development"

& $python -m uvicorn license_manager.app:create_app --factory --host 127.0.0.1 --port 8899 --app-dir (Join-Path $raiz "backend") --reload
