from __future__ import annotations

import asyncio
import json
import mimetypes
import math
import os
import shutil
import tempfile
import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, FiniteFloat

from . import handoff
from .jobs import Job, JobStore
from .pipeline.dub import DEFAULT_OUTPUT_DIR, DEFAULT_VOICE, DEFAULT_VOICE_LABEL
from .pipeline.ffmpeg_io import FFmpegError, probe_video
from .pipeline.post import to_srt, to_vtt
from .pipeline.runner import Cancelled, RunParams, run_pipeline
from .pipeline.translate import (
    ALLOWED_MODELS,
    DEFAULT_MODEL,
    MODEL_LABELS,
    check_key,
    normalize_model,
    probe_model,
    translate_cues,
)
from .settings import get_gemini_keys, load_settings, read_usage, save_settings

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = Path(os.environ.get("SUBEXTRACT_DATA_DIR") or BASE_DIR / "data" / "jobs")

store = JobStore(DATA_DIR)


_prev_loop_exception_handler = None


def _quiet_connection_noise(loop, context):
    """Bỏ traceback ConnectionResetError mà asyncio proactor Windows sinh ra khi
    trình duyệt ngắt kết nối giữa lúc server đang gửi dữ liệu. Lỗi khác vẫn giữ
    đường xử lý cũ để không che lỗi thật."""
    exc = context.get("exception")
    message = str(context.get("message") or "")
    if isinstance(exc, ConnectionResetError) and "_call_connection_lost" in message:
        return
    if _prev_loop_exception_handler is not None:
        _prev_loop_exception_handler(loop, context)
    else:
        loop.default_exception_handler(context)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _prev_loop_exception_handler
    loop = asyncio.get_running_loop()
    _prev_loop_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(_quiet_connection_noise)
    try:
        yield
    finally:
        loop.set_exception_handler(_prev_loop_exception_handler)


app = FastAPI(title="SubExtract Local", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError):
    # Validation input may itself contain NaN/Infinity and cannot be echoed as JSON.
    detail = jsonable_encoder(exc.errors(), custom_encoder={float: lambda value: value if math.isfinite(value) else str(value)})
    return JSONResponse({"detail": detail}, status_code=422)

