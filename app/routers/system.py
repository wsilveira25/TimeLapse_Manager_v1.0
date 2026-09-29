import copy
import logging
import os
import secrets
import sys
import threading
import time
import shutil
from typing import Literal

from sqlalchemy import func
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse
from app.core.auth import require_admin
from app.core.database import SessionLocal, get_db
from app.core.paths import TEMP_DIR
from app.models.storage import StorageBackupFile, StorageConfig, SystemSettings
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
from app.tasks.scheduler import scheduler as app_scheduler, executar_reinicio_processo

router = APIRouter(prefix="/api/system", tags=["Sistema"])
logger = logging.getLogger(__name__)

#PASTA_POOL_BASE = "/var/temp/Capturas"
PASTA_CAPTURAS = str(TEMP_DIR / "Capturas")
TAMANHO_PADRAO_FALLBACK = 450 * 1024  # 450 KB de segurança caso ainda não haja fotos


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
        StorageBackupFile.local_path.like("Capturas/%"),
        StorageBackupFile.backed_up_at > 0,
        StorageBackupFile.last_error == "",
    ).scalar() or 0


def _calcular_status_armazenamento(db: Session):
    os.makedirs(PASTA_CAPTURAS, exist_ok=True)
    total, used, free = shutil.disk_usage(TEMP_DIR)

    arquivos_jpg = []
    for root, dirs, files in os.walk(PASTA_CAPTURAS):
        for f in files:
            if f.endswith('.jpg'):
                arquivos_jpg.append(os.path.join(root, f))

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


@router.get("/storage", dependencies=[Depends(require_admin)])
def obter_status_armazenamento():
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

    total, used, free = shutil.disk_usage(TEMP_DIR)
    local_summary = build_provider_summary(
        nome="Armazenamento Local",
        total_bytes=total,
        usado_bytes=used,
        ativo=True,
        tipo="local",
    )
    return {
        "disco": local_summary,
        "calculo_previo": {
            "capturas_realizadas": None,
            "capturas_restantes": None,
            "capturas_totais": None,
            "tamanho_medio_kb": None,
        },
        "backup_progresso": obter_status_backup(),
        "provedores": [local_summary],
        "metricas_status": "atualizando",
        "metricas_atualizadas_em": None,
    }


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
    tipo: str | None = None,
    config_id: int | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(StorageConfig)
    if config_id is not None:
        config = query.filter(StorageConfig.id == config_id).first()
    else:
        config = query.filter(StorageConfig.tipo == tipo).first() if tipo else query.first()
    if not config:
        return {"configurado": False}
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

    state = secrets.token_urlsafe(32)
    config.oauth_state = state
    db.commit()
    callback_url = str(request.url_for("storage_oauth_callback"))
    authorization_url = build_authorization_url(tipo, config.oauth_client_id, callback_url, state)
    return RedirectResponse(authorization_url, status_code=302)


@router.get("/storage/oauth/callback", name="storage_oauth_callback")
def concluir_oauth(
    request: Request,
    state: str,
    config_id: int | None = None,
    code: str | None = None,
    error: str | None = None,
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
    if not code:
        raise HTTPException(status_code=400, detail="O provedor não retornou o código de autorização.")

    callback_url = str(request.url_for("storage_oauth_callback"))
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