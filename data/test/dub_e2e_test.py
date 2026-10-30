import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = ROOT / "data" / "test" / "tmp_dub_e2e"
JOBS = TMP / "jobs"
SETTINGS = TMP / "settings.json"
BANVE = TMP / "banve"
PORT = "8792"
BASE = f"http://127.0.0.1:{PORT}"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
VIDEO = ROOT / "data" / "test" / "hardsub.mp4"


def wait_health(timeout=60):
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
BANVE.mkdir(parents=True)
SETTINGS.write_text(json.dumps({"translate_model": "gemini-3.5-flash-lite", "dub_output_dir": str(BANVE)}), encoding="utf-8")

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
    print("uploaded:", job["id"], flush=True)

    payload = {"translate_to": "", "dub": True, "sample_fps": 5.0,
               "langs": ["vi", "en", "zh"], "min_conf": 0.28}
    req = urllib.request.Request(
        f"{BASE}/api/jobs/{job['id']}/start", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("started:", resp.status, flush=True)

    status = {}
    started = time.time()
    while time.time() - started < 240:
        with urllib.request.urlopen(f"{BASE}/api/jobs/{job['id']}", timeout=10) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        print(f"  {status['status']} {status['stage']} {status['progress']*100:.0f}% — {status['message']}", flush=True)
        if status["status"] in {"done", "error", "cancelled"}:
            break
        time.sleep(1.0)

    stats = status.get("stats") or {}
    print("status:", status["status"], "| cues:", len(status.get("cues") or []))
    print("dub:", {k: stats.get(k) for k in ("dub_enabled", "dub_cues", "dub_total", "dub_error",
                                             "dub_output", "dub_seconds")})
    with urllib.request.urlopen(f"{BASE}/api/jobs/{job['id']}/dubbed", timeout=30) as resp:
        data = resp.read()
    print("dubbed download bytes:", len(data), "status:", resp.status)
    files = list(BANVE.glob("*.mp4"))
    print("banve files:", [f.name for f in files])
    assert status["status"] == "done", status.get("error")
    assert stats.get("dub_output"), stats
    assert len(data) > 1000
    assert files, "không có file trong thư mục xuất"
    print("E2E DUB OK")
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    tail = (TMP / "server.log").read_text(encoding="utf-8", errors="replace")[-800:]
    if "Traceback" in tail:
        print("SERVER LOG TAIL:\n", tail)