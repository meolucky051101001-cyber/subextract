from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator

import numpy as np


class FFmpegError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    codec: str


@dataclass
class StreamPlan:
    crop: tuple[int, int, int, int]  # x, y, w, h (pixels, even aligned)
    out_w: int
    out_h: int
    sample_fps: float
    est_frames: int


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise FFmpegError(f"Không tìm thấy {tool} trong PATH")
    return path


def probe_video(path: str | Path) -> VideoInfo:
    ffprobe = _need("ffprobe")
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,codec_name,r_frame_rate:stream_side_data=rotation:stream_tags=rotate:format=duration",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.strip() or "ffprobe thất bại")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"Không parse được ffprobe output: {exc}") from exc

    streams = data.get("streams") or []
    if not streams:
        raise FFmpegError("File không có stream video")
    s = streams[0]
    width, height = int(s["width"]), int(s["height"])

    rotation = 0
    for sd in s.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = int(round(float(sd["rotation"])))
            break
    if not rotation and (s.get("tags") or {}).get("rotate"):
        rotation = int(s["tags"]["rotate"])
    if abs(rotation) % 180 == 90:
        width, height = height, width

    duration = float((data.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        duration = float(s.get("duration") or 0)
    if duration <= 0:
        raise FFmpegError("Không đọc được thời lượng video")

    try:
        fps = float(Fraction(s.get("r_frame_rate") or "25/1"))
    except (ZeroDivisionError, ValueError):
        fps = 25.0
    if fps <= 0 or fps > 1000:
        fps = 25.0

    return VideoInfo(duration=duration, width=width, height=height, fps=fps, codec=s.get("codec_name", "unknown"))


def _even(value: int, minimum: int = 2) -> int:
    value -= value % 2
    return max(minimum, value)


def roi_to_pixels(info: VideoInfo, roi: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    x = int(round(roi[0] * info.width))
    y = int(round(roi[1] * info.height))
    w = int(round(roi[2] * info.width))
    h = int(round(roi[3] * info.height))
    x = max(0, min(x, info.width - 4))
    y = max(0, min(y, info.height - 4))
    w = max(8, min(w, info.width - x))
    h = max(8, min(h, info.height - y))
    x = _even(x, 0)
    y = _even(y, 0)
    w = _even(w)
    h = _even(h)
    return x, y, w, h


def make_plan(info: VideoInfo, roi: tuple[float, float, float, float], sample_fps: float, max_width: int = 1280) -> StreamPlan:
    x, y, w, h = roi_to_pixels(info, roi)
    out_w = _even(min(max_width, w), 32)
    out_h = _even(int(round(h * out_w / w)), 16)
    est = int(round(info.duration * sample_fps)) + 1
    return StreamPlan(crop=(x, y, w, h), out_w=out_w, out_h=out_h, sample_fps=sample_fps, est_frames=est)


@dataclass
class MultiPlan:
    plan: StreamPlan
    # Các vùng con (pixel, even-aligned) theo tọa độ của khung đã crop/scale.
    regions: list[tuple[int, int, int, int]]


def make_multi_plan(
    info: VideoInfo,
    rois: list[tuple[float, float, float, float]],
    sample_fps: float,
    max_width: int = 1280,
) -> MultiPlan:
    """Dựng plan crop theo HỢP của các vùng + tọa độ từng vùng con.

    Stream video đúng 1 lần cho mọi vùng: crop theo khung bao quanh tất cả
    các vùng, rồi mỗi khung được cắt ra từng vùng con để detect riêng. Với
    một vùng duy nhất, plan cho ra giống hệt make_plan (tương thích ngược).
    """
    rects = [roi_to_pixels(info, tuple(r)) for r in rois]
    ux1 = min(r[0] for r in rects)
    uy1 = min(r[1] for r in rects)
    ux2 = max(r[0] + r[2] for r in rects)
    uy2 = max(r[1] + r[3] for r in rects)
    uw, uh = ux2 - ux1, uy2 - uy1

    out_w = _even(min(max_width, uw), 32)
    out_h = _even(int(round(uh * out_w / uw)), 16)
    plan = StreamPlan(
        crop=(ux1, uy1, uw, uh),
        out_w=out_w,
        out_h=out_h,
        sample_fps=sample_fps,
        est_frames=int(round(info.duration * sample_fps)) + 1,
    )

    scale_x = out_w / uw
    scale_y = out_h / uh
    regions: list[tuple[int, int, int, int]] = []
    for x, y, w, h in rects:
        rx = int(round((x - ux1) * scale_x))
        ry = int(round((y - uy1) * scale_y))
        rw = int(round(w * scale_x))
        rh = int(round(h * scale_y))
        rx = max(0, min(rx, out_w - 8))
        ry = max(0, min(ry, out_h - 8))
        rw = max(8, min(rw, out_w - rx))
        rh = max(8, min(rh, out_h - ry))
        regions.append((rx, ry, rw, rh))
    return MultiPlan(plan=plan, regions=regions)


def stream_roi_frames(
    path: str | Path,
    plan: StreamPlan,
    cancel_event=None,
) -> Iterator[tuple[float, np.ndarray]]:
    ffmpeg = _need("ffmpeg")
    x, y, w, h = plan.crop
    vf = (
        f"crop={w}:{h}:{x}:{y},"
        f"fps={plan.sample_fps},"
        f"scale={plan.out_w}:{plan.out_h}:flags=bicubic"
    )
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-i", str(path),
        "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ]
    frame_bytes = plan.out_w * plan.out_h * 3
    err_file = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file, bufsize=frame_bytes * 4)
    index = 0
    try:
        assert proc.stdout is not None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                break
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(plan.out_h, plan.out_w, 3)
            yield index / plan.sample_fps, frame
            index += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        err_file.seek(0)
        stderr = err_file.read().decode("utf-8", errors="replace").strip()
        err_file.close()
        if proc.returncode not in (0, -9, None) and index == 0:
            raise FFmpegError(stderr or f"ffmpeg thoát với mã {proc.returncode}")
