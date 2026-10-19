import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import requests

APP_ROOT = Path(__file__).resolve().parents[2]
UPDATE_REPOSITORY = os.getenv("TIMELAPSE_UPDATE_REPO", "wsilveira25/TimeLapse_Manager_v1.0")
DATA_DIR = Path(os.getenv("TIMELAPSE_DATA_DIR", "/var/lib/timelapse"))
REQUEST_PATH = DATA_DIR / "update.request"
STATUS_PATH = DATA_DIR / "update-status.json"
UPDATER_UNIT_PATH = Path("/etc/systemd/system/timelapse-update.path")
CACHE_TTL_SECONDS = 3600
TAG_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")

_cache_lock = threading.Lock()
_cache = {"checked_at": 0.0, "data": None, "error": ""}


def versao_atual() -> str:
    for base in (APP_ROOT, Path(getattr(sys, "_MEIPASS", APP_ROOT))):
        try:
            return (base / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.0.0"


def _versao_tupla(valor: str) -> tuple[int, ...]:
    partes = re.findall(r"\d+", valor or "")
    return tuple(int(parte) for parte in partes[:3]) or (0,)


def atualizacao_disponivel(atual: str, ultima: str) -> bool:
    return bool(ultima) and _versao_tupla(ultima) > _versao_tupla(atual)


def plataforma_linux() -> bool:
    return sys.platform.startswith("linux")


def atualizador_instalado() -> bool:
    return plataforma_linux() and UPDATER_UNIT_PATH.is_file()


def _buscar_ultima_release() -> dict:
    resposta = requests.get(
        f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "TimelapseManager-Updater"},
        timeout=15,
    )
    resposta.raise_for_status()
    release = resposta.json()
    tag = release.get("tag_name", "")
    if not TAG_PATTERN.match(tag):
        raise ValueError("A última Release não usa uma tag de versão válida.")
    instalador = next(
        (a.get("browser_download_url", "") for a in release.get("assets", []) if re.match(r"TimelapseManager-Setup-.*\.exe$", a.get("name", ""))),
        "",
    )
    return {
        "tag": tag,
        "version": tag.lstrip("v"),
        "notes": (release.get("body") or "")[:4000],
        "url": release.get("html_url", ""),
        "windows_installer_url": instalador,
    }


def consultar_ultima_release(forcar: bool = False) -> tuple[dict | None, str]:
    with _cache_lock:
        if not forcar and _cache["checked_at"] and time.time() - _cache["checked_at"] < CACHE_TTL_SECONDS:
            return _cache["data"], _cache["error"]
        try:
            _cache["data"] = _buscar_ultima_release()
            _cache["error"] = ""
        except (requests.RequestException, ValueError):
            _cache["error"] = "Não foi possível consultar as atualizações. Verifique a conexão com a Internet."
        _cache["checked_at"] = time.time()
        return _cache["data"], _cache["error"]


def ler_status_atualizacao() -> dict:
    try:
        status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle", "message": "", "version": "", "updated_at": 0}
    if status.get("state") == "running" and not REQUEST_PATH.exists() and time.time() - int(status.get("updated_at", 0)) > 1800:
        status["state"] = "error"
        status["message"] = "A atualização anterior não foi concluída."
    return status


def atualizacao_em_andamento() -> bool:
    return REQUEST_PATH.exists() or ler_status_atualizacao().get("state") == "running"


def solicitar_atualizacao(tag: str) -> None:
    if not TAG_PATTERN.match(tag or ""):
        raise ValueError("Versão inválida.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporario = REQUEST_PATH.with_suffix(".tmp")
    temporario.write_text(tag, encoding="utf-8")
    os.replace(temporario, REQUEST_PATH)


def resumo_atualizacao(forcar: bool = False) -> dict:
    atual = versao_atual()
    release, erro = consultar_ultima_release(forcar)
    ultima = release["version"] if release else ""
    return {
        "current_version": atual,
        "latest_version": ultima,
        "update_available": atualizacao_disponivel(atual, ultima),
        "release_notes": release["notes"] if release else "",
        "release_url": release["url"] if release else "",
        "windows_installer_url": release["windows_installer_url"] if release else "",
        "check_error": erro,
        "platform": "linux" if plataforma_linux() else "other",
        "updater_installed": atualizador_instalado(),
        "status": ler_status_atualizacao(),
    }


def verificar_atualizacao_automatica() -> None:
    from app.core.database import SessionLocal
    from app.models.storage import SystemSettings

    with SessionLocal() as db:
        settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
        if not settings or not settings.auto_update_enabled:
            return
    if not atualizador_instalado() or atualizacao_em_andamento():
        return
    release, _ = consultar_ultima_release(forcar=True)
    if release and atualizacao_disponivel(versao_atual(), release["version"]):
        solicitar_atualizacao(release["tag"])
