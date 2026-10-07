import cv2
import mimetypes
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional

from app.core.auth import require_admin
from app.core.paths import TEMP_DIR
from app.core.database import get_db
from app.models.camera import Camera
from app.models.storage import StorageConfig
from app.services.onvif_service import descobrir_perfis_camera
from app.services.video_service import gerar_timelapse # <-- Importação do motor de vídeo

from app.services.video_service import gerar_timelapse, obter_info_frames
from app.services.backup_service import baixar_frames_backup, buscar_frames_backup, obter_info_frames_backup

router = APIRouter(prefix="/api/cameras", tags=["Câmeras"])

# ==========================================
# SCHEMAS (Modelos de Validação)
# ==========================================
class VideoRequest(BaseModel):
    data_inicio: str
    data_fim: str
    fps: int
    resolucao: str
    crf: int
    formato: str  
    codec: str    
    storage_config_id: Optional[int] = None

class DescobertaRequest(BaseModel):
    ip: str
    porta: int
    usuario: str
    senha: str
    usar_https: bool = False

class CameraCreate(BaseModel):
    nome: str
    ip: str
    porta: int = 80
    usar_https: bool = False
    usuario: str
    senha: str
    resolucao: Optional[str] = None
    link_rtsp: Optional[str] = None

class CameraResponse(BaseModel):
    id: int
    nome: str
    ip: str
    porta: int
    usar_https: bool
    resolucao: Optional[str] = None
    ativa: bool
    intervalo_minutos: int
    qualidade_captura: int

    class Config:
        from_attributes = True

class CameraUpdate(BaseModel):
    intervalo_minutos: Optional[int] = None
    intervalo_captura: Optional[int] = None
    ativa: bool
    qualidade_captura: int
    resolucao: Optional[str] = None
    link_rtsp: Optional[str] = None

# ==========================================
# ROTAS BÁSICAS (CRUD)
# ==========================================
@router.post("/descobrir", dependencies=[Depends(require_admin)])
async def descobrir_camera(dados: DescobertaRequest):
    resultado = descobrir_perfis_camera(
        ip=dados.ip, porta=dados.porta, 
        usuario=dados.usuario, senha=dados.senha, usar_https=dados.usar_https
    )
    if not resultado["sucesso"]:
        raise HTTPException(status_code=400, detail=resultado["erro"])
    return resultado

@router.post("/", response_model=CameraResponse, dependencies=[Depends(require_admin)])
def salvar_camera(camera: CameraCreate, db: Session = Depends(get_db)):
    nova_camera = Camera(**camera.model_dump())
    db.add(nova_camera)
    db.commit()
    db.refresh(nova_camera)
    return nova_camera

@router.get("/", response_model=List[CameraResponse], dependencies=[Depends(require_admin)])
def listar_cameras(db: Session = Depends(get_db)):
    return db.query(Camera).all()


