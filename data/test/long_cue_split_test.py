"""Nghiệm thu two-pass tách cue dài trên video thực (hộp phụ đề mờ).

Video: Dubbed_telegram…用喷枪画夜景不要太爽.mp4 — trước vá, khung 21.8–30.8s
gộp 2 câu thành 1 cue 9 giây (chỉ hiện text câu đầu). Sau vá phải tách ra:
tồn tại cue bắt đầu trong khoảng [24, 31] với text đọc được.

Chạy: .venv/Scripts/python.exe data/test/long_cue_split_test.py  (cổng 8790)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = ROOT / "data" / "test" / "tmp_split"
JOBS = TMP / "jobs"
PORT = "8790"
BASE = f"http://127.0.0.1:{PORT}"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
VIDEO = ROOT / "data" / "jobs" / "042727024b06" / "source.mp4"


def _base(ch: str) -> str:
    ch = ch.lower()
    ch = unicodedata.normalize("NFD", ch)
    ch = "".join(c for c in ch if not unicodedata.combining(c))
    return "d" if ch == "đ" else ch


import unicodedata  # noqa: E402


def strip_vn(text: str) -> str:
    return "".join(_base(c) if c.isalpha() else c for c in text)


def wait_health(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("server không lên")


def api(path, method="GET", body=None, timeout=30):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload(path: Path) -> dict:
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
    body += b"Content-Type: video/mp4\r\n\r\n" + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    JOBS.mkdir(parents=True)
    (TMP / "settings.json").write_text(json.dumps({"gemini_api_key": "dummy-split"}), encoding="utf-8")
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)
    env.pop("GEMINI_API_KEYS", None)
    env["SUBEXTRACT_DATA_DIR"] = str(JOBS)
    env["SUBEXTRACT_SETTINGS"] = str(TMP / "settings.json")
    log = open(TMP / "server.log", "wb")
    server = subprocess.Popen(
        [str(VENV), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT],
        cwd=str(ROOT), env=env, stdout=log, stderr=log,
    )
    checks = {}
    try:
        wait_health()
        job = upload(VIDEO)
        api(f"/api/jobs/{job['id']}/start", method="POST", body={"translate_to": ""})
        started = time.time()
        status = {}
        while time.time() - started < 300:
            status = api(f"/api/jobs/{job['id']}")
            if status["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(1.0)
        cues = status.get("cues") or []
        print(f"status={status['status']} cues={len(cues)}")
        for c in cues:
            print(f"  [{c['start']:5.1f}-{c['end']:5.1f}] {c['text']!r}")

        all_stripped = strip_vn(" ".join(c["text"].replace("\n", " ") for c in cues)).lower()
        checks["job_done"] = status["status"] == "done"
        checks["cue_count"] = len(cues) >= 11  # trước vá: 10
        # Cue thứ hai trong khung 21.8–30.8 giờ phải đứng độc lập
        split_cues = [c for c in cues if 24.0 <= c["start"] < 31.0 and len(c["text"].strip()) >= 10]
        checks["long_cue_split"] = bool(split_cues)
        checks["second_sentence_text"] = bool(
            re.search(r"bat.{0,12}dau|thap.{0,6}den", all_stripped)
        )
        # fix_ocr_text không phá chữ sạch
        checks["existing_text_intact"] = "Món này mà không dùng vẽ cảnh đêm" in " ".join(
            c["text"] for c in cues
        ).replace("\n", " ") or any("Món này" in c["text"] for c in cues)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        tail = (TMP / "server.log").read_text(encoding="utf-8", errors="replace")[-600:] if (TMP / "server.log").exists() else ""
        shutil.rmtree(TMP, ignore_errors=True)
        if "Traceback" in tail:
            print("SERVER LOG TAIL:\n", tail)
            checks["server_traceback"] = False

    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print("RESULT:", "PASS" if all(checks.values()) else "FAIL")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
