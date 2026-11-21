from __future__ import annotations

import threading

import cv2
import numpy as np

from .post import clean_ocr_lines, han_ratio

Box = tuple[int, int, int, int]

_ENGINES: dict[tuple[str, ...], "OcrEngine"] = {}
_ENGINE_LOCK = threading.Lock()


def _normalize_langs(langs: list[str]) -> tuple[str, ...]:
    order = ("vi", "en", "zh")
    picked = [l for l in order if l in set(langs)]
    return tuple(picked)


def get_engine(langs: list[str], gpu: bool) -> "OcrEngine":
    key = (_normalize_langs(langs), gpu)
    with _ENGINE_LOCK:
        if key not in _ENGINES:
            _ENGINES[key] = OcrEngine(list(key[0]), gpu)
        return _ENGINES[key]


class OcrEngine:
    def __init__(self, langs: list[str], gpu: bool):
        import easyocr

        self.langs = langs
        self.gpu = gpu
        self.readers: dict[str, object] = {}
        # EasyOCR/torch không an toàn khi nhiều job cùng suy luận trên một reader.
        self._read_lock = threading.Lock()
        need_latin = any(l in langs for l in ("vi", "en"))
        need_zh = "zh" in langs
        if need_latin:
            self.readers["latin"] = easyocr.Reader(["vi"], gpu=gpu, verbose=False)
        if need_zh:
            self.readers["zh"] = easyocr.Reader(["ch_sim", "en"], gpu=gpu, verbose=False)

    def _read_with_recognizer(self, reader, gray: np.ndarray) -> tuple[str, float]:
        h, w = gray.shape[:2]
        try:
            results = reader.recognize(
                gray,
                horizontal_list=[[0, w, 0, h]],
                free_list=[],
                detail=1,
                paragraph=False,
            )
        except TypeError:
            results = reader.recognize(gray, horizontal_list=[[0, w, 0, h]], free_list=[])
        if not results:
            return "", 0.0
        item = results[0]
        text = str(item[1]) if len(item) > 2 else ""
        conf = float(item[2]) if len(item) > 2 else 0.0
        return text, conf

    def _read_fallback(self, reader, rgb: np.ndarray) -> tuple[str, float]:
        try:
            results = reader.readtext(rgb, detail=1, paragraph=False, mag_ratio=1.5)
        except Exception:
            return "", 0.0
        if not results:
            return "", 0.0
        items = sorted(results, key=lambda r: (min(p[1] for p in r[0]), min(p[0] for p in r[0])))
        if set(self.langs) == {"zh"}:
            # Keep physical rows separate: a numeric label on another row must
            # not become valid just because readtext also returned Chinese.
            rows = []
            for box, text, conf in items:
                top, bottom = min(p[1] for p in box), max(p[1] for p in box)
                row = next((row for row in rows
                            if min(row["bottom"], bottom) - max(row["top"], top)
                            >= .5 * min(row["bottom"] - row["top"], bottom - top)), None)
                if row is None:
                    row = {"top": top, "bottom": bottom, "parts": []}
                    rows.append(row)
                row["parts"].append((min(p[0] for p in box), str(text).strip(), float(conf)))
            texts, confs = [], []
            for row in rows:
                parts = sorted(row["parts"])
                kept = clean_ocr_lines(" ".join(part[1] for part in parts), chinese_only=True)
                if kept:
                    texts.extend(kept)
                    confs.extend(part[2] for part in parts if part[1])
            return ("\n".join(texts), float(np.mean(confs))) if texts else ("", 0.0)
        texts = [str(r[1]).strip() for r in items if str(r[1]).strip()]
        confs = [float(r[2]) for r in items if str(r[1]).strip()]
        if not texts:
            return "", 0.0
        return " ".join(texts), float(np.mean(confs))

    def _read_crop(self, rgb_crop: np.ndarray) -> tuple[str, float, str]:
        gray = cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2GRAY)
        variants = [gray]
        binary = _binarize(gray)
        if binary is not None:
            variants.append(binary)

        best_score = -1.0
        best: tuple[str, float, str] = ("", 0.0, "")
        for variant in variants:
            for key, reader in self.readers.items():
                text, conf = self._read_with_recognizer(reader, variant)
                if not text.strip() or conf < 0.05:
                    fallback, fallback_conf = self._read_fallback(reader, variant)
                    if fallback_conf > conf:
                        text, conf = fallback, fallback_conf
                text = "\n".join(clean_ocr_lines(text, chinese_only=set(self.langs) == {"zh"}))
                if not text:
                    continue
                # Reader zh (ch_sim+en) đọc chữ Latin không dấu khá tự tin, dễ thắng
                # reader vi và làm mất dấu tiếng Việt: trừ điểm khi output "zh"
                # gần như không có chữ Hán.
                score = conf * 0.8 if key == "zh" and han_ratio(text) < 0.2 else conf
                if score > best_score:
                    best_score = score
                    best = (text, conf, key)
            if best_score >= 0.5:
                break
        return best

    def _read_segments(self, rgb_crop: np.ndarray, segments: list[tuple[int, int]]) -> tuple[str, float]:
        height, width = rgb_crop.shape[:2]
        gray = cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2GRAY)
        bg = int(np.bincount(gray.ravel(), minlength=256).argmax())
        ink = np.abs(gray.astype(np.int16) - bg) > 60
        texts: list[str] = []
        confs: list[float] = []
        last = len(segments)
        for i, (top, bottom) in enumerate(segments):
            pad_v = max(3, int((bottom - top) * 0.3))
            t = top if i == 0 else max((segments[i - 1][1] + top) // 2, top - pad_v)
            b = bottom if i == last - 1 else min((bottom + segments[i + 1][0]) // 2, bottom + pad_v)
            cols = ink[top:bottom].sum(axis=0)
            xs = np.flatnonzero(cols > 0)
            if xs.size == 0:
                continue
            x1 = max(0, int(xs[0]) - 6)
            x2 = min(width, int(xs[-1]) + 7)
            if x2 - x1 < 8:
                continue
            seg = rgb_crop[t:b, x1:x2]
            seg_h = seg.shape[0]
            if seg_h < 56:
                scale = min(3.0, 56.0 / max(1, seg_h))
                seg = cv2.resize(seg, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            text, conf, _key = self._read_crop(seg)
            text = text.strip()
            if text:
                texts.append(text)
                confs.append(conf)
        if not texts:
            return "", 0.0
        return "\n".join(texts), float(np.mean(confs))

    def read_line(self, rgb_crop: np.ndarray) -> tuple[str, float, str]:
        with self._read_lock:
            return self._read_line_unlocked(rgb_crop)

    def _read_line_unlocked(self, rgb_crop: np.ndarray) -> tuple[str, float, str]:
        whole = self._read_crop(rgb_crop)
        segments = _ink_segments(rgb_crop)
        if len(segments) >= 2:
            # Bỏ đoạn nhiễu dính mép trên/dưới crop (nền ngoài hộp chữ).
            largest = max(b - a for a, b in segments)
            segments = [
                (a, b) for a, b in segments
                if not ((a <= 1 or b >= rgb_crop.shape[0] - 1) and (b - a) < 0.5 * largest)
            ]
            if len(segments) >= 2:
                seg_text, seg_conf = self._read_segments(rgb_crop, segments)
                # Đọc tách dòng khắc phục hộp nhiều dòng bị dính chữ/mất dòng;
                # Nguồn zh ưu tiên đọc từng dòng để không ghép số ở hàng riêng;
                # nguồn hỗn hợp vẫn cần confidence cao hơn đọc cả khối.
                if seg_text.strip() and (set(self.langs) == {"zh"} or seg_conf > whole[1]):
                    return seg_text, seg_conf, "multi"
        return whole


def _binarize(gray: np.ndarray) -> np.ndarray | None:
    fg = float(np.percentile(gray, 99))
    bg = float(np.percentile(gray, 20))
    if fg - bg < 60:
        return None
    threshold = int(bg + (fg - bg) * 0.6)
    _, out = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    return out


def _ink_segments(rgb_crop: np.ndarray, min_gap: int = 3, min_h: int = 8, tol: int = 60) -> list[tuple[int, int]]:
    """Tách các dòng chữ trong một crop bằng profile 'mực' theo hàng.

    Crop của phụ đề nhiều dòng (hộp trắng/chữ đen hoặc nền tối/chữ trắng) nếu
    OCR cả khối sẽ dính chữ và mất dòng. Mực = pixel lệch xa nền chủ đạo của
    crop, hoạt động với cả hai kiểu màu.
    """
    gray = cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2GRAY)
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float32)
    bg = int(hist.argmax())
    ink = np.abs(gray.astype(np.int16) - bg) > tol
    rows = ink.sum(axis=1).astype(np.float32)
    if rows.max() < 4:
        return []
    active = rows > max(1.5, rows.max() * 0.06)
    index = 0
    while index < len(active):
        if not active[index]:
            end = index
            while end < len(active) and not active[end]:
                end += 1
            if index > 0 and end < len(active) and (end - index) < min_gap:
                active[index:end] = True
            index = end
        else:
            index += 1
    bounds: list[tuple[int, int]] = []
    in_run = False
    start = 0
    for i, flag in enumerate(active):
        if flag and not in_run:
            start = i
            in_run = True
        elif not flag and in_run:
            if i - start >= min_h:
                bounds.append((start, i))
            in_run = False
    if in_run and len(active) - start >= min_h:
        bounds.append((start, len(active)))
    return bounds


def prepare_line(rgb: np.ndarray, box: Box) -> np.ndarray:
    h, w = rgb.shape[:2]
    x1, y1, x2, y2 = box
    pad_x = max(3, int((y2 - y1) * 0.35))
    pad_y = max(2, int((y2 - y1) * 0.18))
    x1 = max(0, x1 - pad_x)
    x2 = min(w, x2 + pad_x)
    y1 = max(0, y1 - pad_y)
    y2 = min(h, y2 + pad_y)
    crop = rgb[y1:y2, x1:x2]
    if crop.size == 0:
        return crop
    scale = min(3.0, max(1.0, 64.0 / max(1, crop.shape[0])))
    if scale > 1.01:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return crop
