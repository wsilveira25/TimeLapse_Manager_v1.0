import concurrent.futures
import ftplib
import logging
import os
import posixpath
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth
from smb.SMBConnection import SMBConnection
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.paths import TEMP_DIR
from app.models.storage import StorageBackupFile, StorageConfig
from app.services.local_storage import identificar_caminho_local, local_disponivel, listar_locais_armazenamento, resolver_caminho_local
from app.services.alert_service import alertar_espaco_disco, alertar_falha_backup
from app.services.cloud_storage import (
    GOOGLE_API_URL,
    MICROSOFT_GRAPH_URL,
    get_access_token,
    upload_cloud_file,
)


logger = logging.getLogger(__name__)
ROOT_TEMP = TEMP_DIR
BACKUP_FOLDER = "TimeLapseBackup"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi"}
BACKUP_LOCK = threading.Lock()
BACKUP_STATUS_LOCK = threading.Lock()
backup_jobs: dict[str, dict[str, Any]] = {}
active_backup_job_id: str | None = None
last_backup_job_id: str | None = None
MIN_FREE_SPACE_RATIO = 0.20
MIN_LOCAL_FILE_AGE_SECONDS = 30 * 24 * 60 * 60
try:
    BACKUP_UPLOAD_WORKERS = int(os.getenv("TIMELAPSE_BACKUP_UPLOAD_WORKERS", "4"))
except ValueError:
    BACKUP_UPLOAD_WORKERS = 4
BACKUP_UPLOAD_WORKERS = min(max(BACKUP_UPLOAD_WORKERS, 1), 8)
MAX_PENDING_UPLOADS = BACKUP_UPLOAD_WORKERS * 2


def obter_status_backup(job_id: str | None = None) -> dict[str, Any] | None:
    with BACKUP_STATUS_LOCK:
        status_id = job_id or active_backup_job_id or last_backup_job_id
        if status_id is None:
            return None
        status = backup_jobs.get(status_id)
        return dict(status) if status else None


def _backup_record_matches_file(record: StorageBackupFile | None, file_stat: os.stat_result) -> bool:
    if (
        record is None
        or not record.backed_up_at
        or record.file_size != file_stat.st_size
        or record.file_mtime_ns != file_stat.st_mtime_ns
    ):
        return False
    record.last_error = ""
    return True


def _nome_destino_backup(config: StorageConfig) -> str:
    tipo = config.tipo.lower()
    nomes = {
        "smb": "Armazenamento SMB",
        "ftp": "Armazenamento FTP",
        "webdav": "Armazenamento WebDAV",
        "google_drive": "Google Drive",
        "onedrive": "OneDrive",
    }
    return nomes.get(tipo, config.nome or config.tipo)


def _criar_config_upload(config: StorageConfig) -> SimpleNamespace:
    valores = {
        column.name: getattr(config, column.name)
        for column in StorageConfig.__table__.columns
    }
    upload_config = SimpleNamespace(**valores)
    upload_config._oauth_token_lock = threading.RLock()
    upload_config._google_folder_cache_lock = threading.RLock()
    return upload_config


def _criar_job_backup() -> tuple[str | None, bool]:
    global active_backup_job_id, last_backup_job_id
    with BACKUP_STATUS_LOCK:
        if active_backup_job_id:
            active = backup_jobs.get(active_backup_job_id)
            if active and active["estado"] == "executando":
                return active_backup_job_id, False
        if BACKUP_LOCK.locked():
            return None, False

        job_id = uuid.uuid4().hex
        backup_jobs[job_id] = {
            "job_id": job_id,
            "estado": "executando",
            "total": 0,
            "concluidos": 0,
            "enviados": 0,
            "ignorados": 0,
            "falhas": 0,
            "percentual": 0,
            "mensagem": "Preparando o backup.",
        }
        active_backup_job_id = job_id
        last_backup_job_id = job_id
        for old_job_id in list(backup_jobs)[:-10]:
            if old_job_id != active_backup_job_id:
                del backup_jobs[old_job_id]
        return job_id, True