# Limits include multipart framing before FastAPI spools the uploaded file.
MAX_UPLOAD_BYTES = int(os.environ.get("SUBEXTRACT_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
MAX_DATA_BYTES = int(os.environ.get("SUBEXTRACT_MAX_DATA_MB", "20480")) * 1024 * 1024
MIN_FREE_BYTES = int(os.environ.get("SUBEXTRACT_MIN_FREE_MB", "512")) * 1024 * 1024
_upload_lock = threading.Lock()
_upload_admitted = ContextVar("subextract_upload_admitted", default=False)
_log = logging.getLogger(__name__)


class _UploadLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/api/jobs":
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD_BYTES + 1024 * 1024
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Content-Length không hợp lệ"}, status_code=400)(scope, receive, send)
        if declared < 0 or declared > limit:
            return await JSONResponse({"detail": "Video vượt giới hạn tải lên"}, status_code=413)(scope, receive, send)
        # Reserve before multipart parsing, which may spool onto the temp volume.
        if not _upload_lock.acquire(blocking=False):
            return await JSONResponse({"detail": "Đang tải một video khác, vui lòng thử lại sau"}, status_code=429)(scope, receive, send)
        token = _upload_admitted.set(True)
        try:
            temporary_volume = tempfile.gettempdir()
            if shutil.disk_usage(temporary_volume).free - declared < MIN_FREE_BYTES:
                return await JSONResponse({"detail": "Không đủ dung lượng đĩa tạm để nhận video"}, status_code=507)(scope, receive, send)
            consumed = 0

            async def limited_receive():
                nonlocal consumed
                message = await receive()
                if message["type"] == "http.request":
                    chunk_bytes = len(message.get("body", b""))
                    consumed += chunk_bytes
                    if consumed > limit:
                        raise HTTPException(status_code=413, detail="Video vượt giới hạn tải lên")
                    if shutil.disk_usage(temporary_volume).free - chunk_bytes < MIN_FREE_BYTES:
                        raise HTTPException(status_code=507, detail="Không đủ dung lượng đĩa tạm để nhận video")
                return message

            return await self.app(scope, limited_receive, send)
        finally:
            _upload_admitted.reset(token)
            _upload_lock.release()


app.add_middleware(_UploadLimitMiddleware)

_gpu_cache: dict | None = None
GEMINI_STATUS_TTL = 900.0
_gemini_status_lock = threading.Lock()
_gemini_status_cache: dict | None = None


def gpu_info() -> dict:
    global _gpu_cache
    if _gpu_cache is not None:
        return _gpu_cache
    info = {"available": False, "name": "", "torch": ""}
    try:
        import torch

        info["torch"] = torch.__version__
        if torch.cuda.is_available():
            info["available"] = True
            info["name"] = torch.cuda.get_device_name(0)
    except Exception as exc:  # pragma: no cover
        info["error"] = str(exc)
    _gpu_cache = info
    return info


def _build_gemini_status(keys: list[str], probed: bool) -> dict:
    key_state = check_key(keys[0])
    working = keys[0]
    if not key_state["ok"]:
        for alt in keys[1:]:
            alt_state = check_key(alt)
            if alt_state["ok"]:
                key_state, working = alt_state, alt
                break

    results: dict[str, dict] = {}
    if probed and key_state["ok"]:
        with ThreadPoolExecutor(max_workers=len(ALLOWED_MODELS)) as pool:
            for result in pool.map(lambda model: probe_model(working, model), ALLOWED_MODELS):
                results[result["model"]] = result

    listed = key_state["models"]
    entries: list[dict] = []
    best = ""
    for model in ALLOWED_MODELS:
        entry = {"id": model, "label": MODEL_LABELS.get(model, model),
                 "ok": False, "reason": "", "detail": ""}
        if not key_state["ok"]:
            entry.update({"reason": "auth", "detail": key_state["error"]})
        elif model in results:
            result = results[model]
            entry.update({"ok": result["ok"], "reason": result["reason"],
                          "detail": result["detail"], "status": result["status"]})
        elif listed and model not in listed:
            entry.update({"reason": "not_found",
                          "detail": "Model không có trong tài khoản của key này."})
        else:
            entry.update({"ok": True, "reason": "ok", "detail": ""})
        if entry["ok"] and not best:
            best = model
        entries.append(entry)

    return {
        "ok": bool(best),
        "key_configured": True,
        "key_valid": bool(key_state["ok"]),
        "key_error": key_state["error"],
        "keys_total": len(keys),
        "probed": probed,
        "models": entries,
        "best_model": best,
        "checked_at": round(time.time(), 1),
    }


class StartBody(BaseModel):
    roi: list | None = None
    sample_fps: FiniteFloat = 5.0
    langs: list[str] = ["vi", "en", "zh"]
    min_conf: FiniteFloat = 0.28
    merge_gap: FiniteFloat = 0.45
    min_duration: FiniteFloat = 0.25
    translate_to: str = ""
    dub: bool = False


def _parse_rois(raw) -> list[tuple[float, float, float, float]]:
    """Chấp nhận [x,y,w,h] (cũ) hoặc danh sách các vùng [[x,y,w,h],…], tối đa 4."""
    items = raw if raw is not None else [0.0, 0.68, 1.0, 0.32]
    if items and isinstance(items[0], (int, float)):
        items = [items]
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="ROI phải là [x, y, w, h] hoặc danh sách các vùng")
    rois: list[tuple[float, float, float, float]] = []
    for item in items[:4]:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            raise HTTPException(status_code=400, detail="Mỗi vùng ROI phải có 4 giá trị [x, y, w, h]")
        try:
            x, y, w, h = [float(v) for v in item]
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(status_code=400, detail="Giá trị ROI không hợp lệ") from None
        if not all(math.isfinite(v) for v in (x, y, w, h)):
            raise HTTPException(status_code=400, detail="Giá trị ROI phải là số hữu hạn")
        x = min(max(x, 0.0), 0.98)
        y = min(max(y, 0.0), 0.98)
        w = min(max(w, 0.02), 1.0 - x)
        h = min(max(h, 0.02), 1.0 - y)
        rois.append((x, y, w, h))
    return rois


class CueBody(BaseModel):
    start: FiniteFloat
    end: FiniteFloat
    text: str
    trans: str = ""
    region: int | None = None
    id: str | None = None


class CuesBody(BaseModel):
    cues: list[CueBody]


class GlossaryItem(BaseModel):
    src: str
    dst: str


class SettingsBody(BaseModel):
    gemini_api_key: str | None = None
    translate_model: str | None = None
    glossary: list[GlossaryItem] | None = None
    dub_output_dir: str | None = None
    tool_v1_url: str | None = None


_cue_write_lock = threading.RLock()


class RetranslateBody(BaseModel):
    index: int | None = None
    cue_id: str | None = None
    text: str | None = None
    target: str | None = None
    persist: bool = True


class HandoffBody(BaseModel):
    tool_v1_url: str | None = None
    target_lang: str | None = None
    voice_id: str | None = None
    original_volume_db: FiniteFloat | None = None
    dubbing_volume_db: FiniteFloat | None = None
    blur_radius: int = Field(default=16, ge=2, le=32)


def _get_job(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return job


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    import shutil

    return {
        "ok": True,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "gpu": gpu_info(),
    }


@app.get("/api/settings")
def get_settings():
    data = load_settings()
    return {
        "gemini_configured": bool(get_gemini_keys()),
        "translate_model": normalize_model(data.get("translate_model")),
        "models": list(ALLOWED_MODELS),
        "default_model": DEFAULT_MODEL,
        "glossary": data.get("glossary") or [],
        "dub_voice": str(data.get("dub_voice") or DEFAULT_VOICE),
        "dub_voice_label": str(data.get("dub_voice_label") or DEFAULT_VOICE_LABEL),
        "dub_output_dir": str(data.get("dub_output_dir") or DEFAULT_OUTPUT_DIR),
        "tool_v1_url": str(data.get("tool_v1_url") or handoff.DEFAULT_TOOL_V1_URL),
    }


@app.get("/api/tool-v1/status")
def tool_v1_status():
    """Read-only handshake; never import a video or start a render."""
    base = None
    try:
        base = handoff.resolve_url(load_settings().get("tool_v1_url"))
        handoff.connection(base)
        return {"ok": True, "authorized": True, "base_url": base, "dashboard_url": base + "/", "version": 1}
    except handoff.HandoffError as exc:
        return {"ok": False, "base_url": base, "dashboard_url": base + "/" if base else None,
                "detail": str(exc)}


@app.get("/api/gemini/status")
def gemini_status(probe: int = 1, refresh: int = 0):
    """Kiểm tra key Gemini còn dùng được và model nào đang trả lời được.

    probe=1 (mặc định) gọi thật một request siêu nhỏ cho từng model để phát hiện
    quá tải (503) hoặc hết quota (429); kết quả được cache GEMINI_STATUS_TTL giây
    để mở app nhiều lần không gọi API liên tục.
    """
    global _gemini_status_cache
    keys = get_gemini_keys()
    current = normalize_model(load_settings().get("translate_model"))
    if not keys:
        return {
            "ok": False, "key_configured": False, "key_valid": False,
            "key_error": "Chưa cấu hình Gemini API key.", "probed": False,
            "models": [{"id": m, "label": MODEL_LABELS.get(m, m), "ok": False,
                        "reason": "no_key", "detail": ""} for m in ALLOWED_MODELS],
            "best_model": "", "current_model": current, "cached": False, "checked_at": 0,
        }
    with _gemini_status_lock:
        now = time.time()
        cached = _gemini_status_cache
        if (not refresh) and cached and (now - cached["ts"]) < GEMINI_STATUS_TTL \
                and (cached["probed"] or not probe):
            payload = dict(cached["payload"])
            payload["cached"] = True
            payload["current_model"] = current
            return payload
        payload = _build_gemini_status(keys, bool(probe))
        _gemini_status_cache = {"ts": now, "probed": bool(probe), "payload": payload}
        out = dict(payload)
        out["cached"] = False
        out["current_model"] = current
        return out


@app.put("/api/settings")
def update_settings(body: SettingsBody):
    updates: dict = {}
    key = (body.gemini_api_key or "").strip()
    if key:
        updates["gemini_api_key"] = key
    if body.translate_model:
        updates["translate_model"] = normalize_model(body.translate_model)
    if body.glossary is not None:
        pairs = []
        for item in body.glossary:
            src = item.src.strip()[:120]
            dst = item.dst.strip()[:120]
            if src and dst:
                pairs.append({"src": src, "dst": dst})
        updates["glossary"] = pairs[:100]
    if body.dub_output_dir is not None:
        folder = body.dub_output_dir.strip()
        if folder:
            updates["dub_output_dir"] = folder[:260]
    if body.tool_v1_url is not None:
        url = body.tool_v1_url.strip()
        if url:
            try:
                updates["tool_v1_url"] = handoff.resolve_url(url)
            except handoff.HandoffError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            updates["tool_v1_url"] = ""
    if updates:
        save_settings(updates)
    data = load_settings()
    return {"ok": True, "gemini_configured": bool(get_gemini_keys()),
            "translate_model": normalize_model(data.get("translate_model")),
            "glossary": data.get("glossary") or [],
            "dub_output_dir": str(data.get("dub_output_dir") or DEFAULT_OUTPUT_DIR),
            "tool_v1_url": str(data.get("tool_v1_url") or handoff.DEFAULT_TOOL_V1_URL)}


@app.get("/api/usage")
def usage_summary():
    from datetime import datetime

    entries = read_usage(days=14)
    days: dict[str, dict] = {}
    total_chars = 0
    total_cues = 0
    for entry in entries:
        day = datetime.fromtimestamp(float(entry.get("ts") or 0)).strftime("%Y-%m-%d")
        agg = days.setdefault(day, {"date": day, "chars": 0, "cues": 0})
        agg["chars"] += int(entry.get("chars") or 0)
        agg["cues"] += int(entry.get("cues") or 0)
        total_chars += int(entry.get("chars") or 0)
        total_cues += int(entry.get("cues") or 0)
    return {
        "days": sorted(days.values(), key=lambda x: x["date"]),
        "total_chars": total_chars,
        "total_cues": total_cues,
    }


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)):
    # A local UI uploads one video at a time; avoid overcommitting disk across uploads.
    owns_reservation = not _upload_admitted.get()
    if owns_reservation and not _upload_lock.acquire(blocking=False):
        await file.close()
        raise HTTPException(status_code=429, detail="Đang tải một video khác, vui lòng thử lại sau")
    job_id = uuid.uuid4().hex[:12]
    work_dir = DATA_DIR / job_id
    registered = False
    created = False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stored_bytes = sum(path.stat().st_size for path in DATA_DIR.rglob("*") if path.is_file())
        if stored_bytes >= MAX_DATA_BYTES:
            raise HTTPException(status_code=413, detail="Kho video đã đạt giới hạn dung lượng")
        work_dir.mkdir(exist_ok=False)
        created = True
        suffix = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
        if suffix not in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mts", ".flv"}:
            suffix = ".mp4"
        video_path = work_dir / f"source{suffix}"
        written = 0
        with video_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Video vượt giới hạn tải lên")
                if stored_bytes + written > MAX_DATA_BYTES:
                    raise HTTPException(status_code=413, detail="Kho video đã đạt giới hạn dung lượng")
                if shutil.disk_usage(DATA_DIR).free - len(chunk) < MIN_FREE_BYTES:
                    raise HTTPException(status_code=507, detail="Không đủ dung lượng đĩa để lưu video")
                out.write(chunk)
        if not written:
            raise HTTPException(status_code=400, detail="Video tải lên rỗng")
        info = await run_in_threadpool(probe_video, video_path)
        job = Job(
            id=job_id, work_dir=work_dir, filename=file.filename or video_path.name,
            video_path=video_path,
            info={"duration": info.duration, "width": info.width, "height": info.height,
                  "fps": info.fps, "codec": info.codec},
            status="uploaded", message="Đã tải video lên",
        )
        # Only publish a job after its source and metadata are durable.
        job.save()
        store.add(job)
        registered = True
        return job.snapshot()
    except HTTPException:
        raise
    except FFmpegError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail="Không lưu được video trên đĩa") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Không tạo được job video") from exc
    finally:
        try:
            await file.close()
        finally:
            try:
                if created and not registered:
                    # This request exclusively created this exact directory.
                    if work_dir.resolve().parent != DATA_DIR.resolve() or work_dir.name != job_id:
                        raise RuntimeError("Invalid upload cleanup path")
                    shutil.rmtree(work_dir)
            finally:
                if owns_reservation:
                    _upload_lock.release()


