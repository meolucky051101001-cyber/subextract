import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
TMP = ROOT / "data" / "test" / "tmp_translate"
JOBS = TMP / "jobs"
SETTINGS = TMP / "settings.json"
PORT = "8790"
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = ROOT / "data" / "test" / "shots"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
FIXTURE = "fixturejob01"


def api(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


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
fixture_dir = JOBS / FIXTURE
fixture_dir.mkdir()
shutil.copy2(ROOT / "data" / "test" / "portrait.mp4", fixture_dir / "source.mp4")
job = {
    "id": FIXTURE,
    "filename": "demo.mp4",
    "status": "done",
    "cues": [
        {"start": 0.0, "end": 1.5, "text": "Hello there", "trans": "Chào bạn"},
        {"start": 1.5, "end": 3.0, "text": "Xin chào", "trans": ""},
    ],
    "stats": {"cues": 2, "detected_groups": 2, "gpu": True, "total_seconds": 4,
              "translated_cues": 1, "translate_to": "vi", "translate_error": ""},
    "info": {"duration": 4.0, "width": 720, "height": 1280, "fps": 25, "codec": "h264"},
    "params": {"roi": [0, 0.68, 1, 0.32], "sample_fps": 5, "langs": ["vi", "en", "zh"],
               "min_conf": 0.28, "translate_to": "vi", "translate_model": "gemini-3.8-flash"},
    "created_at": 1770000000,
}
(JOBS / FIXTURE / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

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
    print("settings initial:", api("/api/settings"))

    code, body = api(f"/api/jobs/{FIXTURE}/start", "POST", {"translate_to": "vi"})
    print("start without key:", code, body.get("detail"))
    code, body = api(f"/api/jobs/{FIXTURE}/start", "POST", {"translate_to": "fr"})
    print("start invalid lang:", code, body.get("detail"))

    code, body = api("/api/settings", "PUT", {"gemini_api_key": "dummy-test-key", "translate_model": "gemini-3.5-flash-lite"})
    print("settings saved:", code, body)

    code, data = api(f"/api/jobs/{FIXTURE}")
    print("fixture cues after start attempts:", len(data.get("cues") or []))

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1050})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/?t=translate", wait_until="domcontentloaded")
        page.evaluate(f"localStorage.setItem('subx_job', '{FIXTURE}')")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#resultCard:not(.hidden)", timeout=15000)
        page.wait_for_timeout(800)

        print("translate select:", page.input_value("#translateTo"))
        print("gemini box visible:", page.locator("#geminiBox").is_visible())
        print("gemini state:", page.text_content("#geminiState").strip())
        print("model select:", page.input_value("#geminiModel"))
        print("translation textareas:", page.locator(".cue-trans").count())
        print("trans values:", page.locator(".cue-trans").evaluate_all("els => els.map(e => e.value)"))
        print("export variant visible:", page.locator("#exportVariant").is_visible(),
              "value:", page.input_value("#exportVariant"))

        page.locator(".cue-trans").nth(1).fill("Tạm biệt")
        page.wait_for_timeout(1400)
        code, data = api(f"/api/jobs/{FIXTURE}")
        print("saved trans:", [c.get("trans") for c in data.get("cues") or []])

        with page.expect_download() as info:
            page.click("#exportSrt")
        download = info.value
        srt = Path(download.path()).read_text(encoding="utf-8")
        print("download name:", download.suggested_filename)
        print("translated srt has:", "Chào bạn" in srt, "Tạm biệt" in srt, "Hello there" in srt)

        page.select_option("#exportVariant", "bilingual")
        with page.expect_download() as info:
            page.click("#exportSrt")
        bilingual = Path(info.value.path()).read_text(encoding="utf-8")
        print("bilingual name:", info.value.suggested_filename)
        print("bilingual has both:", "Chào bạn" in bilingual and "Hello there" in bilingual)

        page.select_option("#exportVariant", "original")
        with page.expect_download() as info:
            page.click("#exportVtt")
        original = Path(info.value.path()).read_text(encoding="utf-8")
        print("original vtt:", "Hello there" in original, "Chào bạn" in original, "WEBVTT" in original)

        code, _ = api(f"/api/jobs/{FIXTURE}/export?format=srt&variant=bad")
        print("bad variant status:", code)

        page.screenshot(path=str(SHOTS / "translate_ui.png"), full_page=True)
        print("page errors:", errors)
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    shutil.rmtree(TMP, ignore_errors=True)