def _atualizar_job_backup(job_id: str, valores: dict[str, Any]) -> None:
    with BACKUP_STATUS_LOCK:
        status = backup_jobs.get(job_id)
        if status:
            status.update(valores)


def _finalizar_job_backup(job_id: str, resultado: dict[str, Any] | None = None) -> None:
    global active_backup_job_id
    estado = "concluido_com_falhas" if resultado and not resultado["sucesso"] else "concluido"
    mensagem = (
        "Backup concluído com arquivos pendentes. Eles serão tentados novamente."
        if estado == "concluido_com_falhas"
        else "Backup concluído."
    )
    with BACKUP_STATUS_LOCK:
        status = backup_jobs.get(job_id)
        if status:
            status["estado"] = estado
            status["percentual"] = 100
            status["mensagem"] = mensagem
            limpeza = status.get("limpeza_local", {})
            removidos = int(limpeza.get("arquivos_removidos", 0))
            if removidos:
                status["mensagem"] += f" {removidos} arquivos locais antigos foram removidos após validação."
            elif limpeza.get("precisa_limpeza") and not limpeza.get("limite_atingido"):
                status["mensagem"] += " Não havia cópias antigas validadas suficientes para liberar o espaço necessário."
            if resultado:
                status["enviados"] = resultado["enviados"]
                status["ignorados"] = resultado["ignorados"]
        if active_backup_job_id == job_id:
            active_backup_job_id = None


def _executar_job_backup(job_id: str) -> None:
    try:
        with SessionLocal() as db:
            _atualizar_job_backup(job_id, {"mensagem": "Verificando backups locais antes da sincronização."})
            limpeza = limpar_capturas_backupeadas(db)
            _atualizar_job_backup(job_id, {"limpeza_local": limpeza})
            if limpeza["percentual_livre"] < 20:
                limite_alerta = 5 if limpeza["percentual_livre"] <= 5 else 20
                enviado, aviso = alertar_espaco_disco(db, limpeza["percentual_livre"], limite_alerta)
                if aviso and aviso != "Alerta dentro do período de espera.":
                    logger.warning("Alerta de espaço em disco não enviado: %s", aviso)
            if limpeza["arquivos_removidos"]:
                logger.info(
                    "Limpeza local: arquivos=%s bytes=%s livres=%.1f%%",
                    limpeza["arquivos_removidos"],
                    limpeza["bytes_liberados"],
                    limpeza["percentual_livre"],
                )
            elif limpeza["precisa_limpeza"]:
                logger.warning("Espaço local baixo e não há cópias remotas validadas elegíveis para remoção.")
            resultado = executar_backup_integral(
                db,
                progress_callback=lambda progresso: _atualizar_job_backup(job_id, progresso),
            )
        _finalizar_job_backup(job_id, resultado)
    except Exception:
        logger.exception("Falha inesperada na execução do backup")
        _finalizar_job_backup(job_id, {"sucesso": False, "enviados": 0, "ignorados": 0, "erros": []})


def iniciar_backup_assincrono() -> str | None:
    job_id, novo_job = _criar_job_backup()
    if job_id and novo_job:
        threading.Thread(target=_executar_job_backup, args=(job_id,), daemon=True).start()
    return job_id


def _iter_backup_files(db: Session):
    seen_paths = set()
    locations = sorted(listar_locais_armazenamento(db), key=lambda item: len(item["caminho"]), reverse=True)
    for location in locations:
        location_root = Path(location["caminho"])
        if not location["padrao"] and not local_disponivel(location_root):
            continue
        for folder in ("Capturas", "Videos"):
            root = location_root / folder
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if folder == "Capturas" and (path.suffix.lower() != ".jpg" or not path.name.startswith("frame_")):
                    continue
                if folder == "Videos" and path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                resolved_path = path.resolve()
                if resolved_path in seen_paths:
                    continue
                seen_paths.add(resolved_path)
                yield path


