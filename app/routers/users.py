import re
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import hash_password, require_admin
from app.core.database import get_db
from app.models.user import SystemUser


router = APIRouter(prefix="/api/users", tags=["Usuários"])
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{3,80}$")


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "user"] = "user"
    recovery_email: str = Field(min_length=5, max_length=320)


class UserUpdateRequest(BaseModel):
    role: Literal["admin", "user"] | None = None
    active: bool | None = None


def _valid_email(email: str) -> bool:
    local, separator, domain = email.strip().partition("@")
    return bool(separator and local and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def _user_summary(user: SystemUser) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "recovery_email": user.recovery_email or "",
        "active": bool(user.active),
        "created_at": int(user.created_at or 0),
    }


@router.get("")
def listar_usuarios(db: Session = Depends(get_db), _admin: SystemUser = Depends(require_admin)):
    users = db.query(SystemUser).order_by(SystemUser.username).all()
    return [_user_summary(user) for user in users]


@router.post("", status_code=status.HTTP_201_CREATED)
def criar_usuario(
    dados: UserCreateRequest,
    db: Session = Depends(get_db),
    _admin: SystemUser = Depends(require_admin),
):
    username = dados.username.strip().lower()
    recovery_email = dados.recovery_email.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(status_code=422, detail="Use 3 a 80 caracteres: letras, números, ponto, hífen ou sublinhado.")
    if not _valid_email(recovery_email):
        raise HTTPException(status_code=422, detail="Informe um e-mail de recuperação válido.")
    if db.query(SystemUser.id).filter(SystemUser.username == username).first():
        raise HTTPException(status_code=409, detail="Este usuário já existe.")
    if db.query(SystemUser.id).filter(SystemUser.recovery_email == recovery_email).first():
        raise HTTPException(status_code=409, detail="Este e-mail de recuperação já está associado a uma conta.")

    user = SystemUser(
        username=username,
        password_hash=hash_password(dados.password),
        role=dados.role,
        recovery_email=recovery_email,
        active=True,
        created_at=int(time.time()),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_summary(user)


@router.patch("/{user_id}")
def atualizar_usuario(
    user_id: int,
    dados: UserUpdateRequest,
    db: Session = Depends(get_db),
    admin: SystemUser = Depends(require_admin),
):
    user = db.query(SystemUser).filter(SystemUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    role_changed = dados.role is not None and dados.role != user.role
    if user.id == admin.id and (role_changed or dados.active is False):
        raise HTTPException(status_code=409, detail="Não é possível alterar ou remover o próprio acesso de administrador.")

    vai_remover_admin = user.role == "admin" and (
        dados.role == "user" or dados.active is False
    )
    if vai_remover_admin:
        admins_ativos = db.query(SystemUser).filter(SystemUser.role == "admin", SystemUser.active.is_(True)).count()
        if admins_ativos <= 1:
            raise HTTPException(status_code=409, detail="O sistema precisa manter pelo menos um administrador ativo.")

    active_changed = dados.active is not None and dados.active != user.active
    if dados.role is not None:
        user.role = dados.role
    if dados.active is not None:
        user.active = dados.active
    if role_changed or active_changed:
        user.auth_version += 1
    db.commit()
    db.refresh(user)
    return _user_summary(user)
