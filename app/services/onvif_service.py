import urllib3
import requests
from onvif import ONVIFCamera
from urllib.parse import quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Sobrescreve a verificação SSL nativa
old_request = requests.Session.request
def new_request(self, method, url, **kwargs):
    kwargs['verify'] = False
    return old_request(self, method, url, **kwargs)
requests.Session.request = new_request

def descobrir_perfis_camera(ip: str, porta: int, usuario: str, senha: str, usar_https: bool):
    try:
        # Se usar_https for verdadeiro, aplicamos a reescrita de URL que criamos antes
        if usar_https:
            def https_request(self, method, url, **kwargs):
                kwargs['verify'] = False
                if url.startswith('http://'):
                    url = url.replace('http://', 'https://')
                return old_request(self, method, url, **kwargs)
            requests.Session.request = https_request
        else:
            requests.Session.request = new_request

        mycam = ONVIFCamera(ip, porta, usuario, senha)
        media_service = mycam.create_media_service()
        perfis = media_service.GetProfiles()
        
        lista_perfis = []
        
        # Oculta caracteres especiais da senha (como #, @, &) para a URL RTSP
        senha_url = quote(senha, safe='')
        
        for perfil in perfis:
            dados_perfil = {
                "nome": perfil.Name,
                "token": perfil.token,
                "codec": None,
                "resolucao": None,
                "rtsp_url": None
            }
            
            if hasattr(perfil, 'VideoEncoderConfiguration'):
                vec = perfil.VideoEncoderConfiguration
                dados_perfil["codec"] = vec.Encoding
                if hasattr(vec, 'Resolution'):
                    dados_perfil["resolucao"] = f"{vec.Resolution.Width}x{vec.Resolution.Height}"
            
            try:
                req = {'StreamSetup': {'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}}, 'ProfileToken': perfil.token}
                uri = media_service.GetStreamUri(req)
                
                # Injeta o usuário e a senha convertida na URL RTSP devolvida pela câmera
                rtsp_base = uri.Uri.replace("rtsp://", "")
                dados_perfil["rtsp_url"] = f"rtsp://{usuario}:{senha_url}@{rtsp_base}"
                
            except Exception:
                pass
            
            lista_perfis.append(dados_perfil)
            
        return {"sucesso": True, "perfis": lista_perfis}

    except Exception as e:
        return {"sucesso": False, "erro": str(e)}
