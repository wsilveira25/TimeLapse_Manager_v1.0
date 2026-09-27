import os
import ftplib
import requests
from requests.auth import HTTPBasicAuth
from smb.SMBConnection import SMBConnection
import shutil
import time
import uuid
from app.services.cloud_storage import get_storage_quota, upload_cloud_file
from smbprotocol.connection import Connection
from smbprotocol.file_info import FileFsFullSizeInformation
from smbprotocol.open import (
    CreateDisposition,
    CreateOptions,
    FileAttributes,
    FilePipePrinterAccessMask,
    ImpersonationLevel,
    Open,
    SMB2QueryInfoRequest,
    SMB2QueryInfoResponse,
    ShareAccess,
)
from smbprotocol.session import Session
from smbprotocol.tree import TreeConnect
from app.core.paths import TEMP_DIR
# Pastas locais que vamos monitorizar
PASTA_TIMELAPSES = str(TEMP_DIR / "timelapses")
PASTA_SINCRONIZADOS = str(TEMP_DIR / "Sincronizados")
PASTA_VIDEOS = str(TEMP_DIR / "Videos")
ROOT_TEMP = str(TEMP_DIR)


def listar_shares_smb(config):
    smb_connection = SMBConnection(
        config.usuario,
        config.senha,
        "timelapse_linux",
        "truenas_server",
        use_ntlm_v2=True,
    )
    try:
        if not smb_connection.connect(config.host, 445, timeout=5):
            return []
        return [share.name for share in smb_connection.listShares() if share.type == 0]
    finally:
        try:
            smb_connection.close()
        except Exception:
            pass


def obter_espaco_smb(config):
    share_name = (config.caminho_remoto or "").strip("/\\")
    if not share_name:
        raise ValueError("Informe o nome da share SMB no campo Caminho Remoto.")

    connection = Connection(uuid.uuid4(), config.host, 445)
    session = None
    tree = None
    root = None
    try:
        connection.connect(timeout=5)
        session = Session(connection, username=config.usuario, password=config.senha)
        session.connect()
        tree = TreeConnect(session, f"\\\\{config.host}\\{share_name}")
        tree.connect()
        root = Open(tree, "")
        root.create(
            impersonation_level=ImpersonationLevel.Impersonation,
            desired_access=FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
            file_attributes=FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
            share_access=ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE,
            create_disposition=CreateDisposition.FILE_OPEN,
            create_options=CreateOptions.FILE_DIRECTORY_FILE,
        )

        query = SMB2QueryInfoRequest()
        query["info_type"] = FileFsFullSizeInformation.INFO_TYPE
        query["file_info_class"] = FileFsFullSizeInformation.INFO_CLASS
        query["output_buffer_length"] = 1024
        query["file_id"] = root.file_id
        request = connection.send(query, session.session_id, tree.tree_connect_id)
        response = connection.receive(request)
        query_response = SMB2QueryInfoResponse()
        query_response.unpack(response["data"].get_value())
        filesystem = FileFsFullSizeInformation()
        filesystem.unpack(query_response["buffer"].get_value())

        unit_size = int(filesystem["sectors_per_unit"].get_value()) * int(filesystem["bytes_per_sector"].get_value())
        total = int(filesystem["total_allocation_units"].get_value()) * unit_size
        free = int(filesystem["caller_available_units"].get_value()) * unit_size
        free = min(max(free, 0), total)
        return {"total_bytes": total, "livre_bytes": free, "usado_bytes": total - free}
    except Exception as exc:
        if "STATUS_BAD_NETWORK_NAME" in str(exc):
            try:
                shares = listar_shares_smb(config)
            except Exception:
                shares = []
            mensagem = f"A share SMB '{share_name}' não foi encontrada."
            if shares:
                mensagem += f" O servidor anuncia: {', '.join(shares)}."
            mensagem += " Informe o nome publicado no servidor, sem barras, em Caminho Remoto."
            raise ValueError(mensagem) from exc
        raise
    finally:
        if root is not None:
            try:
                root.close()
            except Exception:
                pass
        if tree is not None:
            try:
                tree.disconnect()
            except Exception:
                pass
        if session is not None:
            try:
                session.disconnect()
            except Exception:
                pass
        try:
            connection.disconnect()
        except Exception:
            pass

