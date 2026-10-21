import copy
import ipaddress
import logging
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import tempfile
import time
from urllib.parse import urlsplit
from pathlib import Path
from typing import Literal

from sqlalchemy import func, or_
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse
from app.core.auth import require_admin
from app.core.database import SessionLocal, get_db
from app.core.oauth_urls import obter_oauth_redirect_uri, validar_oauth_redirect_uri
from app.models.storage import LocalStorageLocation, StorageBackupFile, StorageConfig, SystemSettings
from app.services.cloud_storage import (
    build_authorization_url,
    exchange_authorization_code,
    get_storage_quota,
)
from app.services.sync_service import iniciar_sincronizacao_pendentes
from app.services.sync_service import obter_espaco_smb
from app.services.backup_service import iniciar_backup_assincrono, obter_status_backup
from app.services.alert_service import (
    buscar_config_smtp,
    criptografar_senha_smtp,
    enviar_email_configurado,
    resumo_config_smtp,
)
from app.services.local_storage import (
    DEFAULT_LOCATION_ID,
    local_disponivel,
    listar_locais_armazenamento,
    somar_uso_locais_armazenamento,
)
from app.services.update_service import (
    atualizacao_disponivel,
    atualizacao_em_andamento,
    atualizador_instalado,
    consultar_ultima_release,
    resumo_atualizacao,
    solicitar_atualizacao,
    versao_atual,
)
from app.tasks.scheduler import scheduler as app_scheduler, executar_reinicio_processo

router = APIRouter(prefix="/api/system", tags=["Sistema"])
logger = logging.getLogger(__name__)

#PASTA_POOL_BASE = "/var/temp/Capturas"
TAMANHO_PADRAO_FALLBACK = 450 * 1024  # 450 KB de segurança caso ainda não haja fotos
OAUTH_CALLBACK_PATH = "/api/system/storage/oauth/callback"
GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DOMAIN_CONFIG_PATH = Path(os.getenv("TIMELAPSE_DOMAIN_CONFIG_FILE", "/var/lib/timelapse/domain.env"))
DOMAIN_SETUP_UNIT = "timelapse-domain-setup.service"
DOMAIN_SETUP_UNIT_PATH = Path("/etc/systemd/system") / DOMAIN_SETUP_UNIT
DOMAIN_SETUP_SUDOERS_PATH = Path("/etc/sudoers.d/timelapse-domain-setup")
SYSTEMCTL_PATH = "/usr/bin/systemctl"
SUDO_PATH = "/usr/bin/sudo"


def normalizar_dominio(dominio: str) -> str:
    dominio = (dominio or "").strip()
    if not dominio:
        raise ValueError("Informe um domínio para configurar o HTTPS.")
    if "://" in dominio:
        parsed = urlsplit(dominio)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Use um domínio simples ou uma URL iniciada por https://.")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("A porta informada é inválida.") from exc
        if port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Informe somente o domínio, sem porta diferente de 443, caminho ou parâmetros.")
        dominio = parsed.hostname
    dominio = dominio.rstrip(".")
    if any(char in dominio for char in "/:@?#"):
        raise ValueError("Informe somente o hostname, sem porta ou caminho.")
    try:
        dominio = dominio.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("O domínio informado não é válido.") from exc
    if len(dominio) > 253 or "." not in dominio:
        raise ValueError("Informe um domínio DNS público completo, como timelapse.exemplo.com.")
    try:
        ipaddress.ip_address(dominio)
    except ValueError:
        pass
    else:
        raise ValueError("Informe um domínio DNS, não um endereço IP.")
    labels = dominio.split(".")
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("O hostname contém um rótulo DNS inválido.")
    return dominio


def salvar_dominio_helper(dominio: str) -> None:
    DOMAIN_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=DOMAIN_CONFIG_PATH.parent,
            prefix=".domain-",
            delete=False,
        ) as temporary_file:
            temporary_file.write(f"DOMAIN_NAME={dominio}\n")
            temporary_path = Path(temporary_file.name)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, DOMAIN_CONFIG_PATH)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def status_servico_dominio() -> dict[str, str]:
    if not DOMAIN_SETUP_UNIT_PATH.is_file():
        return {"active_state": "not-installed", "result": "unknown"}
    try:
        result = subprocess.run(
            [SYSTEMCTL_PATH, "show", DOMAIN_SETUP_UNIT, "--property=ActiveState", "--property=SubState", "--property=Result"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"active_state": "unknown", "result": "unknown"}
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "result": values.get("Result", "unknown"),
    }


def certificado_dominio_valido(dominio: str) -> tuple[bool, str]:
    if not dominio:
        return False, ""
    context = ssl.create_default_context()
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=2) as connection:
            with context.wrap_socket(connection, server_hostname=dominio) as secured_connection:
                certificate = secured_connection.getpeercert()
        return True, certificate.get("notAfter", "")
    except (OSError, ssl.SSLError, TimeoutError):
        return False, ""