def _remote_backup_path(path, db):
    local_relative = identificar_caminho_local(path, db)
    return posixpath.join(BACKUP_FOLDER, local_relative)


def _send_email_alert(db: Session, config: StorageConfig, errors: list[str]) -> tuple[bool, str]:
    return alertar_falha_backup(db, config.nome or config.tipo, len(errors))


def _upload_smb(path, config, remote_path):
    share = (config.caminho_remoto or "").strip("/\\")
    if not share:
        raise ValueError("Informe o nome da share SMB.")
    connection = SMBConnection(config.usuario, config.senha, "timelapse_linux", "truenas_server", use_ntlm_v2=True)
    if not connection.connect(config.host, 445, timeout=15):
        raise RuntimeError("Não foi possível conectar ao servidor SMB.")

    remote_path = remote_path.strip("/")
    folders = remote_path.split("/")[:-1]
    current = ""
    try:
        for folder in folders:
            current = posixpath.join(current, folder)
            try:
                connection.createDirectory(share, f"/{current}")
            except Exception:
                connection.listPath(share, f"/{current}")

        temporary_path = f"/{remote_path}.uploading"
        final_path = f"/{remote_path}"
        with open(path, "rb") as source:
            connection.storeFile(share, temporary_path, source)
        attributes = connection.getAttributes(share, temporary_path)
        if attributes.file_size != path.stat().st_size:
            raise RuntimeError("Tamanho divergente após envio SMB.")
        try:
            connection.deleteFiles(share, final_path)
        except Exception:
            pass
        connection.rename(share, temporary_path, final_path)
        return {"size": attributes.file_size, "id": ""}
    finally:
        connection.close()


def _upload_ftp(path, config, remote_path):
    ftp = ftplib.FTP()
    ftp.connect(config.host, 21, timeout=30)
    try:
        ftp.login(user=config.usuario, passwd=config.senha)
        base = (config.caminho_remoto or "/").strip("/")
        if base:
            ftp.cwd("/" + base)
        for folder in remote_path.split("/")[:-1]:
            try:
                ftp.cwd(folder)
            except ftplib.error_perm:
                ftp.mkd(folder)
                ftp.cwd(folder)

        filename = remote_path.split("/")[-1]
        temporary_name = f"{filename}.uploading"
        with open(path, "rb") as source:
            ftp.storbinary(f"STOR {temporary_name}", source)
        remote_size = ftp.size(temporary_name)
        if remote_size != path.stat().st_size:
            raise RuntimeError("Tamanho divergente após envio FTP.")
        try:
            ftp.delete(filename)
        except ftplib.error_perm:
            pass
        ftp.rename(temporary_name, filename)
        return {"size": remote_size, "id": ""}
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def _upload_webdav(path, config, remote_path):
    base_url = config.host.rstrip("/")
    auth = HTTPBasicAuth(config.usuario, config.senha)
    folders = remote_path.split("/")[:-1]
    current = base_url
    for folder in folders:
        current = f"{current}/{quote(folder, safe='')}"
        response = requests.request("MKCOL", current, auth=auth, timeout=30)
        if response.status_code not in (200, 201, 204, 405):
            response.raise_for_status()

    url = f"{base_url}/{quote(remote_path, safe='/')}"
    with open(path, "rb") as source:
        response = requests.put(url, data=source, auth=auth, timeout=180)
    if response.status_code not in (200, 201, 204):
        response.raise_for_status()
    response = requests.head(url, auth=auth, timeout=30)
    response.raise_for_status()
    remote_size = int(response.headers.get("Content-Length", "-1"))
    if remote_size != path.stat().st_size:
        raise RuntimeError("Tamanho divergente após envio WebDAV.")
    return {"size": remote_size, "id": ""}


