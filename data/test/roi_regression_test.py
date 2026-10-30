import json
import shutil
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "data" / "test" / "shots"
VIDEO = ROOT / "data" / "test" / "hardsub.mp4"
JOBS = ROOT / "data" / "jobs"

before_jobs = {p.name for p in JOBS.iterdir()} if JOBS.exists() else set()
print("jobs before:", sorted(before_jobs))


def roi_text(frame):
    return frame.locator("#roiText").text_content().strip()


def drag(page, x0, y0, x1, y1):
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=12)
    page.mouse.up()


with sync_playwright() as p:
    try:
        browser = p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        browser = p.chromium.launch(headless=True)

    # --- 1. Landscape video, direct page ---
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:8787/?t=roi", wait_until="domcontentloaded")
    page.set_input_files("#fileInput", str(VIDEO))
    page.wait_for_selector("#workspace:not(.hidden)", timeout=60000)
    page.wait_for_timeout(1200)

    print("[direct] handles:", page.locator(".roi-handle").count())
    print("[direct] hint hidden:", "hidden" in (page.locator("#roiHint").get_attribute("class") or ""))
    print("[direct] roi at load:", roi_text(page))

    layer = page.locator("#roiLayer").bounding_box()
    drag(page, layer["x"] + layer["width"] * 0.2, layer["y"] + layer["height"] * 0.2,
         layer["x"] + layer["width"] * 0.8, layer["y"] + layer["height"] * 0.8)
    print("[direct] roi after drag without select:", roi_text(page))

    page.click("#roiSelectBtn")
    print("[direct] hint after select:", not ("hidden" in (page.locator("#roiHint").get_attribute("class") or "")))
    drag(page, layer["x"] + layer["width"] * 0.10, layer["y"] + layer["height"] * 0.55,
         layer["x"] + layer["width"] * 0.90, layer["y"] + layer["height"] * 0.92)
    print("[direct] roi after draw:", roi_text(page), "| btn:", page.text_content("#roiSelectBtn").strip())

    se = page.locator('.roi-handle[data-dir="se"]').bounding_box()
    drag(page, se["x"] + se["width"] / 2, se["y"] + se["height"] / 2,
         se["x"] + se["width"] / 2 - 140, se["y"] + se["height"] / 2 - 40)
    print("[direct] roi after se resize:", roi_text(page))

    nw = page.locator('.roi-handle[data-dir="nw"]').bounding_box()
    drag(page, nw["x"] + nw["width"] / 2, nw["y"] + nw["height"] / 2,
         nw["x"] + nw["width"] / 2 + 120, nw["y"] + nw["height"] / 2 + 30)
    print("[direct] roi after nw resize:", roi_text(page))
    page.screenshot(path=str(SHOTS / "roi_landscape_fixed.png"))
    print("[direct] pageerror:", errors)
    page.close()

    # --- 2. Inside dashboard iframe ---
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.goto("http://127.0.0.1:8088/#subextract", wait_until="domcontentloaded")
    page.wait_for_selector("#subextractPage.open", timeout=15000)
    frame = page.frame_locator("#sxFrame")
    page.wait_for_timeout(2500)
    print("[iframe] handles:", frame.locator(".roi-handle").count())
    print("[iframe] hint hidden:", "hidden" in (frame.locator("#roiHint").get_attribute("class") or ""))
    frame.locator("#fileInput").set_input_files(str(VIDEO))
    frame.locator("#workspace:not(.hidden)").wait_for(timeout=60000)
    page.wait_for_timeout(1200)
    frame.locator("#roiSelectBtn").click()
    layer = frame.locator("#roiLayer").bounding_box()
    drag(page, layer["x"] + layer["width"] * 0.1, layer["y"] + layer["height"] * 0.6,
         layer["x"] + layer["width"] * 0.85, layer["y"] + layer["height"] * 0.95)
    print("[iframe] roi after draw:", frame.locator("#roiText").text_content().strip())
    se = frame.locator('.roi-handle[data-dir="se"]').bounding_box()
    drag(page, se["x"] + se["width"] / 2, se["y"] + se["height"] / 2,
         se["x"] + se["width"] / 2 - 180, se["y"] + se["height"] / 2)
    print("[iframe] roi after se resize:", frame.locator("#roiText").text_content().strip())
    page.screenshot(path=str(SHOTS / "roi_iframe_fixed.png"))
    browser.close()

after_jobs = {p.name for p in JOBS.iterdir()} if JOBS.exists() else set()
orphans = sorted(n for n in after_jobs - before_jobs if not (JOBS / n / "job.json").exists())
for name in orphans:
    shutil.rmtree(JOBS / name, ignore_errors=True)
print("orphan jobs removed:", orphans)
