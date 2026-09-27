import base64
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.storage import SystemSettings


logger = logging.getLogger(__name__)
AlertCategory = Literal["backup_failure", "disk_warning", "disk_critical"]
CATEGORY_SETTINGS = {
    "backup_failure": ("alert_backup_failure", "last_backup_alert_at"),
    "disk_warning": ("alert_disk_warning", "last_disk_warning_alert_at"),
    "disk_critical": ("alert_disk_critical", "last_disk_critical_alert_at"),
}


def _system_secret() -> str:
    value = os.getenv("TIMELAPSE_SESSION_SECRET", "")
    credentials_directory = os.getenv("TIMELAPSE_CREDENTIALS_DIR") or os.getenv("CREDENTIALS_DIRECTORY", "")
    if not value and credentials_directory:
        try:
            value = (Path(credentials_directory) / "TIMELAPSE_SESSION_SECRET").read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
    if not value:
        raise RuntimeError("A chave de criptografia do serviço não está disponível.")
    return value


def _password_cipher() -> Fernet:
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"timelapse-smtp-password-v1",
    ).derive(_system_secret().encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key_material))


def criptografar_senha_smtp(password: str) -> str:
    if not password:
        return ""
    return _password_cipher().encrypt(password.encode("utf-8")).decode("ascii")


def _descriptografar_senha_smtp(encrypted_password: str) -> str:
    if not encrypted_password:
        return ""
    return _password_cipher().decrypt(encrypted_password.encode("ascii")).decode("utf-8")


def buscar_config_smtp(db: Session) -> SystemSettings | None:
    return db.query(SystemSettings).filter(SystemSettings.id == 1).first()


