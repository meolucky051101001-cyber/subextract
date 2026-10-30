from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import threading
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .jobs import Job, JobStore
from .pipeline.dub import DEFAULT_OUTPUT_DIR, DEFAULT_VOICE, DEFAULT_VOICE_LABEL
from .pipeline.ffmpeg_io import FFmpegError, probe_video
from .pipeline.post import to_srt, to_vtt
from .pipeline.runner import Cancelled, RunParams, run_pipeline
from .pipeline.translate import ALLOWED_MODELS, DEFAULT_MODEL, normalize_model, translate_cues
from .settings import get_gemini_keys, load_settings, read_usage, save_settings

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = Path(os.environ.get("SUBEXTRACT_DATA_DIR") or BASE_DIR / "data" / "jobs")

store = JobStore(DATA_DIR)

app = FastAPI(title="SubExtract Local")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_gpu_cache: dict | None = None


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


class StartBody(BaseModel):
    roi: list | None = None
    sample_fps: float = 5.0
    langs: list[str] = ["vi", "en", "zh"]
    min_conf: float = 0.28
    merge_gap: float = 0.45
    min_duration: float = 0.25
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
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Giá trị ROI không hợp lệ") from None
        x = min(max(x, 0.0), 0.98)
        y = min(max(y, 0.0), 0.98)
        w = min(max(w, 0.02), 1.0 - x)
        h = min(max(h, 0.02), 1.0 - y)
        rois.append((x, y, w, h))
    return rois


class CueBody(BaseModel):
    start: float
    end: float
    text: str
    trans: str = ""


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


class RetranslateBody(BaseModel):
    index: int
    text: str | None = None
    target: str | None = None


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
    }


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
    if updates:
        save_settings(updates)
    data = load_settings()
    return {"ok": True, "gemini_configured": bool(get_gemini_keys()),
            "translate_model": normalize_model(data.get("translate_model")),
            "glossary": data.get("glossary") or [],
            "dub_output_dir": str(data.get("dub_output_dir") or DEFAULT_OUTPUT_DIR)}


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
    job_id = uuid.uuid4().hex[:12]
    work_dir = DATA_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mts", ".flv"}:
        suffix = ".mp4"
    video_path = work_dir / f"source{suffix}"

    try:
        with video_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Không ghi được file: {exc}") from exc
    finally:
        await file.close()

    try:
        info = await run_in_threadpool(probe_video, video_path)
    except FFmpegError as exc:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = Job(
        id=job_id,
        work_dir=work_dir,
        filename=file.filename or video_path.name,
        video_path=video_path,
        info={
            "duration": info.duration,
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "codec": info.codec,
        },
        status="uploaded",
        message="Đã tải video lên",
    )
    store.add(job)
    # Lưu ngay cả khi chưa chạy job để upload-only không bị mất sau restart.
    job.save()
    return job.snapshot()


def _run_job_thread(job: Job, params: RunParams) -> None:
    job.status = "running"
    job.stage = "detect"
    job.progress = 0.0
    job.message = "Bắt đầu xử lý…"
    job.error = ""
    job.save()
    gpu = gpu_info()["available"]

    def progress(stage: str, value: float, message: str) -> None:
        job.stage = stage
        job.progress = value
        job.message = message

    try:
        result = run_pipeline(
            video_path=job.video_path,
            work_dir=job.work_dir,
            params=params,
            progress=progress,
            cancel_event=job.cancel_event,
            gpu=gpu,
        )
        job.cues = result["cues"]
        job.stats = result["stats"]
        job.status = "done"
        job.stage = "done"
        job.progress = 1.0
        translated = int(job.stats.get("translated_cues") or 0)
        translate_error = str(job.stats.get("translate_error") or "")
        translate_total = int(job.stats.get("translate_total") or 0)
        translate_to = str((job.params or {}).get("translate_to") or "")
        if translate_error:
            job.message = f"Hoàn tất: {len(job.cues)} cue · đã dịch {translated} · lỗi dịch: {translate_error[:160]}"
        elif translated:
            job.message = f"Hoàn tất: {len(job.cues)} cue · đã dịch {translated} cue."
        elif translate_total == 0 and translate_to:
            labels = {"vi": "tiếng Việt", "en": "English", "zh": "tiếng Trung"}
            label = labels.get(translate_to, translate_to)
            job.message = (
                f"Hoàn tất: {len(job.cues)} cue · phụ đề gốc đã là {label} nên không có gì để dịch — "
                "chọn ngôn ngữ đích khác nếu muốn dịch."
            )
        else:
            job.message = f"Hoàn tất: {len(job.cues)} cue phụ đề."
        dub_output = str(job.stats.get("dub_output") or "")
        dub_error = str(job.stats.get("dub_error") or "")
        if (job.params or {}).get("dub"):
            voice_label = str(job.stats.get("dub_voice_label") or "Chí Mai")
            if dub_output:
                job.message += f" · lồng tiếng {voice_label} → {dub_output}"
            elif dub_error:
                job.message += f" · lỗi lồng tiếng: {dub_error[:160]}"
    except Cancelled:
        job.status = "cancelled"
        job.message = "Đã hủy."
    except Exception as exc:
        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.message = "Đã hủy."
        else:
            job.status = "error"
            job.error = str(exc) or exc.__class__.__name__
            job.message = "Lỗi khi xử lý video."
            (job.work_dir / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        job.save()
        if job.run_lock.locked():
            try:
                job.run_lock.release()
            except RuntimeError:
                pass


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str, body: StartBody):
    job = _get_job(job_id)
    if job.status == "running":
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

    job.cancel_event.clear()
    job.cues = []
    job.stats = {}
    job.params = {
        "roi": [list(r) for r in rois],
        "sample_fps": params.sample_fps,
        "langs": params.langs,
        "min_conf": params.min_conf,
        "translate_to": params.translate_to,
        "translate_model": params.translate_model if translate_to else "",
        "dub": dub_enabled,
        "dub_voice": dub_voice if dub_enabled else "",
        "dub_output_dir": dub_output_dir if dub_enabled else "",
    }
    if not job.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Job đang chạy")
    try:
        thread = threading.Thread(target=_run_job_thread, args=(job, params), daemon=True)
        thread.start()
    except Exception:
        job.run_lock.release()
        raise
    return {"ok": True, "job": job.snapshot()}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _get_job(job_id)
    job.cancel_event.set()
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _get_job(job_id)
    return job.snapshot(include_cues=True)


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    job = _get_job(job_id)

    async def generator():
        while True:
            payload = json.dumps(job.snapshot(), ensure_ascii=False)
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


