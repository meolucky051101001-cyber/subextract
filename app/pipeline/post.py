from __future__ import annotations

import math
import re
import unicodedata

_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f\U00030000-\U000323af]")
_MEANINGFUL = re.compile(r"[0-9A-Za-z\u00c0-\u1ef9\u3400-\u4dbf\u4e00-\u9fff]")

# Các âm tiết/từ tiếng Việt thường gặp (so sánh KHÔNG DẤU, chữ thường) dùng để
# tách các từ bị OCR dính vào nhau, ví dụ 'khôngdùng' -> 'không dùng'.
_VN_WORDS_RAW = """
ai an anh ao ay ban bao bei ben bo bao buc bi bia bich binh boa boi bom bon bong bua buc bun
bung ca cam can cao cap cau cau chao chi cho chua co coc con cong co dau day de di dau do duong
dua dung duoi dem doi don dung du gia gap gau gio giong goi guong ha hai han hay he hoa ho hoi
hom hon hong hop hu hung kho kha khai khang khau khi khinh khong khuc khui kia kim kinh khoi
khuya la lam lai le loi lung lo lon loai luc luon luot ma mai may mau mo mot mot mua muon mung
nam nao nau nam ngai ngap nghi ngoi ngoai ngu nguoi ngua ngo nhi nhin nhieu nhung noi non nop
nu nua nui o oi on ong oi phai phan pho phuc phai qua que quyet ra rang rau ro rieng roi ru
sa sai san sang sao sen si so soc son song su sua ta tai tan tao tay te thi thao thich thoi
thi thu tuc toi trong tray tro trung tru tu tui tung tuoi ua uong van ve vi vao vu vung vuong
xa xau xe xi xinh xiu xong xuong yeu ai ao bach ban bac bang bat bau ben boc boi bon bot bui
cam canh cao chat chi chieu chinh chu chung cung cot cua cui cum cuop cuu dam dan dat dau den
dieu dinh do duoi dua duc gan gap giua goi han hoa hoi huong khoan khong kia lai lang leu lieu
linh loi lung mai mat mau mong mui nam nat ngang ngon nuoc nut nho nhoi noi oan oai phao phong
phut quanh quan quoc ro rang rua sao sau siu sinh suat tat tan tap tay teo thin thang thuoc
tim toi tuyen tua tuc tuong tuvan tuoi van vien vu vo vong xau xen xoay xua xen xa xoe duoc
dung dang duoc truoc sau giua vao ra di den ve an uong ngu day khoe vui buon gian tut tuc
chao buoi sang trua toi dem hom nay mai ngoai trong ngoai duoi tren tren duoi tren quanh
luon luot rat kha hoi dang bi da duoc phai nen cho le thi ma ho chung ta ban minh toi tui
chung no ha bata bo may ai do cai gi dau sao nhi the nao the nao vi sao tai sao
"""


def _base(ch: str) -> str:
    ch = unicodedata.normalize("NFD", ch)[0].lower()
    return "d" if ch == "đ" else ch


_VN_WORDS = frozenset(
    "".join(_base(c) for c in word)
    for word in _VN_WORDS_RAW.split()
    if len(word) >= 2
)


def _strip_diacritics(text: str) -> str:
    return "".join(_base(c) for c in text)


def fix_ocr_text(text: str) -> str:
    """Sửa các từ OCR bị dính: 'khongdung' -> 'không dùng'.

    Chỉ tách khi tách ra được HAI từ đều có trong danh sách âm tiết tiếng Việt
    phổ biến (so sánh không dấu), giúp tránh tách sai tên riêng. Tối đa 2 lớp
    tách để xử lý dính 3 từ.
    """
    if not text:
        return text
    out: list[str] = []
    for token in text.split(" "):
        out.extend(_split_merged(token, depth=0))
    return " ".join(out)


_PUNCT = "\"'“”‘’.,;:!?()[]{}…—–-"


def _split_merged(token: str, depth: int) -> list[str]:
    lead = ""
    trail = ""
    core = token
    while core and core[0] in _PUNCT:
        lead += core[0]
        core = core[1:]
    while core and core[-1] in _PUNCT:
        trail = core[-1] + trail
        core = core[:-1]
    if len(core) < 7 or depth >= 2:
        return [token]
    key = _strip_diacritics(core).lower()
    if key in _VN_WORDS:
        return [token]
    for i in range(2, len(core) - 1):
        left, right = key[:i], key[i:]
        if left in _VN_WORDS and right in _VN_WORDS:
            return [
                *(_split_merged(lead + core[:i], depth + 1)),
                *(_split_merged(core[i:] + trail, depth + 1)),
            ]
    return [token]


def ocr_text_key(lines: list[str]) -> tuple:
    """Khóa so sánh nội dung chữ giữa các khung: không dấu, chữ thường."""
    return tuple(
        re.sub(r"\s+", " ", _strip_diacritics(line).lower()).strip(" .,;:!?")
        for line in lines
    )


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])", "", text)
    text = text.strip("|_~`\\/ ")
    return text.strip()


