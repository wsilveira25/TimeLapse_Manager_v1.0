from sqlalchemy import Boolean, Column, Integer, String

from app.core.database import Base


class SystemUser(Base):
    __tablename__ = "system_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(80), nullable=False, unique=True, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(16), nullable=False, default="user")
    recovery_email = Column(String(320), nullable=True, unique=True)
    active = Column(Boolean, nullable=False, default=True)
    auth_version = Column(Integer, nullable=False, default=0)
    reset_token_hash = Column(String(64), nullable=False, default="")
    reset_expires_at = Column(Integer, nullable=False, default=0)
    reset_requested_at = Column(Integer, nullable=False, default=0)
    created_at = Column(Integer, nullable=False, default=0)
