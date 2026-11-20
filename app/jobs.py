from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Job:
    id: str
    work_dir: Path
    filename: str
    video_path: Path
    info: dict | None = None
    status: str = "uploaded"  # uploaded | running | done | error | cancelled
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    cues: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    error: str = ""
    params: dict | None = None
    preview_status: str = "none"  # none | running | ready | error
    cancel_event: threading.Event = field(default_factory=threading.Event)
    run_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    created_at: float = field(default_factory=time.time)
    _save_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def preview_path(self) -> Path:
        return self.work_dir / "preview.mp4"

    def snapshot(self, include_cues: bool = False) -> dict:
        data = {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "message": self.message,
            "error": self.error,
            "info": self.info,
            "stats": self.stats,
            "params": self.params,
            "preview_status": self.preview_status,
            "cue_count": len(self.cues),
            "created_at": self.created_at,
        }
        if include_cues:
            data["cues"] = self.cues
        return data

    def save(self) -> None:
        with self._save_lock:
            self._save_atomic()

    def _save_atomic(self) -> None:
        payload = {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "cues": self.cues,
            "stats": self.stats,
            "info": self.info,
            "params": self.params,
            "error": self.error,
            "message": self.message,
            "created_at": self.created_at,
            "preview_status": "ready" if self.preview_path.exists() else self.preview_status,
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.work_dir,
                prefix=".job-", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.work_dir / "job.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        for meta_file in self.root.glob("*/job.json"):
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            work_dir = meta_file.parent
            videos = list(work_dir.glob("source.*"))
            if not videos:
                continue
            stored_status = str(data.get("status") or "")
            job = Job(
                id=data.get("id") or work_dir.name,
                work_dir=work_dir,
                filename=data.get("filename") or videos[0].name,
                video_path=videos[0],
                info=data.get("info"),
                status="done" if stored_status == "done" else "uploaded",
                cues=data.get("cues") or [],
                stats=data.get("stats") or {},
                params=data.get("params"),
                message=str(data.get("message") or ""),
                created_at=float(data.get("created_at") or time.time()),
            )
            if stored_status == "running":
                job.status = "error"
                job.error = (
                    "Server đã dừng khi job đang chạy nên tiến trình bị gián đoạn. "
                    "Bấm Bắt đầu để chạy lại."
                )
                job.message = job.error
                job.save()
            if job.preview_path.exists():
                job.preview_status = "ready"
            self._jobs[job.id] = job

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)
