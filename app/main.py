import hashlib
import os
import secrets
import time
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# Importações do nosso projeto
from app.core.database import engine, Base, SessionLocal, ensure_alert_schema, ensure_storage_config_schema, get_db
from app.core.branding import get_developer_signature
from app.core.oauth_urls import obter_oauth_redirect_uri
from app.core.auth import (
    hash_password,
    load_session_secret,
    require_auth,
    session_cookie_settings,
    seed_initial_admin,
    verify_login,
    verify_password,
)
from app.models import camera
from app.models.user import SystemUser
from app.routers import cameras, system, users
from app.tasks.scheduler import iniciar_agendador

from app.models.storage import StorageConfig, StorageBackupFile
from app.services.alert_service import enviar_email_recuperacao_senha


# Cria as tabelas no banco de dados (se não existirem)
Base.metadata.create_all(bind=engine)
ensure_storage_config_schema()
ensure_alert_schema()

# Inicializa a aplicação FastAPI
app = FastAPI(
    title="Timelapse Manager API",
    description="Sistema de gestão de captura e timelapse para obras",
    version="1.0.0",
    dependencies=[Depends(require_auth)],
)
app.add_middleware(
    SessionMiddleware,
    **session_cookie_settings(load_session_secret()),
)

# Monta a pasta de arquivos estáticos (CSS, JS, Imagens)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# Configura a pasta de templates HTML
templates = Jinja2Templates(directory="app/templates")


@app.get("/privacidade", response_class=HTMLResponse)
async def pagina_privacidade(request: Request):
    return templates.TemplateResponse(request=request, name="privacy.html", context={"request": request})


@app.get("/termos", response_class=HTMLResponse)
async def pagina_termos(request: Request):
    return templates.TemplateResponse(request=request, name="terms.html", context={"request": request})


@app.get("/login", response_class=HTMLResponse)
async def pagina_login(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "erro": False, "developer_signature": get_developer_signature()})


@app.get("/forgot-password", response_class=HTMLResponse)
async def pagina_recuperacao_senha(request: Request):
    return templates.TemplateResponse(request=request, name="forgot_password.html", context={"request": request, "developer_signature": get_developer_signature()})


@app.get("/reset-password", response_class=HTMLResponse)
async def pagina_redefinir_senha(request: Request, token: str = ""):
    return templates.TemplateResponse(request=request, name="reset_password.html", context={"request": request, "token": token, "developer_signature": get_developer_signature()})


@app.post("/login", response_class=HTMLResponse)
async def autenticar_usuario(request: Request):
    body = (await request.body()).decode("utf-8", errors="replace")
    fields = parse_qs(body, keep_blank_values=True)
    username = fields.get("username", [""])[0]
    password = fields.get("password", [""])[0]
    with SessionLocal() as db:
        user = verify_login(username, password, db)
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"request": request, "erro": True, "developer_signature": get_developer_signature()},
            status_code=401,
        )

    request.session.clear()
    request.session["user_id"] = user.id
    request.session["auth_version"] = user.auth_version
    return RedirectResponse("/", status_code=303)


@app.post("/api/auth/forgot")
async def solicitar_recuperacao_senha(request: Request, db=Depends(get_db)):
    body = (await request.body()).decode("utf-8", errors="replace")
    email = parse_qs(body, keep_blank_values=True).get("email", [""])[0].strip().lower()
    user = db.query(SystemUser).filter(SystemUser.recovery_email == email, SystemUser.active.is_(True)).first() if email else None
    now = int(time.time())
    if user and now - int(user.reset_requested_at or 0) >= 60:
        token = secrets.token_urlsafe(32)
        user.reset_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        user.reset_expires_at = now + 30 * 60
        user.reset_requested_at = now
        db.commit()
        public_url = os.getenv("TIMELAPSE_PUBLIC_URL", str(request.base_url).rstrip("/"))
        reset_url = f"{public_url.rstrip('/')}/reset-password?token={token}"
        sent, _ = enviar_email_recuperacao_senha(db, email, reset_url)
        if not sent:
            user.reset_token_hash = ""
            user.reset_expires_at = 0
            user.reset_requested_at = now
            db.commit()
    return {"mensagem": "Se o e-mail estiver cadastrado e SMTP configurado, enviaremos instruções de recuperação."}


