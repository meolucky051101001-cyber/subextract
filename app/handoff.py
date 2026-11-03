"""Đẩy kết quả OCR + bản dịch của SubExtract sang Tool V1 để lồng tiếng Việt và che
phụ đề cứng ngôn ngữ gốc. Tool V1 tự render, SubExtract chỉ gửi và theo dõi tiến độ."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TOOL_V1_URL = "http://127.0.0.1:8088"
DEFAULT_ROIS: list[list[float]] = [[0.0, 0.68, 1.0, 0.32]]
SUPPORTED_TARGETS = ("vi", "en", "zh")

IMPORT_TIMEOUT = 300.0
SHORT_TIMEOUT = 20.0


class HandoffError(RuntimeError):
    pass


def resolve_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return DEFAULT_TOOL_V1_URL
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    host = text.split("://", 1)[1].split("/")[0]
    if not host or not any(part in host for part in ("127.0.0.1", "localhost", "::1")):
        raise HandoffError("Tool V1 phải là địa chỉ vòng lặp (127.0.0.1 hoặc localhost).")
    return text


def _request(base: str, path: str, payload: dict | None = None,
             timeout: float = SHORT_TIMEOUT) -> dict:
    url = base + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("detail", "")
        except (OSError, ValueError):
            detail = exc.reason or ""
        raise HandoffError(f"Tool V1 từ chối ({exc.code}): {str(detail)[:220]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        if isinstance(reason, ConnectionRefusedError):
            reason_text = "không có dịch vụ nào nghe ở cổng này"
        elif isinstance(reason, TimeoutError):
            reason_text = "hết thời gian chờ"
        else:
            reason_text = str(reason)[:120]
        raise HandoffError(
            f"Không gọi được Tool V1 ở {url}. Dashboard Tool V1 chưa chạy? — {reason_text}"
        ) from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise HandoffError("Tool V1 trả về nội dung không phải JSON.") from exc


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _box_for(region: Any, rois: list[list[float]]) -> list[float]:
    try:
        index = int(region)
    except (TypeError, ValueError):
        index = 0
    if index < 0 or index >= len(rois):
        index = 0
    x, y, w, h = [float(v) for v in (rois[index] if len(rois[index]) == 4 else DEFAULT_ROIS[0])]
    x1 = _clamp(x, 0.0, 0.99)
    y1 = _clamp(y, 0.0, 0.99)
    x2 = _clamp(x + w, x1 + 0.01, 1.0)
    y2 = _clamp(y + h, y1 + 0.01, 1.0)
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def _main_y(rois: list[list[float]]) -> float:
    widest = max(rois, key=lambda r: float(r[2]) * float(r[3])) if rois else DEFAULT_ROIS[0]
    return round(_clamp(float(widest[1]) + float(widest[3]) / 2.0, 0.05, 0.99), 4)


def build_payload(job: Any, target_lang: str = "") -> dict:
    stats = job.stats or {}
    info = job.info or {}
    rois: list[list[float]] = []
    for item in stats.get("rois") or DEFAULT_ROIS:
        if isinstance(item, (list, tuple)) and len(item) == 4:
            rois.append([float(v) for v in item])
    if not rois:
        rois = DEFAULT_ROIS

    cues: list[dict] = []
    for cue in job.cues:
        text = str(cue.get("text") or "").strip()
        trans = str(cue.get("trans") or "").strip()
        if not text and not trans:
            continue
        cues.append({
            "start": float(cue.get("start") or 0.0),
            "end": float(cue.get("end") or 0.0),
            "text": text,
            "trans": trans,
            "box": _box_for(cue.get("region"), rois),
        })
    if not cues:
        raise HandoffError("Job chưa có cue nào để gửi sang Tool V1.")

    lang = (target_lang or str((job.params or {}).get("translate_to") or "vi")).strip().lower()
    if lang not in SUPPORTED_TARGETS:
        raise HandoffError(f"Ngôn ngữ đích '{lang}' chưa hỗ trợ — chọn vi, en hoặc zh.")

    return {
        "stem": Path(job.filename).stem,
        "source_video": str(job.video_path),
        "source_job_id": job.id,
        "source_filename": job.filename,
        "video_width": int(stats.get("video_width") or info.get("width") or 0),
        "video_height": int(stats.get("video_height") or info.get("height") or 0),
        "duration": float(stats.get("duration") or info.get("duration") or 0.0),
        "target_lang": lang,
        "main_y_pct": _main_y(rois),
        "cues": cues,
    }


def info(base: str) -> dict:
    return _request(resolve_url(base), "/api/handoff/info")


def import_and_render(base: str, payload: dict, options: dict | None = None) -> dict:
    """Nạp phụ đề vào Studio rồi bật render; trả về cả hai phản hồi của Tool V1."""
    url = resolve_url(base)
    imported = _request(url, "/api/handoff/import", payload, timeout=IMPORT_TIMEOUT)
    render_body = {"stem": imported.get("stem")}
    for key, value in (options or {}).items():
        if value not in (None, ""):
            render_body[key] = value
    started = _request(url, "/api/handoff/render", render_body, timeout=SHORT_TIMEOUT)
    return {"imported": imported, "render": started, "base_url": url}


def status(base: str, remote_job_id: str) -> dict:
    path = "/api/handoff/status?job_id=" + urllib.parse.quote(str(remote_job_id), safe="")
    return _request(resolve_url(base), path)


def cancel(base: str, remote_job_id: str) -> dict:
    return _request(resolve_url(base), "/api/handoff/cancel", {"job_id": remote_job_id})
