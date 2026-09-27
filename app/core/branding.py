import os

from app.core.paths import DATA_DIR


DEFAULT_DEVELOPER_SIGNATURE = "Desenvolvido com Timelapse Manager"


def get_developer_signature() -> str:
    signature = os.getenv("TIMELAPSE_DEVELOPER_SIGNATURE", "").strip()
    if not signature:
        try:
            signature = (DATA_DIR / "developer_signature.txt").read_text(encoding="utf-8").strip()
        except OSError:
            signature = ""
    return " ".join(signature.split())[:120] or DEFAULT_DEVELOPER_SIGNATURE