@app.post("/api/auth/reset")
async def redefinir_senha(request: Request, db=Depends(get_db)):
    body = (await request.body()).decode("utf-8", errors="replace")
    fields = parse_qs(body, keep_blank_values=True)
    token = fields.get("token", [""])[0]
    password = fields.get("password", [""])[0]
    confirmation = fields.get("password_confirm", [""])[0]
    if len(password) < 12:
        raise HTTPException(status_code=422, detail="A senha deve ter pelo menos 12 caracteres.")
    if password != confirmation:
        raise HTTPException(status_code=422, detail="As senhas não coincidem.")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    user = db.query(SystemUser).filter(
        SystemUser.reset_token_hash == token_hash,
        SystemUser.reset_expires_at > int(time.time()),
        SystemUser.active.is_(True),
    ).first()
    if not user:
        raise HTTPException(status_code=400, detail="O link de recuperação é inválido ou expirou.")
    user.password_hash = hash_password(password)
    user.auth_version += 1
    user.reset_token_hash = ""
    user.reset_expires_at = 0
    db.commit()
    return {"mensagem": "Senha alterada. Faça login com a nova senha."}


@app.post("/api/auth/password/change")
async def alterar_senha_usuario(request: Request, db=Depends(get_db)):
    fields = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
    current_password = fields.get("current_password", [""])[0]
    password = fields.get("password", [""])[0]
    confirmation = fields.get("password_confirm", [""])[0]
    user = db.query(SystemUser).filter(SystemUser.id == request.session.get("user_id"), SystemUser.active.is_(True)).first()
    if not user or not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Senha atual inválida.")
    if len(password) < 12:
        raise HTTPException(status_code=422, detail="A senha deve ter pelo menos 12 caracteres.")
    if password != confirmation:
        raise HTTPException(status_code=422, detail="As senhas não coincidem.")
    user.password_hash = hash_password(password)
    user.auth_version += 1
    db.commit()
    request.session["auth_version"] = user.auth_version
    return {"mensagem": "Senha alterada com sucesso."}


@app.post("/api/auth/recovery-email")
async def atualizar_email_recuperacao(request: Request, db=Depends(get_db)):
    fields = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
    email = fields.get("email", [""])[0].strip().lower()
    if email and ("@" not in email or len(email) > 320):
        raise HTTPException(status_code=422, detail="Informe um e-mail válido.")
    user = db.query(SystemUser).filter(SystemUser.id == request.session.get("user_id"), SystemUser.active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=401, detail="Sessão expirada.")
    duplicate = db.query(SystemUser.id).filter(SystemUser.recovery_email == email, SystemUser.id != user.id).first() if email else None
    if duplicate:
        raise HTTPException(status_code=409, detail="Este e-mail de recuperação já está associado a outra conta.")
    user.recovery_email = email or None
    db.commit()
    return {"mensagem": "E-mail de recuperação atualizado."}


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

# Evento que corre assim que a API liga (inicia o motor de fotos)
@app.on_event("startup")
def startup_event():
    with SessionLocal() as db:
        seed_initial_admin(db)
    iniciar_agendador()

# Inclui as rotas da API de Câmeras
app.include_router(cameras.router)
app.include_router(system.router)
app.include_router(users.router)


# Rota principal para carregar o painel web (Frontend)
@app.get("/", response_class=HTMLResponse)
async def pagina_inicial(request: Request):
    user = getattr(request.state, "current_user", None)
    # Parâmetros nomeados corretamente para evitar o erro de TypeError
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={
            "request": request,
            "is_admin": bool(user and user.role == "admin"),
            "username": user.username if user else "",
            "recovery_email": (user.recovery_email or "") if user else "",
            "developer_signature": get_developer_signature(),
            "oauth_redirect_uri": obter_oauth_redirect_uri(request),
        }
    )

# Rota de teste para ver se o backend está online
@app.get("/api/status")
async def status_sistema():
    return {"status": "online", "servico": "Timelapse Manager"}