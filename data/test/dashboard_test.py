import json
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "data" / "test" / "shots"
SHOTS.mkdir(exist_ok=True)
VIDEO = ROOT / "data" / "test" / "hardsub.mp4"


def batch_status():
    with urllib.request.urlopen("http://127.0.0.1:8088/api/status", timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("job_id"), data.get("active"), data.get("queue_index"), data.get("step_name")


before = batch_status()
print("batch before:", before)

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    logs = []
    page.on("console", lambda m: logs.append(f"top {m.type}: {m.text}"))
    page.on("pageerror", lambda e: logs.append(f"top pageerror: {e}"))

    page.goto("http://127.0.0.1:8088/", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    print("nav count:", page.locator('.hw-status a[href="#subextract"]').count())
    page.screenshot(path=str(SHOTS / "dash_01_header.png"))

    page.click('.hw-status a[href="#subextract"]')
    page.wait_for_selector("#subextractPage.open", timeout=10000)
    print("hash:", page.evaluate("location.hash"))
    page.wait_for_selector("#sxStatus.up", timeout=15000)
    print("status:", page.text_content("#sxStatusText"))
    page.wait_for_timeout(2500)
    page.screenshot(path=str(SHOTS / "dash_02_page.png"))

    frame = page.frame_locator("#sxFrame")
    print("embed class:", frame.locator("body").get_attribute("class"))
    print("topbar display:", frame.locator(".topbar").evaluate("el => getComputedStyle(el).display"))
    frame.locator("#fileInput").set_input_files(str(VIDEO))
    frame.locator("#workspace:not(.hidden)").wait_for(timeout=60000)
    page.wait_for_timeout(1500)

    layer = frame.locator("#roiLayer").bounding_box()
    print("roiLayer:", layer)
    print("roi before:", frame.locator("#roiText").text_content())
    frame.locator("#roiSelectBtn").click()
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
    page.wait_for_timeout(400)
    print("roi after drag:", frame.locator("#roiText").text_content())
    page.screenshot(path=str(SHOTS / "dash_03_roi.png"))

    frame.locator("#roiReset").click()
    page.wait_for_timeout(200)
    print("roi after reset:", frame.locator("#roiText").text_content())

    frame.locator("button:has-text('← Dashboard')").count()
    page.click("#subextractPage button:has-text('← Dashboard')")
    page.wait_for_timeout(300)
    print("overlay open after close:", page.locator("#subextractPage.open").count())
    print("hash after close:", page.evaluate("location.hash"))

    page.goto("http://127.0.0.1:8088/#subextract", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    print("deep link open:", page.locator("#subextractPage.open").count())
    page.screenshot(path=str(SHOTS / "dash_04_deeplink.png"))

    print("console:", logs[-8:])
    browser.close()

after = batch_status()
print("batch after:", after)
print("batch untouched:", before[0] == after[0] and after[1] is True)
