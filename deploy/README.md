# Despliegue en un VPS (Ubuntu Server)

Runbook técnico de JHO-106. Al terminar, el sitio queda en
`https://license-manager.tu-dominio.com` con TLS automático, respaldo diario
verificado y la base PostgreSQL solo accesible desde el propio servidor.

> El **manual para el Titular y el Socio** (operación diaria y contingencias)
> está en `docs/manual-operacion-contingencia.md`. Este documento es para quien
> configura el servidor.

---

## 0. Qué necesitas antes de empezar

| Cosa | Detalle |
| --- | --- |
| VPS | Ubuntu 22.04+ (DigitalOcean, Hetzner, Oracle Cloud…), 1 vCPU / 1 GB basta |
| Dominio | Un registro **A** apuntando a la IP del VPS |
| Llave privada | El PEM ECDSA P-256 cuya pública corresponde a `lic-v1-2026-09-29` |
| Acceso | SSH con llave (desactiva el login por contraseña) |

Comprueba el registro DNS antes de seguir: `dig +short license-manager.tu-dominio.com`
debe devolver la IP del VPS (Caddy necesita resolverlo para emitir el certificado).

---

## 1. Instalación base

```bash
sudo apt update && sudo apt install -y git
sudo git clone <URL-DEL-REPO> /opt/license-manager
cd /opt/license-manager
sudo bash deploy/install.sh /opt/license-manager
```

El instalador: instala dependencias y Caddy, crea el usuario de servicio
`license-manager`, el entorno virtual, las unidades de systemd (con el
endurecimiento activo), el temporizador de respaldos y el cortafuegos — que
**no** abre el puerto 8899 del backend: solo 22, 80 y 443.

---

## 2. Base de datos PostgreSQL

```bash
sudo -u postgres psql <<'SQL'
CREATE USER license_manager WITH PASSWORD 'CAMBIA-ESTA-CLAVE-FUERTE';
CREATE DATABASE license_manager OWNER license_manager;
SQL
```

El esquema lo crea la propia aplicación al arrancar (`create_all`, idempotente).
En PostgreSQL, además, aplica solo el **trigger de auditoría inmutable**
(`backend/migrations/sql/postgres_auditoria_trigger.sql`): la tabla `auditoria`
rechaza `UPDATE`, `DELETE` y `TRUNCATE` incluso para quien entre por `psql`.

---

## 3. Llave privada ECDSA

```bash
# Desde tu equipo: sube el PEM (nunca lo subas al repositorio)
scp private.pem ubuntu@TU-IP:/tmp/private.pem

# En el VPS:
sudo install -m 600 -o license-manager -g license-manager \
  /tmp/private.pem /etc/license-manager/private.pem
shred -u /tmp/private.pem
```

Verifica que la llave es la que el POS reconoce **antes** de emitir en
producción:

```bash
sudo -u license-manager /opt/license-manager/.venv/bin/python \
  -m license_manager.tools.verificar_llave --pos /opt/license-manager/Frutter-POS/src
```

Debe terminar en `OK: la llave del servidor es la que Frubber POS verifica.`

> **Por qué esto no es opcional.** El `keyId` del texto de la licencia es solo
> una cadena de configuración: si el servidor tiene otra llave privada, emite
> licencias que se ven perfectas y que el POS **rechaza todas**, sin que nada
> avise. Este chequeo compara la huella de la llave **pública** derivada de la
> privada del servidor contra la constante `CurrentPublicKeyBase64` del POS, que
> es lo único que zanja la duda. El comando sale con código 1 si no coinciden.

Si el POS no está en el servidor, pásale la llave pública a mano:

```bash
python -m license_manager.tools.verificar_llave --esperada "<SPKI Base64 del POS>"
```

---

## 4. Entorno y arranque

```bash
sudo nano /etc/license-manager/license-manager.env   # LM_DATABASE_URL, LM_SESSION_SECRET, LM_TRUSTED_HOSTS
sudo systemctl start license-manager
sudo systemctl status license-manager --no-pager
curl -sS http://127.0.0.1:8899/api/salud
```

