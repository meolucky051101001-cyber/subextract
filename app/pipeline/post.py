from __future__ import annotations

import re
import unicodedata

_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
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


def is_meaningful(text: str) -> bool:
    return bool(_MEANINGFUL.search(text))


def han_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if _HAN.match(c)) / len(chars)


def build_cues(
    groups: list[dict],
    video_duration: float,
    min_conf: float = 0.25,
    merge_gap: float = 0.45,
    min_duration: float = 0.25,
) -> list[dict]:
    cues: list[dict] = []
    for group in groups:
        lines = []
        for line in group.get("lines", []):
            text = clean_text(line.get("text", ""))
            if not text or line.get("conf", 0.0) < min_conf or not is_meaningful(text):
                continue
            lines.append(text)
        if not lines:
            continue
        start = max(0.0, float(group["start"]))
        end = min(float(video_duration), float(group["end"]))
        if end - start < min_duration:
            end = min(float(video_duration), start + min_duration)
        if end <= start:
            continue
        text = "\n".join(lines)
        region = group.get("region")
        if (
            cues and cues[-1]["text"] == text
            and cues[-1].get("_region") == region
            and start - cues[-1]["end"] <= merge_gap
        ):
            cues[-1]["end"] = round(end, 3)
            continue
        cues.append({"start": round(start, 3), "end": round(end, 3), "text": text, "_region": region})

    # Cue cùng vùng không được chồng giờ lên nhau; cue khác vùng (trên/dưới)
    # hoàn toàn có thể chạy song song nên giữ nguyên.
    fixed: list[dict] = []
    for cue in cues:
        if fixed and fixed[-1].get("_region") == cue.get("_region") and fixed[-1]["end"] > cue["start"]:
            fixed[-1]["end"] = round(max(fixed[-1]["start"] + 0.2, cue["start"]), 3)
        fixed.append(cue)
    return [
        {"start": c["start"], "end": c["end"], "text": c["text"], "region": c.get("_region")}
        for c in fixed
    ]


def format_timestamp(seconds: float, sep: str = ",") -> str:
    seconds = max(0.0, float(seconds))
    total_ms = int(round(seconds * 1000))
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
