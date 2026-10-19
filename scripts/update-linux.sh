#!/usr/bin/env bash
# Atualizador do Timelapse Manager (Linux/systemd).
#   sudo bash scripts/update-linux.sh --setup [SERVICO]   ativa o atualizador (uma vez)
# Sem argumentos, executa a atualização solicitada pelo painel (unit timelapse-update.service).
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
fi

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_ROOT="${TIMELAPSE_APP_ROOT:-$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)}"
SERVICE="${TIMELAPSE_APP_SERVICE:-timelapse-manager}"
DATA_DIR="${TIMELAPSE_DATA_DIR:-/var/lib/timelapse}"
REPOSITORY="${TIMELAPSE_UPDATE_REPO:-wsilveira25/TimeLapse_Manager_v1.0}"
UNIT_DIR=/etc/systemd/system
REQUEST_FILE="$DATA_DIR/update.request"
STATUS_FILE="$DATA_DIR/update-status.json"
BACKUP_DIR="$DATA_DIR/backups"

setup_units() {
    [[ -n "${1:-}" ]] && SERVICE="$1"
    systemctl cat "$SERVICE.service" >/dev/null 2>&1 || {
        printf 'Serviço %s.service não encontrado. Informe o nome correto: --setup NOME\n' "$SERVICE" >&2
        exit 1
    }
    cat > "$UNIT_DIR/timelapse-update.service" <<UNIT
[Unit]
Description=Atualiza o Timelapse Manager

[Service]
Type=oneshot
Environment=TIMELAPSE_APP_ROOT=$APP_ROOT
Environment=TIMELAPSE_APP_SERVICE=$SERVICE
Environment=TIMELAPSE_DATA_DIR=$DATA_DIR
Environment=TIMELAPSE_UPDATE_REPO=$REPOSITORY
ExecStart=/bin/bash $APP_ROOT/scripts/update-linux.sh
TimeoutStartSec=1800
UNIT
    cat > "$UNIT_DIR/timelapse-update.path" <<UNIT
[Unit]
Description=Aguarda pedidos de atualização do Timelapse Manager

[Path]
PathExists=$REQUEST_FILE
Unit=timelapse-update.service

[Install]
WantedBy=multi-user.target
UNIT
    install -d -m 0755 "$BACKUP_DIR"
    systemctl daemon-reload
    systemctl enable --now timelapse-update.path
    printf 'Atualizador ativado para %s.service. Use Configurações → Atualizações no painel.\n' "$SERVICE"
}

if [[ "${1:-}" == "--setup" ]]; then
    setup_units "${2:-}"
    exit 0
fi

# Executa uma cópia para que a troca dos arquivos do app não altere o script em uso.
if [[ -z "${TIMELAPSE_UPDATER_COPY:-}" ]]; then
    COPY="$(mktemp)"
    cp "$SCRIPT_PATH" "$COPY"
    TIMELAPSE_UPDATER_COPY=1 exec bash "$COPY" "$@"
fi

DATA_OWNER="$(stat -c '%U:%G' "$DATA_DIR")"
APP_OWNER="$(stat -c '%U:%G' "$APP_ROOT")"
PYTHON="$APP_ROOT/venv/bin/python"
WORK="$(mktemp -d)"
TAG=""
BACKUP_FILE=""
APPLIED=0

write_status() {
    local state="$1" message="${2//[\"\\]/}"
    printf '{"state":"%s","message":"%s","version":"%s","updated_at":%s}\n' \
        "$state" "$message" "${TAG#v}" "$(date +%s)" > "$STATUS_FILE.tmp"
    chmod 0644 "$STATUS_FILE.tmp"
    chown "$DATA_OWNER" "$STATUS_FILE.tmp"
    mv -f "$STATUS_FILE.tmp" "$STATUS_FILE"
}

rollback() {
    [[ -n "$BACKUP_FILE" && -f "$BACKUP_FILE" ]] || return 0
    printf 'Revertendo para a versão anterior...\n' >&2
    for item in app scripts; do rm -rf "${APP_ROOT:?}/$item"; done
    tar -C "$APP_ROOT" -xzf "$BACKUP_FILE"
    systemctl restart "$SERVICE.service" || true
}

on_exit() {
    local code=$?
    rm -rf "$WORK"; rm -f "$0"
    if [[ $code -ne 0 ]]; then
        [[ "$APPLIED" -eq 1 ]] && rollback
        write_status error "A atualização falhou e a versão anterior foi mantida. Consulte: journalctl -u timelapse-update.service"
    fi
}
trap on_exit EXIT