Genera el secreto de sesión con
`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.

---

## 5. HTTPS con Caddy

```bash
sudo cp /opt/license-manager/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile      # sustituye el dominio por el real
sudo systemctl reload caddy
```

Caddy pide el certificado de Let's Encrypt solo y lo renueva sin cron.
Con TLS funcionando, confirma que en el entorno están:

```
LM_COOKIE_SECURE=true
LM_ENABLE_HSTS=true
LM_TRUSTED_HOSTS=license-manager.tu-dominio.com
```

y reinicia: `sudo systemctl restart license-manager`.

---

## 6. Respaldos diarios (ya programados)

El temporizador corre todos los días a las 03:30 UTC y ejecuta:

```
python -m license_manager.tools.backup --verify
```

Qué hace y por qué importa:

* **Copia consistente** (API de respaldo de SQLite o `pg_dump` en producción).
* **Comprime** y guarda un `.sha256` al lado de cada respaldo.
* **Verifica**: abre la copia, corre `PRAGMA integrity_check` y **recomputa la
  cadena de auditoría dentro del respaldo**. Un respaldo que no se abre o que
  trae la cadena rota hace fallar el trabajo (y queda en el journal).
* **Rota**: conserva los últimos `LM_BACKUP_KEEP` (14 por defecto).

```bash
sudo systemctl status license-manager-backup.timer --no-pager
sudo systemctl list-timers license-manager-backup.timer --no-pager
sudo journalctl -u license-manager-backup.service -n 20 --no-pager   # historial de respaldos
sudo systemctl start license-manager-backup.service                  # respaldar ahora
ls -lh /var/backups/license-manager
```

### Copia fuera del VPS (obligatoria para "almacenamiento seguro en la nube")

Un respaldo en el mismo servidor no protege contra perder el servidor. Instala
`rclone` y apunta `LM_BACKUP_UPLOAD` a tu almacenamiento (Backblaze B2, S3…):

```
LM_BACKUP_UPLOAD=rclone copy {file} b2:frubber-backups
```

`{file}` se sustituye por el respaldo; el `.sha256` se sube junto a él. El
comando no pasa por un shell (se ejecuta por argumentos), así que lo que
escribas en esa variable es literalmente lo que corre.

---

## 7. Restaurar (ensayo obligatorio)

Un respaldo que nunca se restauró no es un respaldo. Hazlo en local:

```bash
# 1. Trae un respaldo
scp ubuntu@TU-IP:/var/backups/license-manager/lm-backup-*.db.gz .   # SQLite
#    (con PostgreSQL: lm-backup-*.dump.gz)

# 2. Verifícalo antes de confiar en él
python -m license_manager.tools.backup --dest ./tmp-respaldo --keep 1 --verify   # sobre el entorno local

# 3. Restaura SQLite
gunzip -c lm-backup-20261016-033000.db.gz > license_manager.db

# 3b. Restaura PostgreSQL
gunzip -c lm-backup-20261016-033000.dump.gz > volcado.dump
sudo -u postgres pg_restore --clean --if-exists --no-owner \
  --dbname license_manager volcado.dump
```

Tras restaurar, arranca el sitio y abre **Historial**: la tarjeta de integridad
debe decir *"Íntegra"*. Si dice "Rota", no sigas: avisa al Titular Principal.

---

## 8. Verificación desde el celular y el escritorio (checklist de salida)

1. Abre `https://license-manager.tu-dominio.com` en el celular (datos móviles,
   sin WiFi) y en el escritorio: el candado debe ser válido.
2. Entra con el Titular Principal y crea la cuenta del Socio/Administrador.
3. Emite una licencia de prueba y **verifica que el POS la acepta** en un
   equipo de laboratorio con la balanza conectada (es el único paso que no se
   puede automatizar: requiere el POS y el hardware reales).
4. Registra esa emisión como cupo mayorista de un técnico y confirma el
   descuento de saldo en `Mayorista`.
5. Abre Historial → "Verificar cadena": debe responder *"Cadena integra"*.

---

## 9. Operación del día a día

```bash
sudo systemctl restart license-manager            # reiniciar el sitio
sudo journalctl -u license-manager -f             # ver el registro en vivo
sudo systemctl restart postgresql                 # reiniciar la base
sudo systemctl start license-manager-backup.service   # respaldar fuera de horario
```

Antes de emitir en producción, recuerda: la llave privada **solo** vive en el
servidor, el POS valida con su pública embebida y el `keyId` debe coincidir.
