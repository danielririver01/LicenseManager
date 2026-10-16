# Arranca License Manager en local para usarlo o revisarlo.
# Uso:  .\scripts\dev.ps1
#
# Detalles:
#   - Base de datos: SQLite en backend\dev.db (un solo archivo, sin instalar nada).
#   - Llave de firma: backend\llave-pos.pem si existe (la REAL, la que el POS
#     acepta). Si no existe, cae a la de desarrollo y avisa.
#   - En el servidor real las variables se configuran en el entorno del servicio.

$ErrorActionPreference = "Stop"
$raiz = Split-Path $PSScriptRoot -Parent
$python = Join-Path $raiz ".venv\Scripts\python.exe"
$llaveReal = Join-Path $raiz "backend\llave-pos.pem"
$llaveDev = Join-Path $raiz "backend\.dev-private.pem"
Set-Location (Join-Path $raiz "backend")

if (Test-Path $llaveReal) {
    $env:LM_ECDSA_PRIVATE_KEY_FILE = $llaveReal
    Write-Host "Llave de firma : llave-pos.pem  (la REAL de Frubber POS: las licencias sirven)" -ForegroundColor Green
} else {
    if (-not (Test-Path $llaveDev)) {
        & $python -c "from license_manager.crypto.keys import generate_keypair_pem; open(r'$llaveDev','w').write(generate_keypair_pem()[0]); print('Llave de desarrollo creada en backend\.dev-private.pem')"
    }
    $env:LM_ECDSA_PRIVATE_KEY_FILE = $llaveDev
    Write-Host "Llave de firma : .dev-private.pem  (PRUEBAS: el POS rechazaria estas licencias)" -ForegroundColor Yellow
}

$env:LM_DATABASE_URL = "sqlite:///" + (Join-Path $raiz "backend\dev.db")
$env:LM_COOKIE_SECURE = "false"
$env:LM_SESSION_SECRET = "dev-secret-cambiar"
$env:LM_ENVIRONMENT = "development"
$env:LM_EXPECTED_KEY_ID = "lic-v1-2026-09-29"

Write-Host "Base de datos  : $env:LM_DATABASE_URL" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Abre en el navegador:  http://127.0.0.1:8899" -ForegroundColor Cyan
Write-Host "Para parar:  Ctrl + C" -ForegroundColor DarkGray
Write-Host ""

& $python -m uvicorn license_manager.app:create_app --factory --host 127.0.0.1 --port 8899 --app-dir (Join-Path $raiz "backend") --reload