def _run_params_snapshot(params: RunParams) -> dict:
    return {
        "roi": [list(roi) for roi in params.rois], "sample_fps": params.sample_fps,
        "langs": params.langs, "min_conf": params.min_conf,
        "merge_gap": params.merge_gap, "min_duration": params.min_duration,
        "translate_to": params.translate_to,
        "translate_model": params.translate_model if params.translate_to else "",
        "dub": params.dub, "dub_voice": params.dub_voice if params.dub else "",
        "dub_output_dir": params.dub_output_dir if params.dub else "",
    }


def _result_message(cues: list, stats: dict, params: RunParams) -> str:
    translated = int(stats.get("translated_cues") or 0)
    error = str(stats.get("translate_error") or "")
    if error:
        message = f"Hoàn tất: {len(cues)} cue · đã dịch {translated} · lỗi dịch: {error[:160]}"
    elif translated:
        message = f"Hoàn tất: {len(cues)} cue · đã dịch {translated} cue."
    elif not int(stats.get("translate_total") or 0) and params.translate_to:
        labels = {"vi": "tiếng Việt", "en": "English", "zh": "tiếng Trung"}
        label = labels.get(params.translate_to, params.translate_to)
        message = (f"Hoàn tất: {len(cues)} cue · phụ đề gốc đã là {label} nên không có gì để dịch — "
                   "chọn ngôn ngữ đích khác nếu muốn dịch.")
    else:
        message = f"Hoàn tất: {len(cues)} cue phụ đề."
    fallback = stats.get("translate_fallback_from") or []
    if fallback and stats.get("translate_model_used"):
        message += f" · {fallback[0]} không dùng được, đã chuyển sang {stats['translate_model_used']}"
    if params.dub:
        if stats.get("dub_output"):
            message += f" · lồng tiếng {params.dub_voice_label} → {stats['dub_output']}"
        elif stats.get("dub_error"):
            message += f" · lỗi lồng tiếng: {str(stats['dub_error'])[:160]}"
    return message


