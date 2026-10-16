# License Manager de Frubber POS

Aplicación web privada para emitir y llevar el registro de las licencias
offline de **Frubber POS**. Firmadas criptográficamente en el servidor, el
cliente las pega en el POS y quedan activadas de por vida **sin internet**.

Issues en Linear: proyecto **License Manager Frubber** (JHO-97 → JHO-106).

---

## 1. Cómo funciona, en una frase

El POS ya sabe verificar licencias `FRB1`; este sitio es la herramienta que
**las firma**. Nada más: no consulta el POS, y el POS no lo consulta a él.

```
Técnico instala POS  →  POS muestra código de hardware (16 hex)
        ↓
Técnico manda el código por WhatsApp
        ↓
Administrador lo pega en License Manager  →  el backend firma ECDSA P-256
        ↓
Técnico recibe la cadena FRB1 y la pega en el POS
        ↓
POS verifica con su llave pública embebida y se activa para siempre
```

La clave privada **solo** vive en el servidor. Ni el navegador ni el POS
tienen acceso a ella.

---

## 2. El contrato con el POS (no se puede tocar a la ligera)

Formato exacto que Frubber POS ya implementa en
`FrubberPOS.Domain.Licensing.LicenseKeyFormat`:

```
FRB1|<keyId>|<CODIGOEQUIPO16>|<aaaammdd>.<firmaBase64>
```

* **Payload firmado:** la cadena `FRB1|...|...|...` en UTF-8, sin nada más.
* **Algoritmo:** ECDSA P-256 (SECP256R1) con SHA-256.
* **Formato de firma:** IEEE P1363, `r||s` de 32 bytes cada uno → **64 bytes**.
* **Código de equipo:** 16 caracteres hexadecimales en **mayúsculas**.

Cualquier desviación hace que el POS rechace la licencia. Por eso existe la
prueba de interoperabilidad (ver §5) que ejecuta el verificador **C# real** del
POS sobre lo que firma Python.

---

## 3. Cómo levantarlo en local

Requisitos: Python 3.11+, y .NET 10 SDK solo si quieres correr la interop.

```powershell
# 1. Entorno virtual y dependencias
cd LicenseManager
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "backend[dev]"

# 2. Arrancar (crea SQLite y una llave de desarrollo la primera vez)
.\scripts\dev.ps1
```

Abre <http://127.0.0.1:8899>. La primera vez te lleva a **Primer inicio**, donde
creas la cuenta del Titular Principal.

---

## 4. Configuración en el servidor

Para poner el sitio en un VPS (systemd, HTTPS con Caddy, respaldos diarios) el
runbook completo es **`deploy/README.md`** y el instalador
`deploy/install.sh`. Lo de abajo es la referencia de variables.

Copia `backend/.env.example` y completa:

| Variable | Qué es |
| --- | --- |
| `LM_DATABASE_URL` | `postgresql+psycopg://usuario:clave@host:5432/base` |
| `LM_ECDSA_PRIVATE_KEY_FILE` | Ruta al PEM de la llave privada (permisos restringidos) |
| `LM_ECDSA_PRIVATE_KEY_PEM` | Alternativa: la llave en la variable, con `\n` |
| `LM_EXPECTED_KEY_ID` | `lic-v1-2026-09-29` (el que el POS reconoce) |
| `LM_SESSION_SECRET` | Valor largo aleatorio; nunca reutilizar el de desarrollo |
| `LM_COOKIE_SECURE` | `true` en producción (cookie solo por HTTPS) |
| `LM_ENABLE_HSTS` | `true` en producción, solo tras TLS |

### La llave privada

La llave pública que el POS ya lleva embebida es
`lic-v1-2026-09-29`. Su **privada correspondiente es material del titular** y
por eso no está en ningún repositorio. Para cargarla:

```powershell
# Genera un par nuevo (solo si se rota la llave; habría que actualizar el POS)
.\.venv\Scripts\python.exe -m license_manager.tools.genkey
```

> Si emites con una llave distinta de la que el POS trae embebida, el POS
> **rechazará todas las licencias**. Verifica siempre contra el POS antes de
> emitir en producción.

---

## 5. Pruebas

```powershell
cd LicenseManager\backend
..\.venv\Scripts\python.exe -m pytest -q
```

La suite incluye:

* **Interoperabilidad con el POS**: compila y ejecuta el verificador C# real
  de Frubber POS (`EcdsaLicenseSignatureVerifier`) sobre licencias firmadas por
  Python. Si alguien cambia el formato en el POS, esas pruebas se rompen.
