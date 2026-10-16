#!/usr/bin/env bash
# Instalador de License Manager en un VPS Ubuntu (JHO-106).
#
# Uso (como root, desde la copia del repositorio):
#   sudo bash deploy/install.sh /opt/license-manager
#
# Deja el sitio listo para arrancar; NO carga la llave privada ni crea la base:
# esos dos pasos son manuales y estan en deploy/README.md (son material del
# Titular). Es idempotente: se puede volver a ejecutar sin romper nada.

set -euo pipefail

DESTINO="${1:-/opt/license-manager}"
SERVICIO_USUARIO="license-manager"
CONF_DIR="/etc/license-manager"
RESPALDO_DIR="/var/backups/license-manager"

log() { printf '\n== %s\n' "$1"; }
morir() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || morir "Ejecuta este instalador con sudo (necesita apt y systemd)."
[[ -f "$DESTINO/backend/pyproject.toml" ]] || morir "No encuentro el repositorio en $DESTINO. Clónalo primero."

log "1/7 Paquetes del sistema"
apt-get update -qq
# python3-venv y el cliente de PostgreSQL (pg_dump) que usa el respaldo.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-venv python3-pip postgresql-client debian-keyring debian-archive-keyring apt-transport-https curl gnupg

log "2/7 Caddy (HTTPS automatico con Let's Encrypt)"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
fi

log "3/7 Usuario de servicio y carpetas"
id -u "$SERVICIO_USUARIO" >/dev/null 2>&1 || useradd --system --home "$DESTINO" --shell /usr/sbin/nologin "$SERVICIO_USUARIO"
install -d -m 750 -o root -g "$SERVICIO_USUARIO" "$CONF_DIR"
install -d -m 700 -o "$SERVICIO_USUARIO" -g "$SERVICIO_USUARIO" "$RESPALDO_DIR" /var/lib/license-manager
chown -R "$SERVICIO_USUARIO:$SERVICIO_USUARIO" "$DESTINO"

log "4/7 Entorno virtual y dependencias"
sudo -u "$SERVICIO_USUARIO" python3 -m venv "$DESTINO/.venv"
sudo -u "$SERVICIO_USUARIO" "$DESTINO/.venv/bin/python" -m pip install --quiet --upgrade pip
sudo -u "$SERVICIO_USUARIO" "$DESTINO/.venv/bin/python" -m pip install --quiet -e "$DESTINO/backend"

log "5/7 Archivo de entorno"
if [[ ! -f "$CONF_DIR/license-manager.env" ]]; then
  install -m 640 -o root -g "$SERVICIO_USUARIO" \
    "$DESTINO/deploy/env.production.example" "$CONF_DIR/license-manager.env"
  echo "   Creado $CONF_DIR/license-manager.env — EDITALO antes de arrancar."
else
  echo "   Ya existia $CONF_DIR/license-manager.env (no se toca)."
fi

log "6/7 Servicios de systemd"
install -m 644 "$DESTINO/deploy/license-manager.service" /etc/systemd/system/license-manager.service
install -m 644 "$DESTINO/deploy/license-manager-backup.service" /etc/systemd/system/license-manager-backup.service
install -m 644 "$DESTINO/deploy/license-manager-backup.timer" /etc/systemd/system/license-manager-backup.timer
# Aplica el trigger de auditoria inmutable al arrancar (lo hace la app sola).
systemctl daemon-reload
systemctl enable license-manager.service
systemctl enable --now license-manager-backup.timer

log "7/7 Cortafuegos"
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null || true
  ufw allow 80/tcp >/dev/null || true
  ufw allow 443/tcp >/dev/null || true
  yes | ufw enable >/dev/null || true
  echo "   ufw: 22, 80 y 443 (el 8899 del backend NO se expone)."
else
  echo "   ufw no esta instalado; revisa el cortafuegos del proveedor."
fi

cat <<FIN

Listo. Faltan tres pasos manuales (estan en deploy/README.md):

  1. Crear la base y el usuario de PostgreSQL en el VPS.
  2. Subir la llave privada a $CONF_DIR/private.pem (permisos 600,
     dueno $SERVICIO_USUARIO) y ajustar LM_DATABASE_URL + LM_SESSION_SECRET
     en $CONF_DIR/license-manager.env.
  3. Editar el dominio en /etc/caddy/Caddyfile y recargar Caddy.

Despues:
  sudo systemctl start license-manager
  sudo systemctl status license-manager

FIN
