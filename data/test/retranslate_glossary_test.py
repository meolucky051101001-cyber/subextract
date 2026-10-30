"""Kiểm tra tính năng P2: dịch lại từng cue + glossary trên server cô lập 8790.

- Lưu/đọc glossary qua /api/settings (chuẩn hóa, bỏ dòng thiếu '=').
- Job dịch sang en xong → POST /api/jobs/{id}/retranslate từng cue → trans cập
  nhật và job.json được lưu lại.
- Cue sai chỉ số / cue trống → lỗi 400 rõ ràng.
- Prompt có glossary (kiểm tra import trực tiếp translate).

Chạy: .venv/Scripts/python.exe data/test/retranslate_glossary_test.py
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
TMP = ROOT / "data" / "test" / "tmp_retrans"
JOBS = TMP / "jobs"
PORT = "8790"
BASE = f"http://127.0.0.1:{PORT}"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
VIDEO = ROOT / "data" / "test" / "hardsub.mp4"


def real_key() -> str:
    data = json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))
    key = str(data.get("gemini_api_key") or "").strip()
    if not key:
        raise RuntimeError("Chưa có key thật trong data/settings.json")
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


def api(path, method="GET", body=None, timeout=30):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def expect_error(path, method="POST", body=None):
    try:
        api(path, method=method, body=body)
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read().decode("utf-8")).get("detail")
        return exc.code, detail
    raise AssertionError("endpoint phải trả lỗi nhưng lại thành công")


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
    (TMP / "settings.json").write_text(json.dumps({
        "gemini_api_key": real_key(),
        "translate_model": "gemini-3.5-flash-lite",
    }), encoding="utf-8")
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

        # 1) Glossary qua settings API
        put = api("/api/settings", method="PUT", body={
            "glossary": [
                {"src": "Xin chào", "dst": "Greetings"},
                {"src": "", "dst": "sẽ bị bỏ"},
                {"src": "ok", "dst": ""},
            ]
        })
        got = api("/api/settings")
        checks["glossary_saved"] = put.get("glossary") == [{"src": "Xin chào", "dst": "Greetings"}]
        checks["glossary_readback"] = got.get("glossary") == [{"src": "Xin chào", "dst": "Greetings"}]

        # 2) Job dịch sang English
        job = upload(VIDEO)
        api(f"/api/jobs/{job['id']}/start", method="POST", body={"translate_to": "en"})
        started = time.time()
        status = {}
        while time.time() - started < 300:
            status = api(f"/api/jobs/{job['id']}")
            if status["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(1.0)
        stats = status.get("stats") or {}
        checks["job_done_en"] = status["status"] == "done" and not stats.get("translate_error")
        cues = status.get("cues") or []
        checks["untranslated_field"] = stats.get("untranslated_cues") == 0
        # hardsub.mp4 có 1 cue vốn tiếng Anh → đích en giữ nguyên, hợp lệ.
        checks["has_trans"] = sum(1 for c in cues if (c.get("trans") or "").strip()) >= 3 and len(cues) >= 3

        # 3) Dịch lại cue đầu (bây giờ có glossary "Xin chào = Greetings")
        re = api(f"/api/jobs/{job['id']}/retranslate", method="POST", body={"index": 0}, timeout=150)
        checks["retranslate_ok"] = bool(re.get("ok")) and bool(str(re.get("trans") or "").strip())
        after = api(f"/api/jobs/{job['id']}")
        checks["retranslate_persisted"] = after["cues"][0]["trans"] == re["trans"]

        # 4) Lỗi hợp lý
        code, _ = expect_error(f"/api/jobs/{job['id']}/retranslate", body={"index": 999})
        checks["bad_index_400"] = code == 400
        code, detail = expect_error(f"/api/jobs/{job['id']}/retranslate", body={"index": 0, "text": "   "})
        checks["empty_text_400"] = code == 400

        # 5) Prompt unit (không cần server)
        sys.path.insert(0, str(ROOT))
        import app.pipeline.translate as t
        gt = t._glossary_text(t._norm_glossary([{"src": "A", "dst": "B"}]))
        checks["prompt_glossary"] = "A → B" in t._prompt([(1, "x")], "vi", "", gt)
        checks["retry_prompt_glossary"] = "A → B" in t._retry_prompt(1, "x", "vi", gt)
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
