from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

Box = tuple[int, int, int, int]  # x1, y1, x2, y2


@dataclass
class Detection:
    gray: np.ndarray
    mask: np.ndarray
    boxes: list[Box]
    sharp: float
    sig: np.ndarray


_TOPHAT_KERNEL = cv2.getStructuringElement(cv2.MORPH_RECT, (17, 17))
_CLOSE_KERNEL = cv2.getStructuringElement(cv2.MORPH_RECT, (17, 3))
_OPEN_KERNEL = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))


def _merge_boxes(boxes: list[Box]) -> list[Box]:
    if not boxes:
        return []
    items = sorted(boxes, key=lambda r: (r[1], r[0]))
    changed = True
    while changed:
        changed = False
        out: list[Box] = []
        for b in items:
            merged = False
            for i, a in enumerate(out):
                overlap = min(a[3], b[3]) - max(a[1], b[1])
                min_h = min(a[3] - a[1], b[3] - b[1])
                x_overlap = min(a[2], b[2]) - max(a[0], b[0])
                min_w = min(a[2] - a[0], b[2] - b[0])
                if min_h > 0 and min_w > 0 and (
                    (overlap / min_h > 0.45 and max(a[0], b[0]) - min(a[2], b[2]) < 1.2 * max(a[3] - a[1], b[3] - b[1]))
                    or (overlap / min_h > 0.15 and x_overlap / min_w > 0.6)
                ):
                    out[i] = (
                        min(a[0], b[0]), min(a[1], b[1]),
                        max(a[2], b[2]), max(a[3], b[3]),
                    )
                    merged = True
                    changed = True
                    break
            if not merged:
                out.append(b)
        items = out
    return sorted(items, key=lambda r: (r[1], r[0]))


def _find_boxes(mask: np.ndarray, raw: np.ndarray) -> list[Box]:
    h_img, w_img = mask.shape[:2]
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rects: list[Box] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if h < max(8, h_img * 0.018) or h > h_img * 0.6:
            continue
        if w < max(14, w_img * 0.008):
            continue
        if w / max(h, 1) < 0.5:
            continue
        fill = cv2.countNonZero(mask[y:y + h, x:x + w]) / float(w * h)
        if fill < 0.05 or fill > 0.9:
            continue
        if w > w_img * 0.98 and fill > 0.6:
            continue

        raw_crop = raw[y:y + h, x:x + w]
        count, _, stats, _ = cv2.connectedComponentsWithStats(raw_crop, connectivity=8)
        heights: list[int] = []
        glyphs = 0
        for i in range(1, count):
            comp_h = int(stats[i, cv2.CC_STAT_HEIGHT])
            comp_w = int(stats[i, cv2.CC_STAT_WIDTH])
            if comp_w < 2 or stats[i, cv2.CC_STAT_AREA] < 6:
                continue
            heights.append(comp_h)
            if comp_h >= h * 0.25 and comp_w >= 2:
                glyphs += 1
        if glyphs < 2 or len(heights) < 2:
            continue
        if float(np.median(heights)) < h * 0.16:
            continue
        rects.append((x, y, x + w, y + h))
    return _merge_boxes(rects)


def _primary_boxes(boxes: list[Box]) -> list[Box]:
    if len(boxes) <= 1:
        return boxes
    tallest = max(b[3] - b[1] for b in boxes)
    min_h = max(10.0, tallest * 0.72)
    kept = [b for b in boxes if (b[3] - b[1]) >= min_h]
    if not kept:
        kept = [max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))]
    kept.sort(key=lambda b: (b[1], b[0]))
    return kept[:4]