def _run_job_thread(job: Job, params: RunParams) -> None:
    attempt_dir = None
    published = False

    def progress(stage: str, value: float, message: str) -> None:
        job.stage = stage
        job.progress = value
        job.message = message

    try:
        # Persist the attempt's running status while retaining the last good result.
        with _cue_write_lock:
            job.save()
        pipeline_dir, pipeline_params = job.work_dir, params
        if params.dub:
            attempt_id = uuid.uuid4().hex
            attempt_dir = job.work_dir / "attempts" / attempt_id
            attempt_dir.mkdir(parents=True, exist_ok=False)
            pipeline_dir = attempt_dir
            pipeline_params = replace(params, source_name=f"{attempt_id}_{params.source_name or job.filename}")
        result = run_pipeline(
            video_path=job.video_path, work_dir=pipeline_dir, params=pipeline_params,
            progress=progress, cancel_event=job.cancel_event, gpu=gpu_info()["available"],
        )
        with _cue_write_lock:
            if job.cancel_event.is_set():
                raise Cancelled()
            previous = {name: getattr(job, name) for name in (
                "cues", "stats", "params", "status", "stage", "progress", "message", "error",
            )}
            try:
                stats = dict(result["stats"])
                # Poll/cancel of a remote render may have completed during OCR.
                if (job.stats or {}).get("handoff"):
                    stats["handoff"] = dict(job.stats["handoff"])
                result_cues = []
                for cue in result["cues"]:
                    result_cues.append({**cue, "id": uuid.uuid4().hex})
                stats = _missing_stats(stats, result["cues"], result_cues, params.translate_to)
                job.cues = result_cues
                job.stats = stats
                job.params = _run_params_snapshot(params)
                job.status = "done"
                job.stage = "done"
                job.progress = 1.0
                job.error = ""
                job.message = _result_message(job.cues, stats, params)
                job.save()
                published = True
            except Exception:
                for name, value in previous.items():
                    setattr(job, name, value)
                raise
    except Cancelled:
        job.status = "cancelled"
        job.message = "Đã hủy. Kết quả trước đó được giữ lại." if job.cues else "Đã hủy."
    except Exception as exc:
        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.message = "Đã hủy. Kết quả trước đó được giữ lại." if job.cues else "Đã hủy."
        else:
            job.status = "error"
            job.error = str(exc) or exc.__class__.__name__
            job.message = "Lỗi khi xử lý video. Kết quả trước đó được giữ lại." if job.cues else "Lỗi khi xử lý video."
            try:
                (job.work_dir / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                _log.warning("Cannot write error log for job %s", job.id)
    finally:
        if attempt_dir is not None and not published:
            if attempt_dir.resolve().parent != (job.work_dir / "attempts").resolve():
                _log.error("Invalid attempt cleanup path for job %s", job.id)
            else:
                try:
                    shutil.rmtree(attempt_dir)
                except OSError:
                    _log.warning("Cannot remove failed attempt for job %s", job.id)
        with _cue_write_lock:
            try:
                if job.status != "done":
                    job.save()
            finally:
                job.run_lock.release()


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str, body: StartBody):
    job = _get_job(job_id)
    with _cue_write_lock:
        if job.status == "running" or job.run_lock.locked():
            raise HTTPException(status_code=409, detail="Job đang chạy")

    rois = _parse_rois(body.roi)

    langs = [l for l in body.langs if l in {"vi", "en", "zh"}]
    if not langs:
        raise HTTPException(status_code=400, detail="Chọn ít nhất một ngôn ngữ")

    translate_to = body.translate_to.strip().lower()
    if translate_to and translate_to not in {"vi", "en", "zh"}:
        raise HTTPException(status_code=400, detail="Ngôn ngữ dịch không hỗ trợ")
    if translate_to and not get_gemini_keys():
        raise HTTPException(status_code=400, detail="Chưa cấu hình Gemini API key trong Cài đặt")
    translate_model = normalize_model(load_settings().get("translate_model"))
    settings_data = load_settings()
    dub_enabled = bool(body.dub)
    dub_voice = str(settings_data.get("dub_voice") or DEFAULT_VOICE)
    dub_voice_label = str(settings_data.get("dub_voice_label") or DEFAULT_VOICE_LABEL)
    dub_output_dir = str(settings_data.get("dub_output_dir") or DEFAULT_OUTPUT_DIR)

    params = RunParams(
        rois=tuple(rois),
        sample_fps=min(max(float(body.sample_fps), 1.0), 12.0),
        langs=langs,
        min_conf=min(max(float(body.min_conf), 0.05), 0.95),
        merge_gap=min(max(float(body.merge_gap), 0.0), 2.0),
        min_duration=min(max(float(body.min_duration), 0.1), 3.0),
        translate_to=translate_to,
        translate_model=translate_model,
        dub=dub_enabled,
        dub_voice=dub_voice,
        dub_voice_label=dub_voice_label,
        dub_output_dir=dub_output_dir,
        source_name=job.filename,
    )

    with _cue_write_lock:
        # Reserve before starting the worker; preserve the last successful result.
        with _handoff_lock:
            if job_id in _handoff_busy:
                raise HTTPException(status_code=409, detail="Đang gửi sang Tool V1, vui lòng chờ")
        if job.status == "running" or not job.run_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Job đang chạy")
        previous = {name: getattr(job, name) for name in (
            "cues", "stats", "params", "status", "stage", "progress", "message", "error",
        )}
        was_cancelled = job.cancel_event.is_set()
        try:
            job.cancel_event.clear()
            # Keep cues, stats and params until the replacement result is durable.
            job.status = "running"
            job.stage = "detect"
            job.progress = 0.0
            job.message = "Bắt đầu xử lý…"
            job.error = ""
            thread = threading.Thread(target=_run_job_thread, args=(job, params), daemon=True)
            thread.start()
        except Exception:
            for name, value in previous.items():
                setattr(job, name, value)
            if was_cancelled:
                job.cancel_event.set()
            job.run_lock.release()
            raise
        return {"ok": True, "job": job.snapshot()}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _get_job(job_id)
    job.cancel_event.set()
    return {"ok": True}