def resumo_config_smtp(settings: SystemSettings | None) -> dict[str, object]:
    if not settings:
        return {
            "smtp_enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_username": "",
            "smtp_sender": "",
            "smtp_recipients": "",
            "smtp_security": "starttls",
            "smtp_password_configured": False,
            "alert_backup_failure": True,
            "alert_disk_warning": True,
            "alert_disk_critical": True,
            "alert_camera_failure": True,
            "alert_cooldown_minutes": 30,
            "smtp_error": "",
        }
    return {
        "smtp_enabled": bool(settings.smtp_enabled),
        "smtp_host": settings.smtp_host or "",
        "smtp_port": int(settings.smtp_port or 587),
        "smtp_username": settings.smtp_username or "",
        "smtp_sender": settings.smtp_sender or "",
        "smtp_recipients": settings.smtp_recipients or "",
        "smtp_security": settings.smtp_security or "starttls",
        "smtp_password_configured": bool(settings.smtp_password_encrypted),
        "alert_backup_failure": bool(settings.alert_backup_failure),
        "alert_disk_warning": bool(settings.alert_disk_warning),
        "alert_disk_critical": bool(settings.alert_disk_critical),
        "alert_camera_failure": bool(settings.alert_camera_failure),
        "alert_cooldown_minutes": max(int(settings.alert_cooldown_seconds or 1800) // 60, 1),
        "smtp_error": "Não foi possível enviar o último e-mail de alerta." if settings.smtp_error else "",
    }


def enviar_email_configurado(
    db: Session,
    subject: str,
    body: str,
    category: AlertCategory | None = None,
    force: bool = False,
    recipient_override: str | None = None,
) -> tuple[bool, str]:
    settings = buscar_config_smtp(db)
    if not settings or not settings.smtp_enabled:
        return False, "SMTP não está habilitado."
    if not settings.smtp_host or not settings.smtp_sender or not (recipient_override or settings.smtp_recipients):
        return False, "A configuração SMTP está incompleta."
    if category and category not in CATEGORY_SETTINGS:
        return False, "Categoria de alerta inválida."
    if category and not force:
        enabled_field, timestamp_field = CATEGORY_SETTINGS[category]
        if not getattr(settings, enabled_field):
            return False, "Esta categoria de alerta está desabilitada."
        last_sent = int(getattr(settings, timestamp_field) or 0)
        if int(time.time()) - last_sent < int(settings.alert_cooldown_seconds or 1800):
            return False, "Alerta dentro do período de espera."

    recipients = [address for _, address in getaddresses([recipient_override or settings.smtp_recipients]) if "@" in address]
    if not recipients:
        return False, "Nenhum destinatário de e-mail válido foi configurado."

    message = EmailMessage()
    message["Subject"] = subject[:200]
    message["From"] = settings.smtp_sender
    message["To"] = ", ".join(recipients)
    message.set_content(body[:8000])

    smtp = None
    try:
        security = (settings.smtp_security or "starttls").lower()
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(settings.smtp_host, int(settings.smtp_port), timeout=20, context=ssl.create_default_context())
        elif security in {"starttls", "none"}:
            smtp = smtplib.SMTP(settings.smtp_host, int(settings.smtp_port), timeout=20)
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
        else:
            return False, "Modo de segurança SMTP inválido."

        if settings.smtp_username:
            smtp.login(settings.smtp_username, _descriptografar_senha_smtp(settings.smtp_password_encrypted or ""))
        smtp.send_message(message)
        if category and not force:
            _, timestamp_field = CATEGORY_SETTINGS[category]
            setattr(settings, timestamp_field, int(time.time()))
        settings.smtp_error = ""
        db.commit()
        return True, ""
    except Exception:
        db.rollback()
        settings.smtp_error = "send_failed"
        db.commit()
        logger.exception("Falha ao enviar e-mail de alerta.")
        return False, "Não foi possível enviar o e-mail. Verifique a configuração SMTP e os logs do servidor."
    finally:
        if smtp:
            try:
                smtp.quit()
            except Exception:
                pass


def alertar_falha_backup(db: Session, storage_name: str, error_count: int) -> tuple[bool, str]:
    body = (
        "O backup encontrou arquivos pendentes. Os originais locais foram preservados e serão tentados novamente.\n\n"
        f"Destino: {storage_name}\nArquivos com falha: {error_count}"
    )
    return enviar_email_configurado(
        db,
        f"Falha no backup Timelapse: {storage_name}",
        body,
        category="backup_failure",
    )


def alertar_espaco_disco(db: Session, free_percent: float, threshold_percent: int) -> tuple[bool, str]:
    severity = "critical" if threshold_percent <= 5 else "warning"
    subject = "Espaço crítico no disco local" if severity == "critical" else "Pouco espaço no disco local"
    body = (
        f"O disco local está com {free_percent:.1f}% de espaço livre.\n"
        f"Limite de alerta: {threshold_percent}%.\n"
        "A limpeza automática preserva arquivos recentes ou sem cópia remota verificada."
    )
    return enviar_email_configurado(
        db,
        subject,
        body,
        category=f"disk_{severity}",
    )


def alertar_falha_captura(db: Session, camera_id: int, camera_name: str) -> tuple[bool, str]:
    settings = buscar_config_smtp(db)
    if not settings or not settings.alert_camera_failure:
        return False, "Alerta de falha de câmera desabilitado."

    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        return False, "Câmera não encontrada."

    now = int(time.time())
    if now - int(camera.last_capture_alert_at or 0) < int(settings.alert_cooldown_seconds or 1800):
        return False, "Alerta dentro do período de espera."

    sent, error = enviar_email_configurado(
        db,
        f"Câmera offline ou erro na captura: {camera_name}",
        "A última tentativa de captura desta câmera falhou ou expirou.\n\n"
        f"Câmera: {camera_name}\nID: {camera_id}\n"
        "Verifique a alimentação, conectividade e configuração RTSP. O sistema tentará capturar novamente no próximo intervalo.",
    )
    if sent:
        camera.last_capture_alert_at = now
        db.commit()
    return sent, error


def enviar_email_recuperacao_senha(db: Session, recipient: str, reset_url: str) -> tuple[bool, str]:
    return enviar_email_configurado(
        db,
        "Recuperação de senha · Timelapse Manager",
        "Foi solicitada a redefinição da senha desta conta.\n\n"
        f"Use o link a seguir em até 30 minutos:\n{reset_url}\n\n"
        "Se você não solicitou essa alteração, ignore esta mensagem.",
        force=True,
        recipient_override=recipient,
    )
