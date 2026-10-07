from sqlalchemy import Column, Integer, String, Boolean, Float, ForeignKey, UniqueConstraint, DateTime
from datetime import datetime
from app.core.database import Base

class StorageConfig(Base):
    __tablename__ = "storage_configs"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, default="Armazenamento")
    tipo = Column(String, nullable=False, default="smb")
    host = Column(String, default="")
    usuario = Column(String, default="")
    senha = Column(String, default="")
    caminho_remoto = Column(String, default="/")
    ativo = Column(Boolean, default=False)
    total_bytes = Column(Integer, default=0)
    usado_bytes = Column(Integer, default=0)
    livre_bytes = Column(Integer, default=0)
    percentagem_uso = Column(Float, default=0)
    oauth_client_id = Column(String, default="")
    oauth_client_secret = Column(String, default="")
    oauth_access_token = Column(String, default="")
    oauth_refresh_token = Column(String, default="")
    oauth_token_expires_at = Column(Integer, default=0)
    oauth_state = Column(String, default="")
    oauth_quota_checked_at = Column(Integer, default=0)
    storage_quota_checked_at = Column(Integer, default=0)
    storage_error = Column(String, default="")
    backup_last_success_at = Column(Integer, default=0)
    backup_error = Column(String, default="")
    backup_alert_sent_at = Column(Integer, default=0)


class LocalStorageLocation(Base):
    __tablename__ = "local_storage_locations"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, nullable=False)
    caminho = Column(String, nullable=False, unique=True)
    ativo = Column(Boolean, nullable=False, default=True)
    criada_em = Column(DateTime, nullable=False, default=datetime.utcnow)


class StorageBackupFile(Base):
    __tablename__ = "storage_backup_files"
    __table_args__ = (UniqueConstraint("storage_config_id", "local_path", name="uq_backup_config_path"),)

    id = Column(Integer, primary_key=True, index=True)
    storage_config_id = Column(Integer, ForeignKey("storage_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    local_path = Column(String, nullable=False)
    remote_path = Column(String, nullable=False)
    remote_id = Column(String, default="")
    file_size = Column(Integer, nullable=False, default=0)
    file_mtime_ns = Column(Integer, nullable=False, default=0)
    backed_up_at = Column(Integer, nullable=False, default=0)
    last_error = Column(String, default="")


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, default=1)
    smtp_enabled = Column(Boolean, nullable=False, default=False)
    smtp_host = Column(String, nullable=False, default="")
    smtp_port = Column(Integer, nullable=False, default=587)
    smtp_username = Column(String, nullable=False, default="")
    smtp_password_encrypted = Column(String, nullable=False, default="")
    smtp_sender = Column(String, nullable=False, default="")
    smtp_recipients = Column(String, nullable=False, default="")
    smtp_security = Column(String, nullable=False, default="starttls")
    alert_backup_failure = Column(Boolean, nullable=False, default=True)
    alert_disk_warning = Column(Boolean, nullable=False, default=True)
    alert_disk_critical = Column(Boolean, nullable=False, default=True)
    alert_camera_failure = Column(Boolean, nullable=False, default=True)
    alert_cooldown_seconds = Column(Integer, nullable=False, default=1800)
    last_backup_alert_at = Column(Integer, nullable=False, default=0)
    last_disk_warning_alert_at = Column(Integer, nullable=False, default=0)
    last_disk_critical_alert_at = Column(Integer, nullable=False, default=0)
    smtp_error = Column(String, nullable=False, default="")