def clean_ocr_lines(text: str, *, chinese_only: bool = False) -> list[str]:
    """Keep digits only when their own OCR row contains Han in Chinese mode."""
    rows = []
    for raw in str(text).splitlines():
        row = clean_text(raw)
        if row and (not chinese_only or _HAN.search(row)):
            rows.append(row)
    return rows


def is_meaningful(text: str) -> bool:
    return bool(_MEANINGFUL.search(text) or _HAN.search(text))


def han_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if _HAN.match(c)) / len(chars)


def _timestamp_ms(seconds: float) -> int:
    return int(round(seconds * 1000.0))


def _normalise_region_cues(cues: list[dict], merge_gap_ms: int) -> list[dict]:
    """Merge and de-overlap each ROI independently, then sort globally."""
    regions: list[tuple[object, list[dict]]] = []
    for cue in cues:
        bucket = next((items for region, items in regions if region == cue["region"]), None)
        if bucket is None:
            bucket = []
            regions.append((cue["region"], bucket))
        bucket.append(cue)

    result: list[dict] = []
    for _region, bucket in regions:
        bucket.sort(key=lambda cue: (cue["start_ms"], cue["order"]))
        merged: list[dict] = []
        for cue in bucket:
            previous = merged[-1] if merged else None
            if previous is not None and previous["text"] == cue["text"] and cue["start_ms"] - previous["end_ms"] <= merge_gap_ms:
                previous["end_ms"] = max(previous["end_ms"], cue["end_ms"])
            else:
                merged.append(dict(cue))

        # Clip every cue at the next DISTINCT start in its ROI. Equal-start
        # conflicts (including millisecond rounding ties) retain both texts:
        # silently dropping either observation would lose subtitle content.
        current_start = None
        next_start = None
        for cue in reversed(merged):
            if cue["start_ms"] != current_start:
                next_start = current_start
                current_start = cue["start_ms"]
            if next_start is not None:
                cue["end_ms"] = min(cue["end_ms"], next_start)
        result.extend(cue for cue in merged if cue["end_ms"] > cue["start_ms"])

    result.sort(key=lambda cue: (cue["start_ms"], cue["order"]))
    return result


def build_cues(
    groups: list[dict],
    video_duration: float,
    min_conf: float = 0.25,
    merge_gap: float = 0.45,
    min_duration: float = 0.25,
    *,
    chinese_only: bool = False,
) -> list[dict]:
    try:
        duration = float(video_duration)
        gap = float(merge_gap)
        minimum = float(min_duration)
        confidence_floor = float(min_conf)
    except (TypeError, ValueError):
        return []
    if not all(math.isfinite(value) for value in (duration, gap, minimum, confidence_floor)) or duration <= 0:
        return []

    duration_ms = _timestamp_ms(duration)
    if duration_ms <= 0:
        return []
    gap_ms = max(0, _timestamp_ms(max(0.0, gap)))
    minimum = max(0.0, minimum)
    candidates: list[dict] = []

    for order, group in enumerate(groups):
        lines = []
        for line in group.get("lines", []):
            texts = clean_ocr_lines(line.get("text", ""), chinese_only=chinese_only)
            try:
                confidence = float(line.get("conf", 0.0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(confidence) or confidence < confidence_floor:
                continue
            lines.extend(text for text in texts if is_meaningful(text))
        if not lines:
            continue
        try:
            raw_start = float(group["start"])
            raw_end = float(group["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(raw_start) or not math.isfinite(raw_end) or raw_start >= duration or raw_end <= raw_start:
            continue

        start = max(0.0, raw_start)
        end = min(duration, raw_end)
        if end <= start:
            continue
        if end - start < minimum:
            end = min(duration, start + minimum)

        start_ms = max(0, min(duration_ms, _timestamp_ms(start)))
        end_ms = max(0, min(duration_ms, _timestamp_ms(end)))
        if end_ms <= start_ms:
            # SRT/VTT have millisecond resolution. Preserve a real positive cue
            # as one millisecond, shifting its start by at most 1 ms if needed;
            # never create a zero-length cue or extend beyond video duration.
            if start_ms >= duration_ms:
                start_ms = duration_ms - 1
            end_ms = min(duration_ms, start_ms + 1)
        if end_ms <= start_ms:
            continue
        candidates.append({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": "\n".join(lines),
            "region": group.get("region"),
            "order": order,
        })

    fixed = _normalise_region_cues(candidates, gap_ms)
    return [
        {"start": cue["start_ms"] / 1000.0, "end": cue["end_ms"] / 1000.0,
         "text": cue["text"], "region": cue["region"]}
        for cue in fixed
    ]


def format_timestamp(seconds: float, sep: str = ",") -> str:
    seconds = max(0.0, float(seconds))
    total_ms = _timestamp_ms(seconds)
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues: list[dict]) -> str:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(
            f"{i}\n{format_timestamp(cue['start'])} --> {format_timestamp(cue['end'])}\n{cue['text']}\n"
        )
    return "\n".join(blocks)


def to_vtt(cues: list[dict]) -> str:
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(
            f"{format_timestamp(cue['start'], '.')} --> {format_timestamp(cue['end'], '.')}\n{cue['text']}\n"
        )
    return "\n".join(blocks)