def _timestamp_arquivo_frame(path: Path) -> float:
    try:
        return datetime.strptime(path.stem.removeprefix("frame_"), "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return path.stat().st_mtime


@router.get("/{camera_id}/media", dependencies=[Depends(require_admin)])
def listar_midias_camera(camera_id: int, db: Session = Depends(get_db)):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Câmera não encontrada.")

    captures_root = (TEMP_DIR / "Capturas" / f"camera_{camera_id}").resolve()
    frames = sorted(
        (path for path in captures_root.rglob("frame_*.jpg") if path.is_file()),
        key=_timestamp_arquivo_frame,
    ) if captures_root.is_dir() else []
    days = {}
    camera_capture_bytes = 0
    for path in frames:
        timestamp = _timestamp_arquivo_frame(path)
        day = datetime.fromtimestamp(timestamp).date().isoformat()
        days.setdefault(day, {"data": day, "total": 0})["total"] += 1
        camera_capture_bytes += path.stat().st_size

    disk_total, disk_used, disk_free = shutil.disk_usage(TEMP_DIR)
    sample_count = min(len(frames), 9)
    sample_indexes = sorted({round(index * (len(frames) - 1) / max(sample_count - 1, 1)) for index in range(sample_count)})
    capture_items = []
    for index in sample_indexes:
        path = frames[index]
        relative_path = path.relative_to(captures_root).as_posix()
        capture_items.append({
            "url": f"/api/cameras/{camera_id}/frames/{quote(relative_path, safe='/')}" ,
            "timestamp": _timestamp_arquivo_frame(path),
        })

    videos_root = (TEMP_DIR / "Videos" / f"camera_{camera_id}").resolve()
    video_items = []
    if videos_root.is_dir():
        video_paths = sorted(
            (path for path in videos_root.rglob("*") if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".avi"}),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in video_paths[:50]:
            relative_path = path.relative_to(videos_root).as_posix()
            stat = path.stat()
            video_items.append({
                "nome": path.name,
                "url": f"/api/cameras/{camera_id}/videos/{quote(relative_path, safe='/')}" ,
                "timestamp": stat.st_mtime,
                "tamanho_bytes": stat.st_size,
                "formato": path.suffix.lstrip(".").upper(),
            })

    return {
        "camera": {
            "id": camera.id,
            "nome": camera.nome,
            "ip": camera.ip,
            "ativa": camera.ativa,
            "resolucao": camera.resolucao or "Não informada",
            "intervalo_minutos": camera.intervalo_minutos,
            "qualidade_captura": camera.qualidade_captura,
        },
        "capturas": {
            "total": len(frames),
            "primeira": _timestamp_arquivo_frame(frames[0]) if frames else None,
            "ultima": _timestamp_arquivo_frame(frames[-1]) if frames else None,
            "dias": list(days.values()),
            "amostras": capture_items,
        },
        "armazenamento": {
            "camera_bytes": camera_capture_bytes,
            "disco_total_bytes": disk_total,
            "disco_usado_bytes": disk_used,
            "disco_livre_bytes": disk_free,
            "disco_percentual_usado": round(disk_used / disk_total * 100, 1) if disk_total else 0,
        },
        "videos": video_items,
    }


@router.get("/{camera_id}/frames", dependencies=[Depends(require_admin)])
def listar_capturas_data(camera_id: int, data: date, db: Session = Depends(get_db)):
    if not db.query(Camera.id).filter(Camera.id == camera_id).first():
        raise HTTPException(status_code=404, detail="Câmera não encontrada.")
    captures_root = (TEMP_DIR / "Capturas" / f"camera_{camera_id}").resolve()
    day_directory = captures_root / data.strftime("%d%m%Y")
    paths = sorted(
        (path for path in day_directory.glob("frame_*.jpg") if path.is_file()),
        key=_timestamp_arquivo_frame,
    ) if day_directory.is_dir() else []
    return {
        "data": data.isoformat(),
        "total": len(paths),
        "frames": [
            {
                "url": f"/api/cameras/{camera_id}/frames/{quote(path.relative_to(captures_root).as_posix(), safe='/')}",
                "timestamp": _timestamp_arquivo_frame(path),
            }
            for path in paths
        ],
    }


def _camera_media_path(camera_id: int, media_type: str, relative_path: str) -> Path:
    base = (TEMP_DIR / media_type / f"camera_{camera_id}").resolve()
    requested = (base / relative_path).resolve()
    if base not in requested.parents or not requested.is_file():
        raise HTTPException(status_code=404, detail="Arquivo de mídia não encontrado.")
    return requested


@router.get("/{camera_id}/frames/{relative_path:path}", dependencies=[Depends(require_admin)])
def servir_captura_camera(camera_id: int, relative_path: str, db: Session = Depends(get_db)):
    if not db.query(Camera.id).filter(Camera.id == camera_id).first():
        raise HTTPException(status_code=404, detail="Câmera não encontrada.")
    path = _camera_media_path(camera_id, "Capturas", relative_path)
    if path.suffix.lower() != ".jpg" or not path.name.startswith("frame_"):
        raise HTTPException(status_code=404, detail="Captura não encontrada.")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{camera_id}/videos/{relative_path:path}", dependencies=[Depends(require_admin)])
def servir_video_camera(camera_id: int, relative_path: str, db: Session = Depends(get_db)):
    if not db.query(Camera.id).filter(Camera.id == camera_id).first():
        raise HTTPException(status_code=404, detail="Câmera não encontrada.")
    path = _camera_media_path(camera_id, "Videos", relative_path)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)

# ==========================================
# ROTA DE PREVIEW (STREAMING DE VÍDEO)
# ==========================================
def gerar_frames_video(rtsp_url: str):
    cap = cv2.VideoCapture(rtsp_url)
    while True:
        sucesso, frame = cap.read()
        if not sucesso:
            break
        frame = cv2.resize(frame, (854, 480))
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    cap.release()