def upload_backup_file(path, config, remote_path):
    tipo = config.tipo.lower()
    if tipo == "smb":
        return _upload_smb(path, config, remote_path)
    if tipo == "ftp":
        return _upload_ftp(path, config, remote_path)
    if tipo == "webdav":
        return _upload_webdav(path, config, remote_path)
    if tipo in {"google_drive", "onedrive"}:
        return upload_cloud_file(str(path), config, remote_path=remote_path)
    raise ValueError(f"Protocolo de backup não suportado: {tipo}")


def _remote_copy_has_expected_size(record, config):
    tipo = config.tipo.lower()
    if tipo == "google_drive":
        if not record.remote_id:
            return False
        token = get_access_token(config)
        response = requests.get(
            f"{GOOGLE_API_URL}/files/{quote(record.remote_id, safe='')}",
            params={"fields": "id,size,trashed"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        response.raise_for_status()
        remote = response.json()
        return not remote.get("trashed", False) and int(remote.get("size", -1)) == record.file_size

    if tipo == "onedrive":
        if not record.remote_id:
            return False
        token = get_access_token(config)
        response = requests.get(
            f"{MICROSOFT_GRAPH_URL}/me/drive/items/{quote(record.remote_id, safe='')}",
            params={"$select": "id,size,file,deleted"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        response.raise_for_status()
        remote = response.json()
        return "deleted" not in remote and bool(remote.get("file")) and int(remote.get("size", -1)) == record.file_size

    if tipo == "smb":
        share = (config.caminho_remoto or "").strip("/\\")
        connection = SMBConnection(config.usuario, config.senha, "timelapse_linux", "truenas_server", use_ntlm_v2=True)
        try:
            if not connection.connect(config.host, 445, timeout=15):
                return False
            attributes = connection.getAttributes(share, f"/{record.remote_path.strip('/')}")
            return attributes.file_size == record.file_size
        finally:
            connection.close()

    if tipo == "ftp":
        ftp = ftplib.FTP()
        ftp.connect(config.host, 21, timeout=30)
        try:
            ftp.login(user=config.usuario, passwd=config.senha)
            base = (config.caminho_remoto or "/").strip("/")
            if base:
                ftp.cwd("/" + base)
            parts = record.remote_path.strip("/").split("/")
            for folder in parts[:-1]:
                ftp.cwd(folder)
            return ftp.size(parts[-1]) == record.file_size
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    if tipo == "webdav":
        url = f"{config.host.rstrip('/')}/{quote(record.remote_path, safe='/')}"
        response = requests.head(
            url,
            auth=HTTPBasicAuth(config.usuario, config.senha),
            timeout=30,
        )
        if response.status_code not in (200, 204):
            return False
        return int(response.headers.get("Content-Length", "-1")) == record.file_size

    return False


def limpar_capturas_backupeadas(
    db: Session,
    root=ROOT_TEMP,
    min_free_ratio=MIN_FREE_SPACE_RATIO,
    min_age_seconds=MIN_LOCAL_FILE_AGE_SECONDS,
    now=None,
    disk_usage=None,
    verifier=None,
):
    root_path = Path(root).resolve()
    disk_usage = disk_usage or shutil.disk_usage
    verifier = verifier or _remote_copy_has_expected_size
    default_root = Path(ROOT_TEMP).resolve()
    explicit_root = root_path != default_root
    if explicit_root:
        storage_roots = [root_path]
    else:
        storage_roots = [
            Path(location["caminho"]).resolve()
            for location in listar_locais_armazenamento(db)
            if location["ativo"]
        ]

    usages = []
    seen_devices = set()
    for storage_root in storage_roots:
        try:
            usage = disk_usage(storage_root)
            device = storage_root.stat().st_dev
        except OSError:
            continue
        if device in seen_devices:
            continue
        seen_devices.add(device)
        usages.append({
            "root": storage_root,
            "usage": usage,
            "free_ratio": usage.free / usage.total if usage.total else 0,
        })

    total_bytes = sum(item["usage"].total for item in usages)
    free_bytes = sum(item["usage"].free for item in usages)
    all_locations_full = bool(usages) and all(item["free_ratio"] < min_free_ratio for item in usages)
    percentual_livre = free_bytes / total_bytes if total_bytes else 0
    result = {
        "precisa_limpeza": all_locations_full,
        "arquivos_removidos": 0,
        "bytes_liberados": 0,
        "percentual_livre": round(percentual_livre * 100, 1),
        "limite_atingido": not all_locations_full,
        "locais": len(usages),
    }
    if not result["precisa_limpeza"]:
        return result

    configuracoes = db.query(StorageConfig).filter(StorageConfig.ativo.is_(True)).all()
    if not configuracoes:
        return result

    timestamp_atual = now if now is not None else time.time()
    registros = db.query(StorageBackupFile).filter(
        StorageBackupFile.backed_up_at > 0,
        StorageBackupFile.last_error == "",
    ).all()
    registros_por_destino = {
        (record.storage_config_id, record.local_path): record
        for record in registros
    }
    candidatos = []
    caminhos_adicionados = set()
    for record in registros:
        stored_path = record.local_path.split("/", 1)[1] if record.local_path.startswith("local-") and "/" in record.local_path else record.local_path
        if record.local_path in caminhos_adicionados or not stored_path.startswith(("Capturas/", "Videos/")):
            continue
        caminhos_adicionados.add(record.local_path)
        if explicit_root and not record.local_path.startswith("local-"):
            caminho = (root_path / record.local_path).resolve()
            if root_path not in caminho.parents:
                continue
        else:
            caminho = resolver_caminho_local(record.local_path, db)
        if caminho is None or not caminho.is_file():
            continue
        if not any(storage_root in caminho.parents for storage_root in storage_roots):
            continue
        try:
            stat = caminho.stat()
        except OSError:
            continue
        if stat.st_size != record.file_size or stat.st_mtime_ns != record.file_mtime_ns:
            continue
        if timestamp_atual - stat.st_mtime < min_age_seconds:
            continue
        candidatos.append((stat.st_mtime, caminho, stat, record))

    for _, caminho, stat, record in sorted(candidatos, key=lambda item: item[0]):
        try:
            copia_validada = True
            for config in configuracoes:
                destino_record = registros_por_destino.get((config.id, record.local_path))
                if (
                    not destino_record
                    or destino_record.file_size != stat.st_size
                    or destino_record.file_mtime_ns != stat.st_mtime_ns
                    or not verifier(destino_record, config)
                ):
                    copia_validada = False
                    break
        except Exception:
            copia_validada = False
        if not copia_validada:
            continue
        try:
            caminho.unlink()
        except OSError:
            continue
        result["arquivos_removidos"] += 1
        result["bytes_liberados"] += stat.st_size
        matching_locations = [item for item in usages if item["root"] in caminho.parents]
        if matching_locations:
            item = max(matching_locations, key=lambda location: len(location["root"].parts))
            item["usage"] = disk_usage(item["root"])
            item["free_ratio"] = item["usage"].free / item["usage"].total if item["usage"].total else 0
        total_bytes = sum(item["usage"].total for item in usages)
        free_bytes = sum(item["usage"].free for item in usages)
        percentual_livre = free_bytes / total_bytes if total_bytes else 0
        result["percentual_livre"] = round(percentual_livre * 100, 1)
        result["limite_atingido"] = any(item["free_ratio"] >= min_free_ratio for item in usages)
        if result["limite_atingido"]:
            break

    return result


def download_backup_file(record, config, destination):
    tipo = config.tipo.lower()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if tipo == "google_drive":
        if not record.remote_id:
            raise RuntimeError("O backup não tem ID de arquivo no Google Drive.")
        token = get_access_token(config)
        response = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{quote(record.remote_id, safe='')}?alt=media",
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=60,
        )
        response.raise_for_status()
        with open(destination, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)
    elif tipo == "onedrive":
        if not record.remote_id:
            raise RuntimeError("O backup não tem ID de arquivo no OneDrive.")
        token = get_access_token(config)
        response = requests.get(
            f"https://graph.microsoft.com/v1.0/me/drive/items/{quote(record.remote_id, safe='')}/content",
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=60,
        )
        response.raise_for_status()
        with open(destination, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)
    elif tipo == "smb":
        share = (config.caminho_remoto or "").strip("/\\")
        connection = SMBConnection(config.usuario, config.senha, "timelapse_linux", "truenas_server", use_ntlm_v2=True)
        if not connection.connect(config.host, 445, timeout=15):
            raise RuntimeError("Não foi possível conectar ao servidor SMB.")
        try:
            with open(destination, "wb") as output:
                connection.retrieveFile(share, "/" + record.remote_path, output)
        finally:
            connection.close()
    elif tipo == "ftp":
        ftp = ftplib.FTP()
        ftp.connect(config.host, 21, timeout=30)
        try:
            ftp.login(user=config.usuario, passwd=config.senha)
            base = (config.caminho_remoto or "/").strip("/")
            if base:
                ftp.cwd("/" + base)
            with open(destination, "wb") as output:
                ftp.retrbinary(f"RETR {record.remote_path}", output.write)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
    elif tipo == "webdav":
        url = f"{config.host.rstrip('/')}/{quote(record.remote_path, safe='/')}"
        response = requests.get(url, auth=HTTPBasicAuth(config.usuario, config.senha), stream=True, timeout=60)
        response.raise_for_status()
        with open(destination, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)
    else:
        raise ValueError(f"Protocolo de download não suportado: {tipo}")

    if destination.stat().st_size != record.file_size:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Tamanho divergente no download do backup.")
    return destination


def executar_backup_integral(db: Session, progress_callback=None):
    if not BACKUP_LOCK.acquire(blocking=False):
        return {
            "sucesso": False,
            "enviados": 0,
            "ignorados": 0,
            "erros": ["Já existe uma execução de backup em andamento."],
            "destinos": [],
        }
    try:
        return _executar_backup_integral(db, progress_callback)
    finally:
        BACKUP_LOCK.release()


def _executar_backup_integral(db: Session, progress_callback=None):
    configuracoes = db.query(StorageConfig).filter(StorageConfig.ativo.is_(True)).order_by(StorageConfig.id).all()
    if not configuracoes:
        return {"sucesso": False, "enviados": 0, "ignorados": 0, "erros": ["Nenhum destino ativo."]}

    arquivos = list(_iter_backup_files(db))
    resultado = {"sucesso": True, "enviados": 0, "ignorados": 0, "erros": [], "destinos": []}
    destinos = [
        {
            "id": config.id,
            "nome": _nome_destino_backup(config),
            "total": len(arquivos),
            "concluidos": 0,
            "enviados": 0,
            "ignorados": 0,
            "falhas": 0,
        }
        for config in configuracoes
    ]
    destinos_por_id = {config.id: destino for config, destino in zip(configuracoes, destinos)}
    erros_por_config = {config.id: [] for config in configuracoes}
    progresso = {
        "total": len(arquivos),
        "concluidos": 0,
        "enviados": 0,
        "ignorados": 0,
        "falhas": 0,
        "destino_atual": "",
    }

    def notificar_progresso():
        if progress_callback:
            destino_atual = progresso["destino_atual"]
            progress_callback({
                **progresso,
                "percentual": int(progresso["concluidos"] * 100 / progresso["total"]) if progresso["total"] else 0,
                "mensagem": f"{destino_atual}: processando {progresso['concluidos']:,} de {progresso['total']:,} arquivos únicos.".replace(",", "."),
                "destinos": [dict(destino) for destino in destinos],
            })

    notificar_progresso()
    agora = int(time.time())

    for config in configuracoes:
        erros_destino = erros_por_config[config.id]
        destino = destinos_por_id[config.id]
        progresso.update({
            "concluidos": 0,
            "enviados": 0,
            "ignorados": 0,
            "falhas": 0,
            "destino_atual": destino["nome"],
        })
        notificar_progresso()
        sem_token_oauth = config.tipo.lower() in {"google_drive", "onedrive"} and not config.oauth_refresh_token
        if sem_token_oauth:
            erros_destino.append("Conta cloud não conectada por OAuth.")
            destino["falhas"] = len(arquivos)
            destino["concluidos"] = len(arquivos)
            progresso.update({"falhas": len(arquivos), "concluidos": len(arquivos)})
            notificar_progresso()
        else:
            upload_config = _criar_config_upload(config)
            pendentes = {}

            def concluir_uploads(futuros_concluidos):
                for future in futuros_concluidos:
                    path, local_path, stat, remote_path = pendentes.pop(future)
                    try:
                        remote = future.result()
                        record = db.query(StorageBackupFile).filter_by(
                            storage_config_id=config.id,
                            local_path=local_path,
                        ).first()
                        if record is None:
                            record = StorageBackupFile(storage_config_id=config.id, local_path=local_path)
                            db.add(record)
                        record.remote_path = remote_path
                        record.remote_id = remote.get("id", "") or ""
                        record.file_size = stat.st_size
                        record.file_mtime_ns = stat.st_mtime_ns
                        record.backed_up_at = int(time.time())
                        record.last_error = ""
                        db.commit()
                        resultado["enviados"] += 1
                        progresso["enviados"] += 1
                        destino["enviados"] += 1
                    except Exception as exc:
                        db.rollback()
                        progresso["falhas"] += 1
                        destino["falhas"] += 1
                        erros_destino.append(f"{local_path}: {exc}")
                        existing = db.query(StorageBackupFile).filter_by(
                            storage_config_id=config.id,
                            local_path=local_path,
                        ).first()
                        if existing:
                            existing.last_error = str(exc)[:1000]
                            db.commit()
                    finally:
                        destino["concluidos"] += 1
                        progresso["concluidos"] = destino["concluidos"]
                        notificar_progresso()

            with concurrent.futures.ThreadPoolExecutor(max_workers=BACKUP_UPLOAD_WORKERS) as executor:
                for path in arquivos:
                    local_path = str(path)
                    try:
                        local_path = identificar_caminho_local(path, db)
                        stat = path.stat()
                        if time.time_ns() - stat.st_mtime_ns < 30 * 1_000_000_000:
                            resultado["ignorados"] += 1
                            progresso["ignorados"] += 1
                            destino["ignorados"] += 1
                            destino["concluidos"] += 1
                            progresso["concluidos"] = destino["concluidos"]
                            notificar_progresso()
                            continue
                        record = db.query(StorageBackupFile).filter_by(
                            storage_config_id=config.id,
                            local_path=local_path,
                        ).first()
                        had_stale_error = bool(record and record.last_error)
                        if _backup_record_matches_file(record, stat):
                            if had_stale_error:
                                db.commit()
                            resultado["ignorados"] += 1
                            progresso["ignorados"] += 1
                            destino["ignorados"] += 1
                            destino["concluidos"] += 1
                            progresso["concluidos"] = destino["concluidos"]
                            notificar_progresso()
                            continue

                        remote_path = _remote_backup_path(path, db)
                        future = executor.submit(upload_backup_file, path, upload_config, remote_path)
                        pendentes[future] = (path, local_path, stat, remote_path)
                        if len(pendentes) >= MAX_PENDING_UPLOADS:
                            done, _ = concurrent.futures.wait(
                                pendentes,
                                return_when=concurrent.futures.FIRST_COMPLETED,
                            )
                            concluir_uploads(done)
                    except Exception as exc:
                        db.rollback()
                        progresso["falhas"] += 1
                        destino["falhas"] += 1
                        erros_destino.append(f"{local_path}: {exc}")
                        existing = db.query(StorageBackupFile).filter_by(
                            storage_config_id=config.id,
                            local_path=local_path,
                        ).first()
                        if existing:
                            existing.last_error = str(exc)[:1000]
                            db.commit()
                        destino["concluidos"] += 1
                        progresso["concluidos"] = destino["concluidos"]
                        notificar_progresso()

                while pendentes:
                    done, _ = concurrent.futures.wait(
                        pendentes,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    concluir_uploads(done)

            if config.tipo.lower() in {"google_drive", "onedrive"}:
                config.oauth_access_token = upload_config.oauth_access_token
                config.oauth_refresh_token = upload_config.oauth_refresh_token
                config.oauth_token_expires_at = upload_config.oauth_token_expires_at
                db.commit()

    for config in configuracoes:
        erros_destino = erros_por_config[config.id]
        if erros_destino:
            resultado["sucesso"] = False
            resultado["erros"].append({config.nome or config.tipo: erros_destino})
            config.backup_error = "\n".join(erros_destino[:20])[:4000]
            try:
                enviado, aviso = _send_email_alert(db, config, erros_destino)
                if aviso and aviso not in {"Alerta dentro do período de espera.", "Categoria de alerta desabilitada."}:
                    logger.warning("Alerta de backup não enviado (%s): %s", config.nome or config.tipo, aviso)
            except Exception:
                logger.exception("Falha ao enviar e-mail de alerta de backup")
        else:
            config.backup_error = ""
            config.backup_last_success_at = agora
        resultado["destinos"].append(_nome_destino_backup(config))
        db.commit()

    return resultado


def executar_backup_agendado():
    job_id, novo_job = _criar_job_backup()
    if not novo_job:
        logger.warning("Backup agendado ignorado porque já existe uma execução ativa.")
        return
    _executar_job_backup(job_id)


def buscar_frames_backup(db: Session, config_id: int, camera_id: int, inicio=None, fim=None):
    registros = db.query(StorageBackupFile).filter(
        StorageBackupFile.storage_config_id == config_id,
        or_(
            StorageBackupFile.local_path.startswith(f"Capturas/camera_{camera_id}/"),
            StorageBackupFile.local_path.like(f"local-%/Capturas/camera_{camera_id}/%"),
        ),
        StorageBackupFile.backed_up_at > 0,
        StorageBackupFile.last_error == "",
    ).all()
    filtrados = []
    for record in registros:
        filename = Path(record.local_path).name
        if not filename.startswith("frame_") or not filename.endswith(".jpg"):
            continue
        try:
            frame_time = time.strptime(filename[6:-4], "%Y%m%d_%H%M%S")
            timestamp = time.mktime(frame_time)
        except ValueError:
            continue
        if inicio is not None and timestamp < inicio.timestamp():
            continue
        if fim is not None and timestamp > fim.timestamp():
            continue
        filtrados.append(record)
    return sorted(filtrados, key=lambda item: Path(item.local_path).name)


def obter_info_frames_backup(db: Session, config_id: int, camera_id: int):
    registros = buscar_frames_backup(db, config_id, camera_id)
    if not registros:
        return {"total": 0, "primeiro": None, "ultimo": None}
    primeiro = Path(registros[0].local_path).name.removeprefix("frame_").removesuffix(".jpg")
    ultimo = Path(registros[-1].local_path).name.removeprefix("frame_").removesuffix(".jpg")
    return {"total": len(registros), "primeiro": primeiro, "ultimo": ultimo}


def baixar_frames_backup(records, config, destination_root):
    downloaded = []
    for record in records:
        destination = Path(destination_root) / Path(record.local_path).name
        download_backup_file(record, config, destination)
        downloaded.append(str(destination))
    return downloaded
