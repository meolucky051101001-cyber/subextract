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
from .post import build_cues, clean_ocr_lines, fix_ocr_text, ocr_text_key
from .translate import TARGET_LABELS, translate_cues


MAX_GROUP_SECONDS = 12.0


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
    spread: list[tuple[float, np.ndarray]] = field(default_factory=list)
    spread_step: float = 1.0
    window_continuation: bool = False


def _add_best(group: _Group, sharp: float, t: float, frame: np.ndarray) -> None:
    if len(group.best) < 2:
        group.best.append((sharp, t, frame.copy()))
    elif sharp > group.best[-1][0]:
        group.best[-1] = (sharp, t, frame.copy())
    else:
        return
    group.best.sort(key=lambda item: -item[0])


def _add_spread(group: _Group, t: float, frame: np.ndarray) -> None:
    # Keep the first frame, regularly spaced checkpoints, and a moving tail.
    # Compact checkpoints online instead of freezing once the buffer is full.
    if len(group.spread) >= 24 and group.spread[-1][0] - group.spread[-2][0] >= group.spread_step:
        group.spread = group.spread[:-1:2] + group.spread[-1:]
        group.spread_step *= 2.0
    sample = (t, frame.copy())
    if len(group.spread) < 2 or group.spread[-1][0] - group.spread[-2][0] >= group.spread_step:
        group.spread.append(sample)
    else:
        group.spread[-1] = sample


def _temporal_candidates(candidates: list[dict], limit: int = 16) -> list[dict]:
    # Best/sharp frames can duplicate spread frames or cluster near the start.
    # Deduplicate timestamps, then retain coverage including both endpoints.
    unique = {}
    for candidate in candidates:
        unique.setdefault(float(candidate.get("t", 0.0)), candidate)
    ordered = [unique[t] for t in sorted(unique)]
    if len(ordered) <= limit:
        return ordered
    indices = np.linspace(0, len(ordered) - 1, limit).round().astype(int)
    return [ordered[int(index)] for index in indices]


def _same_line_position(a: tuple, b: tuple) -> bool:
    width = min(a[2] - a[0], b[2] - b[0])
    height = min(a[3] - a[1], b[3] - b[1])
    overlap_x = min(a[2], b[2]) - max(a[0], b[0])
    overlap_y = min(a[3], b[3]) - max(a[1], b[1])
    return width > 0 and height > 0 and overlap_x >= 0.5 * width and overlap_y >= 0.6 * height


def _filter_static_lines(groups: list[dict], duration: float, min_conf: float, interval: float) -> None:
    # A repeated reply is not a watermark. Require the same text AND box in
    # one ROI, continuously present for almost the entire video, alongside
    # changing text. The visual static mask remains the primary logo filter.
    regions: dict[int, list[dict]] = defaultdict(list)
    for group in groups:
        regions[group["region"]].append(group)
    for region_groups in regions.values():
        if len(region_groups) < 12:
            continue
        occurrences = defaultdict(list)
        neighbours = defaultdict(list)
        for group in region_groups:
            seen = {(line["text"].casefold(), tuple(line["box"]))
                    for line in group["lines"]
                    if line["conf"] >= min_conf and "box" in line}
            for key in seen:
                occurrences[key].append((float(group["start"]), float(group["end"])))
                x1, y1, x2, y2 = key[1]
                for text, box in seen:
                    separate = box[2] <= x1 or box[0] >= x2 or box[3] <= y1 or box[1] >= y2
                    if text != key[0] and separate:
                        # Dialogue widths change with their text. Match the
                        # same line by overlap, while keeping columns apart.
                        track = next((texts for ref, texts in neighbours[key]
                                      if _same_line_position(ref, box)), None)
                        if track is None:
                            neighbours[key].append((box, {text}))
                        else:
                            track.add(text)
        static = set()
        for key, spans in occurrences.items():
            # Sequential replies at the same subtitle position are not a
            # changing neighbour: both lines must have been present together.
            changing_neighbour = any(len(texts) >= 2 for _box, texts in neighbours[key])
            if len(spans) < max(6, 0.9 * len(region_groups)) or not changing_neighbour:
                continue
            spans.sort()
            start, end = spans[0]
            covered = end - start
            longest = 0.0
            run_start = start
            gap_limit = min(0.25, interval * 1.1)
            for start, stop in spans[1:]:
                if start > end + gap_limit:
                    longest = max(longest, end - run_start)
                    run_start = start
                covered += max(0.0, stop - max(start, end))
                end = max(end, stop)
            longest = max(longest, end - run_start)
            if covered >= 0.9 * duration and longest >= 0.9 * duration:
                static.add(key)
        for group in region_groups:
            group["lines"] = [line for line in group["lines"]
                              if (line["text"].casefold(), tuple(line.get("box", ()))) not in static]


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
        merge_into = None
        index = len(groups_meta)
        if end - group.start < 0.3:
            if not group.window_continuation:
                return
            # An artificial split must not discard its short continuation at
            # EOF or at the next detector change. OCR it with the prior window
            # so equal text can merge before minimum-duration handling.
            previous = next((i for i in range(len(groups_meta) - 1, -1, -1)
                             if groups_meta[i]["region"] == group.ri), None)
            if previous is None or abs(groups_meta[previous]["end"] - group.start) > 1e-6:
                return
            index = previous
            merge_into = groups_meta[previous]
        min_line_h = max(12.0, group.region_w * 0.013)
        candidates = []
        slot = len(merge_into["candidates"]) if merge_into is not None else 0
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
            if merge_into is not None:
                merge_into["end"] = end
                merge_into["candidates"].extend(candidates)
            else:
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
            elif t - cur.start < MAX_GROUP_SECONDS and same_state(cur.ref, det):
                cur.last_t = t
                _add_best(cur, det.sharp, t, sub_frame)
                _add_spread(cur, t, sub_frame)
            else:
                # Bound OCR spacing even when detector geometry never changes.
                # The boundary frame starts the next window; build_cues merges
                # adjacent windows again when their recognized text is equal.
                window_continuation = t - cur.start >= MAX_GROUP_SECONDS
                close_group(cur, t)
                curs[ri] = _Group(
                    start=t, ref=det, last_t=t, best=[],
                    region_w=regions[ri][2], stable=stable_regions[ri], ri=ri,
                    window_continuation=window_continuation,
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
                candidates = _temporal_candidates(candidates)
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
                    text = "\n".join(fix_ocr_text(row) for row in clean_ocr_lines(
                        text, chinese_only=set(params.langs) == {"zh"},
                    ))
                    if not text:
                        continue
                    lines.append({
                        "text": text,
                        "conf": conf,
                        "y": box[1],
                        "ny": round(box[1] / max(1, plan.out_h), 1),
                        "box": tuple(box),
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

        _filter_static_lines(ocr_groups, info.duration, params.min_conf, interval)

        cues = build_cues(
            ocr_groups,
            video_duration=info.duration,
            min_conf=params.min_conf,
            merge_gap=params.merge_gap,
            min_duration=params.min_duration,
            chinese_only=set(params.langs) == {"zh"},
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
            "untranslated_indices": untranslated_indices,
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