def _job_snapshot(job: Job, include_cues: bool = False) -> dict:
    with _cue_write_lock:
        return job.snapshot(include_cues=include_cues)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job_snapshot(_get_job(job_id), include_cues=True)


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    job = _get_job(job_id)

    async def generator():
        while True:
            snapshot = await run_in_threadpool(_job_snapshot, job)
            payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
            yield f"data: {payload}\n\n"
            if job.status in {"done", "error", "cancelled"}:
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _cue_fingerprint(cue: dict) -> tuple:
    return (round(float(cue.get("start", 0)), 3), round(float(cue.get("end", 0)), 3),
            str(cue.get("text") or "").strip(), cue.get("region"))


def _missing_stats(stats: dict, old_cues: list[dict], new_cues: list[dict], target: str, *, mark_edits: bool = False) -> dict:
    """Keep missing-translation metadata attached to identities, including legacy jobs."""
    ids = stats.get("untranslated_ids")
    if isinstance(ids, list):
        missing_ids = {item for item in ids if isinstance(item, str)}
        flagged = [cue for cue in old_cues if cue.get("id") in missing_ids]
    else:
        indices = stats.get("untranslated_indices") or []
        flagged = [old_cues[i] for i in indices if type(i) is int and 0 <= i < len(old_cues)]
        missing_ids = {cue.get("id") for cue in flagged if cue.get("id")}
    legacy_keys = {_cue_fingerprint(cue) for cue in flagged if not cue.get("id")}
    old_by_id = {cue["id"]: cue for cue in old_cues if cue.get("id")}
    old_keys = {_cue_fingerprint(cue) for cue in old_cues}
    indices = []
    ids = []
    for index, cue in enumerate(new_cues):
        cue_id = cue.get("id")
        if not str(cue.get("text") or "").strip() or str(cue.get("trans") or "").strip():
            continue
        missing = cue_id in missing_ids or _cue_fingerprint(cue) in legacy_keys
        if mark_edits and target:
            previous = old_by_id.get(cue_id)
            if previous is not None:
                missing = missing or str(previous.get("text") or "").strip() != cue["text"].strip()
            elif _cue_fingerprint(cue) not in old_keys:
                missing = True
        if missing:
            indices.append(index)
            if cue_id:
                ids.append(cue_id)
    return {**stats, "untranslated_indices": indices, "untranslated_ids": ids, "untranslated_cues": len(indices)}


