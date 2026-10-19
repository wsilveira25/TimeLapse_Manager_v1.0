import shutil
import os
import subprocess
from datetime import datetime
from pathlib import Path
from app.core.paths import FFMPEG_BINARY, SYSTEM_TEMP_DIR, TEMP_DIR
from app.services.local_storage import escolher_local_de_captura, listar_raizes_ativas

ROOT_TEMP = str(TEMP_DIR)

def obter_info_frames(camera_id: int):
    todas_imagens = []
    seen_paths = set()
    for storage_root in listar_raizes_ativas():
        camera_root = storage_root / "Capturas" / f"camera_{camera_id}"
        if not camera_root.is_dir():
            continue
        for image in camera_root.rglob("frame_*.jpg"):
            resolved = image.resolve()
            if image.is_file() and resolved not in seen_paths:
                seen_paths.add(resolved)
                todas_imagens.append(image.name)
                    
    if not todas_imagens:
        return {"total": 0, "primeiro": None, "ultimo": None}
        
    todas_imagens.sort()
    return {
        "total": len(todas_imagens),
        "primeiro": todas_imagens[0].replace("frame_", "").replace(".jpg", ""),
        "ultimo": todas_imagens[-1].replace("frame_", "").replace(".jpg", "")
    }


def _resumir_erro_ffmpeg(stderr: str) -> str:
    linhas = [linha.strip() for linha in (stderr or "").splitlines() if linha.strip()]
    resumo = "\n".join(linhas[-12:])
    if len(resumo) > 3000:
        resumo = resumo[-3000:]
    return resumo or "O FFmpeg terminou sem gerar o arquivo de vídeo."

def gerar_timelapse(camera_id: int, data_inicio: str, data_fim: str, fps: int = 30, resolucao: str = "original", crf: int = 23, formato: str = "mp4", codec: str = "libx264", caminhos_frames=None):
    dt_inicio = datetime.strptime(data_inicio, "%Y-%m-%dT%H:%M")
    dt_fim = datetime.strptime(data_fim, "%Y-%m-%dT%H:%M")

    todas_imagens_paths = list(caminhos_frames or [])
    if caminhos_frames is None:
        seen_paths = set()
        for storage_root in listar_raizes_ativas():
            camera_root = storage_root / "Capturas" / f"camera_{camera_id}"
            if camera_root.is_dir():
                for path in camera_root.rglob("frame_*.jpg"):
                    resolved = path.resolve()
                    if path.is_file() and resolved not in seen_paths:
                        seen_paths.add(resolved)
                        todas_imagens_paths.append(str(path))

    imagens_filtradas = []
    for caminho_img in todas_imagens_paths:
        img_nome = Path(caminho_img).name
        data_str = img_nome.removeprefix("frame_").removesuffix(".jpg")
        dt_img = datetime.strptime(data_str, "%Y%m%d_%H%M%S")
        if dt_inicio <= dt_img <= dt_fim:
            imagens_filtradas.append((dt_img, caminho_img))

    if len(imagens_filtradas) < 10:
        return {"sucesso": False, "erro": f"Apenas {len(imagens_filtradas)} fotos neste período. Mínimo: 10."}

    imagens_filtradas.sort(key=lambda item: (item[0], item[1]))
    imagens_filtradas = [caminho for _, caminho in imagens_filtradas]

    lista_txt = os.path.join(SYSTEM_TEMP_DIR, f"lista_ffmpeg_cam{camera_id}.txt")
    with open(lista_txt, "w") as f:
        for img_path in imagens_filtradas:
            f.write(f"file '{Path(img_path).as_posix()}'\n")

    # Destino: /var/temp/Videos/camera_X/DDMMYYYY
    hoje_dir = datetime.now().strftime("%d%m%Y")
    video_storage_root = escolher_local_de_captura()
    if video_storage_root is None:
        return {"sucesso": False, "erro": "Todos os discos locais configurados estão com menos de 5% de espaço livre."}
    pasta_destino = os.path.join(str(video_storage_root), "Videos", f"camera_{camera_id}", hoje_dir)
    os.makedirs(pasta_destino, exist_ok=True)

    nome_video = f"timelapse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{formato}"
    caminho_video = os.path.join(pasta_destino, nome_video)
    caminho_video_tmp = os.path.join(pasta_destino, f".timelapse_tmp_{nome_video}")

    filtros_video = [f"settb=expr=1/{fps}", "setpts=N"]
    if resolucao != "original":
        altura = resolucao.replace("p", "")
        filtros_video.append(f"scale=-2:{altura}")
    filtros_video.append(f"fps={fps}")

    comando = [
        FFMPEG_BINARY, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lista_txt,
        "-vf", ",".join(filtros_video), "-c:v", codec, "-pix_fmt", "yuv420p", "-r", str(fps),
        "-crf", str(crf)
    ]

    comando.append(caminho_video_tmp)

    try:
        resultado = subprocess.run(comando, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        
        if resultado.returncode == 0 and os.path.isfile(caminho_video_tmp) and os.path.getsize(caminho_video_tmp) > 0:
            os.replace(caminho_video_tmp, caminho_video)
            return {"sucesso": True, "caminho_video": caminho_video, "total_frames": len(imagens_filtradas)}
            
        return {"sucesso": False, "erro": _resumir_erro_ffmpeg(resultado.stderr)}
    except Exception as e:
        return {"sucesso": False, "erro": str(e)}
    finally:
        if os.path.exists(lista_txt):
            os.remove(lista_txt)
        if os.path.exists(caminho_video_tmp):
            os.remove(caminho_video_tmp)