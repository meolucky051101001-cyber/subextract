from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .cancellation import Cancelled

DEFAULT_VOICE = "BV562_streaming"
DEFAULT_VOICE_LABEL = "Chí Mai"
DEFAULT_OUTPUT_DIR = r"D:\banve"
DEFAULT_BASE_VOLUME = 0.18
DEFAULT_DUB_VOLUME = 1.0

_WORKER = Path(__file__).resolve().parents[1] / "dub_worker.py"
_PYTHON_CANDIDATES = (
    r"C:\tool v1\backend\venv\Scripts\python.exe",
    r"C:\tool v1\backend\venv_py312\Scripts\python.exe",
)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_tts_python_cache: str | None = None


def _tts_python() -> str | None:
    global _tts_python_cache
    if _tts_python_cache:
        return _tts_python_cache
    candidates = []
    env_python = (os.environ.get("SUBEXTRACT_TTS_PYTHON") or "").strip()
    if env_python:
        candidates.append(env_python)
    try:
        from ..settings import load_settings

        configured = str(load_settings().get("dub_python") or "").strip()
        if configured:
            candidates.append(configured)
    except Exception:
        pass
    candidates.extend(_PYTHON_CANDIDATES)
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            probe = subprocess.run(
                [str(path), "-c", "import capcut_tts_api"],
                capture_output=True,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if probe.returncode == 0:
                _tts_python_cache = str(path)
                return _tts_python_cache
        except Exception:
            continue
    return None


def _run_quiet(args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def _media_duration(path: Path) -> float:
    try:
        out = _run_quiet(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            timeout=30,
        )
        return max(0.0, float(out.stdout.decode("utf-8", "replace").strip() or 0.0))
    except Exception:
        return 0.0


def _has_audio(path: Path) -> bool:
    try:
        out = _run_quiet(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            timeout=30,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def _clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"[ \t\r\n]+", " ", text).strip()
    return text


def _cjk_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if _CJK.match(ch)) / len(letters)


def _atempo_chain(factor: float) -> str:
    factor = max(0.5, min(8.0, float(factor)))
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    if abs(factor - 1.0) > 0.01:
        parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)


def safe_stem(filename: str, fallback: str = "video") -> str:
    stem = Path(str(filename or "")).stem
    stem = "".join(ch for ch in stem if ch.isprintable() and ch not in '<>:"/\\|?*')
    stem = stem.strip(" .")
    if len(stem) > 80:
        stem = stem[:80].rstrip(" .")
    return stem or fallback


def unique_output(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = folder / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}_{int(time.time())}{suffix}"


def _pick_texts(cues: list[dict]) -> tuple[list[dict], list[int]]:
    items: list[dict] = []
    skipped: list[int] = []
    for index, cue in enumerate(cues):
        raw = str(cue.get("trans") or "").strip() or str(cue.get("text") or "").strip()
        text = _clean_text(raw)
        if not text or not any(ch.isalpha() for ch in text) or _cjk_ratio(text) > 0.25:
            skipped.append(index)
            continue
        items.append({"id": index, "text": text[:600]})
    return items, skipped