@app.put("/api/jobs/{job_id}/cues")
def update_cues(job_id: str, body: CuesBody):
    job = _get_job(job_id)
    cues = []
    seen_ids = set()
    for cue in sorted(body.cues, key=lambda c: c.start):
        text = cue.text.strip()
        if not text:
            continue
        start = max(0.0, float(cue.start))
        end = max(start + 0.05, float(cue.end))
        cue_id = (cue.id or "").strip()
        if not cue_id or len(cue_id) > 128 or cue_id in seen_ids:
            cue_id = uuid.uuid4().hex
            while cue_id in seen_ids:
                cue_id = uuid.uuid4().hex
        seen_ids.add(cue_id)
        cues.append({
            "id": cue_id,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "trans": cue.trans.strip(),
            "region": cue.region,
        })
    with _cue_write_lock:
        if job.status == "running" or job.run_lock.locked():
            raise HTTPException(status_code=409, detail="Job đang chạy, chưa thể sửa")
        previous, previous_stats = job.cues, job.stats
        job.cues = cues
        job.stats = _missing_stats(previous_stats or {}, previous, cues,
                                   str((job.params or {}).get("translate_to") or ""), mark_edits=True)
        try:
            job.save()
        except Exception:
            job.cues, job.stats = previous, previous_stats
            raise
    return {"ok": True, "count": len(cues), "cues": cues}


