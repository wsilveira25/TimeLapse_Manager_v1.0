from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
OUTPUT = ROOT / "dist" / f"timelapse-manager-{VERSION}.zip"
EXCLUDED_PARTS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "tests", "credentials", "certs", "secrets"}
EXCLUDED_NAMES = {"windows-config.json", ".env", ".env.local", ".env.production", "test_capture.py", "test_onvif.py"}


def should_include(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in EXCLUDED_PARTS for part in relative.parts) or path.name in EXCLUDED_NAMES:
        return False
    if path.suffix.lower() in {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".crt", ".p12", ".pfx"}:
        return False
    return not path.name.lower().endswith((".db-shm", ".db-wal"))


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(OUTPUT, "w", ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file() and should_include(path):
                archive.write(path, Path("TimelapseManager") / path.relative_to(ROOT))
    print(f"Pacote criado: {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()