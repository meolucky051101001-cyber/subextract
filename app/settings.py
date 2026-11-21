from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
SETTINGS_PATH = Path(os.environ.get("SUBEXTRACT_SETTINGS") or BASE_DIR / "data" / "settings.json")
_settings_lock = threading.Lock()


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(updates: dict) -> None:
    # Hold the lock across the read as well as the replacement so independent
    # request fields merge against the latest committed settings.
    with _settings_lock:
        data = load_settings()
        data.update({key: value for key, value in updates.items() if value is not None})
        content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=SETTINGS_PATH.parent,
                prefix=f".{SETTINGS_PATH.name}-", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, SETTINGS_PATH)
            try:
                os.chmod(SETTINGS_PATH, 0o600)
            except OSError:
                pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def get_gemini_keys() -> list[str]:
    """Danh sách key theo thứ tự ưu tiên: env GEMINI_API_KEYS → env GEMINI_API_KEY
    → settings gemini_api_keys → settings gemini_api_key. Dùng để xoay key khi
    key chính hết quota hoặc bị từ chối."""
    keys: list[str] = []
    for part in (os.environ.get("GEMINI_API_KEYS") or "").split(","):
        key = part.strip()
        if key and key not in keys:
            keys.append(key)
    env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if env_key and env_key not in keys:
        keys.append(env_key)
    data = load_settings()
    for key in data.get("gemini_api_keys") or []:
        key = str(key).strip()
        if key and key not in keys:
            keys.append(key)
    single = str(data.get("gemini_api_key") or "").strip()
    if single and single not in keys:
        keys.append(single)
    return keys


def get_gemini_key() -> str:
    keys = get_gemini_keys()
    return keys[0] if keys else ""


def log_usage(entry: dict) -> None:
    """Ghi mỗi lần dịch vào data/usage.jsonl để theo dõi khối lượng Gemini."""
    import time as _time

    try:
        path = BASE_DIR / "data" / "usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": round(_time.time(), 1), **entry}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_usage(days: int = 14) -> list[dict]:
    import time as _time

    path = BASE_DIR / "data" / "usage.jsonl"
    if not path.exists():
        return []
    cutoff = _time.time() - days * 86400
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(record.get("ts") or 0) >= cutoff:
                out.append(record)
    except OSError:
        return []
    return out
