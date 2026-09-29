#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="${1:?Uso: install-from-github.sh OWNER/REPOSITORY [TAG]}"
VERSION="${2:-latest}"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

command -v gh >/dev/null 2>&1 || {
    printf 'Instale GitHub CLI e autentique com gh auth login para acessar o repositório privado.\n' >&2
    exit 1
}
gh auth status --hostname github.com >/dev/null

DOWNLOAD_ARGS=(release download)
if [[ "$VERSION" != "latest" ]]; then
    DOWNLOAD_ARGS+=("$VERSION")
fi
DOWNLOAD_ARGS+=(--repo "$REPOSITORY" --pattern 'timelapse-manager-*.zip' --pattern 'SHA256SUMS.txt' --dir "$TEMP_ROOT")
gh "${DOWNLOAD_ARGS[@]}"
ARCHIVE="$(find "$TEMP_ROOT" -maxdepth 1 -type f -name 'timelapse-manager-*.zip' -print -quit)"
[[ -n "$ARCHIVE" ]] || { printf 'A Release não contém o pacote Linux.\n' >&2; exit 1; }
SUMS="$TEMP_ROOT/SHA256SUMS.txt"
[[ -f "$SUMS" ]] || { printf 'A Release não contém SHA256SUMS.txt.\n' >&2; exit 1; }
EXPECTED_HASH="$(awk -v file="$(basename "$ARCHIVE")" '{ sub(/\r$/, "", $2) } $2 == file { print tolower($1); exit }' "$SUMS")"
[[ -n "$EXPECTED_HASH" ]] || { printf 'O SHA256 do pacote não foi encontrado na Release.\n' >&2; exit 1; }
ACTUAL_HASH="$(sha256sum "$ARCHIVE" | awk '{print tolower($1)}')"
[[ "$EXPECTED_HASH" == "$ACTUAL_HASH" ]] || { printf 'O SHA256 do pacote não corresponde ao manifesto.\n' >&2; exit 1; }

unzip -q "$ARCHIVE" -d "$TEMP_ROOT"
INSTALLER="$TEMP_ROOT/TimelapseManager/scripts/install-linux.sh"
[[ -f "$INSTALLER" ]] || { printf 'O instalador Linux não foi encontrado no pacote.\n' >&2; exit 1; }
bash "$INSTALLER"