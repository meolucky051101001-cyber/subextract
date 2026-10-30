"""Kiểm tra sửa dòng phụ đề trực tiếp trong UI bằng trình duyệt thật (Playwright).

Chạy trên server cô lập 8790 (job 6 cue). Test tự đưa job về baseline trước khi chạy.
- Textarea nội dung gốc / bản dịch sửa được, có tooltip, tự lưu xuống job.json + đĩa.
- Thêm dòng, tách dòng tại con trỏ, gộp dòng dưới, hoàn tác nhiều bước, xóa dòng.
- Xuất SRT chứa nội dung đã sửa.
- Chụp ảnh UI vào data/test/shots/cue_edit_after.png (hoặc --shot ten).

Chạy: python data/test/cue_edit_browser.py [--shot ten]
"""
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8790"
JOB = "6ca01c10b3db"
ROOT = Path(__file__).resolve().parents[2]
SRC_JOB = ROOT / "data" / "jobs" / JOB / "job.json"
TMP_JOB = ROOT / "data" / "test" / "tmp_cueedit" / "jobs" / JOB / "job.json"
SHOT = ROOT / "data" / "test" / "shots"


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def server_cues() -> list[dict]:
    return api(f"/api/jobs/{JOB}")["cues"]


def main() -> int:
    name = "cue_edit_after"
    if "--shot" in sys.argv:
        name = sys.argv[sys.argv.index("--shot") + 1]
    checks: dict[str, bool] = {}
    baseline = json.loads(SRC_JOB.read_text(encoding="utf-8"))["cues"]

    # Đưa job về baseline để test lặp lại được
    api(f"/api/jobs/{JOB}/cues", method="PUT", body={
        "cues": [{"start": c["start"], "end": c["end"], "text": c["text"], "trans": c.get("trans", "")} for c in baseline]
    })

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.add_init_script(f"try{{localStorage.setItem('subx_job','{JOB}')}}catch(e){{}}")
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_selector(".cue", timeout=15000)
        checks["rendered"] = page.locator(".cue").count() == 6

        first = page.locator('.cue[data-index="0"] .cue-text').first
        checks["not_readonly"] = first.is_editable()
        checks["has_title"] = first.get_attribute("title") == "Nhấp để sửa nội dung gốc"
        checks["add_btn_visible"] = page.locator("#addCueBtn").is_visible()

        # Sửa nội dung gốc
        first.click()
        first.press("Control+a")
        first.type("Mở hộp", delay=15)
        page.wait_for_timeout(1400)
        checks["saved_state"] = page.locator("#saveState").inner_text().strip() == "Đã lưu"
        checks["text_persisted"] = server_cues()[0]["text"] == "Mở hộp"

        # Sửa bản dịch cue 2
        trans = page.locator('.cue[data-index="1"] .cue-trans').first
        trans.click()
        trans.press("Control+a")
        trans.type("Đây là loại một lớp.", delay=10)
        page.wait_for_timeout(1400)
        checks["trans_persisted"] = server_cues()[1]["trans"] == "Đây là loại một lớp."

        # Sửa thời gian bắt đầu cue 1
        t = page.locator('.cue[data-index="0"] .time-input[data-field="start"]').first
        t.click()
        t.press("Control+a")
        t.type("00:00:02,900")
        t.press("Tab")
        page.wait_for_timeout(1400)
        checks["time_persisted"] = abs(server_cues()[0]["start"] - 2.9) < 0.001

        # Thêm dòng mới
        page.locator("#addCueBtn").click()
        page.keyboard.type("Dòng mới", delay=15)
        page.wait_for_timeout(1400)
        added = server_cues()
        checks["add_persisted"] = len(added) == 7 and added[-1]["text"] == "Dòng mới"
        checks["add_ui_count"] = page.locator(".cue").count() == 7
        checks["undo_visible"] = page.locator("#undoBtn").is_visible()

        # Tách dòng cue 2 tại con trỏ (sau 3 ký tự)
        ta = page.locator('.cue[data-index="1"] .cue-text').first
        ta.evaluate("el => { el.focus(); el.setSelectionRange(3, 3); }")
        page.locator('.cue[data-index="1"] .split-btn').click()
        page.wait_for_timeout(1400)
        split = server_cues()
        checks["split_count"] = len(split) == 8
        checks["split_text"] = split[1]["text"] == "这个是" and split[2]["text"] == "单层的"
        checks["split_time"] = abs(split[1]["end"] - 8.875) < 0.01 and abs(split[2]["start"] - 8.875) < 0.01

        # Gộp dòng dưới
        page.locator('.cue[data-index="1"] .merge-btn').click()
        page.wait_for_timeout(1400)
        merged = server_cues()
        checks["merge_count"] = len(merged) == 7
        checks["merge_text"] = merged[1]["text"] == "这个是 单层的"
        checks["merge_end"] = abs(merged[1]["end"] - 10.375) < 0.01

        # Hoàn tác 2 bước: gộp → tách, rồi tách → trước khi tách
        page.locator("#undoBtn").click()
        page.wait_for_timeout(1400)
        checks["undo_merge"] = len(server_cues()) == 8
        page.locator("#undoBtn").click()
        page.wait_for_timeout(1400)
        back = server_cues()
        checks["undo_split"] = len(back) == 7 and back[1]["text"] == "这个是单层的"

        # Xóa dòng vừa thêm
        page.locator('.cue[data-index="6"] .del-btn').click()
        page.wait_for_timeout(1400)
        checks["delete_persisted"] = len(server_cues()) == 6
        checks["delete_ui_count"] = page.locator(".cue").count() == 6

        # Xuất SRT chứa nội dung đã sửa
        with page.expect_download() as dl:
            page.locator("#exportSrt").click()
        text = dl.value.path().read_text(encoding="utf-8", errors="replace")
        checks["export_edited"] = "Mở hộp" in text
        checks["export_trans"] = "Đây là loại một lớp." in text

        SHOT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOT / f"{name}.png"), full_page=False)
        checks["no_page_errors"] = not errors
        if errors:
            print("PAGE ERRORS:", errors)
        browser.close()

    disk = json.loads(TMP_JOB.read_text(encoding="utf-8"))
    checks["disk_persisted"] = disk["cues"][0]["text"] == "Mở hộp"

    for key, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {key}")
    print("RESULT:", "PASS" if all(checks.values()) else "FAIL")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())