@router.get("/{camera_id}/stream", dependencies=[Depends(require_admin)])
def stream_camera(camera_id: int, db: Session = Depends(get_db)):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera or not camera.link_rtsp:
        raise HTTPException(status_code=404, detail="Câmera não encontrada ou sem link")
    return StreamingResponse(
        gerar_frames_video(camera.link_rtsp), 
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

# ==========================================
# ROTAS DE CONFIGURAÇÃO AVANÇADA
# ==========================================
@router.get("/{camera_id}/perfis", dependencies=[Depends(require_admin)])
def listar_perfis_camera_salva(camera_id: int, db: Session = Depends(get_db)):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Câmera não encontrada")
    
    resultado = descobrir_perfis_camera(
        ip=camera.ip, porta=camera.porta, 
        usuario=camera.usuario, senha=camera.senha, usar_https=camera.usar_https
    )
    if not resultado["sucesso"]:
        raise HTTPException(status_code=400, detail=resultado["erro"])
    return resultado["perfis"]

@router.put("/{camera_id}", dependencies=[Depends(require_admin)])
def atualizar_camera(camera_id: int, dados: CameraUpdate, db: Session = Depends(get_db)):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Câmera não encontrada")

    intervalo = dados.intervalo_minutos if dados.intervalo_minutos is not None else dados.intervalo_captura
    if intervalo is not None:
        camera.intervalo_minutos = int(intervalo)

    camera.ativa = dados.ativa
    camera.qualidade_captura = dados.qualidade_captura

    if dados.resolucao and dados.link_rtsp:
        camera.resolucao = dados.resolucao
        camera.link_rtsp = dados.link_rtsp

    db.commit()
    return {"sucesso": True, "mensagem": "Configurações atualizadas"}

@router.delete("/{camera_id}", dependencies=[Depends(require_admin)])
def deletar_camera(camera_id: int, db: Session = Depends(get_db)):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Câmera não encontrada")
    db.delete(camera)
    db.commit()
    return {"sucesso": True}

# ==========================================
# ROTAS DE TIMELAPSE
# ==========================================
@router.get("/{camera_id}/frames-info", dependencies=[Depends(require_admin)])
def info_frames_camera(camera_id: int, storage_config_id: Optional[int] = None, db: Session = Depends(get_db)):
    # Rota para o frontend saber quantas fotos existem antes de abrir o modal
    if storage_config_id is not None:
        return obter_info_frames_backup(db, storage_config_id, camera_id)
    return obter_info_frames(camera_id)

@router.post("/{camera_id}/gerar-video", dependencies=[Depends(require_admin)])
def gerar_video_timelapse(camera_id: int, config: VideoRequest, db: Session = Depends(get_db)):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Câmera não encontrada")
    
    caminhos_frames = None
    if config.storage_config_id is not None:
        storage = db.query(StorageConfig).filter(StorageConfig.id == config.storage_config_id).first()
        if not storage:
            raise HTTPException(status_code=404, detail="Destino de backup não encontrado.")
        inicio = datetime.strptime(config.data_inicio, "%Y-%m-%dT%H:%M")
        fim = datetime.strptime(config.data_fim, "%Y-%m-%dT%H:%M")
        registros = buscar_frames_backup(db, storage.id, camera.id, inicio, fim)
        if len(registros) < 10:
            raise HTTPException(status_code=400, detail=f"Apenas {len(registros)} frames disponíveis no backup neste período. Mínimo: 10.")
        try:
            with tempfile.TemporaryDirectory(prefix="timelapse_backup_", dir=str(TEMP_DIR)) as pasta_temporaria:
                caminhos_frames = baixar_frames_backup(registros, storage, pasta_temporaria)
                resultado = gerar_timelapse(
                    camera_id=camera.id,
                    data_inicio=config.data_inicio,
                    data_fim=config.data_fim,
                    fps=config.fps,
                    resolucao=config.resolucao,
                    crf=config.crf,
                    formato=config.formato,
                    codec=config.codec,
                    caminhos_frames=caminhos_frames,
                )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Falha ao baixar frames do backup: {exc}") from exc
    else:
        resultado = gerar_timelapse(
            camera_id=camera.id,
            data_inicio=config.data_inicio,
            data_fim=config.data_fim,
            fps=config.fps,
            resolucao=config.resolucao,
            crf=config.crf,
            formato=config.formato,
            codec=config.codec,
        )
    
    if not resultado["sucesso"]:
        raise HTTPException(status_code=400, detail=resultado["erro"])
        
    return resultado