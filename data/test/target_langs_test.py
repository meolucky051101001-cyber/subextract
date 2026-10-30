"""Kiểm tra đích dịch English và 中文 trên dịch vụ thật (8787).

- hardsub.mp4 (phụ đề Trung) → dịch sang English.
- hardsub_logo.mp4 (Việt + Anh + Trung) → dịch sang 中文.

Chạy: .venv/Scripts/python.exe data/test/target_langs_test.py
"""
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8787"
CASES = [
    ("hardsub.mp4", "en", 1),
    ("hardsub_logo.mp4", "zh", 1),
]


def api(path: str, method: str = "GET", body: dict | None = None, timeout: float = 15):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload(name: str) -> dict:
    path = ROOT / "data" / "test" / name
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode()
    body += b"Content-Type: video/mp4\r\n\r\n" + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ok_all = True
    for name, target, min_translated in CASES:
        job = upload(name)
        api(f"/api/jobs/{job['id']}/start", method="POST", body={"translate_to": target})
        started = time.time()
        status = {}
        while time.time() - started < 300:
            status = api(f"/api/jobs/{job['id']}")
            if status["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(1.0)
        stats = status.get("stats") or {}
        cues = status.get("cues") or []
        translated = sum(1 for c in cues if (c.get("trans") or "").strip())
        label = {"en": "English", "zh": "中文"}.get(target, target)
        sample = next((c["trans"] for c in cues if c.get("trans")), "")
        ok = (
            status["status"] == "done"
            and translated == int(stats.get("translate_total") or -1)
            and translated >= min_translated
            and not stats.get("translate_error")
        )
        ok_all = ok_all and ok
        print(f"[{name} → {label}] status={status['status']} cues={len(cues)} "
              f"dịch={translated}/{stats.get('translate_total')} "
              f"{stats.get('translate_seconds')}s lỗi='{stats.get('translate_error')}' → "
              f"{'PASS' if ok else 'FAIL'}")
        if sample:
            print(f"  mẫu: {sample[:70]}")
        # Xuất file đích để kiểm tra thêm
        with urllib.request.urlopen(
            f"{BASE}/api/jobs/{job['id']}/export?format=srt&variant=translated", timeout=15
        ) as resp:
            srt = resp.read().decode("utf-8-sig")
        (ROOT / "data" / "test" / "out_acceptance" / f"{Path(name).stem}.{target}.srt").write_text(
            srt, encoding="utf-8"
        )
    print("RESULT:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
