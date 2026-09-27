import os
import tempfile
from pathlib import Path


def _configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default


if os.name == "nt":
    _program_data = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "TimelapseManager"
    DATA_DIR = _configured_path("TIMELAPSE_DATA_DIR", _program_data)
    TEMP_DIR = _configured_path("TIMELAPSE_TEMP_DIR", DATA_DIR / "temp")
else:
    DATA_DIR = _configured_path("TIMELAPSE_DATA_DIR", Path("/var/lib/timelapse"))
    TEMP_DIR = _configured_path("TIMELAPSE_TEMP_DIR", Path("/var/temp"))

SYSTEM_TEMP_DIR = Path(tempfile.gettempdir())
FFMPEG_BINARY = os.getenv("TIMELAPSE_FFMPEG_PATH", "ffmpeg")