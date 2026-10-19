#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
fi

if [[ "$#" -ne 1 ]]; then
    printf 'Uso: sudo bash scripts/configure-domain.sh dominio.exemplo.com\n' >&2
    exit 2
fi

DOMAIN="${1,,}"
DOMAIN="${DOMAIN%.}"
if ! python3 - "$DOMAIN" <<'PY'
import ipaddress
import re
import sys

domain = sys.argv[1]
try:
    ipaddress.ip_address(domain)
    raise SystemExit(1)
except ValueError:
    pass
labels = domain.split(".")
if len(domain) > 253 or len(labels) < 2 or any(
    not label
    or len(label) > 63
    or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
    for label in labels
):
    raise SystemExit(1)
PY
then
    printf 'Hostname inválido. Informe somente um domínio DNS, sem esquema, porta ou caminho.\n' >&2
    exit 2
fi

command -v caddy >/dev/null 2>&1 || {
    printf 'Caddy não encontrado. Instale-o pelo guia oficial: https://caddyserver.com/docs/install#debian-ubuntu-raspbian\n' >&2
    exit 1
}
systemctl cat caddy.service >/dev/null 2>&1 || {
    printf 'O unit caddy.service não foi instalado. Reinstale o pacote Caddy antes de ativar o domínio.\n' >&2
    exit 1
}
getent ahosts "$DOMAIN" >/dev/null || {
    printf 'O DNS de %s ainda não resolve neste servidor. Configure o registro A/AAAA e aguarde a propagação.\n' "$DOMAIN" >&2
    exit 1
}

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -x "$APP_ROOT/venv/bin/uvicorn" ]] || {
    printf 'Ambiente Python não encontrado em %s/venv.\n' "$APP_ROOT" >&2
    exit 1
}

SERVICE=""
for candidate in timelapse.service timelapse-manager.service; do
    if systemctl cat "$candidate" >/dev/null 2>&1; then
        SERVICE="$candidate"
        break
    fi
done
[[ -n "$SERVICE" ]] || {
    printf 'Não encontrei timelapse.service nem timelapse-manager.service.\n' >&2
    exit 1
}
SERVICE_USER="$(systemctl show --property=User --value "$SERVICE")"
[[ -n "$SERVICE_USER" ]] || SERVICE_USER=root
[[ "$SERVICE_USER" != root ]] || {
    printf 'O app precisa rodar com um usuário de serviço dedicado para habilitar o botão de certificados.\n' >&2
    exit 1
}

CADDYFILE=/etc/caddy/Caddyfile
SITE_DIR=/etc/caddy/sites-enabled
SITE_FILE="$SITE_DIR/timelapse.caddy"
IMPORT_LINE="import $SITE_DIR/*.caddy"
DROPIN_DIR="/etc/systemd/system/$SERVICE.d"
DROPIN_FILE="$DROPIN_DIR/20-timelapse-domain.conf"
MARKER="# Managed by Timelapse Manager configure-domain.sh"
DOMAIN_ENV_FILE="${TIMELAPSE_DOMAIN_CONFIG_FILE:-/var/lib/timelapse/domain.env}"
DOMAIN_UNIT_FILE=/etc/systemd/system/timelapse-domain-setup.service
DOMAIN_SUDOERS_FILE=/etc/sudoers.d/timelapse-domain-setup

[[ -f "$CADDYFILE" ]] || {
    printf 'Arquivo Caddyfile não encontrado em %s.\n' "$CADDYFILE" >&2
    exit 1
}
if [[ -e "$SITE_FILE" ]] && ! grep -Fqx "$MARKER" "$SITE_FILE"; then
    printf 'O arquivo %s já existe e não pertence ao configurador; nada foi alterado.\n' "$SITE_FILE" >&2
    exit 1
fi
if [[ -e "$DROPIN_FILE" ]] && ! grep -Fqx "$MARKER" "$DROPIN_FILE"; then
    printf 'O override %s já existe e não pertence ao configurador; nada foi alterado.\n' "$DROPIN_FILE" >&2
    exit 1
fi
if [[ -e "$DOMAIN_UNIT_FILE" ]] && ! grep -Fqx "$MARKER" "$DOMAIN_UNIT_FILE"; then
    printf 'O unit %s já existe e não pertence ao configurador; nada foi alterado.\n' "$DOMAIN_UNIT_FILE" >&2
    exit 1
fi
if [[ -e "$DOMAIN_SUDOERS_FILE" ]] && ! grep -Fqx "$MARKER" "$DOMAIN_SUDOERS_FILE"; then
    printf 'A regra sudoers %s já existe e não pertence ao configurador; nada foi alterado.\n' "$DOMAIN_SUDOERS_FILE" >&2
    exit 1
fi

install -d -o root -g root -m 0755 "$SITE_DIR" "$DROPIN_DIR"
SITE_TMP="$(mktemp)"
DROPIN_TMP="$(mktemp)"
SITE_BACKUP="$(mktemp)"
DROPIN_BACKUP="$(mktemp)"
SITE_EXISTED=0
DROPIN_EXISTED=0
if [[ -f "$SITE_FILE" ]]; then
    cp -a "$SITE_FILE" "$SITE_BACKUP"
    SITE_EXISTED=1
