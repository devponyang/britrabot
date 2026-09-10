"""Small, atomic JSON stores. A damaged store fails closed instead of losing data."""
import json
import logging
import os
from pathlib import Path
import tempfile

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def load_json(path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value
    except (ValueError, OSError):
        logger.exception("설정 파일 읽기 실패: %s (원본 보존)", path.name)
        raise


def save_json(path, data: dict):
    path = Path(path)
    # Serialize before touching the existing file.
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as file:
            name = file.name
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
