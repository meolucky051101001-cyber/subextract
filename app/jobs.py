from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger(__name__)
_JOB_STATUSES = {"uploaded", "running", "done", "error", "cancelled"}
_PREVIEW_STATUSES = {"none", "running", "ready", "error"}


def _finite_number(value, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")


def _validate_finite_values(value) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("metadata contains a nonfinite number")
    if isinstance(value, dict):
        for item in value.values():
            _validate_finite_values(item)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_values(item)


def _validate_metadata(data) -> None:
    if not isinstance(data, dict):
        raise ValueError("metadata must be an object")
    _validate_finite_values(data)
    for name in ("id", "filename", "status", "error", "message", "preview_status"):
        if name in data and not isinstance(data[name], str):
            raise ValueError(f"{name} must be a string")
    if "id" in data and not data["id"]:
        raise ValueError("id must not be empty")
    if data.get("status", "uploaded") not in _JOB_STATUSES:
        raise ValueError("status is not recognized")
    if data.get("preview_status", "none") not in _PREVIEW_STATUSES:
        raise ValueError("preview_status is not recognized")
    for name in ("info", "params"):
        if data.get(name) is not None and not isinstance(data[name], dict):
            raise ValueError(f"{name} must be an object or null")
    if not isinstance(data.get("stats", {}), dict):
        raise ValueError("stats must be an object")
    if "created_at" in data:
        _finite_number(data["created_at"], "created_at")
    info = data.get("info") or {}
    for name in ("duration", "width", "height", "fps"):
        if name in info:
            _finite_number(info[name], f"info.{name}")
    cues = data.get("cues", [])
    if not isinstance(cues, list):
        raise ValueError("cues must be an array")
    for cue in cues:
        if not isinstance(cue, dict):
            raise ValueError("each cue must be an object")
        for name in ("start", "end"):
            _finite_number(cue.get(name), f"cue.{name}")
        if not isinstance(cue.get("text"), str):
            raise ValueError("cue.text must be a string")
        if "trans" in cue and not isinstance(cue["trans"], str):
            raise ValueError("cue.trans must be a string")
        if cue.get("id") is not None and not isinstance(cue["id"], str):
            raise ValueError("cue.id must be a string or null")
        if cue.get("region") is not None and type(cue["region"]) is not int:
            raise ValueError("cue.region must be an integer or null")


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
    preview_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    created_at: float = field(default_factory=time.time)
    _save_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def preview_path(self) -> Path:
        return self.work_dir / "preview.mp4"

    @property
    def preview_ready_path(self) -> Path:
        return self.work_dir / ".preview-ready"

    @property
    def preview_ready(self) -> bool:
        return self.preview_path.is_file() and self.preview_ready_path.is_file()

    def _preview_state(self) -> str:
        if self.preview_ready:
            return "ready"
        return "none" if self.preview_status == "ready" else self.preview_status

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
            "preview_status": self._preview_state(),
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
            "preview_status": self._preview_state(),
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
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
                _validate_metadata(data)
                work_dir = meta_file.parent
                videos = sorted(path for path in work_dir.glob("source.*") if path.is_file())
                if not videos:
                    raise ValueError("source video is missing")
                job = Job(
                    id=data.get("id", work_dir.name),
                    work_dir=work_dir,
                    filename=data.get("filename") or videos[0].name,
                    video_path=videos[0],
                    info=data.get("info"),
                    status=data.get("status", "uploaded"),
                    cues=data.get("cues", []),
                    stats=data.get("stats", {}),
                    params=data.get("params"),
                    error=data.get("error", ""),
                    message=data.get("message", ""),
                    created_at=float(data.get("created_at", time.time())),
                    preview_status=data.get("preview_status", "none"),
                )
                if job.id in self._jobs:
                    raise ValueError("duplicate job id")
                # An interrupted encoder cannot resume after a process restart.
                if job.preview_status == "running":
                    job.preview_status = "none"
                job.preview_status = job._preview_state()
            except Exception as exc:
                logger.warning("Skipping job metadata %s: %s", meta_file, exc)
                continue
            if job.status == "running":
                job.status = "error"
                job.error = (
                    "Server đã dừng khi job đang chạy nên tiến trình bị gián đoạn. "
                    "Bấm Bắt đầu để chạy lại."
                )
                job.message = job.error
                try:
                    job.save()
                except Exception as exc:
                    logger.warning("Could not persist recovered job %s: %s", meta_file, exc)
            self._jobs[job.id] = job

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)