@app.post("/api/jobs/{job_id}/retranslate")
def retranslate_cue(job_id: str, body: RetranslateBody):
    job = _get_job(job_id)
    with _cue_write_lock:
        if job.status == "running" or job.run_lock.locked():
            raise HTTPException(status_code=409, detail="Job đang chạy, hãy hủy hoặc chờ xong")
        if not job.cues:
            raise HTTPException(status_code=400, detail="Job chưa có cue để dịch")
        if body.cue_id is not None:
            cue_id = body.cue_id.strip()
            matches = [i for i, cue in enumerate(job.cues) if cue.get("id") == cue_id]
            if not cue_id or len(matches) != 1:
                raise HTTPException(status_code=400, detail="ID cue không hợp lệ")
            index = matches[0]
        else:
            index = body.index
            if index is None or index < 0 or index >= len(job.cues):
                raise HTTPException(status_code=400, detail="Chỉ số cue không hợp lệ")
        original_cues = job.cues
        cue = original_cues[index]
        snapshot = dict(cue)
        text = (body.text if body.text is not None else str(snapshot.get("text") or "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="Cue không có nội dung để dịch")

        target = (body.target or "").strip().lower()
        if not target:
            target = str((job.params or {}).get("translate_to") or "").strip().lower()
        if target not in {"vi", "en", "zh"}:
            raise HTTPException(status_code=400, detail="Chưa chọn ngôn ngữ đích (vi/en/zh)")

    keys = get_gemini_keys()
    if not keys:
        raise HTTPException(status_code=400, detail="Chưa cấu hình Gemini API key trong Cài đặt")
    settings_data = load_settings()
    model = normalize_model(settings_data.get("translate_model"))
    glossary = settings_data.get("glossary")

    # Gemini runs outside the lock so edits and other jobs remain responsive.
    tmp = {"start": snapshot.get("start"), "end": snapshot.get("end"), "text": text, "trans": ""}
    result = translate_cues([tmp], target=target, api_key=keys, model=model, glossary=glossary)
    trans = str(tmp.get("trans") or "").strip()
    if not trans:
        if int(result.get("total") or 0) == 0 and not result.get("error"):
            raise HTTPException(status_code=400, detail="Cue này đã ở ngôn ngữ đích, không cần dịch")
        raise HTTPException(status_code=502, detail=result.get("error") or "Không dịch được cue này")

    if body.persist:
        with _cue_write_lock:
            # A PUT or rerun replaces the list even if its contents later match.
            if (job.status == "running" or job.run_lock.locked()
                    or job.cues is not original_cues
                    or index >= len(job.cues) or job.cues[index] is not cue
                    or cue != snapshot):
                raise HTTPException(status_code=409, detail="Cue đã thay đổi trong lúc dịch, hãy thử lại")
            updated = {**snapshot, "trans": trans}
            previous_stats = job.stats
            old_cues = list(job.cues)
            job.cues[index] = updated
            job.stats = _missing_stats(previous_stats or {}, old_cues, job.cues, target)
            try:
                job.save()
            except Exception:
                job.cues[index] = cue
                job.stats = previous_stats
                raise
    return {"ok": True, "index": index, "cue_id": snapshot.get("id"),
            "trans": trans, "persisted": body.persist}


_handoff_lock = threading.Lock()
_handoff_busy: set[str] = set()
_handoff_recovery: dict[tuple[str, str], dict] = {}
_HANDOFF_ACTIVE = {"queued", "running", "cancelling"}


def _handoff_record(job: Job) -> dict:
    return dict((job.stats or {}).get("handoff") or {})


def _save_handoff(job: Job, record: dict | None) -> None:
    # All callers hold _cue_write_lock, followed by _handoff_lock.
    previous = job.stats
    updated = dict(previous or {})
    if record is None:
        updated.pop("handoff", None)
    else:
        updated["handoff"] = dict(record)
    job.stats = updated
    try:
        job.save()
    except Exception:
        job.stats = previous
        raise


def _handoff_version(record: dict) -> tuple:
    return (record.get("attempt_id"), record.get("base_url"), record.get("remote_job_id"))


@app.post("/api/jobs/{job_id}/handoff")
def send_handoff(job_id: str, body: HandoffBody):
    """Persist a send intent before the remote side effect, then commit its ID."""
    job = _get_job(job_id)
    try:
        base = handoff.resolve_url(body.tool_v1_url or load_settings().get("tool_v1_url"))
    except handoff.HandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _cue_write_lock, _handoff_lock:
        if job.status == "running" or job.run_lock.locked():
            raise HTTPException(status_code=409, detail="Job đang chạy, hãy chờ xong rồi gửi sang Tool V1")
        if not job.cues:
            raise HTTPException(status_code=400, detail="Job chưa có phụ đề để gửi")
        previous = _handoff_record(job)
        if job_id in _handoff_busy or str(previous.get("status") or "") in _HANDOFF_ACTIVE:
            raise HTTPException(status_code=409, detail="Đang có lần gửi/render chưa kết thúc. Kiểm tra Tool V1 trước khi gửi lại.")
        target = ((body.target_lang or "").strip().lower()
                  or str((job.params or {}).get("translate_to") or "vi"))
        try:
            payload = handoff.build_payload(job, target)
        except handoff.HandoffError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        intent = {
            "attempt_id": uuid.uuid4().hex, "status": "queued", "progress": 0,
            "stage": "Đang gửi sang Tool V1...", "base_url": base, "remote_job_id": None,
            "target_lang": payload["target_lang"], "sent_at": round(time.time(), 1),
            "error": "", "result": None,
            "sub_cover_style": "localized_blur", "blur_radius": body.blur_radius,
        }
        _handoff_busy.add(job_id)
        try:
            _save_handoff(job, intent)
        except Exception:
            _handoff_busy.discard(job_id)
            raise
    try:
        result = handoff.import_and_render(base, payload, {
            "voice_id": body.voice_id, "original_volume_db": body.original_volume_db,
            "dubbing_volume_db": body.dubbing_volume_db,
            "sub_cover_style": "localized_blur", "sub_blur_radius": body.blur_radius,
            "sub_blur_padding_x": 0.015, "sub_blur_padding_y": 0.008,
        })
        imported = result.get("imported") or {}
        started = result.get("render") or {}
        if not started.get("job_id"):
            raise handoff.HandoffError("Tool V1 không trả mã render")
        record = {
            **intent, "stage": "Đã gửi sang Tool V1, đang chờ render...",
            "base_url": result.get("base_url") or base, "remote_job_id": started["job_id"],
            "stem": imported.get("stem"), "job_dir": imported.get("job_dir"),
            "video_link": imported.get("video_link"), "cue_count": imported.get("cue_count"),
            "translated_count": imported.get("translated_count"),
            **({"queue_position": started["queue_position"]}
               if type(started.get("queue_position")) is int and started["queue_position"] > 0 else {}),
        }
        with _cue_write_lock, _handoff_lock:
            try:
                _save_handoff(job, record)
            except Exception as exc:
                # The durable intent blocks a resend after restart. A later GET
                # can retry this local commit without starting a second render.
                _handoff_recovery[(job_id, intent["attempt_id"])] = record
                raise HTTPException(status_code=507, detail=f"Tool V1 đã nhận render {record['remote_job_id']} nhưng chưa lưu được trạng thái. Không gửi lại; kiểm tra Tool V1.") from exc
        return {"ok": True, "handoff": record}
    except handoff.HandoffError as exc:
        with _cue_write_lock, _handoff_lock:
            try:
                if getattr(exc, "safe_to_retry", False):
                    _save_handoff(job, previous or None)
                else:
                    _save_handoff(job, {**intent, "stage": "Chưa xác định kết quả gửi. Kiểm tra Tool V1 trước khi gửi lại.", "error": str(exc)})
            except Exception:
                _log.warning("Cannot record handoff failure for job %s", job_id)
        status_code = 429 if isinstance(exc, handoff.HandoffQueueFull) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    finally:
        with _handoff_lock:
            _handoff_busy.discard(job_id)


@app.get("/api/jobs/{job_id}/handoff")
def get_handoff(job_id: str):
    job = _get_job(job_id)
    with _cue_write_lock, _handoff_lock:
        record = _handoff_record(job)
    if not record.get("remote_job_id"):
        with _cue_write_lock, _handoff_lock:
            key = (job_id, record.get("attempt_id"))
            recovered = _handoff_recovery.get(key)
            if recovered and _handoff_version(_handoff_record(job)) == _handoff_version(record):
                try:
                    _save_handoff(job, recovered)
                except Exception:
                    return {"ok": True, "handoff": {**record, "unreachable": "Chưa lưu được trạng thái render, kiểm tra Tool V1."}}
                record = recovered
                _handoff_recovery.pop(key, None)
            elif record:
                view = dict(record)
                if job_id not in _handoff_busy:
                    view["stage"] = "Chưa xác định kết quả gửi. Kiểm tra Tool V1 trước khi gửi lại."
                return {"ok": True, "handoff": view}
            else:
                return {"ok": True, "handoff": None}
    try:
        remote = handoff.status(record.get("base_url"), record["remote_job_id"])
    except handoff.HandoffError as exc:
        with _cue_write_lock, _handoff_lock:
            current = _handoff_record(job)
            if _handoff_version(current) != _handoff_version(record) or current != record:
                return {"ok": True, "handoff": current or None}
            return {"ok": True, "handoff": {**current, "unreachable": str(exc)}}
    with _cue_write_lock, _handoff_lock:
        current = _handoff_record(job)
        if _handoff_version(current) != _handoff_version(record) or current != record:
            return {"ok": True, "handoff": current or None}
        updated = {
            **current, "status": str(remote.get("status") or current.get("status") or "queued"),
            "progress": int(remote.get("progress") or 0), "stage": str(remote.get("stage") or ""),
            "error": str(remote.get("error") or ""), "result": remote.get("result"), "unreachable": "",
        }
        position = remote.get("queue_position")
        if updated["status"] == "queued" and type(position) is int and position > 0:
            updated["queue_position"] = position
        else:
            updated.pop("queue_position", None)
        if updated != current:
            _save_handoff(job, updated)
        return {"ok": True, "handoff": updated}


@app.post("/api/jobs/{job_id}/handoff/cancel")
def cancel_handoff(job_id: str):
    job = _get_job(job_id)
    with _cue_write_lock, _handoff_lock:
        record = _handoff_record(job)
    if not record.get("remote_job_id"):
        raise HTTPException(status_code=400, detail="Chưa có bản gửi Tool V1 nào để hủy")
    try:
        response = handoff.cancel(record.get("base_url"), record["remote_job_id"])
    except handoff.HandoffError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    with _cue_write_lock, _handoff_lock:
        current = _handoff_record(job)
        if _handoff_version(current) != _handoff_version(record):
            return {"ok": True, "handoff": current or None}
        if str(current.get("status")) in _HANDOFF_ACTIVE:
            cancelled = isinstance(response, dict) and response.get("status") == "cancelled"
            current = {**current, "status": "cancelled" if cancelled else "cancelling",
                       "stage": "Đã rút khỏi hàng chờ." if cancelled else "Đã yêu cầu Tool V1 dừng render..."}
            current.pop("queue_position", None)
            _save_handoff(job, current)
        return {"ok": True, "handoff": current}


def _with_variant(cues: list[dict], variant: str) -> list[dict]:
    view = []
    for cue in cues:
        text = str(cue.get("text") or "")
        trans = str(cue.get("trans") or "").strip()
        if variant == "translated" and trans:
            text = trans
        elif variant == "bilingual" and trans:
            text = f"{trans}\n{text}"
        view.append({"start": cue["start"], "end": cue["end"], "text": text})
    return sorted(view, key=lambda cue: cue["start"])


@app.get("/api/jobs/{job_id}/export")
def export(job_id: str, format: str = "srt", variant: str = "original"):
    job = _get_job(job_id)
    if not job.cues:
        raise HTTPException(status_code=400, detail="Chưa có phụ đề để xuất")
    fmt = format.lower()
    if fmt not in {"srt", "vtt"}:
        raise HTTPException(status_code=400, detail="Định dạng không hỗ trợ")
    variant = variant.lower()
    if variant not in {"original", "translated", "bilingual"}:
        raise HTTPException(status_code=400, detail="Biến thể không hỗ trợ")

    with _cue_write_lock:
        cues = _with_variant(job.cues, variant)
        result_params = dict(job.params or {})
    if fmt == "srt":
        content = to_srt(cues)
        media = "application/x-subrip; charset=utf-8"
    else:
        content = to_vtt(cues)
        media = "text/vtt; charset=utf-8"

    stem = Path(job.filename).stem or "subtitles"
    lang = str(result_params.get("translate_to") or "")
    suffix = ""
    if variant == "translated":
        suffix = f".{lang}" if lang else ".dich"
    elif variant == "bilingual":
        suffix = ".song-ngu"
    filename = f"{stem}{suffix}.{fmt}"
    headers = {
        "Content-Disposition": f"attachment; filename=\"subtitles.{fmt}\"; filename*=UTF-8''{quote(filename)}"
    }
    return Response(content=content.encode("utf-8"), media_type=media, headers=headers)


@app.get("/api/jobs/{job_id}/dubbed")
def get_dubbed(job_id: str):
    job = _get_job(job_id)
    stats = job.stats or {}
    for key in ("dub_local", "dub_output"):
        path = Path(str(stats.get(key) or ""))
        if path.is_file():
            return FileResponse(path, media_type="video/mp4", filename=path.name)
    raise HTTPException(status_code=404, detail="Job chưa có video lồng tiếng")


@app.get("/api/jobs/{job_id}/video")
def get_video(job_id: str, preview: int = 0):
    job = _get_job(job_id)
    if preview and not job.preview_ready:
        raise HTTPException(status_code=404, detail="Bản xem trước chưa sẵn sàng")
    path = job.preview_path if preview else job.video_path
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return FileResponse(path, media_type=media_type)


def _make_preview(job: Job) -> None:
    import subprocess

    temporary = None
    try:
        # A marker only exists after the final file has passed validation.
        job.preview_ready_path.unlink(missing_ok=True)
        fd, name = tempfile.mkstemp(prefix=".preview-", suffix=".mp4", dir=job.work_dir)
        os.close(fd)
        temporary = Path(name)
        cmd = [
            "ffmpeg", "-y", "-v", "error", "-i", str(job.video_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        ]
        if int((job.info or {}).get("width") or 0) > 1280:
            cmd += ["-vf", "scale=1280:-2"]
        cmd.append(str(temporary))
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=3600)
        if temporary.stat().st_size == 0 or probe_video(temporary).duration <= 0:
            raise FFmpegError("Bản xem trước không có video hợp lệ")
        os.replace(temporary, job.preview_path)
        job.preview_ready_path.write_text("ready\n", encoding="utf-8")
        job.preview_status = "ready"
    except Exception:
        job.preview_ready_path.unlink(missing_ok=True)
        job.preview_status = "error"
        _log.warning("Preview failed for job %s", job.id, exc_info=True)
    finally:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            with _cue_write_lock:
                job.save()
        finally:
            job.preview_lock.release()


@app.post("/api/jobs/{job_id}/preview")
def create_preview(job_id: str):
    job = _get_job(job_id)
    if not job.preview_lock.acquire(blocking=False):
        return {"status": "running"}
    if job.preview_ready:
        job.preview_status = "ready"
        job.preview_lock.release()
        return {"status": "ready"}
    previous = job.preview_status
    job.preview_status = "running"
    try:
        threading.Thread(target=_make_preview, args=(job,), daemon=True).start()
    except Exception:
        job.preview_status = previous
        job.preview_lock.release()
        raise
    return {"status": "running"}
