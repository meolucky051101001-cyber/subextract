"""Nghiệm thu P0 trên dịch vụ thật (8787): video ≥2 phút, ≥50 cue.

Upload hardsub_long.mp4, chạy pipeline kèm dịch sang tiếng Việt, chờ done,
tải 6 biến thể xuất (srt/vtt × original/translated/bilingual) và kiểm tra
cấu trúc file phụ đề. Không đụng 8088.

Chạy: .venv/Scripts/python.exe data/test/acceptance_p0_test.py
"""
import json
import re
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8787"
VIDEO = ROOT / "data" / "test" / "hardsub_long.mp4"
OUT_DIR = ROOT / "data" / "test" / "out_acceptance"
MIN_CUES = 50
MIN_TRANSLATED_RATIO = 0.98
POLL_TIMEOUT = 1500

TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3} --> \d{2}:\d{2}:\d{2}[,.]\d{3}$")


def parse_srt(content: str):
    blocks = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        ts_line = lines[1] if "-->" in lines[1] else lines[0]
        text_lines = lines[2:] if "-->" in lines[1] else lines[1:]
        if not TS_RE.match(ts_line):
            raise AssertionError(f"Timestamp SRT sai định dạng: {ts_line!r}")
        blocks.append({"ts": ts_line, "text": "\n".join(text_lines)})
    return blocks


def ts_seconds(ts: str) -> float:
    part = ts.split("-->")[0].strip().replace(",", ".")
    h, m, s = part.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def upload(path: Path) -> dict:
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
    body += b"Content-Type: video/mp4\r\n\r\n"
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    job = upload(VIDEO)
    print("uploaded:", job["id"], job["filename"])

    req = urllib.request.Request(
        f"{BASE}/api/jobs/{job['id']}/start",
        data=json.dumps({"translate_to": "vi"}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()

    started = time.time()
    status, last_print = {}, ""
    while time.time() - started < POLL_TIMEOUT:
        with urllib.request.urlopen(f"{BASE}/api/jobs/{job['id']}", timeout=15) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        line = f"{status['stage']} {status['progress'] * 100:.0f}% — {status['message']}"
        if line != last_print:
            print(f"  [{time.time() - started:6.1f}s] {line}")
            last_print = line
        if status["status"] in {"done", "error", "cancelled"}:
            break
        time.sleep(2.0)

    stats = status.get("stats") or {}
    cues = status.get("cues") or []
    translatable = int(stats.get("translate_total") or 0)
    translated = int(stats.get("translated_cues") or 0)
    error = str(stats.get("translate_error") or "")
    print(f"status={status['status']} cues={len(cues)} translatable={translatable} "
          f"translated={translated} error='{error}' "
          f"ocr={stats.get('ocr_seconds')}s translate={stats.get('translate_seconds')}s "
          f"total={stats.get('total_seconds')}s")

    checks = {
        "job_done": status["status"] == "done",
        "cue_ge_50": len(cues) >= MIN_CUES,
        "translatable_ge_20": translatable >= 20,
        "translated_ratio": translatable == 0 or translated >= MIN_TRANSLATED_RATIO * translatable,
        "no_translate_error": not error,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {}
    for fmt in ("srt", "vtt"):
        for variant in ("original", "translated", "bilingual"):
            url = f"{BASE}/api/jobs/{job['id']}/export?format={fmt}&variant={variant}"
            name = f"hardsub_long.{variant}.{fmt}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                content = resp.read().decode("utf-8-sig")
            (OUT_DIR / name).write_text(content, encoding="utf-8")
            files[(fmt, variant)] = content

    srt_orig = parse_srt(files[("srt", "original")])
    srt_trans = parse_srt(files[("srt", "translated")])
    srt_biling = parse_srt(files[("srt", "bilingual")])
    prev = -1.0
    ordered = True
    for block in srt_orig:
        t0 = ts_seconds(block["ts"])
        if t0 < prev:
            ordered = False
        prev = t0
    changed = sum(1 for a, b in zip(srt_orig, srt_trans) if a["text"] != b["text"])
    checks["export_count_match"] = len(srt_orig) == len(cues) == len(srt_trans) == len(srt_biling)
    checks["export_ts_ordered"] = ordered
    checks["vtt_header"] = files[("vtt", "original")].lstrip().startswith("WEBVTT")
    # Cue có bản dịch thì song ngữ phải 2 dòng (dịch trên, gốc dưới);
    # cue giữ nguyên ngôn ngữ đích thì song ngữ trùng bản gốc 1 dòng.
    two_lines = sum(1 for b in srt_biling if len(b["text"].splitlines()) == 2)
    same_as_orig = sum(1 for a, b in zip(srt_orig, srt_biling) if b["text"] == a["text"])
    checks["bilingual_two_lines"] = (
        two_lines == changed and same_as_orig == len(srt_orig) - changed
    )
    checks["translated_changed"] = translatable == 0 or changed >= MIN_TRANSLATED_RATIO * translatable

    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print("job_id:", job["id"])
    print("export_dir:", OUT_DIR)
    print("RESULT:", "PASS" if all(checks.values()) else "FAIL")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
