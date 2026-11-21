"""Đẩy kết quả OCR + bản dịch của SubExtract sang Tool V1 để lồng tiếng Việt và che
phụ đề cứng ngôn ngữ gốc. Tool V1 tự render, SubExtract chỉ gửi và theo dõi tiến độ."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .settings import load_settings

DEFAULT_TOOL_V1_URL = "http://127.0.0.1:8088"
DEFAULT_ROIS: list[list[float]] = [[0.0, 0.68, 1.0, 0.32]]
SUPPORTED_TARGETS = ("vi", "en", "zh")

IMPORT_TIMEOUT = 300.0
SHORT_TIMEOUT = 20.0


class HandoffError(RuntimeError):
    def __init__(self, message: str, *, safe_to_retry: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.safe_to_retry = safe_to_retry
        self.http_status = http_status


class HandoffQueueFull(HandoffError):
    pass


def resolve_url(value: Any) -> str:
    """Normalize a loopback HTTP(S) origin for the fixed handoff endpoints."""
    try:
        if value is None:
            return DEFAULT_TOOL_V1_URL
        if not isinstance(value, str):
            raise ValueError
        text = value.strip()
        if not text:
            return DEFAULT_TOOL_V1_URL
        # urlsplit can strip controls or hide empty query/fragment delimiters.
        if (any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in text)
                or any(char in text for char in "\\?#%")):
            raise ValueError
        parsed = urllib.parse.urlsplit(text if "://" in text else "http://" + text)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in ("http", "https") or parsed.path not in ("", "/")
                or parsed.username is not None or not host or port == 0):
            raise ValueError
        authority_host = f"[{host}]" if ":" in host else host
        authority = parsed.netloc.lower()
        # Reject an empty port or text that urlsplit ignores after a closing bracket.
        if (authority != authority_host
                and (port is None or not authority.startswith(authority_host + ":"))):
            raise ValueError
        if host != "localhost":
            address = ipaddress.ip_address(host)
            if not address.is_loopback:
                raise ValueError
            authority_host = f"[{address}]" if address.version == 6 else str(address)
        port_text = f":{port}" if port is not None else ""
        return f"{parsed.scheme}://{authority_host}{port_text}"
    except ValueError:
        raise HandoffError(
            "Tool V1 phải là URL HTTP(S) vòng lặp (localhost, IPv4 hoặc IPv6), không kèm đường dẫn."
        ) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Reject the response before urllib can inspect or follow Location.
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _control_token(base: str) -> str:
    """Read the configured Tool V1 credential only for its paired origin."""
    settings = load_settings()
    token_file = settings.get("tool_v1_token_file")
    paired_origin = settings.get("tool_v1_token_origin")
    if not token_file or not paired_origin or resolve_url(base) != resolve_url(paired_origin):
        return ""
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except (OSError, ValueError, TypeError) as exc:
        raise HandoffError("Không đọc được khóa kết nối Tool V1 trên máy. Hãy ghép nối lại.", safe_to_retry=True) from exc
    if len(token) < 16 or any(not 33 <= ord(char) <= 126 for char in token):
        raise HandoffError("Khóa kết nối Tool V1 không hợp lệ. Hãy ghép nối lại.", safe_to_retry=True)
    return token


def _request(base: str, path: str, payload: dict | None = None,
             timeout: float = SHORT_TIMEOUT, *, expected_status: int | None = None) -> dict:
    url = base + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    token = _control_token(base)
    if token:
        headers["X-Local-Control-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if expected_status is not None:
                raise HandoffError("Tool V1 trả phản hồi không phù hợp khi kiểm tra quyền kết nối.")
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        with exc:
            if exc.code == expected_status:
                return {"probe_status": exc.code}
            if 300 <= exc.code < 400:
                raise HandoffError(f"Tool V1 không được chuyển hướng HTTP ({exc.code}).") from exc
            detail = exc.reason or ""
            try:
                error = json.loads(exc.read().decode("utf-8", errors="replace"))
                if isinstance(error, dict):
                    detail = error.get("detail", detail)
            except (OSError, ValueError):
                pass
        if token:
            detail = str(detail).replace(token, "[redacted]")
        raise HandoffError(f"Tool V1 từ chối ({exc.code}): {str(detail)[:220]}",
                           safe_to_retry=exc.code in (401, 403, 404), http_status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        if isinstance(reason, ConnectionRefusedError):
            reason_text = "không có dịch vụ nào nghe ở cổng này"
        elif isinstance(reason, TimeoutError):
            reason_text = "hết thời gian chờ"
        else:
            reason_text = str(reason)[:120]
        raise HandoffError(
            f"Không gọi được Tool V1 ở {url}. Dashboard Tool V1 chưa chạy? — {reason_text}",
            safe_to_retry=isinstance(reason, ConnectionRefusedError),
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


def connection(base: str) -> dict:
    remote = info(base)
    if (not isinstance(remote, dict) or remote.get("ok") is not True
            or type(remote.get("version")) is not int or remote["version"] != 1):
        raise HandoffError("Tool V1 chưa hỗ trợ kết nối nhận phụ đề phiên bản 1.")
    # A unique nonexistent API route exercises the POST authentication middleware
    # and returns 404 after authorization. It cannot import a project or render.
    probe = _request(resolve_url(base), "/api/handoff/_connection_probe_" + uuid.uuid4().hex,
                     {}, expected_status=404)
    if not isinstance(probe, dict) or probe.get("probe_status") != 404:
        raise HandoffError("Không xác minh được quyền gửi sang Tool V1.")
    return remote


def import_and_render(base: str, payload: dict, options: dict | None = None) -> dict:
    """Nạp phụ đề vào Studio rồi bật render; trả về cả hai phản hồi của Tool V1."""
    url = resolve_url(base)
    imported = _request(url, "/api/handoff/import", payload, timeout=IMPORT_TIMEOUT)
    render_body = {"stem": imported.get("stem")}
    for key, value in (options or {}).items():
        if value not in (None, ""):
            render_body[key] = value
    try:
        started = _request(url, "/api/handoff/render", render_body, timeout=SHORT_TIMEOUT)
    except HandoffError as exc:
        if exc.http_status == 429:
            # The render was not admitted; at most an isolated import exists.
            raise HandoffQueueFull(str(exc), safe_to_retry=True) from exc
        # Import already had side effects; do not automatically repeat the send.
        exc.safe_to_retry = False
        raise
    return {"imported": imported, "render": started, "base_url": url}


def status(base: str, remote_job_id: str) -> dict:
    path = "/api/handoff/status?job_id=" + urllib.parse.quote(str(remote_job_id), safe="")
    return _request(resolve_url(base), path)


def cancel(base: str, remote_job_id: str) -> dict:
    return _request(resolve_url(base), "/api/handoff/cancel", {"job_id": remote_job_id})
