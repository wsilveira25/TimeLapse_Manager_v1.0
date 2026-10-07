import subprocess
import os
from datetime import datetime
from app.core.paths import FFMPEG_BINARY
from app.services.local_storage import escolher_local_de_captura


def capturar_frame_camera(camera_id: int, rtsp_url: str, qualidade: int = 2):
    storage_root = escolher_local_de_captura()
    if storage_root is None:
        print(f"[Câmara {camera_id}] Captura ignorada: todos os discos configurados têm menos de 5% de espaço livre.")
        return None

    agora_dt = datetime.now()
    pasta_dia = agora_dt.strftime("%d%m%Y")
    qualidade = max(1, min(int(qualidade), 31))

    # Estrutura otimizada: /var/temp/Capturas/camera_X/DDMMYYYY
    pasta_destino = os.path.join(str(storage_root), "Capturas", f"camera_{camera_id}", pasta_dia)
    os.makedirs(pasta_destino, exist_ok=True)

    agora_str = agora_dt.strftime("%Y%m%d_%H%M%S")
    nome_arquivo = f"frame_{agora_str}.jpg"
    caminho_completo = os.path.join(pasta_destino, nome_arquivo)
    caminho_temporario = os.path.join(pasta_destino, f".{nome_arquivo}.part.jpg")

    comando = [
        FFMPEG_BINARY,
        "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vframes", "1",
        "-q:v", str(qualidade),
        caminho_temporario,
    ]

    try:
        resultado = subprocess.run(comando, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
        if resultado.returncode == 0 and os.path.exists(caminho_temporario):
            os.replace(caminho_temporario, caminho_completo)
            print(f"[Câmara {camera_id}] Captura efetuada: {pasta_dia} -> {nome_arquivo}")
            return True
        print(f"[Câmara {camera_id}] Falha na captura. FFmpeg: {resultado.stderr}")
        return False
    except Exception as e:
        print(f"[Câmara {camera_id}] Erro no processo: {e}")
        return False
    finally:
        if os.path.exists(caminho_temporario):
            os.remove(caminho_temporario)