def _split_multi_lines(box: Box, raw: np.ndarray) -> list[Box]:
    x1, y1, x2, y2 = box
    sub = raw[y1:y2, x1:x2]
    if sub.size == 0:
        return [box]
    rows = (sub > 0).sum(axis=1).astype(np.float32)
    peak = float(rows.max())
    if peak < 6:
        return [box]

    inactive = rows < max(2.0, peak * 0.28)
    for index in range(1, len(inactive) - 1):
        if inactive[index - 1] and inactive[index + 1]:
            inactive[index] = True

    gaps: list[tuple[int, int]] = []
    start = None
    for index, flag in enumerate(inactive):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            gaps.append((start, index))
            start = None
    if start is not None:
        gaps.append((start, len(inactive)))

    cuts: list[int] = []
    for top, bottom in gaps:
        if top <= 3 or bottom >= len(rows) - 3 or bottom - top < 4:
            continue
        if rows[:top].max(initial=0) < peak * 0.3 or rows[bottom:].max(initial=0) < peak * 0.3:
            continue
        cuts.append((top + bottom) // 2)
    if not cuts:
        return [box]

    bounds = [0] + cuts[:3] + [len(rows)]
    out: list[Box] = []
    for top, bottom in zip(bounds, bounds[1:]):
        segment = sub[top:bottom]
        cols = (segment > 0).sum(axis=0)
        xs = np.flatnonzero(cols > 0)
        if xs.size == 0:
            continue
        bx1 = x1 + int(xs[0])
        bx2 = x1 + int(xs[-1]) + 1
        by1 = y1 + top
        by2 = y1 + bottom
        if bx2 - bx1 < 8 or by2 - by1 < 8:
            continue
        out.append((bx1, by1, bx2, by2))
    return out or [box]


def _drop_static_boxes(boxes: list[Box], mask: np.ndarray, stable: np.ndarray, threshold: float = 0.85) -> list[Box]:
    kept: list[Box] = []
    for x1, y1, x2, y2 in boxes:
        crop_mask = mask[y1:y2, x1:x2]
        crop_stable = stable[y1:y2, x1:x2]
        if crop_mask.size == 0 or crop_stable.shape != crop_mask.shape:
            kept.append((x1, y1, x2, y2))
            continue
        selected = crop_mask > 0
        if not selected.any():
            kept.append((x1, y1, x2, y2))
            continue
        static_ratio = float((crop_stable[selected] >= threshold).mean())
        if static_ratio < 0.72:
            kept.append((x1, y1, x2, y2))
    return kept


def _sharpness(gray: np.ndarray, boxes: list[Box]) -> float:
    if not boxes:
        return 0.0
    scores = []
    h_img, w_img = gray.shape[:2]
    for x1, y1, x2, y2 in boxes:
        pad = max(2, (y2 - y1) // 4)
        crop = gray[max(0, y1 - pad):min(h_img, y2 + pad), max(0, x1 - pad):min(w_img, x2 + pad)]
        if crop.size == 0:
            continue
        scores.append(float(cv2.Laplacian(crop, cv2.CV_64F).var()))
    return float(np.mean(scores)) if scores else 0.0


def detect_text(frame_rgb: np.ndarray, stable: np.ndarray | None = None) -> Detection:
    gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
    tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, _TOPHAT_KERNEL)
    _, raw = cv2.threshold(tophat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, _CLOSE_KERNEL)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _OPEN_KERNEL)

    density = cv2.countNonZero(mask) / float(mask.size)
    if density < 0.0008 or density > 0.35:
        mask = np.zeros_like(mask)
        boxes: list[Box] = []
    else:
        boxes = []
        for box in _primary_boxes(_find_boxes(mask, raw)):
            boxes.extend(_split_multi_lines(box, raw))
        boxes.sort(key=lambda b: (b[1], b[0]))
        boxes = boxes[:4]

    if stable is not None:
        boxes = _drop_static_boxes(boxes, mask, stable)

    sig = cv2.resize(mask, (96, 32), interpolation=cv2.INTER_AREA)
    return Detection(gray=gray, mask=mask, boxes=boxes, sharp=_sharpness(gray, boxes), sig=sig)


def same_state(ref: Detection, cur: Detection, diff_threshold: float = 0.075) -> bool:
    if len(ref.boxes) != len(cur.boxes):
        return False
    diff = float(np.mean(cv2.absdiff(ref.sig, cur.sig))) / 255.0
    if diff > diff_threshold:
        return False
    for a, b in zip(ref.boxes, cur.boxes):
        min_h = min(a[3] - a[1], b[3] - b[1])
        overlap = min(a[3], b[3]) - max(a[1], b[1])
        if min_h <= 0 or overlap / min_h < 0.4:
            return False
        width_ratio = (a[2] - a[0]) / max(1, b[2] - b[0])
        if width_ratio < 0.6 or width_ratio > 1.7:
            return False
    return True


def update_stable_counts(counts: np.ndarray, mask: np.ndarray) -> None:
    small = cv2.resize(mask, (counts.shape[1], counts.shape[0]), interpolation=cv2.INTER_AREA)
    counts += (small > 80).astype(counts.dtype)


def stable_ratio_map(counts: np.ndarray, frames_seen: int) -> np.ndarray:
    if frames_seen <= 0:
        return np.zeros(counts.shape, dtype=np.float32)
    return counts.astype(np.float32) / float(frames_seen)
