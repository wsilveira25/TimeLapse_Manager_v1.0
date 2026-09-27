import shutil
import os
import subprocess
from datetime import datetime
from pathlib import Path
from app.core.paths import FFMPEG_BINARY, SYSTEM_TEMP_DIR, TEMP_DIR

ROOT_TEMP = str(TEMP_DIR)

def obter_info_frames(camera_id: int):
    todas_imagens = []
    pasta_camera = os.path.join(ROOT_TEMP, "Capturas", f"camera_{camera_id}")
    
    if not os.path.exists(pasta_camera):
        return {"total": 0, "primeiro": None, "ultimo": None}
        
    # Vasculha apenas as pastas de dias dentro desta câmara específica
    for dia_dir in os.listdir(pasta_camera):
        caminho_dia = os.path.join(pasta_camera, dia_dir)
        if os.path.isdir(caminho_dia):
            for img in os.listdir(caminho_dia):
                if img.endswith(".jpg"):
                    todas_imagens.append(img)
                    
    if not todas_imagens:
        return {"total": 0, "primeiro": None, "ultimo": None}
        
    todas_imagens.sort()
    return {
        "total": len(todas_imagens),
        "primeiro": todas_imagens[0].replace("frame_", "").replace(".jpg", ""),
        "ultimo": todas_imagens[-1].replace("frame_", "").replace(".jpg", "")
    }

def gerar_timelapse(camera_id: int, data_inicio: str, data_fim: str, fps: int = 30, resolucao: str = "original", crf: int = 23, formato: str = "mp4", codec: str = "libx264", caminhos_frames=None):
    dt_inicio = datetime.strptime(data_inicio, "%Y-%m-%dT%H:%M")
    dt_fim = datetime.strptime(data_fim, "%Y-%m-%dT%H:%M")

    todas_imagens_paths = list(caminhos_frames or [])
    pasta_camera = os.path.join(ROOT_TEMP, "Capturas", f"camera_{camera_id}")
    
    if caminhos_frames is None and os.path.exists(pasta_camera):
        for dia_dir in os.listdir(pasta_camera):
            caminho_dia = os.path.join(pasta_camera, dia_dir)
            if os.path.isdir(caminho_dia):
                for img in os.listdir(caminho_dia):
                    if img.endswith(".jpg"):
                        todas_imagens_paths.append(os.path.join(caminho_dia, img))

    imagens_filtradas = []
    for caminho_img in todas_imagens_paths:
        img_nome = os.path.basename(caminho_img)
        data_str = img_nome.replace("frame_", "").replace(".jpg", "")
        dt_img = datetime.strptime(data_str, "%Y%m%d_%H%M%S")
        if dt_inicio <= dt_img <= dt_fim:
            imagens_filtradas.append(caminho_img)

    if len(imagens_filtradas) < 10:
        return {"sucesso": False, "erro": f"Apenas {len(imagens_filtradas)} fotos neste período. Mínimo: 10."}

    imagens_filtradas.sort()

    lista_txt = os.path.join(SYSTEM_TEMP_DIR, f"lista_ffmpeg_cam{camera_id}.txt")
    with open(lista_txt, "w") as f:
        for img_path in imagens_filtradas:
            f.write(f"file '{Path(img_path).as_posix()}'\n")

    # Destino: /var/temp/Videos/camera_X/DDMMYYYY
    hoje_dir = datetime.now().strftime("%d%m%Y")
    pasta_destino = os.path.join(ROOT_TEMP, "Videos", f"camera_{camera_id}", hoje_dir)
    os.makedirs(pasta_destino, exist_ok=True)

    nome_video = f"timelapse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{formato}"
    caminho_video = os.path.join(pasta_destino, nome_video)
    caminho_video_tmp = os.path.join(SYSTEM_TEMP_DIR, nome_video)

    comando = [
        FFMPEG_BINARY, "-y", "-f", "concat", "-safe", "0", "-i", lista_txt,
        "-c:v", codec, "-pix_fmt", "yuv420p", "-r", str(fps),
        "-crf", str(crf)
    ]
    
    if resolucao != "original":
        altura = resolucao.replace("p", "")
        comando.extend(["-vf", f"scale=-2:{altura}"])

    comando.append(caminho_video_tmp)

    try:
        resultado = subprocess.run(comando, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        
        if resultado.returncode == 0 and os.path.exists(caminho_video_tmp):
            shutil.move(caminho_video_tmp, caminho_video)
            return {"sucesso": True, "caminho_video": caminho_video, "total_frames": len(imagens_filtradas)}
            
        return {"sucesso": False, "erro": resultado.stderr}
    except Exception as e:
        return {"sucesso": False, "erro": str(e)}
    finally:
        if os.path.exists(lista_txt):
            os.remove(lista_txt)
        if os.path.exists(caminho_video_tmp):
            os.remove(caminho_video_tmp)