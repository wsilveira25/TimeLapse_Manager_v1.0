import subprocess
import os
from datetime import datetime

def capturar_frame(rtsp_url, pasta_destino):
    # Cria o nome do arquivo baseado na data e hora exata
    agora = datetime.now().strftime("%Y%m%d_%H%M%S")
    nome_arquivo = f"frame_{agora}.jpg"
    caminho_completo = os.path.join(pasta_destino, nome_arquivo)
    
    print(f"Iniciando captura do frame para: {caminho_completo}")
    
    # Comando FFmpeg otimizado para extrair apenas 1 frame com alta qualidade
    # -rtsp_transport tcp: Força o uso de TCP (evita artefatos/falhas na imagem comuns no UDP)
    # -i: O link de origem
    # -vframes 1: Extrai apenas um quadro
    # -q:v 2: Mantém a qualidade alta da imagem (escala de 1 a 31, menor é melhor)
    comando = [
        "ffmpeg",
        "-y", # Sobrescreve se já existir
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vframes", "1",
        "-q:v", "2",
        caminho_completo
    ]
    
    try:
        # Executa o comando em background escondendo os logs gigantes do FFmpeg
        resultado = subprocess.run(comando, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        
        # Verifica se o arquivo foi realmente criado no disco
        if resultado.returncode == 0 and os.path.exists(caminho_completo):
            print(f"✅ Sucesso! Frame salvo em: {caminho_completo}")
            tamanho = os.path.getsize(caminho_completo) / 1024
            print(f"Tamanho do arquivo: {tamanho:.2f} KB")
        else:
            print("❌ Erro ao capturar o frame. Saída do FFmpeg:")
            print(resultado.stderr)
            
    except Exception as e:
        print(f"Erro na execução do subprocesso: {e}")

if __name__ == '__main__':
    LINK_RTSP = os.getenv("TEST_RTSP_URL")
    if not LINK_RTSP:
        raise SystemExit("Defina TEST_RTSP_URL no ambiente antes de executar este teste.")
    PASTA_POOL = "/var/temp/frame_pool/"
    
    capturar_frame(LINK_RTSP, PASTA_POOL)