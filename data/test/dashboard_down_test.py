from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "data" / "test" / "shots"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.route("http://127.0.0.1:8787/**", lambda route: route.abort())
    page = context.new_page()
    page.goto("http://127.0.0.1:8088/#subextract", wait_until="domcontentloaded")
    page.wait_for_selector("#sxDown", state="visible", timeout=10000)
    print("down status:", page.text_content("#sxStatusText"))
    page.screenshot(path=str(SHOTS / "dash_05_down.png"))

    context.unroute("http://127.0.0.1:8787/**")
    page.click("#sxDown button:has-text('Thử lại')")
    page.wait_for_selector("#sxStatus.up", timeout=15000)
    print("recovered status:", page.text_content("#sxStatusText"))
    page.wait_for_timeout(2500)
    print("down hidden:", page.locator("#sxDown").is_hidden())
    frame = page.frame_locator("#sxFrame")
    print("frame heading:", frame.locator("h1").first.text_content())

    direct = context.new_page()
    direct.goto("http://127.0.0.1:8787/", wait_until="domcontentloaded")
    print("normal topbar display:", direct.locator(".topbar").evaluate("el => getComputedStyle(el).display"))
    print("normal body class:", direct.locator("body").get_attribute("class"))
    browser.close()
