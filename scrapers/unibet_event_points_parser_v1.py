import os
import re
import json
import traceback
from pathlib import Path
from datetime import datetime

from playwright.sync_api import sync_playwright


MARKET_TEXTS = [
    "NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)",
    "Points",
    "Point",
]

IGNORE_LINES = {
    "Voir plus",
    "Voir moins",
    "Parier",
    "S'inscrire",
    "Se connecter",
    "Paramétrer",
    "Centre d'aide",
}


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


def log(logs, message):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    logs.append(line)
    print(line)


def wait_settle(page, ms=1800):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def body_text(page):
    try:
        return page.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


def snapshot(page, out_dir: Path, name: str):
    try:
        write_text(out_dir / f"{name}.html", page.content())
    except Exception:
        write_text(out_dir / f"{name}.html", "")
    write_text(out_dir / f"{name}_body.txt", body_text(page))
    try:
        page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
    except Exception:
        pass


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
                page.wait_for_timeout(1200)
                return sel
        except Exception:
            pass
    log(logs, "cookie not clicked")
    return None


def click_first_visible_text(page, text, logs):
    candidates = [
        page.get_by_text(text, exact=True),
        page.get_by_text(text),
        page.locator(f"button:has-text('{text}')"),
        page.locator(f"a:has-text('{text}')"),
        page.locator(f"h1:has-text('{text}')"),
        page.locator(f"h2:has-text('{text}')"),
        page.locator(f"h3:has-text('{text}')"),
        page.locator(f"h4:has-text('{text}')"),
        page.locator(f"div:has-text('{text}')"),
        page.locator(f"span:has-text('{text}')"),
    ]

    for base in candidates:
        try:
            count = min(base.count(), 10)
        except Exception:
            count = 0

        for i in range(count):
            loc = base.nth(i)
            try:
                if not loc.is_visible(timeout=800):
                    continue
                label = loc.inner_text(timeout=800).strip()
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=4000)
                log(logs, f"clicked text='{text}' label='{label[:120]}'")
                wait_settle(page, 1200)
                return True, label
            except Exception:
                continue

    log(logs, f"not clickable: {text}")
    return False, None


def get_visible_section_candidates(page):
    patterns = [
        "NOMBRE DE POINTS DU JOUEUR",
        "Points",
        "Point",
    ]
    seen = set()
    results = []

    for pattern in patterns:
        try:
            loc = page.locator("section, article, div").filter(has_text=re.compile(pattern, re.I))
            count = min(loc.count(), 30)
        except Exception:
            count = 0

        for i in range(count):
            item = loc.nth(i)
            try:
                if not item.is_visible(timeout=500):
                    continue
                txt = item.inner_text(timeout=1200)
                txt = re.sub(r"\s+", " ", txt).strip()
                if len(txt) < 20:
                    continue
                if len(txt) > 12000:
                    continue
                key = txt[:300]
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "index": i,
                    "pattern": pattern,
                    "text_preview": txt[:500]
                })
            except Exception:
                continue

    return results


def pick_best_points_section(page, logs):
    loc = page.locator("section, article, div").filter(
        has_text=re.compile(r"NOMBRE DE POINTS DU JOUEUR|Points|Point", re.I)
    )

    try:
        count = min(loc.count(), 40)
    except Exception:
        count = 0

    best = None
    best_text = ""
    best_score = -1

    for i in range(count):
        item = loc.nth(i)
        try:
            if not item.is_visible(timeout=500):
                continue
            txt = item.inner_text(timeout=1200)
            txt_clean = re.sub(r"\s+", " ", txt).strip()
            if len(txt_clean) < 30 or len(txt_clean) > 15000:
                continue

            score = 0
            if "NOMBRE DE POINTS DU JOUEUR" in txt.upper():
                score += 5
            if "VOIR PLUS" in txt.upper() or "VOIR MOINS" in txt.upper():
                score += 3
            score += min(len(txt_clean), 4000) / 1000

            if score > best_score:
                best = item
                best_text = txt
                best_score = score
        except Exception:
            continue

    if best is None:
        log(logs, "no points section found")
        return None, ""

    log(logs, f"points section selected score={best_score}")
    return best, best_text


