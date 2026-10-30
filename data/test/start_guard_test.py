import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = ROOT / "data" / "test" / "tmp_guard"
JOBS = TMP / "jobs"
SETTINGS = TMP / "settings.json"
PORT = "8793"
BASE = f"http://127.0.0.1:{PORT}"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
VIDEO = ROOT / "data" / "test" / "hardsub.mp4"

shutil.rmtree(TMP, ignore_errors=True)
JOBS.mkdir(parents=True)
SETTINGS.write_text(json.dumps({"translate_model": "gemini-3.5-flash-lite"}), encoding="utf-8")
env = os.environ.copy()
env["SUBEXTRACT_DATA_DIR"] = str(JOBS)
env["SUBEXTRACT_SETTINGS"] = str(SETTINGS)
log = open(TMP / "server.log", "wb")
server = subprocess.Popen(
    [str(VENV), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT],
    cwd=str(ROOT), env=env, stdout=log, stderr=log,
)

try:
    end = time.time() + 60
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1):
                break
        except Exception:
            time.sleep(0.3)
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="file"; filename="hardsub.mp4"\r\n'
    body += b"Content-Type: video/mp4\r\n\r\n" + VIDEO.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/jobs", data=bytes(body), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        job = json.loads(resp.read())

    payload = json.dumps({"translate_to": "", "dub": False, "sample_fps": 3.0}).encode()
    req = urllib.request.Request(f"{BASE}/api/jobs/{job['id']}/start", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("first start:", resp.status)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("second start:", resp.status)
        raise SystemExit("FAIL: second start was allowed")
    except urllib.error.HTTPError as exc:
        print("second start:", exc.code, "expected 409")
        assert exc.code == 409
    req = urllib.request.Request(f"{BASE}/api/jobs/{job['id']}/cancel", data=b"", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("cancel:", resp.status)
    time.sleep(2.0)
    with urllib.request.urlopen(f"{BASE}/api/jobs/{job['id']}", timeout=10) as resp:
        status = json.loads(resp.read())
    print("status after cancel:", status["status"], status["stage"], "| error:", status.get("error"))
    if status["status"] != "cancelled":
        print((TMP / "server.log").read_text(encoding="utf-8", errors="replace")[-1500:])
    assert status["status"] == "cancelled"
    print("GUARD_OK")
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    shutil.rmtree(TMP, ignore_errors=True)