* **Pruebas integrales E2E** (`test_e2e_flujo.py`): recorren el negocio
  completo por HTTP —primer arranque, emisión firmada, paquetes mayoristas,
  cobros, cierre contable, liquidación, auditoría y respaldo— y comprueban que
  las cifras cuadran entre módulos.
* **Respaldo verificado** (`test_backup.py`): la copia se abre, conserva la
  cadena de auditoría, se restaura y la rotación no deja firmas huérfanas.

Para apuntar a otra copia del POS:

```powershell
$env:FRUBBER_POS_SRC = "C:\ruta\a\Frutter POS\src"
```

---

## 6. Respaldos

```powershell
cd LicenseManager\backend
..\.venv\Scripts\python.exe -m license_manager.tools.backup --verify
```

Copia la base (API de respaldo de SQLite o `pg_dump`), la comprime, deja su
`.sha256` al lado, **verifica** que se pueda abrir y que la cadena de auditoría
esté sana dentro de la copia, y rota los respaldos viejos (14 por defecto).
La configuración sale del entorno: `LM_BACKUP_DIR`, `LM_BACKUP_KEEP` y
`LM_BACKUP_UPLOAD` (para subir a B2/S3 con `rclone`).

En el servidor corre solo con el temporizador de systemd
(`deploy/license-manager-backup.timer`, todos los días a las 03:30 UTC).

---

## 7. Estructura

```
backend/
  license_manager/
    app.py              Punto de entrada y composition root
    deps.py             Estado del proceso y dependencias de FastAPI
    config.py           Configuración desde variables de entorno (LM_*)
    db.py               Motor y sesiones de SQLAlchemy
    models.py           Esquema: usuarios, verdulerías, equipos, licencias,
                        ingresos, auditoría, técnicos, liquidaciones
    crypto/
      frb1.py           Formato y firma FRB1 (contrato con el POS)
      keys.py           Custodia de la llave privada
    security/
      headers.py        CSP, HSTS, no-cache y demás cabeceras
      passwords.py      Argon2id
      sessions.py       Cookies HttpOnly firmadas + revocación en BD
    services/
      audit.py          Auditoría append-only encadenada por hash
      historial.py      Lectura del historial (UI-08)
      cierres.py        Cierre contable mensual (UI-07B)
      liquidaciones.py  Reparto de utilidades (UI-11)
      mayorista.py      Paquetes prepago y saldo de cupos (UI-10)
    tools/
      genkey.py         Genera el par ECDSA
      backup.py         Respaldo verificado con rotación (JHO-106)
    web/
      routes.py         Páginas y API
      templates/        Jinja2 con los tokens de la maqueta del dueño
      static/           CSS propio, sin CDNs
  migrations/sql/       Scripts específicos de PostgreSQL (trigger de auditoría)
  tests/                Suite, incluida la interop con el POS y el E2E

deploy/                 VPS: systemd, Caddy (HTTPS), instalador y runbook
docs/                   Manual de operación y contingencia
scripts/                Arranque local en Windows
```

---

## 8. Decisiones tomadas

| Tema | Decisión | Por qué |
| --- | --- | --- |
| Stack | Python + FastAPI + SQLAlchemy | Elegido por el dueño (2026-10-15) |
| Base de datos | PostgreSQL en producción, SQLite en desarrollo | Mismo esquema, sin instalar nada para trabajar |
| Hosting | VPS Cloud con HTTPS (Caddy + Let's Encrypt) | Docs 04 v2.0 y 01 LM-RF-001 (15 oct) mandan sobre los del 14 oct |
| Interfaz | Jinja2 server-side, CSS propio | Una sola pieza desplegable y usable desde el celular |
| Contraseñas | Argon2id | Lo pide JHO-98; es el estándar actual |
| Auditoría | Append-only + cadena de hash + trigger en PostgreSQL | Detecta manipulación aunque alguien toque la BD por SSH |
| Respaldos | Diarios, comprimidos, con sha256 y **verificados** | Un respaldo que no se abre no es un respaldo |
| Despliegue | systemd endurecido + Caddy, sin exponer el backend | El 8899 nunca sale a internet |

---

## 9. Pendiente

* **Migraciones con Alembic**: hoy el esquema se crea con
  `Base.metadata.create_all` (idempotente) y el trigger de auditoría se aplica
  al arrancar. Funciona para instalaciones nuevas; para evolucionar columnas
  hará falta Alembic.
* **Verificación final en campo**: probar la activación de una licencia en un
  POS real con la balanza conectada (requiere el hardware; el contrato con el
  POS ya está cubierto por la prueba de interoperabilidad en C#).
* **Puesta en producción**: el VPS, el dominio y la carga de la llave privada
  son acciones del Titular; el runbook está en `deploy/README.md`.
