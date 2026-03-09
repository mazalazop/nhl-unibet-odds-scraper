import os
import re
import json
import traceback
from pathlib import Path
from datetime import datetime

from playwright.sync_api import sync_playwright


TARGETS = [
    "Buteurs",
    "Buteur",
    "Points",
    "Point",
    "Voir plus",
]


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def body_text(page):
    try:
        return page.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


def html_text(page):
    try:
        return page.content()
    except Exception:
        return ""


def snapshot(page, out_dir: Path, name: str):
    write_text(out_dir / f"{name}.html", html_text(page))
    write_text(out_dir / f"{name}_body.txt", body_text(page))
    try:
        page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
    except Exception:
        pass


def log(logs, message):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    logs.append(line)
    print(line)


def wait_settle(page, ms=2000):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def accept_cookies(page, logs):
    selectors = [
        "button:has-text('Accepter')",
        "button:has-text('Tout accepter')",
        "button:has-text('Tout Accepter')",
        "button:has-text('J’accepte')",
        "button:has-text('Accept')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                loc.click(timeout=3000)
                log(logs, f"cookie clicked: {sel}")
                page.wait_for_timeout(1500)
                return sel
        except Exception:
            pass
    log(logs, "cookie not clicked")
    return None


def find_visible_text_candidates(page):
    selector = "button, a, h1, h2, h3, h4, [role='button'], [aria-expanded]"
    locs = page.locator(selector)
    results = []

    try:
        count = locs.count()
    except Exception:
        return results

    for i in range(min(count, 300)):
        loc = locs.nth(i)
        try:
            if not loc.is_visible(timeout=500):
                continue
        except Exception:
            continue

        try:
            txt = loc.inner_text(timeout=1000)
        except Exception:
            txt = ""

        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue

        try:
            tag = loc.evaluate("(el) => (el.tagName || '').toLowerCase()")
        except Exception:
            tag = ""

        results.append({
            "index": i,
            "text": txt[:300],
            "tag": tag
        })

    return results


def click_first_text(page, text, logs, exact=False):
    candidates = []

    try:
        if exact:
            candidates.append(page.get_by_text(text, exact=True).first)
        candidates.append(page.get_by_text(text).first)
    except Exception:
        pass

    selectors = [
        f"button:has-text('{text}')",
        f"a:has-text('{text}')",
        f"h1:has-text('{text}')",
        f"h2:has-text('{text}')",
        f"h3:has-text('{text}')",
        f"h4:has-text('{text}')",
        f"[role='button']:has-text('{text}')",
    ]

    for sel in selectors:
        try:
            candidates.append(page.locator(sel).first)
        except Exception:
            pass

    for loc in candidates:
        try:
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=1200):
                continue
            label = ""
            try:
                label = loc.inner_text(timeout=1000)
            except Exception:
                pass
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=4000)
            log(logs, f"clicked: target={text} label={label[:120]}")
            page.wait_for_timeout(1500)
            wait_settle(page, 1200)
            return True
        except Exception:
            continue

    log(logs, f"not found/clickable: {text}")
    return False


def main():
    event_url = os.getenv("UNIBET_EVENT_URL", "").strip()
    if not event_url:
        raise ValueError("UNIBET_EVENT_URL is required")

    ts = now_ts()
    out_dir = Path("artifacts") / "unibet_event_discovery_v2" / ts
    ensure_dir(out_dir)

    logs = []
    click_results = []

    with sync_playwright() as p:
        browser = None
        context = None
        page = None

        try:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1600, "height": 2200},
                locale="fr-FR",
                timezone_id="Europe/Paris",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.set_default_timeout(45000)

            log(logs, f"goto: {event_url}")
            page.goto(event_url, wait_until="domcontentloaded", timeout=45000)
            wait_settle(page, 3000)

            cookie_clicked = accept_cookies(page, logs)
            wait_settle(page, 2000)

            snapshot(page, out_dir, "initial")

            initial_candidates = find_visible_text_candidates(page)
            write_json(out_dir / "visible_clickables_initial.json", initial_candidates)

            sequences = [
                ["Buteurs", "Voir plus"],
                ["Buteur", "Voir plus"],
                ["Points", "Voir plus"],
                ["Point", "Voir plus"],
                ["Voir plus"],
            ]

            for seq_idx, seq in enumerate(sequences, start=1):
                log(logs, f"sequence_start: {seq}")
                seq_result = {
                    "sequence": seq,
                    "steps": []
                }

                try:
                    page.goto(event_url, wait_until="domcontentloaded", timeout=45000)
                    wait_settle(page, 2500)
                    accept_cookies(page, logs)
                    wait_settle(page, 1500)
                except Exception:
                    pass

                snapshot(page, out_dir, f"seq_{seq_idx}_before")

                for step_idx, target in enumerate(seq, start=1):
                    ok = click_first_text(page, target, logs, exact=False)
                    snapshot(page, out_dir, f"seq_{seq_idx}_step_{step_idx}_{target.lower().replace(' ', '_')}")
                    seq_result["steps"].append({
                        "target": target,
                        "success": ok,
                        "url": page.url,
                        "body_len": len(body_text(page))
                    })

                seq_result["final_body_preview"] = body_text(page)[:5000]
                click_results.append(seq_result)

            final_candidates = find_visible_text_candidates(page)
            write_json(out_dir / "visible_clickables_final.json", final_candidates)

            summary = {
                "title": page.title(),
                "initial_url": event_url,
                "final_url": page.url,
                "cookie_clicked": cookie_clicked,
                "initial_visible_clickables": len(initial_candidates),
                "final_visible_clickables": len(final_candidates),
                "sequences_tested": len(sequences),
                "run_dir": str(out_dir),
            }

            write_json(out_dir / "summary.json", summary)
            write_json(out_dir / "click_results.json", click_results)
            write_text(out_dir / "events.log", "\n".join(logs))

        except Exception as e:
            write_text(out_dir / "fatal_traceback.txt", traceback.format_exc())
            write_text(out_dir / "events.log", "\n".join(logs + [f"fatal: {type(e).__name__}: {e}"]))
            if page is not None:
                try:
                    snapshot(page, out_dir, "fatal_state")
                except Exception:
                    pass
            raise
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