def upload_webdav_nextcloud(caminho_local, config):
    """
    Sincroniza com Nextcloud via WebDAV.
    Exemplo de host: https://nuvem.cliente.com/remote.php/webdav/timelapses
    """
    nome_arquivo = os.path.basename(caminho_local)
    url_destino = f"{config.host.rstrip('/')}/{nome_arquivo}"
    tamanho_local = os.path.getsize(caminho_local)
    
    try:
        # 1. Faz o Upload (PUT)
        with open(caminho_local, 'rb') as f:
            resposta_upload = requests.put(
                url_destino, 
                data=f, 
                auth=HTTPBasicAuth(config.usuario, config.senha),
                timeout=120 # Timeout longo para arquivos grandes
            )
            
        if resposta_upload.status_code not in (200, 201, 204):
            return {"sucesso": False, "erro": f"Upload WebDAV falhou. Status: {resposta_upload.status_code}"}

        # 2. O DE-PARA: Consulta o tamanho do ficheiro remoto (HEAD)
        resposta_head = requests.head(
            url_destino, 
            auth=HTTPBasicAuth(config.usuario, config.senha)
        )
        
        tamanho_remoto = int(resposta_head.headers.get('Content-Length', 0))
        
        # 3. Validação Crucial
        if tamanho_remoto == tamanho_local:
            return {"sucesso": True, "protocolo": "WebDAV", "arquivo": nome_arquivo}
        else:
            return {"sucesso": False, "erro": f"Tamanho divergente (Local: {tamanho_local}, Remoto: {tamanho_remoto})"}
            
    except Exception as e:
        return {"sucesso": False, "erro": str(e)}


def upload_ftp_truenas(caminho_local, config):
    """
    Sincroniza com TrueNAS via FTP.
    Exemplo de host: 192.168.0.50 (apenas o IP)
    """
    nome_arquivo = os.path.basename(caminho_local)
    tamanho_local = os.path.getsize(caminho_local)
    
    try:
        # 1. Conecta ao TrueNAS
        ftp = ftplib.FTP(config.host)
        ftp.login(user=config.usuario, passwd=config.senha)
        
        # Navega para a pasta de destino (se configurada)
        if config.caminho_remoto and config.caminho_remoto != '/':
            ftp.cwd(config.caminho_remoto)
            
        # 2. Faz o Upload
        with open(caminho_local, 'rb') as f:
            ftp.storbinary(f'STOR {nome_arquivo}', f)
            
        # 3. O DE-PARA: Consulta o tamanho no TrueNAS
        tamanho_remoto = ftp.size(nome_arquivo)
        ftp.quit()
        
        # 4. Validação Crucial
        if tamanho_remoto == tamanho_local:
            return {"sucesso": True, "protocolo": "FTP", "arquivo": nome_arquivo}
        else:
            return {"sucesso": False, "erro": f"Tamanho divergente (Local: {tamanho_local}, Remoto: {tamanho_remoto})"}
            
    except Exception as e:
        return {"sucesso": False, "erro": str(e)}

def upload_smb_truenas(caminho_local, config):
    nome_arquivo = os.path.basename(caminho_local)
    nome_temporario = f"{nome_arquivo}.uploading"
    tamanho_local = os.path.getsize(caminho_local)
    nome_partilha = config.caminho_remoto.strip('/')

    try:
        conn = SMBConnection(
            config.usuario, config.senha, "timelapse_linux", "truenas_server", use_ntlm_v2=True
        )
        conectado = conn.connect(config.host, 445)

        if not conectado:
            return {"sucesso": False, "erro": "Falha na autenticação SMB."}

        with open(caminho_local, 'rb') as f:
            conn.storeFile(nome_partilha, f'/{nome_temporario}', f)

        atributos = conn.getAttributes(nome_partilha, f'/{nome_temporario}')
        tamanho_remoto = atributos.file_size

        if tamanho_remoto == tamanho_local:
            conn.rename(nome_partilha, f'/{nome_temporario}', f'/{nome_arquivo}')
            conn.close()
            return {"sucesso": True, "protocolo": "SMB", "arquivo": nome_arquivo}
        conn.close()
        return {"sucesso": False, "erro": "Tamanho divergente."}

    except Exception as e:
        return {"sucesso": False, "erro": str(e)}

