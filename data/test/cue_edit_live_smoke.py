"""Smoke test UI sửa phụ đề trên 8787 thật — chỉ đọc, không sửa dữ liệu job.

Chạy: python data/test/cue_edit_live_smoke.py
"""
import json
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
JOB = "6ca01c10b3db"
SHOT = Path(__file__).resolve().parents[2] / "data" / "test" / "shots" / "cue_edit_live.png"


def main() -> int:
    with urllib.request.urlopen(f"{BASE}/api/jobs/{JOB}", timeout=10) as resp:
        before = json.loads(resp.read().decode("utf-8"))["cues"]
    checks = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.add_init_script(f"try{{localStorage.setItem('subx_job','{JOB}')}}catch(e){{}}")
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_selector(".cue", timeout=15000)
        checks["rows"] = page.locator(".cue").count() == len(before)
        checks["editable"] = page.locator('.cue[data-index="1"] .cue-text').first.is_editable()
        checks["tooltip"] = page.locator('.cue[data-index="1"] .cue-text').first.get_attribute("title") == "Nhấp để sửa nội dung gốc"
        checks["add_btn"] = page.locator("#addCueBtn").is_visible()
        checks["split_btn"] = page.locator('.cue[data-index="0"] .split-btn').count() == 1
        checks["merge_disabled_last"] = page.locator(f'.cue[data-index="{len(before) - 1}"] .merge-btn').is_disabled()
        checks["undo_hidden"] = not page.locator("#undoBtn").is_visible()
        checks["no_errors"] = not errors
        if errors:
            print("PAGE ERRORS:", errors)
        page.screenshot(path=str(SHOT), full_page=False)
        browser.close()
    with urllib.request.urlopen(f"{BASE}/api/jobs/{JOB}", timeout=10) as resp:
        after = json.loads(resp.read().decode("utf-8"))["cues"]
    checks["data_untouched"] = before == after
    for key, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {key}")
    print("RESULT:", "PASS" if all(checks.values()) else "FAIL")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())