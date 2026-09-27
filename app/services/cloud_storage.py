import os
import posixpath
import time
import mimetypes
from urllib.parse import quote, urlencode

import requests


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_API_URL = "https://www.googleapis.com/drive/v3"
GOOGLE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"
MICROSOFT_AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MICROSOFT_GRAPH_URL = "https://graph.microsoft.com/v1.0"
CHUNK_SIZE = 10 * 1024 * 1024


def _google_error_message(response):
    try:
        error = response.json().get("error", {})
    except (AttributeError, ValueError):
        error = {}
    reasons = [item.get("reason") for item in error.get("errors", []) if item.get("reason")]
    message = error.get("message") or response.reason
    reason = f" ({', '.join(reasons)})" if reasons else ""
    return f"Google Drive API HTTP {response.status_code}{reason}: {message}"


def _raise_for_google_status(response):
    if not response.ok:
        raise RuntimeError(_google_error_message(response))


def _provider_config(tipo):
    if tipo == "google_drive":
        return {
            "authorization_url": GOOGLE_AUTH_URL,
            "token_url": GOOGLE_TOKEN_URL,
            "scopes": "openid email https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive.metadata.readonly",
        }
    if tipo == "onedrive":
        return {
            "authorization_url": MICROSOFT_AUTH_URL.format(tenant="common"),
            "token_url": MICROSOFT_TOKEN_URL.format(tenant="common"),
            "scopes": "offline_access User.Read Files.ReadWrite",
        }
    raise ValueError("Provedor OAuth não suportado.")


def build_authorization_url(tipo, client_id, redirect_uri, state):
    provider = _provider_config(tipo)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider["scopes"],
        "state": state,
    }
    if tipo == "google_drive":
        params.update({"access_type": "offline", "prompt": "consent"})
    else:
        params["response_mode"] = "query"
    return f"{provider['authorization_url']}?{urlencode(params)}"