def iniciar_sincronizacao_pendentes(config_ativa):
    arquivos_sincronizados = []
    erros = []
    avisos = []
    configuracoes = config_ativa if isinstance(config_ativa, (list, tuple)) else [config_ativa]
    configuracoes = [config for config in configuracoes if config and config.ativo]

    if not configuracoes:
        return {"sucesso": False, "enviados": [], "erros": [{"storage": "Nenhum destino ativo."}]}

    if not os.path.exists(PASTA_VIDEOS):
        return {"sucesso": True, "enviados": [], "erros": [], "destinos": [c.tipo for c in configuracoes]}

    for root_dir, _, files in os.walk(PASTA_VIDEOS):
        for file in files:
            if file.endswith(('.mp4', '.mkv', '.avi')):
                caminho_completo = os.path.join(root_dir, file)
                erros_arquivo = []

                for config in configuracoes:
                    tipo = config.tipo.lower()
                    try:
                        if tipo == 'webdav':
                            resultado = upload_webdav_nextcloud(caminho_completo, config)
                        elif tipo == 'ftp':
                            resultado = upload_ftp_truenas(caminho_completo, config)
                        elif tipo == 'smb':
                            resultado = upload_smb_truenas(caminho_completo, config)
                        elif tipo in {'google_drive', 'onedrive'}:
                            metadata = upload_cloud_file(caminho_completo, config)
                            resultado = {
                                "sucesso": int(metadata.get("size", -1)) == os.path.getsize(caminho_completo),
                                "erro": "Tamanho divergente após upload cloud.",
                            }
                        else:
                            resultado = {"sucesso": False, "erro": "Protocolo desconhecido."}
                    except Exception as exc:
                        resultado = {"sucesso": False, "erro": str(exc)}

                    if not resultado["sucesso"]:
                        erros_arquivo.append(f"{tipo}: {resultado.get('erro', 'falha no envio')}")

                if erros_arquivo:
                    erros.append({file: erros_arquivo})
                    continue

                caminho_relativo = os.path.relpath(caminho_completo, PASTA_VIDEOS)
                caminho_quarentena = os.path.join(PASTA_SINCRONIZADOS, caminho_relativo)
                os.makedirs(os.path.dirname(caminho_quarentena), exist_ok=True)
                try:
                    shutil.move(caminho_completo, caminho_quarentena)
                    arquivos_sincronizados.append(file)
                except Exception as exc:
                    erros.append({file: [f"upload confirmado, mas falhou mover para quarentena: {exc}"]})

    for config in configuracoes:
        if config.tipo.lower() in {'google_drive', 'onedrive'} and config.oauth_refresh_token:
            try:
                get_storage_quota(config)
                config.storage_quota_checked_at = int(time.time())
            except Exception as exc:
                avisos.append({config.tipo: f"Upload concluído; não foi possível atualizar a quota: {exc}"})

    return {
        "sucesso": not erros,
        "enviados": arquivos_sincronizados,
        "erros": erros,
        "avisos": avisos,
        "destinos": [config.tipo for config in configuracoes],
    }


def limpar_quarentena_48h():
    if not os.path.exists(PASTA_SINCRONIZADOS):
        return

    agora = time.time()
    limite_tempo = 48 * 3600

    for root_dir, _, files in os.walk(PASTA_SINCRONIZADOS):
        for arquivo in files:
            caminho = os.path.join(root_dir, arquivo)
            idade = agora - os.path.getmtime(caminho)
            if idade > limite_tempo:
                try:
                    os.remove(caminho)
                except Exception:
                    pass