"""Kiểm tra đa vùng ROI trên server cô lập 8790.

Video tổng hợp có phụ đề cả TRÊN lẫn DƯỜNG; chạy pipeline với 2 vùng
[[0,0.02,1,0.12],[0,0.84,1,0.14]] phải bóc được câu của cả hai vùng.
Cũng kiểm tra API từ chối ROI sai hình dạng.

Chạy: .venv/Scripts/python.exe data/test/multi_roi_test.py
"""
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
TMP = ROOT / "data" / "test" / "tmp_multiroi"
JOBS = TMP / "jobs"
PORT = "8790"
BASE = f"http://127.0.0.1:{PORT}"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
VIDEO = ROOT / "data" / "test" / "hardsub_dual.mp4"


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


def expect_error(path, body):
    try:
        api(path, method="POST", body=body)
    except urllib.error.HTTPError as exc:
        return exc.code
    return None


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
    (TMP / "settings.json").write_text(json.dumps({"gemini_api_key": "dummy-multi-roi"}), encoding="utf-8")
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

        # ROI sai hình dạng bị từ chối
        job = upload(VIDEO)
        checks["bad_roi_rejected"] = expect_error(
            f"/api/jobs/{job['id']}/start", {"roi": [0.1, 0.2]}
        ) == 400

        # Chạy với 2 vùng trên + dưới (không dịch để nhanh)
        api(f"/api/jobs/{job['id']}/start", method="POST", body={
            "roi": [[0.0, 0.02, 1.0, 0.12], [0.0, 0.84, 1.0, 0.14]],
            "translate_to": "",
        })
        started = time.time()
        status = {}
        while time.time() - started < 300:
            status = api(f"/api/jobs/{job['id']}")
            if status["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(1.0)
        cues = status.get("cues") or []
        stats = status.get("stats") or {}
        all_text = " ".join(c["text"].replace("\n", " ") for c in cues)
        print(f"status={status['status']} cues={len(cues)} groups={stats.get('detected_groups')}")
        for c in cues:
            print(f"  [{c['start']:.1f}-{c['end']:.1f}] {c['text']!r}")

        checks["job_done"] = status["status"] == "done"
        checks["cue_count"] = len(cues) >= 14  # 8 trên + 8 dưới, chờ dung sai
        checks["top_text_found"] = "painting channel" in all_text and "night city" in all_text
        # OCR nhỏ chữ có thể mất vài dấu ('phố'→'phô') — so chuỗi đọc được ổn định
        checks["bottom_text_found"] = (
            "Chào mừng" in all_text and "Hôm nay chúng ta vẽ" in all_text and "Xanh đậm" in all_text
        )
        checks["params_two_rois"] = isinstance((status.get("params") or {}).get("roi"), list) \
            and len(status["params"]["roi"]) == 2
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
