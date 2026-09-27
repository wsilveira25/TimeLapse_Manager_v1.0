#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
fi

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="/opt/timelapse-manager"
DATA_ROOT="/var/lib/timelapse"
TEMP_ROOT="/var/temp"
CREDENTIAL_ROOT="/etc/credstore.encrypted"
TLS_ROOT="/etc/timelapse"
SERVICE_USER="timelapse"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TEMP_PASSWORD=""
TEMP_SECRET=""
cleanup() {
    [[ -z "$TEMP_PASSWORD" ]] || rm -f "$TEMP_PASSWORD"
    [[ -z "$TEMP_SECRET" ]] || rm -f "$TEMP_SECRET"
}
trap cleanup EXIT

for command in "$PYTHON_BIN" systemd-creds systemctl ffmpeg tar; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'Dependência ausente: %s. Instale Python 3.11+, systemd, tar e FFmpeg e repita.\n' "$command" >&2
        exit 1
    }
done
systemd-creds setup

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Python 3.11 ou superior é necessário")'

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_ROOT" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o root -g "$SERVICE_USER" -m 0750 "$INSTALL_ROOT"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_ROOT" "$TEMP_ROOT"
install -d -o root -g root -m 0700 "$CREDENTIAL_ROOT"
install -d -o root -g "$SERVICE_USER" -m 0750 "$TLS_ROOT"

tar -C "$SOURCE_ROOT" \
    --exclude='./venv' --exclude='./.venv' --exclude='./dist' --exclude='./tests' \
    --exclude='./timelapse.db' --exclude='./*.db' --exclude='__pycache__' --exclude='*.pyc' \
    -cf - . | tar -C "$INSTALL_ROOT" -xf -

"$PYTHON_BIN" -m venv "$INSTALL_ROOT/venv"
"$INSTALL_ROOT/venv/bin/python" -m pip install --upgrade pip
"$INSTALL_ROOT/venv/bin/python" -m pip install -r "$INSTALL_ROOT/requirements.txt"

if [[ ! -f "$CREDENTIAL_ROOT/TIMELAPSE_PASSWORD" ]]; then
    read -r -s -p 'Senha inicial do admin (mínimo 12 caracteres): ' ADMIN_PASSWORD
    printf '\n'
    if [[ ${#ADMIN_PASSWORD} -lt 12 ]]; then
        printf 'A senha deve ter pelo menos 12 caracteres.\n' >&2
        exit 1
    fi
    TEMP_PASSWORD="$(mktemp)"
    chmod 600 "$TEMP_PASSWORD"
    printf '%s' "$ADMIN_PASSWORD" > "$TEMP_PASSWORD"
    unset ADMIN_PASSWORD
    systemd-creds encrypt --name=TIMELAPSE_PASSWORD "$TEMP_PASSWORD" "$CREDENTIAL_ROOT/TIMELAPSE_PASSWORD"
    rm -f "$TEMP_PASSWORD"
    TEMP_PASSWORD=""
fi

if [[ ! -f "$CREDENTIAL_ROOT/TIMELAPSE_SESSION_SECRET" ]]; then
    TEMP_SECRET="$(mktemp)"
    chmod 600 "$TEMP_SECRET"
    "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48), end="")' > "$TEMP_SECRET"
    systemd-creds encrypt --name=TIMELAPSE_SESSION_SECRET "$TEMP_SECRET" "$CREDENTIAL_ROOT/TIMELAPSE_SESSION_SECRET"
    rm -f "$TEMP_SECRET"
    TEMP_SECRET=""
fi

DEFAULT_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
DEFAULT_HOST="${DEFAULT_HOST:-$(hostname -f 2>/dev/null || hostname)}"
read -r -p "Hostname/IP de acesso [$DEFAULT_HOST]: " SERVER_HOST
SERVER_HOST="${SERVER_HOST:-$DEFAULT_HOST}"
if [[ ! "$SERVER_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
    printf 'Use um hostname DNS ou endereço IPv4 simples.\n' >&2
    exit 1
fi

if [[ ! -f "$TLS_ROOT/timelapse.crt" || ! -f "$TLS_ROOT/timelapse.key" ]]; then
    "$INSTALL_ROOT/venv/bin/python" "$INSTALL_ROOT/scripts/create_self_signed_cert.py" \
        --hostname "$SERVER_HOST" --cert "$TLS_ROOT/timelapse.crt" --key "$TLS_ROOT/timelapse.key"
    chown root:"$SERVICE_USER" "$TLS_ROOT/timelapse.key"
    chmod 0640 "$TLS_ROOT/timelapse.key"
fi

if [[ ! -f "$DATA_ROOT/developer_signature.txt" ]]; then
    read -r -p 'Assinatura de desenvolvimento [Desenvolvido com Timelapse Manager]: ' DEVELOPER_SIGNATURE
    printf '%s\n' "${DEVELOPER_SIGNATURE:-Desenvolvido com Timelapse Manager}" > "$DATA_ROOT/developer_signature.txt"
fi

chown -R root:"$SERVICE_USER" "$INSTALL_ROOT"
chmod -R g+rX "$INSTALL_ROOT"
chown "$SERVICE_USER":"$SERVICE_USER" "$DATA_ROOT" "$TEMP_ROOT"
chown "$SERVICE_USER":"$SERVICE_USER" "$DATA_ROOT/developer_signature.txt"

cat > /etc/systemd/system/timelapse-manager.service <<UNIT
[Unit]
Description=Timelapse Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_ROOT
Environment=PYTHONUNBUFFERED=1
Environment=TIMELAPSE_USERNAME=admin
Environment=TIMELAPSE_DATA_DIR=$DATA_ROOT
Environment=TIMELAPSE_TEMP_DIR=$TEMP_ROOT
Environment=TIMELAPSE_DATABASE_URL=sqlite:////var/lib/timelapse/timelapse.db
Environment=TIMELAPSE_PUBLIC_URL=https://$SERVER_HOST:8000
Environment=TIMELAPSE_SSL_CERTFILE=$TLS_ROOT/timelapse.crt
Environment=TIMELAPSE_SSL_KEYFILE=$TLS_ROOT/timelapse.key
LoadCredentialEncrypted=TIMELAPSE_PASSWORD:$CREDENTIAL_ROOT/TIMELAPSE_PASSWORD
LoadCredentialEncrypted=TIMELAPSE_SESSION_SECRET:$CREDENTIAL_ROOT/TIMELAPSE_SESSION_SECRET
StateDirectory=timelapse
StateDirectoryMode=0750
UMask=0077
ExecStart=$INSTALL_ROOT/venv/bin/python $INSTALL_ROOT/run_server.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
PrivateTmp=yes
ReadWritePaths=$TEMP_ROOT

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now timelapse-manager.service
printf '\nInstalado. Abra https://%s:8000 e aceite/confie no certificado local.\n' "$SERVER_HOST"
printf 'Estado: systemctl status timelapse-manager.service\n'