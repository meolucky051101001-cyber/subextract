from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from collections import defaultdict

import cv2
import numpy as np

from ..settings import get_gemini_keys, load_settings, log_usage
from .cancellation import Cancelled
from .detect import (
    detect_text,
    same_state,
    stable_ratio_map,
    update_stable_counts,
)
from .ffmpeg_io import make_multi_plan, probe_video, stream_roi_frames
from .ocr import get_engine, prepare_line
from .post import build_cues, clean_text, fix_ocr_text, ocr_text_key
from .translate import TARGET_LABELS, translate_cues


@dataclass
class RunParams:
    rois: tuple[tuple[float, float, float, float], ...] = ((0.0, 0.68, 1.0, 0.32),)
    sample_fps: float = 5.0
    langs: list[str] = field(default_factory=lambda: ["vi", "en", "zh"])
    min_conf: float = 0.28
    merge_gap: float = 0.45
    min_duration: float = 0.25
    max_width: int = 1280
    translate_to: str = ""
    translate_model: str = "gemini-3.5-flash-lite"
    dub: bool = False
    dub_voice: str = "BV562_streaming"
    dub_voice_label: str = "Chí Mai"
    dub_output_dir: str = r"D:\banve"
    source_name: str = ""


@dataclass
class _Group:
    start: float
    ref: object
    last_t: float
    best: list[tuple[float, float, np.ndarray]]
    region_w: int = 0
    stable: object = None
    ri: int = 0
    spread: list = field(default_factory=list)


def _add_best(group: _Group, sharp: float, t: float, frame: np.ndarray) -> None:
    if len(group.best) < 2:
        group.best.append((sharp, t, frame.copy()))
    elif sharp > group.best[-1][0]:
        group.best[-1] = (sharp, t, frame.copy())
    else:
        return
    group.best.sort(key=lambda item: -item[0])


def _add_spread(group: _Group, t: float, frame: np.ndarray) -> None:
    # Khung phủ đều theo thời gian: dùng cho cue dài để phát hiện chữ đổi
    # khi hộp phụ đề giữ nguyên. Bước giãn thích ứng (1s → 2s → 3s) để group
    # dài vẫn được phủ tới cuối mà không tốn quá nhiều bộ nhớ.
    count = len(group.spread)
    if count >= 24:
        return
    step = 1.0 if count < 8 else (2.0 if count < 16 else 3.0)
    if not group.spread or t - group.spread[-1][0] >= step:
        group.spread.append((t, frame.copy()))


def _same_text(a: tuple, b: tuple) -> bool:
    sa = " ".join(a)
    sb = " ".join(b)
    if not sa or not sb:
        return sa == sb
    return SequenceMatcher(None, sa, sb).ratio() >= 0.72


def _mean_conf(lines: list[dict]) -> float:
    if not lines:
        return -1.0
    return float(np.mean([line["conf"] for line in lines]))