def exchange_authorization_code(tipo, client_id, client_secret, code, redirect_uri):
    provider = _provider_config(tipo)
    response = requests.post(
        provider["token_url"],
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _refresh_access_token(config):
    tipo = config.tipo.lower()
    provider = _provider_config(tipo)
    response = requests.post(
        provider["token_url"],
        data={
            "client_id": config.oauth_client_id,
            "client_secret": config.oauth_client_secret,
            "refresh_token": config.oauth_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    response.raise_for_status()
    token_data = response.json()
    config.oauth_access_token = token_data["access_token"]
    config.oauth_token_expires_at = int(time.time()) + int(token_data.get("expires_in", 3600))
    if token_data.get("refresh_token"):
        config.oauth_refresh_token = token_data["refresh_token"]
    return config.oauth_access_token


def get_access_token(config):
    if not config.oauth_refresh_token:
        raise RuntimeError("A conta ainda não foi conectada por OAuth.")
    if not config.oauth_access_token or int(config.oauth_token_expires_at or 0) <= int(time.time()) + 60:
        return _refresh_access_token(config)
    return config.oauth_access_token


def get_storage_quota(config):
    token = get_access_token(config)
    headers = {"Authorization": f"Bearer {token}"}
    if config.tipo.lower() == "google_drive":
        response = requests.get(
            f"{GOOGLE_API_URL}/about",
            params={"fields": "storageQuota(limit,usage)"},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        quota = response.json().get("storageQuota", {})
        total = int(quota.get("limit") or 0)
        used = int(quota.get("usage") or 0)
    elif config.tipo.lower() == "onedrive":
        response = requests.get(
            f"{MICROSOFT_GRAPH_URL}/me/drive",
            params={"$select": "quota"},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        quota = response.json().get("quota", {})
        total = int(quota.get("total") or 0)
        used = int(quota.get("used") or 0)
    else:
        raise ValueError("Provedor cloud não suportado.")

    config.oauth_access_token = token
    config.total_bytes = total
    config.usado_bytes = used
    config.livre_bytes = max(total - used, 0)
    config.percentagem_uso = round((used / total) * 100, 1) if total else 0
    return {"total_bytes": total, "usado_bytes": used}


def _find_or_create_google_folder(name, parent_id, token):
    escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
    response = requests.get(
        f"{GOOGLE_API_URL}/files",
        params={
            "q": f"name = '{escaped_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            "fields": "files(id,name)",
            "pageSize": 10,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    _raise_for_google_status(response)
    folders = response.json().get("files", [])
    if folders:
        return folders[0]["id"]

    response = requests.post(
        f"{GOOGLE_API_URL}/files",
        params={"fields": "id,name"},
        json={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    _raise_for_google_status(response)
    return response.json()["id"]


def _google_folder_for_remote_path(config, remote_path, token):
    parent_id = "root"
    base_path = (config.caminho_remoto or "").strip("/")
    folder_path = posixpath.join(base_path, posixpath.dirname(remote_path or ""))
    folder_cache = getattr(config, "_google_folder_id_cache", None)
    if folder_cache is None:
        folder_cache = {}
        config._google_folder_id_cache = folder_cache

    for folder in folder_path.split("/"):
        if folder:
            cache_key = (parent_id, folder)
            if cache_key not in folder_cache:
                folder_cache[cache_key] = _find_or_create_google_folder(folder, parent_id, token)
            parent_id = folder_cache[cache_key]
    return parent_id


def _upload_google_drive(path, config, token, remote_path=None):
    filename = os.path.basename(path)
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    metadata = {"name": filename, "mimeType": content_type}
    folder_id = _google_folder_for_remote_path(config, remote_path, token) if remote_path else (config.caminho_remoto or "").strip().strip("/")
    if folder_id:
        parent_query = f"'{folder_id}' in parents"
    else:
        parent_query = "'root' in parents"
    escaped_name = filename.replace("\\", "\\\\").replace("'", "\\'")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": content_type,
        "X-Upload-Content-Length": str(os.path.getsize(path)),
    }
    response = requests.get(
        f"{GOOGLE_API_URL}/files",
        params={
            "q": f"name = '{escaped_name}' and {parent_query} and trashed = false",
            "fields": "files(id,name,size)",
            "pageSize": 10,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    _raise_for_google_status(response)
    existing = response.json().get("files", [])
    file_id = existing[0]["id"] if existing else None
    if folder_id and not file_id:
        metadata["parents"] = [folder_id]
    session_url = f"{GOOGLE_UPLOAD_URL}/files/{file_id}" if file_id else f"{GOOGLE_UPLOAD_URL}/files"
    create_session = requests.patch if file_id else requests.post
    response = create_session(
        session_url,
        params={"uploadType": "resumable", "fields": "id,size"},
        json=metadata,
        headers=headers,
        timeout=30,
    )
    _raise_for_google_status(response)
    upload_url = response.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Google Drive não retornou a URL de upload.")

    size = os.path.getsize(path)
    with open(path, "rb") as source:
        offset = 0
        result = None
        while offset < size:
            chunk = source.read(CHUNK_SIZE)
            end = offset + len(chunk) - 1
            attempt = 0
            while True:
                response = requests.put(
                    upload_url,
                    data=chunk,
                    headers={"Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{size}"},
                    timeout=180,
                )
                if response.status_code == 403:
                    try:
                        error = response.json().get("error", {})
                        reasons = {item.get("reason") for item in error.get("errors", [])}
                    except (AttributeError, ValueError):
                        reasons = set()
                    if reasons & {"rateLimitExceeded", "userRateLimitExceeded"} and attempt < 5:
                        time.sleep(min(2**attempt, 30))
                        attempt += 1
                        continue
                    _raise_for_google_status(response)
                if response.status_code not in (200, 201, 308):
                    _raise_for_google_status(response)
                break
            offset = end + 1
            if response.status_code in (200, 201):
                result = response.json()
    if not result or int(result.get("size", -1)) != size:
        raise RuntimeError("A validação do tamanho enviado ao Google Drive falhou.")
    return result


def _ensure_onedrive_folders(config, remote_path, token):
    base_path = (config.caminho_remoto or "").strip("/")
    backup_folders = posixpath.dirname(remote_path or "")
    folder_parts = [part for part in posixpath.join(base_path, backup_folders).split("/") if part]
    current_path = ""
    for folder in folder_parts:
        current_path = posixpath.join(current_path, folder)
        escaped_path = quote(current_path, safe="/")
        check_url = f"{MICROSOFT_GRAPH_URL}/me/drive/root:/{escaped_path}"
        check = requests.get(
            check_url,
            params={"$select": "id,name,folder"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if check.status_code == 200:
            continue
        if check.status_code != 404:
            check.raise_for_status()

        parent_path = posixpath.dirname(current_path)
        parent_url = f"{MICROSOFT_GRAPH_URL}/me/drive/root:/{quote(parent_path, safe='/')}:/children" if parent_path else f"{MICROSOFT_GRAPH_URL}/me/drive/root/children"
        create = requests.post(
            parent_url,
            json={"name": folder, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if create.status_code == 409:
            continue
        create.raise_for_status()
    return posixpath.join(base_path, remote_path or "")


def _upload_onedrive(path, config, token, remote_path=None):
    filename = quote(os.path.basename(path), safe="")
    if remote_path:
        destination = _ensure_onedrive_folders(config, remote_path, token)
        item_path = quote(posixpath.join(destination, os.path.basename(remote_path)), safe="/")
    else:
        folder = quote((config.caminho_remoto or "").strip("/"), safe="/")
        item_path = f"{folder}/{filename}" if folder else filename
    url = f"{MICROSOFT_GRAPH_URL}/me/drive/root:/{item_path}:/createUploadSession"
    response = requests.post(
        url,
        json={"item": {"@microsoft.graph.conflictBehavior": "replace", "name": os.path.basename(path)}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    upload_url = response.json()["uploadUrl"]

    size = os.path.getsize(path)
    with open(path, "rb") as source:
        offset = 0
        result = None
        while offset < size:
            chunk = source.read(CHUNK_SIZE)
            end = offset + len(chunk) - 1
            response = requests.put(
                upload_url,
                data=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                },
                timeout=180,
            )
            if response.status_code not in (200, 201, 202):
                response.raise_for_status()
            offset = end + 1
            if response.status_code in (200, 201):
                result = response.json()
    if not result or int(result.get("size", -1)) != size:
        raise RuntimeError("A validação do tamanho enviado ao OneDrive falhou.")
    return result


def upload_cloud_file(path, config, remote_path=None):
    token = get_access_token(config)
    if config.tipo.lower() == "google_drive":
        return _upload_google_drive(path, config, token, remote_path)
    if config.tipo.lower() == "onedrive":
        return _upload_onedrive(path, config, token, remote_path)
    raise ValueError("Provedor cloud não suportado.")