@app.put("/api/jobs/{job_id}/cues")
def update_cues(job_id: str, body: CuesBody):
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(status_code=409, detail="Job đang chạy, chưa thể sửa")
    cues = []
    for cue in sorted(body.cues, key=lambda c: c.start):
        text = cue.text.strip()
        if not text:
            continue
        start = max(0.0, float(cue.start))
        end = max(start + 0.05, float(cue.end))
        cues.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "trans": cue.trans.strip(),
        })
    job.cues = cues
    job.save()
    return {"ok": True, "count": len(cues)}


@app.post("/api/jobs/{job_id}/retranslate")
def retranslate_cue(job_id: str, body: RetranslateBody):
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(status_code=409, detail="Job đang chạy, hãy hủy hoặc chờ xong")
    if not job.cues:
        raise HTTPException(status_code=400, detail="Job chưa có cue để dịch")
    if body.index < 0 or body.index >= len(job.cues):
        raise HTTPException(status_code=400, detail="Chỉ số cue không hợp lệ")
    cue = job.cues[body.index]
    text = (body.text if body.text is not None else str(cue.get("text") or "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Cue không có nội dung để dịch")

    target = (body.target or "").strip().lower()
    if not target:
        target = str((job.params or {}).get("translate_to") or "").lower()
    if target not in {"vi", "en", "zh"}:
        raise HTTPException(status_code=400, detail="Chưa chọn ngôn ngữ đích (vi/en/zh)")

    keys = get_gemini_keys()
    if not keys:
        raise HTTPException(status_code=400, detail="Chưa cấu hình Gemini API key trong Cài đặt")
    model = normalize_model(load_settings().get("translate_model"))
    glossary = load_settings().get("glossary")

    tmp = {"start": cue.get("start"), "end": cue.get("end"), "text": text, "trans": ""}
    result = translate_cues([tmp], target=target, api_key=keys, model=model, glossary=glossary)
    trans = str(tmp.get("trans") or "").strip()
    if not trans:
        if int(result.get("total") or 0) == 0 and not result.get("error"):
            raise HTTPException(status_code=400, detail="Cue này đã ở ngôn ngữ đích, không cần dịch")
        raise HTTPException(status_code=502, detail=result.get("error") or "Không dịch được cue này")

    cue["trans"] = trans
    job.save()
    return {"ok": True, "index": body.index, "trans": trans}


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
    return view


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

    cues = _with_variant(job.cues, variant)
    if fmt == "srt":
        content = to_srt(cues)
        media = "application/x-subrip; charset=utf-8"
    else:
        content = to_vtt(cues)
        media = "text/vtt; charset=utf-8"

    stem = Path(job.filename).stem or "subtitles"
    lang = str((job.params or {}).get("translate_to") or "")
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
    path = job.preview_path if (preview and job.preview_path.exists()) else job.video_path
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return FileResponse(path, media_type=media_type)


def _make_preview(job: Job) -> None:
    import subprocess

    job.preview_status = "running"
    try:
        info = job.info or {}
        cmd = [
            "ffmpeg", "-y", "-v", "error", "-i", str(job.video_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        ]
        if int(info.get("width") or 0) > 1280:
            cmd += ["-vf", "scale=1280:-2"]
        cmd.append(str(job.preview_path))
        subprocess.run(cmd, capture_output=True, check=True)
        job.preview_status = "ready"
    except Exception as exc:
        job.preview_status = "error"
        job.message = f"Không tạo được bản xem trước: {exc}"


@app.post("/api/jobs/{job_id}/preview")
def create_preview(job_id: str):
    job = _get_job(job_id)
    if job.preview_status == "running":
        return {"status": "running"}
    if job.preview_path.exists():
        job.preview_status = "ready"
        return {"status": "ready"}
    threading.Thread(target=_make_preview, args=(job,), daemon=True).start()
    return {"status": "running"}
