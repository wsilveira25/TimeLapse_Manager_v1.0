import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.user import SystemUser


def _read_credential(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    credentials_directory = os.getenv("TIMELAPSE_CREDENTIALS_DIR") or os.getenv("CREDENTIALS_DIRECTORY")
    if not credentials_directory:
        return None
    try:
        return (Path(credentials_directory) / name).read_text(encoding="utf-8").rstrip("\n") or None
    except OSError:
        return None


def configured_login() -> tuple[str | None, str | None]:
    return _read_credential("TIMELAPSE_USERNAME"), _read_credential("TIMELAPSE_PASSWORD")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_text, r_text, p_text, salt_text, hash_text = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(hash_text.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, MemoryError):
        return False


def seed_initial_admin(db: Session) -> SystemUser | None:
    if db.query(SystemUser.id).first():
        return None
    username, password = configured_login()
    if not username or not password:
        raise RuntimeError("Não é possível inicializar o admin sem as credenciais provisionadas pelo instalador.")
    admin = SystemUser(
        username=username.strip().lower(),
        password_hash=hash_password(password),
        role="admin",
        recovery_email=None,
        active=True,
        created_at=int(time.time()),
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def load_session_secret() -> str:
    return _read_credential("TIMELAPSE_SESSION_SECRET") or secrets.token_urlsafe(48)


def verify_login(username: str, password: str, db: Session) -> SystemUser | None:
    normalized = username.strip().lower()
    user = db.query(SystemUser).filter(SystemUser.username == normalized, SystemUser.active.is_(True)).first()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def require_auth(request: Request, db: Session = Depends(get_db)) -> SystemUser | None:
    if not db.query(SystemUser.id).first():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Os usuários do sistema ainda não foram inicializados.",
        )

    public_paths = {
        "/login",
        "/forgot-password",
        "/reset-password",
        "/api/auth/forgot",
        "/api/auth/reset",
    }
    if request.url.path in public_paths:
        return None

    user_id = request.session.get("user_id")
    auth_version = request.session.get("auth_version")
    user = db.query(SystemUser).filter(SystemUser.id == user_id, SystemUser.active.is_(True)).first() if user_id else None
    if not user or auth_version is None or int(auth_version) != user.auth_version:
        request.session.clear()
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão expirada.")
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )

    request.state.current_user = user
    return user


def require_admin(user: SystemUser = Depends(require_auth)) -> SystemUser:
    if user is None or user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso restrito ao administrador.")
    return user


def session_cookie_settings(secret_key: str) -> dict[str, str | int | bool]:
    return {
        "secret_key": secret_key,
        "session_cookie": "timelapse_session",
        "max_age": 8 * 60 * 60,
        "same_site": "lax",
        "https_only": True,
    }