def click_see_more_in_section(section, page, logs):
    if section is not None:
        for sel in [
            "button:has-text('Voir plus')",
            "a:has-text('Voir plus')",
            "text=Voir plus",
        ]:
            try:
                loc = section.locator(sel)
                count = min(loc.count(), 10)
            except Exception:
                count = 0

            for i in range(count):
                item = loc.nth(i)
                try:
                    if not item.is_visible(timeout=500):
                        continue
                    item.scroll_into_view_if_needed(timeout=2000)
                    label = item.inner_text(timeout=800).strip()
                    item.click(timeout=4000)
                    log(logs, f"clicked see more in section label='{label}'")
                    wait_settle(page, 1500)
                    return True
                except Exception:
                    continue

    try:
        loc = page.get_by_text("Voir plus")
        count = min(loc.count(), 10)
    except Exception:
        count = 0

    for i in range(count):
        item = loc.nth(i)
        try:
            if not item.is_visible(timeout=500):
                continue
            item.scroll_into_view_if_needed(timeout=2000)
            label = item.inner_text(timeout=800).strip()
            item.click(timeout=4000)
            log(logs, f"clicked global see more label='{label}'")
            wait_settle(page, 1500)
            return True
        except Exception:
            continue

    log(logs, "see more not clicked")
    return False


def extract_outer_html(locator):
    try:
        return locator.evaluate("(el) => el.outerHTML")
    except Exception:
        return ""


def normalize_lines(text):
    lines = []
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if not clean:
            continue
        lines.append(clean)
    return lines


def is_odds_line(text):
    return bool(re.fullmatch(r"\d{1,2}[.,]\d{2}", text))


def looks_like_player_name(text):
    if text in IGNORE_LINES:
        return False
    if is_odds_line(text):
        return False
    if text.isupper() and len(text) > 20:
        return False
    if any(x in text.lower() for x in ["nombre de points", "prolongations", "voir plus", "voir moins"]):
        return False
    return len(text) >= 3


def parse_player_odds(lines):
    rows = []
    for i in range(len(lines) - 1):
        a = lines[i]
        b = lines[i + 1]
        if looks_like_player_name(a) and is_odds_line(b):
            rows.append({
                "player_name_raw": a,
                "odds_raw": b.replace(",", ".")
            })

    dedup = []
    seen = set()
    for row in rows:
        key = (row["player_name_raw"], row["odds_raw"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)
    return dedup


def main():
    event_url = os.getenv("UNIBET_EVENT_URL", "").strip()
    if not event_url:
        raise ValueError("UNIBET_EVENT_URL is required")

    headless = os.getenv("PW_HEADLESS", "true").lower() == "true"

    ts = now_ts()
    out_dir = Path("artifacts") / "unibet_event_points_parser_v1" / ts
    ensure_dir(out_dir)

    logs = []

    with sync_playwright() as p:
        browser = None
        context = None
        page = None

        try:
            browser = p.chromium.launch(headless=headless)
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
            wait_settle(page, 2500)

            cookie_clicked = accept_cookies(page, logs)
            wait_settle(page, 1500)

            snapshot(page, out_dir, "initial")

            clicked_market = None
            clicked_market_label = None
            for text in MARKET_TEXTS:
                ok, label = click_first_visible_text(page, text, logs)
                if ok:
                    clicked_market = text
                    clicked_market_label = label
                    break

            snapshot(page, out_dir, "after_market_click")

            section_before, section_before_text = pick_best_points_section(page, logs)
            write_text(out_dir / "points_section_before.txt", section_before_text)
            write_text(out_dir / "points_section_before.html", extract_outer_html(section_before) if section_before else "")

            section_candidates = get_visible_section_candidates(page)
            write_json(out_dir / "points_section_candidates.json", section_candidates)

            see_more_clicked = click_see_more_in_section(section_before, page, logs)
            snapshot(page, out_dir, "after_see_more")

            section_after, section_after_text = pick_best_points_section(page, logs)
            write_text(out_dir / "points_section_after.txt", section_after_text)
            write_text(out_dir / "points_section_after.html", extract_outer_html(section_after) if section_after else "")

            lines = normalize_lines(section_after_text or section_before_text)
            write_json(out_dir / "points_section_lines.json", lines)

            parsed_rows = parse_player_odds(lines)
            write_json(out_dir / "points_market_rows_raw.json", parsed_rows)

            summary = {
                "title": page.title(),
                "event_url": event_url,
                "final_url": page.url,
                "cookie_clicked": cookie_clicked,
                "clicked_market": clicked_market,
                "clicked_market_label": clicked_market_label,
                "see_more_clicked": see_more_clicked,
                "section_candidates": len(section_candidates),
                "section_lines": len(lines),
                "parsed_rows": len(parsed_rows),
                "run_dir": str(out_dir),
            }

            write_json(out_dir / "summary.json", summary)
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
