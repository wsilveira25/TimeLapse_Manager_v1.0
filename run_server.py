import os
import shutil
import argparse
import sys
from pathlib import Path

from app.core.paths import DATA_DIR, FFMPEG_BINARY, TEMP_DIR


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--create-certificate":
        parser = argparse.ArgumentParser()
        parser.add_argument("--create-certificate", action="store_true")
        parser.add_argument("--hostname", required=True)
        parser.add_argument("--cert", required=True, type=Path)
        parser.add_argument("--key", required=True, type=Path)
        options = parser.parse_args()
        from scripts.create_self_signed_cert import create_certificate

        create_certificate(options.hostname, options.cert, options.key)
        return

    certificate = os.getenv("TIMELAPSE_SSL_CERTFILE", "")
    private_key = os.getenv("TIMELAPSE_SSL_KEYFILE", "")
    if not certificate or not private_key:
        raise SystemExit("Configure TIMELAPSE_SSL_CERTFILE e TIMELAPSE_SSL_KEYFILE para iniciar com HTTPS.")
    if not Path(certificate).is_file() or not Path(private_key).is_file():
        raise SystemExit("O certificado ou a chave TLS configurados não existem.")
    if not shutil.which(FFMPEG_BINARY):
        raise SystemExit(f"FFmpeg não foi encontrado no PATH: {FFMPEG_BINARY}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("TIMELAPSE_HOST", "0.0.0.0"),
        port=int(os.getenv("TIMELAPSE_PORT", "8000")),
        ssl_certfile=certificate,
        ssl_keyfile=private_key,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()