import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = ROOT / "data" / "test" / "tmp_e2e"
JOBS = TMP / "jobs"
SETTINGS = TMP / "settings.json"
PORT = "8790"
BASE = f"http://127.0.0.1:{PORT}"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
VIDEO = ROOT / "data" / "test" / "hardsub.mp4"


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


shutil.rmtree(TMP, ignore_errors=True)
JOBS.mkdir(parents=True)
SETTINGS.write_text(json.dumps({"gemini_api_key": "dummy-e2e-key", "translate_model": "gemini-3.8-flash"}), encoding="utf-8")

env = os.environ.copy()
env["SUBEXTRACT_DATA_DIR"] = str(JOBS)
env["SUBEXTRACT_SETTINGS"] = str(SETTINGS)
log = open(TMP / "server.log", "wb")
server = subprocess.Popen(
    [str(VENV), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT],
    cwd=str(ROOT), env=env, stdout=log, stderr=log,
)

try:
    wait_health()
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="file"; filename="hardsub.mp4"\r\n'
    body += b"Content-Type: video/mp4\r\n\r\n"
    body += VIDEO.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        job = json.loads(resp.read().decode("utf-8"))
    print("uploaded:", job["id"], job["filename"])

    req = urllib.request.Request(
        f"{BASE}/api/jobs/{job['id']}/start", data=json.dumps({"translate_to": "vi"}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("started:", resp.status)

    started = time.time()
    status = {}
    while time.time() - started < 240:
        with urllib.request.urlopen(f"{BASE}/api/jobs/{job['id']}", timeout=10) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        if status["status"] in {"done", "error", "cancelled"}:
            break
        print(f"  {status['stage']} {status['progress']*100:.0f}% — {status['message']}")
        time.sleep(1.5)

    print("status:", status["status"], "stage:", status["stage"])
    print("message:", status["message"])
    stats = status.get("stats") or {}
    print("stats:", {k: stats.get(k) for k in
                     ("cues", "translated_cues", "translate_total", "translate_seconds",
                      "translate_error", "ocr_seconds", "total_seconds")})
    cues = status.get("cues") or []
    print("cues:", len(cues), "with trans:", sum(1 for c in cues if c.get("trans")))
    print("sample:", cues[0] if cues else None)

    with urllib.request.urlopen(f"{BASE}/api/jobs/{job['id']}/export?format=srt&variant=original", timeout=10) as resp:
        srt = resp.read().decode("utf-8")
    with urllib.request.urlopen(f"{BASE}/api/jobs/{job['id']}/export?format=srt&variant=translated", timeout=10) as resp:
        fallback = resp.read().decode("utf-8")
    print("original cues in srt:", srt.count("-->"), "translated fallback same:", srt == fallback)
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    if (TMP / "server.log").exists():
        tail = (TMP / "server.log").read_text(encoding="utf-8", errors="replace")[-600:]
        if "Traceback" in tail:
            print("SERVER LOG TAIL:\n", tail)
    shutil.rmtree(TMP, ignore_errors=True)
