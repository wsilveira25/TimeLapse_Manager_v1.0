import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import Request

OAUTH_CALLBACK_PATH = "/api/system/storage/oauth/callback"


def obter_oauth_redirect_uri(request: Request) -> str:
    configured_uri = os.getenv("TIMELAPSE_OAUTH_REDIRECT_URI", "").strip()
    if configured_uri:
        return configured_uri
    return str(request.url_for("storage_oauth_callback"))


def validar_oauth_redirect_uri(uri: str) -> None:
    parsed = urlsplit(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Configure um callback OAuth como URL absoluta HTTPS.")
    if parsed.path != OAUTH_CALLBACK_PATH or parsed.query or parsed.fragment:
        raise ValueError(f"O callback OAuth precisa terminar exatamente em {OAUTH_CALLBACK_PATH}.")

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None

    is_loopback = hostname == "localhost" or (address is not None and address.is_loopback)
    if address is not None and not is_loopback:
        raise ValueError("O Google OAuth não aceita IP público/privado no callback. Configure um domínio DNS.")
    if parsed.scheme != "https" and not is_loopback:
        raise ValueError("O callback OAuth precisa usar HTTPS com certificado válido.")
