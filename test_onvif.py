import os
import requests
from onvif import ONVIFCamera

CAMERA_PORT = int(os.getenv('TEST_CAMERA_PORT', '80'))

old_request = requests.Session.request
def new_request(self, method, url, **kwargs):
    if CAMERA_PORT == 443 and url.startswith('http://'):
        url = url.replace('http://', 'https://')
    return old_request(self, method, url, **kwargs)
requests.Session.request = new_request

def testar_camera(ip, porta, utilizador, palavra_passe):
    print(f"Conectando na câmera {ip}:{porta} via HTTPS...\n")
    
    try:
        mycam = ONVIFCamera(ip, porta, utilizador, palavra_passe)
        media_service = mycam.create_media_service()
        perfis = media_service.GetProfiles()
        
        print(f"✅ Sucesso! Encontrados {len(perfis)} perfil(is) de vídeo:")
        
        for idx, perfil in enumerate(perfis):
            print(f"\n--- Perfil {idx + 1}: {perfil.Name} ---")
            
            # Resgata Resolução
            if hasattr(perfil, 'VideoEncoderConfiguration'):
                vec = perfil.VideoEncoderConfiguration
                print(f"Codec: {vec.Encoding}")
                if hasattr(vec, 'Resolution'):
                    print(f"Resolução: {vec.Resolution.Width} x {vec.Resolution.Height}")
            
            # Resgata o Link RTSP com a sintaxe correta do onvif-zeep moderno
            try:
                req = {
                    'StreamSetup': {
                        'Stream': 'RTP-Unicast',
                        'Transport': {'Protocol': 'RTSP'}
                    },
                    'ProfileToken': perfil.token
                }
                uri = media_service.GetStreamUri(req)
                print("Link RTSP obtido com sucesso; URL ocultada.")
            except Exception as e:
                print(f"Não foi possível resgatar o RTSP: {e}")

    except Exception as e:
        print(f"\n❌ Erro ao conectar na câmera: {e}")

if __name__ == '__main__':
    CAMERA_IP = os.getenv('TEST_CAMERA_IP')
    USUARIO = os.getenv('TEST_CAMERA_USERNAME')
    SENHA = os.getenv('TEST_CAMERA_PASSWORD')
    if not all((CAMERA_IP, USUARIO, SENHA)):
        raise SystemExit(
            "Defina TEST_CAMERA_IP, TEST_CAMERA_USERNAME e TEST_CAMERA_PASSWORD no ambiente."
        )

    testar_camera(CAMERA_IP, CAMERA_PORT, USUARIO, SENHA)
