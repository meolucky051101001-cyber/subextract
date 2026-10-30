"""Sao lưu SubExtract: job.json của mọi job + settings.json (đã XOÁ key).

Backup nằm ở data/backups/YYYY-MM-DD/ — an toàn để chép đi nơi khác vì không
chứa API key. Chạy thủ công hoặc lập lịch (Task Scheduler) hằng ngày:
    .venv/Scripts/python.exe backup.py
"""
import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DEST = DATA / "backups" / date.today().isoformat()


def backup_settings(dest: Path) -> int:
    src = DATA / "settings.json"
    if not src.exists():
        return 0
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    # Không đưa key vào backup (an toàn khi chia sẻ/đồng bộ).
    data.pop("gemini_api_key", None)
    data.pop("gemini_api_keys", None)
    out = dest / "settings.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1


def backup_jobs(dest: Path) -> int:
    count = 0
    for job_dir in (DATA / "jobs").glob("*/job.json"):
        out_dir = dest / "jobs" / job_dir.parent.name
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(job_dir, out_dir / "job.json")
        count += 1
    return count


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    n_settings = backup_settings(DEST)
    n_jobs = backup_jobs(DEST)
    print(f"Backup xong → {DEST} (settings: {n_settings}, jobs: {n_jobs})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