[[ -f "$REQUEST_FILE" ]] || { printf 'Nenhum pedido de atualização.\n'; trap - EXIT; rm -rf "$WORK"; exit 0; }
TAG="$(head -c 32 "$REQUEST_FILE" | tr -d '[:space:]')"
rm -f "$REQUEST_FILE"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'Versão inválida no pedido.\n' >&2; TAG=""; exit 1; }

CURRENT="$(tr -d '[:space:]' < "$APP_ROOT/VERSION" 2>/dev/null || echo 0.0.0)"
if [[ "$CURRENT" == "${TAG#v}" ]]; then
    write_status success "O sistema já está na versão ${TAG#v}."
    trap - EXIT; rm -rf "$WORK"
    exit 0
fi

write_status running "Baixando a versão ${TAG#v}..."
ARCHIVE_NAME="timelapse-manager-${TAG#v}.zip"
BASE_URL="https://github.com/$REPOSITORY/releases/download/$TAG"
curl -fsSL --retry 3 --max-time 600 -o "$WORK/$ARCHIVE_NAME" "$BASE_URL/$ARCHIVE_NAME"
curl -fsSL --retry 3 --max-time 60 -o "$WORK/SHA256SUMS.txt" "$BASE_URL/SHA256SUMS.txt"
EXPECTED="$(awk -v f="$ARCHIVE_NAME" '{ sub(/\r$/, "", $2) } $2 == f { print tolower($1); exit }' "$WORK/SHA256SUMS.txt")"
ACTUAL="$(sha256sum "$WORK/$ARCHIVE_NAME" | awk '{print tolower($1)}')"
[[ -n "$EXPECTED" && "$EXPECTED" == "$ACTUAL" ]] || { printf 'SHA256 do pacote não confere.\n' >&2; exit 1; }

write_status running "Criando backup e aplicando a versão ${TAG#v}..."
"$PYTHON" -m zipfile -e "$WORK/$ARCHIVE_NAME" "$WORK/extract"
NEW_ROOT="$WORK/extract/TimelapseManager"
[[ -f "$NEW_ROOT/VERSION" && -f "$NEW_ROOT/run_server.py" ]] || { printf 'Pacote inválido.\n' >&2; exit 1; }

install -d -m 0700 "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/app-$CURRENT-$(date +%Y%m%d%H%M%S).tar.gz"
tar -C "$APP_ROOT" --exclude='./venv' --exclude='./.venv' --exclude='./.git' --exclude='./dist' \
    --exclude='./*.db' --exclude='__pycache__' -czf "$BACKUP_FILE" .
if [[ -f "$DATA_DIR/timelapse.db" ]]; then
    "$PYTHON" - "$DATA_DIR/timelapse.db" "$BACKUP_DIR/timelapse-$CURRENT-$(date +%Y%m%d%H%M%S).db" <<'PY'
import sqlite3, sys
source, target = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
PY
fi
for pattern in 'app-*.tar.gz' 'timelapse-*.db'; do
    find "$BACKUP_DIR" -maxdepth 1 -name "$pattern" -printf '%T@ %p\n' | sort -rn | tail -n +4 | cut -d' ' -f2- | xargs -r rm -f
done

APPLIED=1
for item in app scripts; do rm -rf "${APP_ROOT:?}/$item"; done
tar -C "$NEW_ROOT" --no-same-owner --exclude='./venv' --exclude='./tests' --exclude='./*.db' \
    --exclude='__pycache__' --exclude='*.pyc' -cf - . | tar -C "$APP_ROOT" --no-same-owner -xf -
chown -R "$APP_OWNER" "$APP_ROOT/app" "$APP_ROOT/scripts"
chmod -R g+rX "$APP_ROOT/app" "$APP_ROOT/scripts"

write_status running "Instalando dependências..."
"$PYTHON" -m pip install --quiet -r "$APP_ROOT/requirements.txt"

write_status running "Reiniciando o serviço..."
systemctl restart "$SERVICE.service"
sleep 20
systemctl is-active --quiet "$SERVICE.service" || { printf 'O serviço não iniciou após a atualização.\n' >&2; exit 1; }
sleep 10
systemctl is-active --quiet "$SERVICE.service" || { printf 'O serviço ficou instável após a atualização.\n' >&2; exit 1; }

APPLIED=0
write_status success "Atualizado da versão $CURRENT para ${TAG#v}."
printf 'Atualização concluída: %s -> %s\n' "$CURRENT" "${TAG#v}"
