import os
import re
import json
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

UNIBET_URL = os.getenv("UNIBET_URL", "https://www.unibet.fr/sport/hockey-sur-glace")
ARTIFACTS_DIR = Path("artifacts")
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_SELECTORS = [
    "button:has-text('Accepter')",
    "button:has-text('Tout accepter')",
    "button:has-text('J’accepte')",
    "button:has-text('J\\'accepte')",
    "button:has-text('OK')",
]

async def click_first_existing(page, selectors):
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if await loc.count() > 0:
                await loc.first.click(timeout=3000)
                await page.wait_for_timeout(2000)
                return selector
        except Exception:
            continue
    return None

async def collect_texts(locator, limit=200):
    items = []
    try:
        count = await locator.count()
        count = min(count, limit)
        for i in range(count):
            try:
                txt = await locator.nth(i).inner_text(timeout=1000)
                txt = re.sub(r"\s+", " ", txt).strip()
                if txt:
                    items.append(txt)
            except Exception:
                continue
    except Exception:
        pass
    return items

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        page = await browser.new_page(locale="fr-FR", viewport={"width": 1440, "height": 2200})
        await page.goto(UNIBET_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(5000)

        clicked_cookie = await click_first_existing(page, COOKIE_SELECTORS)

        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

        await page.screenshot(path=str(ARTIFACTS_DIR / "01_page.png"), full_page=True)

        html = await page.content()
        (ARTIFACTS_DIR / "02_page.html").write_text(html, encoding="utf-8")

        body_text = await page.locator("body").inner_text(timeout=15000)
        (ARTIFACTS_DIR / "03_body.txt").write_text(body_text, encoding="utf-8")

        buttons = await collect_texts(page.locator("button"), limit=300)
        links = await collect_texts(page.locator("a"), limit=500)
        headings = await collect_texts(page.locator("h1, h2, h3, h4"), limit=100)

        debug = {
            "url": UNIBET_URL,
            "cookie_clicked": clicked_cookie,
            "body_len": len(body_text),
            "buttons_count": len(buttons),
            "links_count": len(links),
            "headings_count": len(headings),
            "buttons_sample": buttons[:100],
            "links_sample": links[:150],
            "headings_sample": headings[:50],
        }

        (ARTIFACTS_DIR / "04_debug.json").write_text(
            json.dumps(debug, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print("=== URL ===")
        print(UNIBET_URL)
        print("=== BODY LEN ===")
        print(len(body_text))
        print("=== HEADINGS SAMPLE ===")
        print(headings[:20])

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())

