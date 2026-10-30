"""Kiểm tra hủy job tức thì trong lúc đang chờ Gemini (tiêu chí ≤ 2 giây).

Dùng model chậm gemini-3.8-flash để chắc chắn job đang-blocked trong request
Gemini khi bấm hủy. Xong test khôi phục model cũ trong settings.

Chạy: .venv/Scripts/python.exe data/test/cancel_speed_test.py  (dịch vụ thật 8787)
"""
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8787"
VIDEO = ROOT / "data" / "test" / "hardsub_logo.mp4"
SLOW_MODEL = "gemini-3.8-flash"
SETTLE_IN_TRANSLATE = 5.0
MAX_CANCEL_SECONDS = 2.0


def api(path: str, method: str = "GET", body: dict | None = None, timeout: float = 15):
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
    body += b"Content-Type: video/mp4\r\n\r\n"
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    old_settings = api("/api/settings")
    old_model = old_settings.get("translate_model")
    print("model cũ:", old_model, "→ tạm đổi sang", SLOW_MODEL)
    api("/api/settings", method="PUT", body={"translate_model": SLOW_MODEL})
    job = upload(VIDEO)
    print("uploaded:", job["id"])
    ok = False
    try:
        api(f"/api/jobs/{job['id']}/start", method="POST", body={"translate_to": "vi"})
        started = time.time()
        stage = ""
        while time.time() - started < 120:
            status = api(f"/api/jobs/{job['id']}")
            stage = status.get("stage") or ""
            if stage == "translate":
                break
            if status["status"] in {"done", "error", "cancelled"}:
                raise RuntimeError(f"job kết thúc sớm: {status['status']}")
            time.sleep(0.2)
        if stage != "translate":
            raise RuntimeError("job không vào được giai đoạn translate trong 120s")
        print(f"vào translate sau {time.time() - started:.1f}s, chờ {SETTLE_IN_TRANSLATE}s "
              "để chắc chắn đang chờ Gemini…")
        time.sleep(SETTLE_IN_TRANSLATE)

        t0 = time.time()
        api(f"/api/jobs/{job['id']}/cancel", method="POST", body={})
        terminal = ""
        while time.time() - t0 < 30:
            status = api(f"/api/jobs/{job['id']}")
            if status["status"] in {"cancelled", "done", "error"}:
                terminal = status["status"]
                break
            time.sleep(0.1)
        elapsed = time.time() - t0
        print(f"cancel → '{terminal}' sau {elapsed:.2f}s (message: {status.get('message')})")
        ok = terminal == "cancelled" and elapsed <= MAX_CANCEL_SECONDS
    finally:
        api("/api/settings", method="PUT", body={"translate_model": old_model})
        restored = api("/api/settings")
        print("khôi phục model:", restored.get("translate_model"))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
