"""Kiểm tra xoay key Gemini (GEMINI_API_KEYS / gemini_api_keys).

Kịch bản 1: hai key giả + key thật ở cuối -> batch lỗi auth/quota trên key giả
phải xoay sang key thật và dịch thành công.
Kịch bản 2: chỉ có key giả -> job vẫn done, giữ đủ cue OCR, báo lỗi dịch rõ ràng.

Chạy: .venv/Scripts/python.exe data/test/translate_key_rotation_test.py
Dùng cổng 8790 và tự dọn thư mục tạm; không ảnh hưởng 8787/8088.
Không in key ra console.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = ROOT / "data" / "test" / "tmp_rotation"
JOBS = TMP / "jobs"
PORT = "8790"
BASE = f"http://127.0.0.1:{PORT}"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
VIDEO = ROOT / "data" / "test" / "hardsub_logo.mp4"


def real_key() -> str:
    data = json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))
    key = str(data.get("gemini_api_key") or "").strip()
    if not key:
        raise RuntimeError("Chưa có key thật trong data/settings.json để test xoay key")
    return key


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


def upload(filename: str) -> dict:
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: video/mp4\r\n\r\n"
    body += VIDEO.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def start_and_wait(job_id: str, translate_to: str = "vi", timeout=300) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/jobs/{job_id}/start", data=json.dumps({"translate_to": translate_to}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    end = time.time() + timeout
    status = {}
    while time.time() < end:
        with urllib.request.urlopen(f"{BASE}/api/jobs/{job_id}", timeout=10) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        if status["status"] in {"done", "error", "cancelled"}:
            return status
        time.sleep(1.0)
    raise RuntimeError("job quá thời gian chờ")


def start_server(settings: dict):
    shutil.rmtree(TMP, ignore_errors=True)
    JOBS.mkdir(parents=True)
    (TMP / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)
    env.pop("GEMINI_API_KEYS", None)
    env["SUBEXTRACT_DATA_DIR"] = str(JOBS)
    env["SUBEXTRACT_SETTINGS"] = str(TMP / "settings.json")
    log = open(TMP / "server.log", "wb")
    proc = subprocess.Popen(
        [str(VENV), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT],
        cwd=str(ROOT), env=env, stdout=log, stderr=log,
    )
    wait_health()
    return proc, log


def stop_server(proc, log):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    tail = ""
    if (TMP / "server.log").exists():
        tail = (TMP / "server.log").read_text(encoding="utf-8", errors="replace")[-600:]
    shutil.rmtree(TMP, ignore_errors=True)
    return tail


def scenario_rotation() -> bool:
    keys = ["dummy-rot-invalid-1", "dummy-rot-invalid-2", real_key()]
    proc, log = start_server({"gemini_api_keys": keys, "translate_model": "gemini-3.5-flash-lite"})
    ok = False
    try:
        job = upload("hardsub_logo.mp4")
        status = start_and_wait(job["id"])
        cues = status.get("cues") or []
        stats = status.get("stats") or {}
        translated = sum(1 for c in cues if (c.get("trans") or "").strip())
        print(f"[rotation] status={status['status']} cues={len(cues)} "
              f"translated={translated}/{stats.get('translate_total')} "
              f"error='{stats.get('translate_error')}' {stats.get('translate_seconds')}s")
        ok = (
            status["status"] == "done"
            and int(stats.get("translate_total") or 0) >= 2
            and translated == int(stats.get("translate_total") or 0)
            and not stats.get("translate_error")
        )
    finally:
        tail = stop_server(proc, log)
    if "Traceback" in tail:
        print("SERVER LOG TAIL:\n", tail)
        ok = False
    return ok


def scenario_all_invalid() -> bool:
    proc, log = start_server({"gemini_api_key": "dummy-rot-all-bad", "translate_model": "gemini-3.5-flash-lite"})
    ok = False
    try:
        job = upload("hardsub_logo.mp4")
        status = start_and_wait(job["id"])
        cues = status.get("cues") or []
        stats = status.get("stats") or {}
        print(f"[all-invalid] status={status['status']} cues={len(cues)} "
              f"error='{str(stats.get('translate_error'))[:80]}'")
        ok = (
            status["status"] == "done"
            and len(cues) >= 3
            and not any((c.get("trans") or "").strip() for c in cues)
            and bool(stats.get("translate_error"))
        )
    finally:
        tail = stop_server(proc, log)
    if "Traceback" in tail:
        print("SERVER LOG TAIL:\n", tail)
        ok = False
    return ok


if __name__ == "__main__":
    results = {"rotation": scenario_rotation(), "all_invalid": scenario_all_invalid()}
    print("RESULTS:", json.dumps(results))
    sys.exit(0 if all(results.values()) else 1)