fi
if [[ -f "$DROPIN_FILE" ]]; then
    cp -a "$DROPIN_FILE" "$DROPIN_BACKUP"
    DROPIN_EXISTED=1
fi
trap 'rm -f "$SITE_TMP" "$DROPIN_TMP" "$SITE_BACKUP" "$DROPIN_BACKUP"' EXIT
cat > "$SITE_TMP" <<EOF
$MARKER
$DOMAIN {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8000
}
EOF
cat > "$DROPIN_TMP" <<EOF
$MARKER
[Service]
ExecStart=
ExecStart="$APP_ROOT/venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Environment=TIMELAPSE_PUBLIC_URL=https://$DOMAIN
Environment=TIMELAPSE_OAUTH_REDIRECT_URI=https://$DOMAIN/api/system/storage/oauth/callback
EOF

install -o root -g root -m 0644 "$SITE_TMP" "$SITE_FILE"
install -o root -g root -m 0644 "$DROPIN_TMP" "$DROPIN_FILE"

IMPORTED=0
CADDYFILE_BACKUP=""
if ! grep -Fqx "$IMPORT_LINE" "$CADDYFILE"; then
    CADDYFILE_BACKUP="$CADDYFILE.timelapse-backup.$(date +%Y%m%d%H%M%S)"
    cp -a "$CADDYFILE" "$CADDYFILE_BACKUP"
    printf '\n%s\n' "$IMPORT_LINE" >> "$CADDYFILE"
    IMPORTED=1
fi

if ! caddy validate --config "$CADDYFILE" --adapter caddyfile; then
    if [[ "$SITE_EXISTED" -eq 1 ]]; then
        cp -a "$SITE_BACKUP" "$SITE_FILE"
    else
        rm -f "$SITE_FILE"
    fi
    if [[ "$DROPIN_EXISTED" -eq 1 ]]; then
        cp -a "$DROPIN_BACKUP" "$DROPIN_FILE"
    else
        rm -f "$DROPIN_FILE"
    fi
    if [[ "$IMPORTED" -eq 1 ]]; then
        cp -a "$CADDYFILE_BACKUP" "$CADDYFILE"
    fi
    printf 'Configuração Caddy inválida; as alterações gerenciadas foram revertidas.\n' >&2
    exit 1
fi

DOMAIN_ENV_TMP="$(mktemp)"
DOMAIN_UNIT_TMP="$(mktemp)"
SUDOERS_TMP="$(mktemp /etc/sudoers.d/.timelapse-domain-setup.XXXXXX)"
trap 'rm -f "$SITE_TMP" "$DROPIN_TMP" "$SITE_BACKUP" "$DROPIN_BACKUP" "$DOMAIN_ENV_TMP" "$DOMAIN_UNIT_TMP" "$SUDOERS_TMP"' EXIT
printf 'DOMAIN_NAME=%s\n' "$DOMAIN" > "$DOMAIN_ENV_TMP"
cat > "$DOMAIN_UNIT_TMP" <<EOF
$MARKER
[Unit]
Description=Configure HTTPS domain for Timelapse Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=$DOMAIN_ENV_FILE
WorkingDirectory=$APP_ROOT
ExecStart=/bin/bash "$APP_ROOT/scripts/configure-domain.sh" \${DOMAIN_NAME}
EOF
printf '%s\n%s ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block restart timelapse-domain-setup.service\n' "$MARKER" "$SERVICE_USER" > "$SUDOERS_TMP"
chmod 0440 "$SUDOERS_TMP"
visudo -cf "$SUDOERS_TMP"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$(dirname "$DOMAIN_ENV_FILE")"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 "$DOMAIN_ENV_TMP" "$DOMAIN_ENV_FILE"
install -o root -g root -m 0644 "$DOMAIN_UNIT_TMP" "$DOMAIN_UNIT_FILE"
install -o root -g root -m 0440 "$SUDOERS_TMP" "$DOMAIN_SUDOERS_FILE"

systemctl daemon-reload
systemctl enable --now caddy
systemctl restart "$SERVICE"
systemctl reload-or-restart caddy

if ! curl --retry 10 --retry-connrefused --retry-delay 1 --retry-max-time 30 \
    --fail --silent --show-error --max-time 15 http://127.0.0.1:8000/login -o /dev/null; then
    printf 'O app não respondeu em HTTP local. Consulte: journalctl -u %s -n 100\n' "$SERVICE" >&2
    exit 1
fi

printf 'Proxy HTTPS ativado para %s.\n' "$DOMAIN"
printf 'O Caddy solicitará e renovará o certificado automaticamente quando as portas públicas TCP 80 e 443 chegarem a este servidor.\n'
printf 'A porta 8000 agora aceita conexões somente locais; ajuste o roteador para encaminhar 80 e 443 ao servidor.\n'
printf 'Verifique: systemctl status caddy %s\n' "$SERVICE"
printf 'Logs ACME: journalctl -u caddy -n 100\n'
if ! curl --fail --silent --show-error --max-time 45 "https://$DOMAIN/login" -o /dev/null; then
    printf 'O HTTPS ainda não respondeu. Verifique DNS público, encaminhamento/firewall TCP 80 e 443 e os logs do Caddy.\n' >&2
fi