def google_drive_file_scope_concedido(scope: str) -> bool:
    return GOOGLE_DRIVE_FILE_SCOPE in (scope or "").split()


def build_provider_summary(
    nome: str,
    total_bytes: int,
    usado_bytes: int,
    ativo: bool = True,
    tipo: str = "local",
    conectado: bool = True,
):
    total = max(int(total_bytes or 0), 0)
    usado = max(int(usado_bytes or 0), 0)
    livre = max(total - usado, 0)
    percent = round((usado / total) * 100, 1) if total > 0 else 0.0

    return {
        "nome": nome,
        "tipo": tipo,
        "ativo": bool(ativo),
        "total_bytes": total,
        "usado_bytes": usado,
        "livre_bytes": livre,
        "percentagem_uso": percent,
        "conectado": conectado,
    }


def build_storage_forecast(total_bytes: int, livre_bytes: int, tamanho_medio: float, capturas_realizadas: int):
    tamanho_medio = max(tamanho_medio, 1)
    return {
        "capturas_realizadas": capturas_realizadas,
        "capturas_totais": int(max(total_bytes, 0) // tamanho_medio),
        "capturas_restantes": int(max(livre_bytes, 0) // tamanho_medio),
        "tamanho_medio_kb": round(tamanho_medio / 1024),
    }


def resumir_erro_backup(erro: str) -> str:
    if not erro:
        return ""
    return "O backup encontrou arquivos pendentes. Eles serão tentados novamente automaticamente."


def contar_frames_no_backup(db: Session, storage_config_id: int) -> int:
    return db.query(func.count(StorageBackupFile.id)).filter(
        StorageBackupFile.storage_config_id == storage_config_id,
        or_(
            StorageBackupFile.local_path.like("Capturas/%"),
            StorageBackupFile.local_path.like("local-%/Capturas/%"),
        ),
        StorageBackupFile.backed_up_at > 0,
        StorageBackupFile.last_error == "",
    ).scalar() or 0


def _calcular_status_armazenamento(db: Session):
    total, used, free = somar_uso_locais_armazenamento(db)

    arquivos_jpg = []
    seen_captures = set()
    for location in sorted(listar_locais_armazenamento(db), key=lambda item: len(item["caminho"]), reverse=True):
        if not location["padrao"] and not local_disponivel(location["caminho"]):
            continue
        captures_root = Path(location["caminho"]) / "Capturas"
        for root, dirs, files in os.walk(captures_root):
            for filename in files:
                if filename.endswith('.jpg'):
                    path = Path(root) / filename
                    resolved_path = path.resolve()
                    if resolved_path in seen_captures:
                        continue
                    seen_captures.add(resolved_path)
                    arquivos_jpg.append(str(path))

    capturas_realizadas = len(arquivos_jpg)
    tamanho_medio = TAMANHO_PADRAO_FALLBACK

    if capturas_realizadas > 0:
        amostra = arquivos_jpg[-100:]
        tamanho_total_amostra = sum(os.path.getsize(img) for img in amostra)
        tamanho_medio = tamanho_total_amostra / len(amostra)

    capturas_restantes = int(free // tamanho_medio)
    capturas_totais_possiveis = int(total // tamanho_medio)
    percentagem_uso = (used / total) * 100 if total > 0 else 0
    tamanho_medio_kb = round(tamanho_medio / 1024)

    local_summary = build_provider_summary(
        nome="Armazenamento Local",
        total_bytes=total,
        usado_bytes=used,
        ativo=True,
        tipo="local",
    )
    livre_ratio = free / total if total else 0
    if livre_ratio <= 0.05:
        local_summary["alerta"] = "Espaço crítico: novas capturas estão pausadas até liberar disco."
        local_summary["alerta_tipo"] = "danger"
    elif livre_ratio < 0.20:
        local_summary["alerta"] = "Pouco espaço livre. A limpeza verifica cópias remotas antes de remover arquivos locais com mais de 30 dias."
        local_summary["alerta_tipo"] = "warning"

    provedores = [local_summary]
    configuracoes = db.query(StorageConfig).all()
    agora = int(time.time())

    for config in configuracoes:
        tipo = (config.tipo or '').lower()
        if tipo in {'google_drive', 'onedrive', 'smb', 'ftp', 'webdav'}:
            conectado = bool(config.oauth_refresh_token) if tipo in {'google_drive', 'onedrive'} else bool(config.host)
            deve_atualizar_quota = agora - int(config.storage_quota_checked_at or 0) >= 60
            if tipo == 'smb' and conectado and deve_atualizar_quota:
                try:
                    quota = obter_espaco_smb(config)
                    config.total_bytes = quota['total_bytes']
                    config.usado_bytes = quota['usado_bytes']
                    config.livre_bytes = quota['livre_bytes']
                    config.percentagem_uso = round((quota['usado_bytes'] / quota['total_bytes']) * 100, 1) if quota['total_bytes'] else 0
                    config.storage_error = ""
                except Exception as exc:
                    config.storage_error = str(exc)[:500]
                config.storage_quota_checked_at = agora

            if tipo in {'google_drive', 'onedrive'} and conectado and deve_atualizar_quota:
                try:
                    get_storage_quota(config)
                    config.storage_error = ""
                except Exception as exc:
                    config.storage_error = str(exc)[:500]
                config.storage_quota_checked_at = agora

            provider = build_provider_summary(
                nome=config.nome if config.nome and config.nome != "Armazenamento" else _nome_padrao_storage(tipo),
                total_bytes=int(config.total_bytes or 0),
                usado_bytes=int(config.usado_bytes or 0),
                ativo=bool(config.ativo),
                tipo=tipo,
                conectado=conectado,
            )
            provider["id"] = config.id
            provider["destino"] = _destino_storage(config)
            provider["erro"] = config.storage_error or ""
            provider["backup_ultimo_sucesso"] = int(config.backup_last_success_at or 0)
            provider["backup_erro"] = resumir_erro_backup(config.backup_error)
            capturas_backupeadas = contar_frames_no_backup(db, config.id)
            provider["capturas_backupeadas"] = capturas_backupeadas
            provider["calculo_previo"] = build_storage_forecast(
                provider["total_bytes"],
                provider["livre_bytes"],
                tamanho_medio,
                capturas_backupeadas,
            )
            provedores.append(provider)
    db.commit()

    return {
        "disco": local_summary,
        "calculo_previo": {
            "capturas_realizadas": capturas_realizadas,
            "capturas_restantes": capturas_restantes,
            "capturas_totais": capturas_totais_possiveis,
            "tamanho_medio_kb": tamanho_medio_kb,
        },
        "backup_progresso": obter_status_backup(),
        "provedores": provedores,
    }


_STORAGE_STATUS_CACHE: dict | None = None
_STORAGE_STATUS_UPDATED_AT = 0.0
_STORAGE_STATUS_REFRESHING = False
_STORAGE_STATUS_LOCK = threading.Lock()
_STORAGE_STATUS_TTL_SECONDS = 30


def _atualizar_cache_status_armazenamento():
    global _STORAGE_STATUS_CACHE, _STORAGE_STATUS_UPDATED_AT, _STORAGE_STATUS_REFRESHING
    try:
        with SessionLocal() as db:
            snapshot = _calcular_status_armazenamento(db)
        with _STORAGE_STATUS_LOCK:
            _STORAGE_STATUS_CACHE = snapshot
            _STORAGE_STATUS_UPDATED_AT = time.monotonic()
    except Exception:
        logger.exception("Falha ao atualizar métricas de armazenamento em segundo plano")
    finally:
        with _STORAGE_STATUS_LOCK:
            _STORAGE_STATUS_REFRESHING = False


def _montar_status_rapido_armazenamento(db: Session):
    total, used, free = somar_uso_locais_armazenamento(db)
    local_summary = build_provider_summary(
        nome="Armazenamento Local",
        total_bytes=total,
        usado_bytes=used,
        ativo=True,
        tipo="local",
    )
    livre_ratio = free / total if total else 0
    if livre_ratio <= 0.05:
        local_summary["alerta"] = "Espaço crítico: novas capturas estão pausadas até liberar disco."
        local_summary["alerta_tipo"] = "danger"
    elif livre_ratio < 0.20:
        local_summary["alerta"] = "Pouco espaço livre. A limpeza verifica cópias remotas antes de remover arquivos locais com mais de 30 dias."
        local_summary["alerta_tipo"] = "warning"

    provedores = [local_summary]
    for config in db.query(StorageConfig).all():
        tipo = (config.tipo or "").lower()
        if tipo not in {"google_drive", "onedrive", "smb", "ftp", "webdav"}:
            continue

        conectado = bool(config.oauth_refresh_token) if tipo in {"google_drive", "onedrive"} else bool(config.host)
        provider = build_provider_summary(
            nome=config.nome if config.nome and config.nome != "Armazenamento" else _nome_padrao_storage(tipo),
            total_bytes=int(config.total_bytes or 0),
            usado_bytes=int(config.usado_bytes or 0),
            ativo=bool(config.ativo),
            tipo=tipo,
            conectado=conectado,
        )
        provider["id"] = config.id
        provider["destino"] = _destino_storage(config)
        provider["erro"] = config.storage_error or ""
        provider["backup_ultimo_sucesso"] = int(config.backup_last_success_at or 0)
        provider["backup_erro"] = resumir_erro_backup(config.backup_error)
        capturas_backupeadas = contar_frames_no_backup(db, config.id)
        provider["capturas_backupeadas"] = capturas_backupeadas
        provider["calculo_previo"] = build_storage_forecast(
            provider["total_bytes"],
            provider["livre_bytes"],
            TAMANHO_PADRAO_FALLBACK,
            capturas_backupeadas,
        )
        provedores.append(provider)

    return {
        "disco": local_summary,
        "calculo_previo": {
            "capturas_realizadas": None,
            "capturas_restantes": None,
            "capturas_totais": None,
            "tamanho_medio_kb": None,
        },
        "backup_progresso": obter_status_backup(),
        "provedores": provedores,
        "metricas_status": "atualizando",
        "metricas_atualizadas_em": None,
    }


@router.get("/storage", dependencies=[Depends(require_admin)])
def obter_status_armazenamento(db: Session = Depends(get_db)):
    global _STORAGE_STATUS_REFRESHING
    now = time.monotonic()
    with _STORAGE_STATUS_LOCK:
        cache_is_stale = (
            _STORAGE_STATUS_CACHE is None
            or now - _STORAGE_STATUS_UPDATED_AT >= _STORAGE_STATUS_TTL_SECONDS
        )
        if cache_is_stale and not _STORAGE_STATUS_REFRESHING:
            _STORAGE_STATUS_REFRESHING = True
            threading.Thread(target=_atualizar_cache_status_armazenamento, daemon=True).start()

        snapshot = copy.deepcopy(_STORAGE_STATUS_CACHE)
        refreshing = _STORAGE_STATUS_REFRESHING
        updated_at = _STORAGE_STATUS_UPDATED_AT

    if snapshot is not None:
        snapshot["metricas_status"] = "atualizando" if refreshing else "atualizado"
        snapshot["metricas_atualizadas_em"] = int(time.time() - (now - updated_at)) if updated_at else None
        return snapshot

    return _montar_status_rapido_armazenamento(db)


def _nome_padrao_storage(tipo: str) -> str:
    nomes = {
        "smb": "Armazenamento SMB",
        "ftp": "Armazenamento FTP",
        "webdav": "Armazenamento WebDAV",
        "google_drive": "Google Drive",
        "onedrive": "OneDrive",
    }
    return nomes.get(tipo.lower(), tipo.upper())


def _destino_storage(config: StorageConfig) -> str:
    tipo = (config.tipo or "").lower()
    if tipo == "smb":
        share = (config.caminho_remoto or "").strip("/\\")
        return f"\\\\{config.host}\\{share}" if config.host and share else "Share SMB não definida"
    if tipo in {"google_drive", "onedrive"}:
        return config.caminho_remoto or "/"
    return f"{config.host}{config.caminho_remoto or '/'}"


class StorageConfigRequest(BaseModel):
    config_id: int | None = None
    criar_novo: bool = False
    nome: str = ""
    tipo: str
    host: str = ""
    usuario: str = ""
    senha: str = ""
    caminho_remoto: str = "/"
    ativo: bool = False
    oauth_client_id: str = ""
    oauth_client_secret: str = ""


class LocalStorageLocationRequest(BaseModel):
    id: int | None = None
    nome: str
    caminho: str
    ativo: bool = True


@router.get("/storage/local-locations", dependencies=[Depends(require_admin)])
def listar_destinos_locais(db: Session = Depends(get_db)):
    locations = []
    for location in listar_locais_armazenamento(db):
        path = Path(location["caminho"])
        mounted = location["padrao"] or local_disponivel(path)
        try:
            if not mounted:
                raise OSError("Disco não montado.")
            usage = os.statvfs(path)
            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bavail * usage.f_frsize
            error = ""
        except OSError as exc:
            total = 0
            free = 0
            error = str(exc)[:240]
        locations.append({
            **location,
            "montado": mounted,
            "total_bytes": total,
            "livre_bytes": free,
            "erro": error,
        })
    return locations


@router.post("/storage/local-locations", dependencies=[Depends(require_admin)])
def salvar_destino_local(dados: LocalStorageLocationRequest, db: Session = Depends(get_db)):
    nome = dados.nome.strip()
    raw_path = Path(dados.caminho).expanduser()
    if not nome or not raw_path.is_absolute():
        raise HTTPException(status_code=422, detail="Informe um nome e um caminho absoluto do servidor.")
    path = raw_path.resolve()
    if not path.is_dir():
        raise HTTPException(status_code=422, detail="A pasta não existe no servidor. Crie-a (ou monte o disco) antes de cadastrar.")
    default_path = Path(listar_locais_armazenamento(db)[0]["caminho"]).resolve()
    if path == default_path:
        raise HTTPException(status_code=422, detail="O destino principal já é gerenciado automaticamente.")
    if path in default_path.parents:
        raise HTTPException(status_code=422, detail="O destino não pode conter o diretório principal de capturas.")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise HTTPException(status_code=422, detail="O usuário do serviço não tem leitura e escrita nessa pasta (verifique o dono e o ReadWritePaths do systemd).")

    duplicate = db.query(LocalStorageLocation).filter(LocalStorageLocation.caminho == str(path)).first()
    location = db.query(LocalStorageLocation).filter(LocalStorageLocation.id == dados.id).first() if dados.id else duplicate
    if duplicate and location and duplicate.id != location.id:
        raise HTTPException(status_code=409, detail="Esse ponto de montagem já está cadastrado.")
    if location is None:
        location = LocalStorageLocation(nome=nome, caminho=str(path), ativo=dados.ativo)
        db.add(location)
    else:
        location.nome = nome
        location.caminho = str(path)
        location.ativo = dados.ativo
    db.commit()
    db.refresh(location)
    return {"id": location.id, "nome": location.nome, "caminho": location.caminho, "ativo": location.ativo}


@router.delete("/storage/local-locations/{location_id}", dependencies=[Depends(require_admin)])
def excluir_destino_local(location_id: int, db: Session = Depends(get_db)):
    if location_id == DEFAULT_LOCATION_ID:
        raise HTTPException(status_code=400, detail="O destino principal não pode ser removido.")
    location = db.query(LocalStorageLocation).filter(LocalStorageLocation.id == location_id).first()
    if not location:
        raise HTTPException(status_code=404, detail="Destino local não encontrado.")
    db.delete(location)
    db.commit()
    return {"sucesso": True, "mensagem": "Destino removido da lista; os arquivos no disco foram preservados."}


class AlertSettingsRequest(BaseModel):
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str | None = None
    clear_smtp_password: bool = False
    smtp_sender: str = ""
    smtp_recipients: str = ""
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    alert_backup_failure: bool = True
    alert_disk_warning: bool = True
    alert_disk_critical: bool = True
    alert_camera_failure: bool = True
    alert_cooldown_minutes: int = 30


class DomainSettingsRequest(BaseModel):
    domain_name: str = ""


@router.get("/domain", dependencies=[Depends(require_admin)])
def obter_config_dominio(db: Session = Depends(get_db)):
    settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    domain_name = settings.domain_name if settings else ""
    expected_public_url = f"https://{domain_name}" if domain_name else ""
    expected_callback = f"{expected_public_url}{OAUTH_CALLBACK_PATH}" if domain_name else ""
    configured_public_url = os.getenv("TIMELAPSE_PUBLIC_URL", "").rstrip("/")
    configured_callback = os.getenv("TIMELAPSE_OAUTH_REDIRECT_URI", "").strip()
    provision_status = status_servico_dominio()
    certificate_valid, certificate_expires_at = certificado_dominio_valido(domain_name)
    return {
        "domain_name": domain_name,
        "public_url": configured_public_url,
        "oauth_redirect_uri": expected_callback,
        "aplicado": bool(domain_name and configured_public_url == expected_public_url and configured_callback == expected_callback),
        "provisioner_installed": DOMAIN_SETUP_UNIT_PATH.is_file() and DOMAIN_SETUP_SUDOERS_PATH.is_file(),
        "provisioning_state": provision_status["active_state"],
        "provisioning_result": provision_status["result"],
        "provisioning_in_progress": provision_status["active_state"] in {"activating", "reloading"},
        "certificate_valid": certificate_valid,
        "certificate_expires_at": certificate_expires_at,
        "script_path": str(Path(__file__).resolve().parents[2] / "scripts" / "configure-domain.sh"),
    }


@router.post("/domain", dependencies=[Depends(require_admin)])
def salvar_config_dominio(dados: DomainSettingsRequest, db: Session = Depends(get_db)):
    try:
        domain_name = normalizar_dominio(dados.domain_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if settings is None:
        settings = SystemSettings(id=1)
        db.add(settings)
    settings.domain_name = domain_name
    try:
        db.flush()
        salvar_dominio_helper(domain_name)
    except OSError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Não foi possível gravar o domínio para o serviço de certificados.") from exc
    db.commit()
    return {"sucesso": True, "domain_name": domain_name, "mensagem": "Domínio salvo. Use Gerar certificado para configurar HTTPS."}


@router.post("/domain/provision", status_code=202, dependencies=[Depends(require_admin)])
def provisionar_certificado_dominio(db: Session = Depends(get_db)):
    settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    domain_name = settings.domain_name if settings else ""
    if not domain_name:
        raise HTTPException(status_code=400, detail="Salve um domínio válido antes de gerar o certificado.")
    if not (DOMAIN_SETUP_UNIT_PATH.is_file() and DOMAIN_SETUP_SUDOERS_PATH.is_file()):
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "configure-domain.sh"
        raise HTTPException(
            status_code=503,
            detail=f"A ativação inicial ainda não foi preparada. Execute uma vez: sudo bash {script_path} {domain_name}",
        )
    try:
        result = subprocess.run(
            [SUDO_PATH, "-n", SYSTEMCTL_PATH, "--no-block", "restart", DOMAIN_SETUP_UNIT],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail="Não foi possível iniciar o serviço de certificados.") from exc
    if result.returncode:
        raise HTTPException(status_code=503, detail="A ativação privilegiada não está autorizada. Execute novamente o configurador como administrador.")
    return {"sucesso": True, "mensagem": f"Configuração HTTPS iniciada para {domain_name}. O Caddy solicitará o certificado automaticamente."}


class UpdateSettingsRequest(BaseModel):
    auto_update_enabled: bool = False


@router.get("/update", dependencies=[Depends(require_admin)])
def obter_estado_atualizacao(refresh: bool = False, db: Session = Depends(get_db)):
    settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    resumo = resumo_atualizacao(forcar=refresh)
    resumo["auto_update_enabled"] = bool(settings and settings.auto_update_enabled)
    return resumo


@router.post("/update/settings", dependencies=[Depends(require_admin)])
def salvar_config_atualizacao(dados: UpdateSettingsRequest, db: Session = Depends(get_db)):
    settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if settings is None:
        settings = SystemSettings(id=1)
        db.add(settings)
    settings.auto_update_enabled = dados.auto_update_enabled
    db.commit()
    return {"sucesso": True, "auto_update_enabled": settings.auto_update_enabled}


@router.post("/update/apply", status_code=202, dependencies=[Depends(require_admin)])
def aplicar_atualizacao():
    if not atualizador_instalado():
        raise HTTPException(
            status_code=503,
            detail="O atualizador automático ainda não foi ativado. Execute uma vez no servidor: sudo bash scripts/update-linux.sh --setup",
        )
    if atualizacao_em_andamento():
        raise HTTPException(status_code=409, detail="Já existe uma atualização em andamento.")
    release, erro = consultar_ultima_release(forcar=True)
    if not release:
        raise HTTPException(status_code=502, detail=erro or "Não foi possível consultar a última versão.")
    if not atualizacao_disponivel(versao_atual(), release["version"]):
        raise HTTPException(status_code=409, detail="O sistema já está na versão mais recente.")
    try:
        solicitar_atualizacao(release["tag"])
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Não foi possível registrar o pedido de atualização.") from exc
    return {"sucesso": True, "version": release["version"], "mensagem": f"Atualização para a versão {release['version']} iniciada. O sistema reiniciará automaticamente."}


@router.get("/alerts/config", dependencies=[Depends(require_admin)])
def obter_config_alertas(db: Session = Depends(get_db)):
    return resumo_config_smtp(buscar_config_smtp(db))


@router.post("/alerts/config", dependencies=[Depends(require_admin)])
def salvar_config_alertas(dados: AlertSettingsRequest, db: Session = Depends(get_db)):
    if not 1 <= dados.smtp_port <= 65535:
        raise HTTPException(status_code=422, detail="Porta SMTP inválida.")
    if not 1 <= dados.alert_cooldown_minutes <= 1440:
        raise HTTPException(status_code=422, detail="O intervalo de alerta deve ficar entre 1 minuto e 24 horas.")
    if dados.smtp_enabled and not all((dados.smtp_host.strip(), dados.smtp_sender.strip(), dados.smtp_recipients.strip())):
        raise HTTPException(status_code=422, detail="Informe servidor, remetente e destinatários SMTP.")

    settings = buscar_config_smtp(db)
    if settings is None:
        settings = SystemSettings(id=1)
        db.add(settings)

    settings.smtp_enabled = dados.smtp_enabled
    settings.smtp_host = dados.smtp_host.strip()
    settings.smtp_port = dados.smtp_port
    settings.smtp_username = dados.smtp_username.strip()
    settings.smtp_sender = dados.smtp_sender.strip()
    settings.smtp_recipients = dados.smtp_recipients.strip()
    settings.smtp_security = dados.smtp_security
    settings.alert_backup_failure = dados.alert_backup_failure
    settings.alert_disk_warning = dados.alert_disk_warning
    settings.alert_disk_critical = dados.alert_disk_critical
    settings.alert_camera_failure = dados.alert_camera_failure
    settings.alert_cooldown_seconds = dados.alert_cooldown_minutes * 60
    if dados.clear_smtp_password:
        settings.smtp_password_encrypted = ""
    elif dados.smtp_password:
        try:
            settings.smtp_password_encrypted = criptografar_senha_smtp(dados.smtp_password)
        except RuntimeError as exc:
            db.rollback()
            raise HTTPException(status_code=503, detail="A chave de criptografia do serviço não está disponível.") from exc

    if dados.smtp_enabled and settings.smtp_username and not settings.smtp_password_encrypted:
        db.rollback()
        raise HTTPException(status_code=422, detail="Informe a senha SMTP para esta conta.")

    settings.smtp_error = ""
    db.commit()
    db.refresh(settings)
    return {"sucesso": True, "configuracao": resumo_config_smtp(settings)}


@router.post("/alerts/test", dependencies=[Depends(require_admin)])
def testar_config_alertas(db: Session = Depends(get_db)):
    enviado, mensagem = enviar_email_configurado(
        db,
        "Teste de e-mail · Timelapse Manager",
        "Este é um teste da configuração SMTP do Timelapse Manager.",
        force=True,
    )
    if not enviado:
        raise HTTPException(status_code=400, detail=mensagem)
    return {"sucesso": True, "mensagem": "E-mail de teste enviado."}

@router.get("/storage/config", dependencies=[Depends(require_admin)])
def obter_config_nuvem(
    request: Request,
    tipo: str | None = None,
    config_id: int | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(StorageConfig)
    oauth_redirect_uri = obter_oauth_redirect_uri(request)
    if config_id is not None:
        config = query.filter(StorageConfig.id == config_id).first()
    else:
        config = query.filter(StorageConfig.tipo == tipo).first() if tipo else query.first()
    if not config:
        return {"configurado": False, "oauth_redirect_uri": oauth_redirect_uri}
    return {
        "configurado": True,
        "id": config.id,
        "nome": config.nome if config.nome and config.nome != "Armazenamento" else _nome_padrao_storage(config.tipo),
        "tipo": config.tipo,
        "host": config.host,
        "usuario": config.usuario,
        "caminho_remoto": config.caminho_remoto,
        "ativo": config.ativo,
        "total_bytes": config.total_bytes,
        "usado_bytes": config.usado_bytes,
        "oauth_client_id": config.oauth_client_id,
        "oauth_redirect_uri": oauth_redirect_uri,
        "possui_client_id": bool(config.oauth_client_id),
        "conectado": bool(config.oauth_refresh_token) if config.tipo in {'google_drive', 'onedrive'} else False,
        "storage_error": config.storage_error or "",
    }

@router.post("/storage/config", dependencies=[Depends(require_admin)])
def salvar_config_nuvem(dados: StorageConfigRequest, db: Session = Depends(get_db)):
    config = db.query(StorageConfig).filter(StorageConfig.id == dados.config_id).first() if dados.config_id else None
    if dados.config_id and not config:
        raise HTTPException(status_code=404, detail="Configuração de armazenamento não encontrada.")
    if config is None and not dados.criar_novo:
        config = db.query(StorageConfig).filter(StorageConfig.tipo == dados.tipo).first()
    if not config:
        config = StorageConfig(tipo=dados.tipo)
        db.add(config)

    destino_alterado = config.host != dados.host or config.caminho_remoto != dados.caminho_remoto
    config.nome = dados.nome.strip() or _nome_padrao_storage(dados.tipo)
    config.tipo = dados.tipo
    config.host = dados.host or config.host or ""
    config.usuario = dados.usuario or config.usuario or ""
    config.senha = dados.senha or config.senha or ""
    config.caminho_remoto = dados.caminho_remoto or config.caminho_remoto or "/"
    config.ativo = dados.ativo
    config.oauth_client_id = dados.oauth_client_id or config.oauth_client_id or ""
    config.oauth_client_secret = dados.oauth_client_secret or config.oauth_client_secret or ""
    if destino_alterado:
        config.storage_quota_checked_at = 0
        config.storage_error = ""

    db.commit()
    db.refresh(config)
    return {"sucesso": True, "id": config.id, "mensagem": "Configurações salvas."}


@router.delete("/storage/config/{config_id}", dependencies=[Depends(require_admin)])
def excluir_config_storage(config_id: int, db: Session = Depends(get_db)):
    config = db.query(StorageConfig).filter(StorageConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuração de armazenamento não encontrada.")
    db.delete(config)
    db.commit()
    return {"sucesso": True}


@router.get("/storage/oauth/{tipo}/start", dependencies=[Depends(require_admin)])
def iniciar_oauth(
    tipo: str,
    request: Request,
    config_id: int | None = None,
    db: Session = Depends(get_db),
):
    if tipo not in {'google_drive', 'onedrive'}:
        raise HTTPException(status_code=400, detail="Provedor OAuth inválido.")
    query = db.query(StorageConfig).filter(StorageConfig.tipo == tipo)
    config = query.filter(StorageConfig.id == config_id).first() if config_id else query.first()
    if not config or not config.oauth_client_id or not config.oauth_client_secret:
        raise HTTPException(status_code=400, detail="Salve o Client ID e o Client Secret antes de conectar.")

    callback_url = obter_oauth_redirect_uri(request)
    try:
        validar_oauth_redirect_uri(callback_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    state = secrets.token_urlsafe(32)
    config.oauth_state = state
    db.commit()
    authorization_url = build_authorization_url(tipo, config.oauth_client_id, callback_url, state)
    return RedirectResponse(authorization_url, status_code=302)


@router.get("/storage/oauth/callback", name="storage_oauth_callback")
def concluir_oauth(
    request: Request,
    state: str,
    config_id: int | None = None,
    code: str | None = None,
    error: str | None = None,
    scope: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        return RedirectResponse("/?storage_oauth=denied", status_code=303)
    query = db.query(StorageConfig).filter(StorageConfig.oauth_state == state)
    config = query.filter(StorageConfig.id == config_id).first() if config_id else query.first()
    if not config or not state or not secrets.compare_digest(config.oauth_state or "", state):
        raise HTTPException(status_code=400, detail="Estado OAuth inválido ou expirado.")
    tipo = config.tipo.lower()
    if tipo not in {'google_drive', 'onedrive'}:
        raise HTTPException(status_code=400, detail="O estado OAuth não pertence a um provedor compatível.")
    if tipo == "google_drive" and not google_drive_file_scope_concedido(scope or ""):
        config.oauth_state = ""
        config.oauth_access_token = ""
        config.oauth_refresh_token = ""
        config.oauth_token_expires_at = 0
        config.storage_error = "O Google não concedeu o escopo drive.file. Autorize novamente e marque a permissão do Drive."
        db.commit()
        return RedirectResponse("/?storage_oauth=drive_scope_missing", status_code=303)
    if not code:
        raise HTTPException(status_code=400, detail="O provedor não retornou o código de autorização.")

    try:
        callback_url = obter_oauth_redirect_uri(request)
        validar_oauth_redirect_uri(callback_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        token_data = exchange_authorization_code(
            tipo,
            config.oauth_client_id,
            config.oauth_client_secret,
            code,
            callback_url,
        )
        refresh_token = token_data.get("refresh_token") or config.oauth_refresh_token
        if not refresh_token:
            raise RuntimeError("O provedor não retornou um refresh token; revogue o acesso e autorize novamente.")
        config.oauth_access_token = token_data["access_token"]
        config.oauth_refresh_token = refresh_token
        config.oauth_token_expires_at = int(time.time()) + int(token_data.get("expires_in", 3600))
        config.oauth_state = ""
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Não foi possível concluir OAuth: {exc}") from exc

    try:
        get_storage_quota(config)
        config.storage_quota_checked_at = int(time.time())
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse("/?storage_oauth=quota_warning", status_code=303)

    return RedirectResponse("/?storage_oauth=connected", status_code=303)

@router.post("/storage/sync", dependencies=[Depends(require_admin)])
def forcar_sincronizacao(db: Session = Depends(get_db)):
    configuracoes = db.query(StorageConfig).filter(StorageConfig.ativo.is_(True)).all()
    if not configuracoes:
        raise HTTPException(status_code=400, detail="Nenhuma configuração de nuvem ativa encontrada.")

    resultado = iniciar_sincronizacao_pendentes(configuracoes)
    db.commit()
    return resultado


@router.get("/storage/backup/sources", dependencies=[Depends(require_admin)])
def listar_destinos_backup(db: Session = Depends(get_db)):
    configs = db.query(StorageConfig).filter(StorageConfig.ativo.is_(True)).all()
    return [
        {
            "id": config.id,
            "nome": config.nome if config.nome and config.nome != "Armazenamento" else _nome_padrao_storage(config.tipo),
            "tipo": config.tipo,
            "conectado": bool(config.oauth_refresh_token) if config.tipo in {"google_drive", "onedrive"} else bool(config.host),
        }
        for config in configs
        if config.tipo in {"smb", "ftp", "webdav", "google_drive", "onedrive"}
    ]


@router.get("/storage/backup/progress/{job_id}", dependencies=[Depends(require_admin)])
def consultar_progresso_backup(job_id: str):
    status = obter_status_backup(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Status do backup não encontrado.")
    return status


@router.post("/storage/backup", status_code=202, dependencies=[Depends(require_admin)])
def executar_backup_manual():
    job_id = iniciar_backup_assincrono()
    if not job_id:
        raise HTTPException(status_code=409, detail="Já existe um backup em andamento. Aguarde a conclusão.")
    return {"job_id": job_id}

def executar_reinicializacao():
    """Espera 2 segundos para o FastAPI devolver a resposta e mata o processo."""
    time.sleep(2)
    os.execv(sys.executable, ['python'] + sys.argv)

@router.post("/restart", dependencies=[Depends(require_admin)])
async def reiniciar_sistema(modo: str = "agora"):
    if modo == "agora":
        # Dispara o reinício numa thread separada para não travar a resposta HTTP
        threading.Thread(target=executar_reinicio_processo).start()
        return {"sucesso": True, "mensagem": "Servidor a reiniciar imediatamente..."}
        
    elif modo == "agendar":
        # Usa o relógio cron para executar exatamente à 00:00
        app_scheduler.add_job(
            executar_reinicio_processo,
            'cron',
            hour=0,
            minute=0,
            id='reinicio_madrugada',
            replace_existing=True
        )
        return {"sucesso": True, "mensagem": "O reinício foi agendado com sucesso para as 00:00."}

    return {"sucesso": False, "mensagem": "Modo inválido."}