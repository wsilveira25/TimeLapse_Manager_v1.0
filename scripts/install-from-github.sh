#!/usr/bin/env bash
# Instalação pública do Timelapse Manager (Linux Debian/Ubuntu com systemd):
#   curl -fsSL https://raw.githubusercontent.com/wsilveira25/TimeLapse_Manager_v1.0/main/scripts/install-from-github.sh | sudo bash
# Opcional: bash install-from-github.sh [OWNER/REPOSITORY] [vX.Y.Z]
set -euo pipefail

REPOSITORY="${1:-wsilveira25/TimeLapse_Manager_v1.0}"
VERSION="${2:-latest}"

if [[ "$(id -u)" -ne 0 ]]; then
    printf 'Execute como root: curl -fsSL <url> | sudo bash\n' >&2
    exit 1
fi

TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

missing=()
for pkg in curl python3 unzip ffmpeg; do
    command -v "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || missing+=(python3-venv)
if [[ ${#missing[@]} -gt 0 ]]; then
    command -v apt-get >/dev/null 2>&1 || { printf 'Instale manualmente: %s\n' "${missing[*]}" >&2; exit 1; }
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
    apt-get update </dev/null
    apt-get install --yes "${missing[@]}" </dev/null
fi

if [[ "$VERSION" == "latest" ]]; then
    BASE_URL="https://github.com/$REPOSITORY/releases/latest/download"
else
    BASE_URL="https://github.com/$REPOSITORY/releases/download/$VERSION"
fi

SUMS="$TEMP_ROOT/SHA256SUMS.txt"
curl -fsSL --retry 3 -o "$SUMS" "$BASE_URL/SHA256SUMS.txt"
ARCHIVE_NAME="$(awk '{ sub(/\r$/, "", $2) } $2 ~ /^timelapse-manager-[0-9.]+\.zip$/ { print $2; exit }' "$SUMS")"
[[ -n "$ARCHIVE_NAME" ]] || { printf 'A Release não contém o pacote Linux.\n' >&2; exit 1; }
ARCHIVE="$TEMP_ROOT/$ARCHIVE_NAME"
curl -fsSL --retry 3 -o "$ARCHIVE" "$BASE_URL/$ARCHIVE_NAME"

EXPECTED_HASH="$(awk -v file="$ARCHIVE_NAME" '{ sub(/\r$/, "", $2) } $2 == file { print tolower($1); exit }' "$SUMS")"
ACTUAL_HASH="$(sha256sum "$ARCHIVE" | awk '{print tolower($1)}')"
[[ -n "$EXPECTED_HASH" && "$EXPECTED_HASH" == "$ACTUAL_HASH" ]] || { printf 'O SHA256 do pacote não corresponde ao manifesto.\n' >&2; exit 1; }

unzip -q "$ARCHIVE" -d "$TEMP_ROOT"
INSTALLER="$TEMP_ROOT/TimelapseManager/scripts/install-linux.sh"
[[ -f "$INSTALLER" ]] || { printf 'O instalador Linux não foi encontrado no pacote.\n' >&2; exit 1; }

# As perguntas do instalador precisam do terminal, mesmo quando o script vem de um pipe.
if [[ -r /dev/tty ]]; then
    TIMELAPSE_UPDATE_REPO="$REPOSITORY" bash "$INSTALLER" </dev/tty
else
    TIMELAPSE_UPDATE_REPO="$REPOSITORY" bash "$INSTALLER"
fi