def _synthesize(
    items: list[dict],
    work_dir: Path,
    voice: str,
    cancel_event,
    progress,
) -> tuple[dict[int, Path], str]:
    if not items:
        return {}, ""
    python = _tts_python()
    if not python:
        return {}, "Không tìm thấy Python của Tool V1 có capcut_tts_api để chạy TTS Chí Mai."
    dub_dir = Path(work_dir) / "dub"
    dub_dir.mkdir(parents=True, exist_ok=True)
    request_path = dub_dir / "tts_request.json"
    result_path = dub_dir / "tts_result.json"
    request_path.write_text(
        json.dumps({"voice": voice, "out_dir": str(dub_dir), "items": items, "workers": 4}, ensure_ascii=False),
        encoding="utf-8",
    )
    if result_path.exists():
        result_path.unlink()
    if progress:
        progress(0, len(items))
    proc = subprocess.Popen(
        [python, str(_WORKER), str(request_path), str(result_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    deadline = time.time() + max(150.0, 8.0 * math.ceil(len(items) / 4))
    stdout = b""
    stderr = b""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            proc.kill()
            proc.communicate()
            raise Cancelled()
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            break
        if time.time() > deadline:
            proc.kill()
            proc.communicate()
            return {}, f"TTS Chí Mai quá thời gian ({len(items)} câu)."
        time.sleep(0.3)
    if not result_path.exists():
        tail = stderr.decode("utf-8", "replace").strip()[-300:]
        return {}, f"TTS Chí Mai lỗi: {tail or 'không có kết quả'}"
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, f"TTS Chí Mai trả kết quả không đọc được: {exc}"
    files: dict[int, Path] = {}
    failures: list[str] = []
    for item in data.get("results") or []:
        if item.get("file"):
            path = Path(item["file"])
            if path.is_file() and path.stat().st_size > 128:
                files[int(item["id"])] = path
                continue
        if item.get("error") and item.get("error") != "empty":
            failures.append(f"cue {item.get('id')}: {item['error']}")
    if progress:
        progress(len(items), len(items))
    if not files and failures:
        return {}, "TTS Chí Mai lỗi: " + "; ".join(failures[:3])
    if failures:
        return files, f"{len(failures)} cue lỗi TTS: " + "; ".join(failures[:3])
    return files, ""


def _clip_filters(
    cue: dict,
    cues: list[dict],
    cue_index: int,
    clip_path: Path,
    dub_volume: float,
    order: int,
    input_index: int,
) -> str:
    start = max(0.0, float(cue.get("start") or 0.0))
    end = max(start + 0.2, float(cue.get("end") or start + 0.2))
    next_start = math.inf
    for other in range(cue_index + 1, len(cues)):
        other_start = float(cues[other].get("start") or 0.0)
        if other_start > start:
            next_start = other_start
            break
    available = max(0.25, min(end, next_start) - start)
    clip_duration = _media_duration(clip_path)
    filters = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    if clip_duration > available * 1.02:
        chain = _atempo_chain(clip_duration / available)
        if chain:
            filters.append(chain)
    filters.append(f"atrim=0:{available:.3f}")
    filters.append("asetpts=N/SR/TB")
    ms = int(round(start * 1000))
    filters.append(f"adelay={ms}|{ms}")
    filters.append(f"volume={dub_volume:.3f}")
    return f"[{input_index}:a]" + ",".join(filters) + f"[dub{order}]"


def _mux(
    video_path: Path,
    cues: list[dict],
    files: dict[int, Path],
    output_path: Path,
    duration: float,
    base_volume: float,
    dub_volume: float,
    cancel_event,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    has_audio = _has_audio(video_path)
    ordered_clips = sorted(files)
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(video_path)]
    for cue_index in ordered_clips:
        args += ["-i", str(files[cue_index])]
    audio_input_index = None
    if not has_audio:
        args += ["-f", "lavfi", "-t", f"{max(0.1, duration):.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_input_index = len(ordered_clips) + 1

    lines: list[str] = [
        f"[{audio_input_index}:a]anull[abase]" if not has_audio
        else "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
             f"volume={base_volume:.3f}[abase]"
    ]
    labels: list[str] = []
    for order, cue_index in enumerate(ordered_clips):
        lines.append(
            _clip_filters(
                cues[cue_index], cues, cue_index, files[cue_index], dub_volume, order, order + 1
            )
        )
        labels.append(f"dub{order}")
    mix_inputs = "".join(f"[{label}]" for label in ["abase"] + labels)
    lines.append(
        f"{mix_inputs}amix=inputs={len(labels) + 1}:duration=first:normalize=0:dropout_transition=0,"
        "alimiter=limit=0.95[aout]"
    )
    filter_path = output_path.parent / "dub_filter.txt"
    filter_path.write_text(";\n".join(lines), encoding="utf-8")
    args += ["-filter_complex_script", str(filter_path), "-map", "0:v:0", "-map", "[aout]"]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output_path)]

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.communicate()
                raise Cancelled()
            time.sleep(0.2)
        out, err = proc.communicate()
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    result = run(args)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip()[-400:]
        # Codec video lạ không copy được vào MP4: thử mã hóa lại.
        retry = list(args)
        retry[retry.index("copy")] = "libx264"
        insert_at = retry.index("-movflags")
        retry[insert_at:insert_at] = ["-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
        result = run(retry)
        if result.returncode != 0:
            tail2 = result.stderr.decode("utf-8", "replace").strip()[-400:]
            raise RuntimeError(f"Ghép video lồng tiếng thất bại: {tail2 or tail}")


def run_dubbing(
    video_path: Path,
    cues: list[dict],
    work_dir: Path,
    duration: float,
    output_dir: str,
    voice: str = DEFAULT_VOICE,
    voice_label: str = DEFAULT_VOICE_LABEL,
    source_name: str = "",
    cancel_event=None,
    progress=None,
) -> dict:
    started = time.time()
    empty = {
        "output": "",
        "local": "",
        "cues": 0,
        "total": 0,
        "voice": voice,
        "voice_label": voice_label,
        "seconds": 0.0,
        "error": "",
    }
    if not cues:
        return {**empty, "error": "Không có cue để lồng tiếng."}
    items, skipped = _pick_texts(cues)
    empty["total"] = len(items)
    if not items:
        return {**empty, "error": "Không có câu tiếng Việt/La-tinh nào để lồng tiếng."}

    def tts_progress(done: int, total: int) -> None:
        if progress:
            progress(0.05 + 0.65 * (done / max(1, total)), f"Đang tạo giọng Chí Mai {done}/{total} câu…")

    files, tts_error = _synthesize(items, work_dir, voice, cancel_event, tts_progress)
    if not files:
        return {**empty, "seconds": round(time.time() - started, 1), "error": tts_error or "TTS thất bại."}
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()
    if progress:
        progress(0.75, "Đang ghép giọng vào video…")
    local_path = Path(work_dir) / "dub" / "dubbed.mp4"
    try:
        _mux(
            video_path,
            cues,
            files,
            local_path,
            duration,
            DEFAULT_BASE_VOLUME,
            DEFAULT_DUB_VOLUME,
            cancel_event,
        )
    except Cancelled:
        raise
    except Exception as exc:
        return {
            **empty,
            "cues": len(files),
            "seconds": round(time.time() - started, 1),
            "error": str(exc) or "Ghép video lồng tiếng thất bại.",
        }
    if progress:
        progress(0.95, "Đang lưu video lồng tiếng…")
    output_path = ""
    copy_error = ""
    try:
        folder = Path(output_dir or DEFAULT_OUTPUT_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        target = unique_output(
            folder,
            f"Dubbed_{safe_stem(source_name or Path(video_path).name, fallback=Path(work_dir).name)}.mp4",
        )
        shutil.copy2(local_path, target)
        output_path = str(target)
    except Exception as exc:
        copy_error = f"Không lưu được vào {output_dir}: {exc}"
    if progress:
        progress(1.0, "Đã tạo video lồng tiếng.")
    error = tts_error or copy_error
    return {
        "output": output_path,
        "local": str(local_path),
        "cues": len(files),
        "total": len(items),
        "skipped": len(skipped),
        "voice": voice,
        "voice_label": voice_label,
        "seconds": round(time.time() - started, 1),
        "error": error,
    }
