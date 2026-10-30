import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "data" / "test" / "shots"
SHOTS.mkdir(exist_ok=True)
VIDEO = ROOT / "data" / "test" / "hardsub.mp4"

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 950})
    logs = []
    page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
    page.goto("http://127.0.0.1:8787/", wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.screenshot(path=str(SHOTS / "01_landing.png"))

    page.set_input_files("#fileInput", str(VIDEO))
    page.wait_for_selector("#workspace:not(.hidden)", timeout=60000)
    page.wait_for_timeout(1500)
    page.screenshot(path=str(SHOTS / "02_analyzer.png"))

    layer = page.locator("#roiLayer").bounding_box()
    print("roiLayer:", layer)
    print("roi before:", page.text_content("#roiText"))

    page.click("#roiSelectBtn")
    box = layer
    x0 = box["x"] + box["width"] * 0.10
    x1 = box["x"] + box["width"] * 0.90
    y0 = box["y"] + box["height"] * 0.55
    y1 = box["y"] + box["height"] * 0.92
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=10)
    page.mouse.move(x1, y1, steps=10)
    page.mouse.up()
    page.wait_for_timeout(300)
    print("roi after drag:", page.text_content("#roiText"))
    page.screenshot(path=str(SHOTS / "03_roi_drag.png"))

    page.click("#roiReset")
    page.wait_for_timeout(200)
    print("roi after reset:", page.text_content("#roiText"))
    page.check("#langZh")
    page.select_option("#fpsSelect", "5")
    page.click("#startBtn")
    page.wait_for_timeout(1500)
    page.screenshot(path=str(SHOTS / "04_running.png"))

    expect(page.locator("#resultCard")).to_be_visible(timeout=180000)
    page.wait_for_timeout(800)
    print("cue count:", page.text_content("#cueCount"))
    print("stats:", page.text_content("#statsLine"))
    print("cues:", json.dumps(page.evaluate("() => state.cues"), ensure_ascii=False))
    page.screenshot(path=str(SHOTS / "05_result.png"), full_page=True)

    first = page.locator(".cue-text").first
    first.fill("Xin chào, đây là phụ đề tiếng Việt có dấu.")
    page.wait_for_timeout(1400)
    print("save state:", page.text_content("#saveState"))

    with page.expect_download() as dl:
        page.click("#exportSrt")
    print("downloaded:", dl.value.suggested_filename)

    page.click("#roiSelectBtn")
    page.click("#roiFull")
    page.wait_for_timeout(200)
    print("roi full:", page.text_content("#roiText"))
    page.click("#roiSelectBtn")

    print("console:", logs[-6:])
    browser.close()
