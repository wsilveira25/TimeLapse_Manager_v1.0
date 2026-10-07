import os
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.paths import TEMP_DIR
from app.models.storage import LocalStorageLocation


DEFAULT_LOCATION_ID = 0
MIN_CAPTURE_FREE_RATIO = 0.05


def listar_locais_armazenamento(db: Session) -> list[dict]:
    default_path = TEMP_DIR.resolve()
    locations = [{
        "id": DEFAULT_LOCATION_ID,
        "nome": "Principal",
        "caminho": str(default_path),
        "ativo": True,
        "padrao": True,
    }]
    configured = db.query(LocalStorageLocation).order_by(LocalStorageLocation.id).all()
    for location in configured:
        path = Path(location.caminho).expanduser().resolve()
        if path == default_path:
            continue
        locations.append({
            "id": location.id,
            "nome": location.nome,
            "caminho": str(path),
            "ativo": bool(location.ativo),
            "padrao": False,
        })
    return locations


def obter_locais_ativos(db: Session) -> list[dict]:
    return [
        location
        for location in listar_locais_armazenamento(db)
        if location["ativo"] and (location["padrao"] or os.path.ismount(location["caminho"]))
    ]


def listar_raizes_ativas() -> list[Path]:
    with SessionLocal() as db:
        roots = [Path(location["caminho"]) for location in obter_locais_ativos(db)]
    return sorted(roots, key=lambda path: len(path.parts), reverse=True)


def resolver_caminho_local(local_path: str, db: Session) -> Path | None:
    if local_path.startswith("local-"):
        location_key, separator, relative_path = local_path.partition("/")
        if not separator:
            return None
        try:
            location_id = int(location_key.removeprefix("local-"))
        except ValueError:
            return None
        location = db.query(LocalStorageLocation).filter_by(id=location_id).first()
        if not location:
            return None
        root = Path(location.caminho).expanduser().resolve()
        if not os.path.ismount(root):
            return None
        path = (root / relative_path).resolve()
        return path if root in path.parents else None

    root = TEMP_DIR.resolve()
    path = (root / local_path).resolve()
    return path if root in path.parents else None


def identificar_caminho_local(path: Path, db: Session) -> str:
    resolved = path.resolve()
    locations = sorted(listar_locais_armazenamento(db), key=lambda location: len(location["caminho"]), reverse=True)
    for location in locations:
        root = Path(location["caminho"])
        try:
            relative_path = resolved.relative_to(root).as_posix()
        except ValueError:
            continue
        if location["id"] == DEFAULT_LOCATION_ID:
            return relative_path
        return f"local-{location['id']}/{relative_path}"
    raise ValueError(f"O arquivo está fora dos destinos locais configurados: {resolved}")


def obter_local_por_id(location_id: int, db: Session) -> dict | None:
    if location_id == DEFAULT_LOCATION_ID:
        return {
            "id": DEFAULT_LOCATION_ID,
            "nome": "Principal",
            "caminho": str(TEMP_DIR.resolve()),
            "ativo": True,
            "padrao": True,
        }
    location = db.query(LocalStorageLocation).filter_by(id=location_id).first()
    if not location:
        return None
    resolved_path = Path(location.caminho).expanduser().resolve()
    if not os.path.ismount(resolved_path):
        return None
    return {
        "id": location.id,
        "nome": location.nome,
        "caminho": str(resolved_path),
        "ativo": bool(location.ativo),
        "padrao": False,
    }


def escolher_local_de_captura() -> Path | None:
    with SessionLocal() as db:
        candidates = []
        for location in obter_locais_ativos(db):
            path = Path(location["caminho"])
            try:
                if not location["padrao"] and not os.path.ismount(path):
                    continue
                path.mkdir(parents=True, exist_ok=True)
                if not os.access(path, os.W_OK | os.X_OK):
                    continue
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            free_ratio = usage.free / usage.total if usage.total else 0
            if free_ratio > MIN_CAPTURE_FREE_RATIO:
                candidates.append((usage.free, path))
        return max(candidates, default=(0, None), key=lambda candidate: candidate[0])[1]


def somar_uso_locais_armazenamento(db: Session | None = None) -> tuple[int, int, int]:
    def somar(locations: list[dict]) -> tuple[int, int, int]:
        usages = []
        seen_devices = set()
        for location in locations:
            path = Path(location["caminho"])
            try:
                usage = shutil.disk_usage(path)
                device = path.stat().st_dev
            except OSError:
                continue
            if device in seen_devices:
                continue
            seen_devices.add(device)
            usages.append(usage)
        return (
            sum(usage.total for usage in usages),
            sum(usage.used for usage in usages),
            sum(usage.free for usage in usages),
        )

    if db is not None:
        return somar(obter_locais_ativos(db))
    with SessionLocal() as session:
        return somar(obter_locais_ativos(session))