def run_pipeline(
    video_path: Path,
    work_dir: Path,
    params: RunParams,
    progress,
    cancel_event,
    gpu: bool,
) -> dict:
    started = time.time()
    info = probe_video(video_path)
    multi = make_multi_plan(info, list(params.rois), params.sample_fps, params.max_width)
    plan = multi.plan
    regions = multi.regions
    crops_dir = Path(work_dir) / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    interval = 1.0 / params.sample_fps
    stable_counts = np.zeros((40, 160), dtype=np.uint32)
    profile_frames = 0
    frames_seen = 0
    groups_meta: list[dict] = []
    curs: list[_Group | None] = [None] * len(regions)
    last_report = 0.0

    def report(stage: str, value: float, message: str) -> None:
        progress(stage, max(0.0, min(1.0, value)), message)

    profile_fps = min(params.sample_fps, 2.5)
    profile_plan = make_multi_plan(info, list(params.rois), profile_fps, params.max_width).plan
    report("detect", 0.0, "Đang phân tích logo/watermark trong video…")
    for t, frame in stream_roi_frames(video_path, profile_plan, cancel_event):
        if cancel_event.is_set():
            raise Cancelled()
        update_stable_counts(stable_counts, detect_text(frame).mask)
        profile_frames += 1
        frac = min(1.0, t / max(1e-6, info.duration))
        report("detect", 0.16 * frac, f"Đang phân tích logo/watermark… {frac * 100:.0f}%")

    stable_small = stable_ratio_map(stable_counts, profile_frames)
    stable_full = cv2.resize(stable_small, (plan.out_w, plan.out_h), interpolation=cv2.INTER_NEAREST)
    stable_regions = [
        stable_full[ry:ry + rh, rx:rx + rw] for (rx, ry, rw, rh) in regions
    ]

    def close_group(group: _Group, end: float) -> None:
        end = min(float(info.duration), float(end))
        if end - group.start < 0.3:
            return
        min_line_h = max(12.0, group.region_w * 0.013)
        candidates = []
        index = len(groups_meta)
        slot = 0
        for _sharp, frame_t, frame in group.best:
            det = detect_text(frame, stable=group.stable)
            if not det.boxes:
                continue
            if max(b[3] - b[1] for b in det.boxes) < min_line_h:
                continue
            path = crops_dir / f"g{index:05d}_{slot}.jpg"
            ok, buf = cv2.imencode(
                ".jpg",
                cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                [cv2.IMWRITE_JPEG_QUALITY, 93],
            )
            if not ok:
                continue
            path.write_bytes(buf.tobytes())
            candidates.append({"path": str(path), "boxes": det.boxes, "t": frame_t})
            slot += 1
        # Cue dài: thêm khung phủ đều thời gian để bắt được lúc chữ đổi.
        if end - group.start > 4.0:
            for frame_t, frame in group.spread:
                det = detect_text(frame, stable=group.stable)
                if not det.boxes:
                    continue
                if max(b[3] - b[1] for b in det.boxes) < min_line_h:
                    continue
                path = crops_dir / f"g{index:05d}_{slot}.jpg"
                ok, buf = cv2.imencode(
                    ".jpg",
                    cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 93],
                )
                if not ok:
                    continue
                path.write_bytes(buf.tobytes())
                candidates.append({"path": str(path), "boxes": det.boxes, "t": frame_t})
                slot += 1
        if candidates:
            groups_meta.append({"start": group.start, "end": end, "candidates": candidates,
                                "region": group.ri})

    def feed_region(ri: int, t: float, det, sub_frame: np.ndarray) -> None:
        cur = curs[ri]
        if det.boxes:
            if cur is None:
                curs[ri] = _Group(
                    start=t, ref=det, last_t=t, best=[],
                    region_w=regions[ri][2], stable=stable_regions[ri], ri=ri,
                )
                _add_best(curs[ri], det.sharp, t, sub_frame)
                _add_spread(curs[ri], t, sub_frame)
            elif same_state(cur.ref, det):
                cur.last_t = t
                _add_best(cur, det.sharp, t, sub_frame)
                _add_spread(cur, t, sub_frame)
            else:
                close_group(cur, t)
                curs[ri] = _Group(
                    start=t, ref=det, last_t=t, best=[],
                    region_w=regions[ri][2], stable=stable_regions[ri], ri=ri,
                )
                _add_best(curs[ri], det.sharp, t, sub_frame)
                _add_spread(curs[ri], t, sub_frame)
        else:
            if cur is not None:
                close_group(cur, min(t, cur.last_t + interval))
                curs[ri] = None

    report("detect", 0.16, "Đang dò phụ đề trong video…")
    try:
        for t, frame in stream_roi_frames(video_path, plan, cancel_event):
            if cancel_event.is_set():
                raise Cancelled()
            frames_seen += 1
            for ri, (rx, ry, rw, rh) in enumerate(regions):
                sub = frame[ry:ry + rh, rx:rx + rw]
                det = detect_text(sub, stable=stable_regions[ri])
                feed_region(ri, t, det, sub)

            now = time.time()
            if now - last_report > 0.25:
                last_report = now
                frac = min(1.0, t / max(1e-6, info.duration))
                total_groups = sum(1 for g in groups_meta)
                report(
                    "detect",
                    0.16 + 0.39 * frac,
                    f"Đang dò phụ đề… {frac * 100:.0f}% · {total_groups} cue",
                )

        for ri in range(len(regions)):
            if curs[ri] is not None:
                close_group(curs[ri], min(float(info.duration), curs[ri].last_t + interval))
        if cancel_event.is_set():
            raise Cancelled()

        report("detect", 0.56, f"Tìm thấy {len(groups_meta)} đoạn có chữ. Đang nạp model OCR…")

        detect_seconds = time.time() - started

        if not groups_meta:
            stats = {
                "duration": round(info.duration, 2),
                "video_width": info.width,
                "video_height": info.height,
                "video_fps": round(info.fps, 3),
                "codec": info.codec,
                "sampled_frames": frames_seen,
                "detected_groups": 0,
                "cues": 0,
                "gpu": gpu,
                "detect_seconds": round(detect_seconds, 1),
                "ocr_seconds": 0.0,
                "translate_seconds": 0.0,
                "translate_to": params.translate_to,
                "translate_model": params.translate_model if params.translate_to else "",
                "translated_cues": 0,
                "translate_error": "",
                "untranslated_cues": 0,
                "untranslated_indices": [],
                "total_seconds": round(time.time() - started, 1),
                "languages": params.langs,
                "rois": [list(r) for r in params.rois],
                "sample_fps": params.sample_fps,
            }
            report("done", 1.0, "Không tìm thấy phụ đề trong vùng đã chọn.")
            return {"cues": [], "stats": stats, "info": {
                "duration": info.duration, "width": info.width, "height": info.height,
                "fps": info.fps, "codec": info.codec,
            }}

        ocr_started = time.time()
        gpu_label = "GPU" if gpu else "CPU"
        report("load_model", 0.57, f"Đang nạp model OCR ({gpu_label})…", )
        holder: dict = {}

        def _load_engine() -> None:
            holder["engine"] = get_engine(params.langs, gpu)

        loader = threading.Thread(target=_load_engine, daemon=True)
        loader.start()
        waited = 0.0
        while loader.is_alive():
            if cancel_event.is_set():
                raise Cancelled()
            time.sleep(0.5)
            waited += 0.5
            report(
                "load_model",
                min(0.595, 0.57 + 0.025 * min(1.0, waited / 20.0)),
                f"Đang nạp model OCR ({gpu_label})… {waited:.0f}s",
            )
        loader.join()
        engine = holder.get("engine")
        if engine is None:
            raise RuntimeError("Không nạp được model OCR")
        report("ocr", 0.6, "Đang OCR bằng " + gpu_label + "…")

        ocr_groups: list[dict] = []
        total = max(1, len(groups_meta))
        for index, group in enumerate(groups_meta):
            if cancel_event.is_set():
                raise Cancelled()
            duration = float(group["end"]) - float(group["start"])
            candidates = list(group["candidates"])
            long_cue = duration > 4.0
            if long_cue:
                # Cue dài: OCR đủ các khung phủ thời gian rồi tách theo lúc chữ đổi.
                candidates.sort(key=lambda c: c.get("t", 0.0))
                candidates = candidates[:16]
            else:
                candidates = candidates[:4]
            reads: list[tuple[float, list[dict], float]] = []
            for candidate in candidates:
                image = cv2.imread(candidate["path"], cv2.IMREAD_COLOR)
                if image is None:
                    continue
                rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                lines = []
                for box in candidate["boxes"]:
                    crop = prepare_line(rgb, box)
                    if crop.size == 0:
                        continue
                    text, conf, _reader = engine.read_line(crop)
                    text = fix_ocr_text(clean_text(text))
                    if not text:
                        continue
                    lines.append({
                        "text": text,
                        "conf": conf,
                        "y": box[1],
                        "ny": round(box[1] / max(1, plan.out_h), 1),
                    })
                lines.sort(key=lambda line: line["y"])
                score = _mean_conf(lines)
                reads.append((float(candidate.get("t") or 0.0), lines, score))
                if not long_cue and score >= 0.6:
                    break

            if long_cue and len(reads) >= 2:
                reads.sort(key=lambda r: r[0])
                runs: list[dict] = []
                for t, lines, score in reads:
                    key = ocr_text_key([line["text"] for line in lines])
                    if runs and runs[-1]["lines"] and _same_text(runs[-1]["key"], key):
                        runs[-1]["end"] = t
                        if score > runs[-1]["score"]:
                            runs[-1]["lines"], runs[-1]["score"] = lines, score
                    else:
                        runs.append({"key": key, "start": t, "end": t,
                                     "lines": lines, "score": score})
                runs = [r for r in runs if r["lines"]]
                for i, run in enumerate(runs):
                    run_end = runs[i + 1]["start"] if i + 1 < len(runs) else float(group["end"])
                    if run_end - run["start"] < params.min_duration:
                        run_end = min(float(group["end"]), run["start"] + params.min_duration)
                    ocr_groups.append({"start": run["start"], "end": run_end,
                                       "lines": run["lines"], "region": group["region"]})
            else:
                best = max(reads, key=lambda r: r[2]) if reads else None
                ocr_groups.append({
                    "start": group["start"], "end": group["end"],
                    "lines": best[1] if best else [], "region": group["region"],
                })
            report(
                "ocr",
                0.6 + 0.37 * ((index + 1) / total),
                f"OCR {index + 1}/{total} đoạn…",
            )

        # Loại các dòng chữ tĩnh lặp lại ở gần như mọi cue (logo/kênh truyền hình).
        if len(ocr_groups) >= 12:
            counts: dict[tuple[str, float], int] = defaultdict(int)
            for group in ocr_groups:
                seen = {
                    (line["text"].lower(), line["ny"])
                    for line in group["lines"]
                    if line["conf"] >= params.min_conf
                }
                for key in seen:
                    counts[key] += 1
            threshold = max(6, int(0.45 * len(ocr_groups)))
            for group in ocr_groups:
                group["lines"] = [
                    line for line in group["lines"]
                    if counts.get((line["text"].lower(), line["ny"]), 0) < threshold
                ]

        cues = build_cues(
            ocr_groups,
            video_duration=info.duration,
            min_conf=params.min_conf,
            merge_gap=params.merge_gap,
            min_duration=params.min_duration,
        )
        ocr_seconds = time.time() - ocr_started

        translated_cues = 0
        translate_total = 0
        translate_seconds = 0.0
        translate_error = ""
        translate_model_used = ""
        translate_fallback_from: list[str] = []
        untranslated_indices: list[int] = []
        if params.translate_to and cues:
            if cancel_event.is_set():
                raise Cancelled()
            label = TARGET_LABELS.get(params.translate_to, params.translate_to)
            report("translate", 0.93, f"Đang dịch {len(cues)} cue sang {label}…")
            api_keys = get_gemini_keys()

            def on_translate(done: int, total: int) -> None:
                report(
                    "translate",
                    0.93 + 0.03 * (done / max(1, total)),
                    f"Đang dịch {done}/{total} cue sang {label}…",
                )

            translation = translate_cues(
                cues,
                target=params.translate_to,
                api_key=api_keys,
                model=params.translate_model,
                cancel_event=cancel_event,
                progress=on_translate,
                glossary=load_settings().get("glossary"),
            )
            translated_cues = translation["translated"]
            translate_total = translation["total"]
            translate_seconds = translation["seconds"]
            translate_error = translation["error"]
            translate_model_used = str(translation.get("model") or params.translate_model)
            translate_fallback_from = list(translation.get("fallback_from") or [])
            untranslated_indices = translation.get("untranslated") or []
            if translate_fallback_from:
                report(
                    "translate",
                    0.96,
                    f"{translate_fallback_from[0]} không dùng được, đã chuyển sang dịch bằng {translate_model_used}.",
                )
            log_usage({
                "job": Path(work_dir).name,
                "model": translate_model_used,
                "target": params.translate_to,
                "chars": translation.get("chars") or 0,
                "cues": translated_cues,
                "error": translate_error[:80],
            })
        if cancel_event.is_set():
            raise Cancelled()

        dub_cues = 0
        dub_total = 0
        dub_seconds = 0.0
        dub_error = ""
        dub_output = ""
        dub_local = ""
        if params.dub and cues:
            from .dub import run_dubbing

            report("dub", 0.96, f"Đang lồng tiếng {params.dub_voice_label}…")

            def on_dub(value: float, message: str) -> None:
                report("dub", 0.96 + 0.03 * max(0.0, min(1.0, value)), message)

            try:
                dub_result = run_dubbing(
                    video_path=video_path,
                    cues=cues,
                    work_dir=work_dir,
                    duration=info.duration,
                    output_dir=params.dub_output_dir,
                    voice=params.dub_voice,
                    voice_label=params.dub_voice_label,
                    source_name=params.source_name,
                    cancel_event=cancel_event,
                    progress=on_dub,
                )
            except Cancelled:
                raise
            except Exception as exc:
                dub_result = {"error": str(exc) or "Lồng tiếng thất bại."}
            dub_cues = int(dub_result.get("cues") or 0)
            dub_total = int(dub_result.get("total") or 0)
            dub_seconds = float(dub_result.get("seconds") or 0.0)
            dub_error = str(dub_result.get("error") or "")
            dub_output = str(dub_result.get("output") or "")
            dub_local = str(dub_result.get("local") or "")
        if cancel_event.is_set():
            raise Cancelled()

        stats = {
            "duration": round(info.duration, 2),
            "video_width": info.width,
            "video_height": info.height,
            "video_fps": round(info.fps, 3),
            "codec": info.codec,
            "sampled_frames": frames_seen,
            "detected_groups": len(groups_meta),
            "cues": len(cues),
            "gpu": gpu,
            "detect_seconds": round(detect_seconds, 1),
            "ocr_seconds": round(ocr_seconds, 1),
            "translate_seconds": translate_seconds,
            "translate_to": params.translate_to,
            "translate_model": params.translate_model if params.translate_to else "",
            "translated_cues": translated_cues,
            "translate_total": translate_total,
            "translate_error": translate_error,
            "translate_model_used": translate_model_used,
            "translate_fallback_from": translate_fallback_from,
            "untranslated_cues": len(untranslated_indices),
            "untranslated_indices": untranslated_indices[:200],
            "dub_enabled": bool(params.dub),
            "dub_voice": params.dub_voice if params.dub else "",
            "dub_voice_label": params.dub_voice_label if params.dub else "",
            "dub_cues": dub_cues,
            "dub_total": dub_total,
            "dub_seconds": dub_seconds,
            "dub_error": dub_error,
            "dub_output": dub_output,
            "dub_local": dub_local,
            "total_seconds": round(time.time() - started, 1),
            "languages": params.langs,
            "rois": [list(r) for r in params.rois],
            "sample_fps": params.sample_fps,
        }
        if translate_error:
            done_message = (
                f"Hoàn tất: {len(cues)} cue · đã dịch {translated_cues} · "
                f"lỗi dịch: {translate_error[:160]}"
            )
        elif translated_cues:
            done_message = f"Hoàn tất: {len(cues)} cue · đã dịch {translated_cues} cue."
            if untranslated_indices:
                done_message = f"Hoàn tất: {len(cues)} cue · đã dịch {translated_cues} cue · {len(untranslated_indices)} cue chưa dịch."
        elif translate_total == 0 and params.translate_to:
            label = TARGET_LABELS.get(params.translate_to, params.translate_to)
            done_message = (
                f"Hoàn tất: {len(cues)} cue · phụ đề gốc đã là {label} nên không có gì để dịch — "
                "chọn ngôn ngữ đích khác nếu muốn dịch."
            )
        else:
            done_message = f"Hoàn tất: {len(cues)} cue phụ đề."
        if params.dub:
            if dub_output:
                done_message += f" · lồng tiếng {params.dub_voice_label} → {dub_output}"
            elif dub_error:
                done_message += f" · lỗi lồng tiếng: {dub_error[:160]}"
        report("done", 1.0, done_message)
        return {"cues": cues, "stats": stats, "info": {
            "duration": info.duration, "width": info.width, "height": info.height,
            "fps": info.fps, "codec": info.codec,
        }}
    finally:
        import shutil
        shutil.rmtree(crops_dir, ignore_errors=True)
