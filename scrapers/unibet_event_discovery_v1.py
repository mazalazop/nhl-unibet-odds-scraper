import os
import re
import json
import time
import traceback
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


KEYWORDS = [
    "joueur", "joueurs", "player", "players",
    "point", "points",
    "but", "buteur", "buteurs", "goal", "goals", "scorer", "score",
    "tir", "tirs", "shot", "shots",
    "performance", "performances",
    "passe", "passes", "assist", "assists"
]


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def slugify(text: str, max_len: int = 80) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9_àâäéèêëïîôöùûüç-]", "", text)
    text = text.strip("_")
    return text[:max_len] if text else "untitled"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class RunLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.lines = []

    def log(self, message: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.lines.append(line)
        print(line)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("\n".join(self.lines), encoding="utf-8")


def wait_settle(page, logger: RunLogger, ms: int = 2500):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception as e:
        logger.log(f"wait domcontentloaded failed: {type(e).__name__}: {e}")
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception as e:
        logger.log(f"wait networkidle skipped/failed: {type(e).__name__}: {e}")
    page.wait_for_timeout(ms)


def safe_body_text(page, logger: RunLogger) -> str:
    try:
        return page.locator("body").inner_text(timeout=10000)
    except Exception as e:
        logger.log(f"body inner_text failed: {type(e).__name__}: {e}")
        return ""


def safe_html(page, logger: RunLogger) -> str:
    try:
        return page.content()
    except Exception as e:
        logger.log(f"page content failed: {type(e).__name__}: {e}")
        return ""


def full_page_scroll(page, logger: RunLogger, steps: int = 10, wait_ms: int = 500):
    try:
        last_y = -1
        for i in range(steps):
            y = page.evaluate("() => window.scrollY")
            height = page.evaluate("() => document.body.scrollHeight")
            viewport = page.evaluate("() => window.innerHeight")
            logger.log(f"scroll step={i+1}/{steps} y={y} height={height} viewport={viewport}")
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(wait_ms)
            new_y = page.evaluate("() => window.scrollY")
            if new_y == last_y:
                break
            last_y = new_y
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(800)
    except Exception as e:
        logger.log(f"scroll failed: {type(e).__name__}: {e}")


def save_snapshot(page, out_dir: Path, prefix: str, logger: RunLogger):
    html = safe_html(page, logger)
    body_text = safe_body_text(page, logger)

    write_text(out_dir / f"{prefix}.html", html)
    write_text(out_dir / f"{prefix}_body.txt", body_text)

    try:
        page.screenshot(path=str(out_dir / f"{prefix}.png"), full_page=True)
        logger.log(f"screenshot saved: {prefix}.png")
    except Exception as e:
        logger.log(f"screenshot failed for {prefix}: {type(e).__name__}: {e}")


def get_locator_text(locator) -> str:
    try:
        txt = locator.inner_text(timeout=2000)
        txt = re.sub(r"\s+", " ", txt).strip()
        return txt
    except Exception:
        return ""


def accept_cookies(page, logger: RunLogger):
    patterns = [
        re.compile(r"accepter", re.I),
        re.compile(r"accept", re.I),
        re.compile(r"j'?accepte", re.I),
        re.compile(r"tout accepter", re.I),
        re.compile(r"allow all", re.I),
    ]

    for pattern in patterns:
        try:
            btn = page.get_by_role("button", name=pattern).first
            if btn.is_visible(timeout=2000):
                btn.click(timeout=3000)
                logger.log(f"cookie button clicked via role/button pattern={pattern.pattern}")
                page.wait_for_timeout(1500)
                return True
        except Exception:
            pass

    selectors = [
        "button:has-text('Accepter')",
        "button:has-text('Tout accepter')",
        "button:has-text('J’accepte')",
        "button:has-text('J\\'accepte')",
        "button:has-text('Accept')",
        "button:has-text('Allow all')",
        "[id*='cookie'] button",
        "[class*='cookie'] button",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=2000):
                txt = get_locator_text(loc)
                loc.click(timeout=3000)
                logger.log(f"cookie button clicked via selector={sel} text={txt[:80]}")
                page.wait_for_timeout(1500)
                return True
        except Exception:
            pass

    logger.log("no cookie button clicked")
    return False


def collect_clickables(page, logger: RunLogger):
    selector = "button, [role='button'], a, summary, h1, h2, h3, h4, h5, [aria-expanded]"
    locs = page.locator(selector)
    items = []
    seen = set()

    try:
        count = locs.count()
    except Exception as e:
        logger.log(f"collect_clickables count failed: {type(e).__name__}: {e}")
        return items

    logger.log(f"collect_clickables total raw={count}")

    for i in range(min(count, 400)):
        loc = locs.nth(i)
        try:
            visible = loc.is_visible(timeout=800)
        except Exception:
            visible = False

        try:
            enabled = loc.is_enabled(timeout=800)
        except Exception:
            enabled = False

        text = get_locator_text(loc)
        if not text:
            try:
                text = (loc.get_attribute("aria-label") or "").strip()
            except Exception:
                text = ""

        try:
            meta = loc.evaluate(
                """el => ({
                    tag: (el.tagName || '').toLowerCase(),
                    role: el.getAttribute('role') || '',
                    ariaExpanded: el.getAttribute('aria-expanded') || '',
                    href: el.getAttribute('href') || '',
                    cls: el.className || ''
                })"""
            )
        except Exception:
            meta = {"tag": "", "role": "", "ariaExpanded": "", "href": "", "cls": ""}

        text_norm = re.sub(r"\s+", " ", text).strip()
        if not text_norm:
            continue

        fingerprint = (
            meta.get("tag", ""),
            meta.get("role", ""),
            text_norm[:160],
            meta.get("href", "")
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        item = {
            "index": i,
            "text": text_norm[:500],
            "visible": visible,
            "enabled": enabled,
            "tag": meta.get("tag", ""),
            "role": meta.get("role", ""),
            "aria_expanded": meta.get("ariaExpanded", ""),
            "href": meta.get("href", ""),
            "class": str(meta.get("cls", ""))[:300],
        }
        items.append(item)

    logger.log(f"collect_clickables unique_kept={len(items)}")
    return items


def filter_candidates(clickables):
    candidates = []
    seen_texts = set()

    for item in clickables:
        text = item.get("text", "")
        text_l = text.lower()
        if len(text_l) > 220:
            continue
        if any(k in text_l for k in KEYWORDS):
            text_key = text_l[:180]
            if text_key in seen_texts:
                continue
            seen_texts.add(text_key)
            candidates.append(item)

    return candidates


def click_candidate_by_index(page, index: int, logger: RunLogger) -> dict:
    selector = "button, [role='button'], a, summary, h1, h2, h3, h4, h5, [aria-expanded]"
    result = {
        "success": False,
        "method": None,
        "error": None,
        "before_text": None,
        "after_text": None,
    }

    loc = page.locator(selector).nth(index)
    result["before_text"] = get_locator_text(loc)

    try:
        loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    try:
        loc.click(timeout=4000)
        result["success"] = True
        result["method"] = "locator.click"
        result["after_text"] = get_locator_text(loc)
        return result
    except Exception as e1:
        result["error"] = f"locator.click failed: {type(e1).__name__}: {e1}"

    try:
        loc.evaluate("(el) => el.click()")
        result["success"] = True
        result["method"] = "dom.click()"
        result["after_text"] = get_locator_text(loc)
        return result
    except Exception as e2:
        result["error"] = (
            result["error"] + " | " if result["error"] else ""
        ) + f"dom.click failed: {type(e2).__name__}: {e2}"

    logger.log(f"candidate index={index} click failed")
    return result


def main():
    event_url = os.getenv("UNIBET_EVENT_URL", "").strip()
    if not event_url:
        raise ValueError("UNIBET_EVENT_URL is required")

    headless = env_bool("PW_HEADLESS", True)
    slow_mo = int(os.getenv("PW_SLOWMO_MS", "0"))
    timeout_ms = int(os.getenv("PW_TIMEOUT_MS", "45000"))
    max_candidates = int(os.getenv("DISCOVERY_MAX_CANDIDATES", "20"))

    ts = now_ts()
    host = slugify(urlparse(event_url).netloc.replace(".", "_"))
    run_dir = Path("artifacts") / "unibet_event_discovery" / f"{ts}_{host}"
    ensure_dir(run_dir)
    logger = RunLogger(run_dir / "events.log")

    write_json(
        run_dir / "run_meta.json",
        {
            "event_url": event_url,
            "headless": headless,
            "slow_mo": slow_mo,
            "timeout_ms": timeout_ms,
            "max_candidates": max_candidates,
            "keywords": KEYWORDS,
            "run_ts": ts,
        },
    )

    logger.log("run started")
    logger.log(f"event_url={event_url}")
    logger.log(f"headless={headless} slow_mo={slow_mo} timeout_ms={timeout_ms}")

    with sync_playwright() as p:
        browser = None
        context = None
        page = None
        try:
            browser = p.chromium.launch(
                headless=headless,
                slow_mo=slow_mo,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
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
            page.set_default_timeout(timeout_ms)

            logger.log("goto start")
            page.goto(event_url, wait_until="domcontentloaded", timeout=timeout_ms)
            wait_settle(page, logger, ms=3500)

            try:
                title = page.title()
            except Exception:
                title = ""

            write_text(run_dir / "page_title.txt", title)
            write_text(run_dir / "final_url_initial.txt", page.url)
            logger.log(f"title={title}")
            logger.log(f"resolved_url={page.url}")

            accept_cookies(page, logger)
            wait_settle(page, logger, ms=2500)

            full_page_scroll(page, logger, steps=12, wait_ms=450)
            wait_settle(page, logger, ms=1200)

            save_snapshot(page, run_dir, "initial", logger)

            clickables = collect_clickables(page, logger)
            write_json(run_dir / "clickables_all.json", clickables)

            visible_lines = []
            for item in clickables:
                visible_lines.append(
                    f"[idx={item['index']}] tag={item['tag']} role={item['role']} "
                    f"visible={item['visible']} enabled={item['enabled']} "
                    f"expanded={item['aria_expanded']} text={item['text']}"
                )
            write_text(run_dir / "clickables_all.txt", "\n".join(visible_lines))

            candidates = filter_candidates(clickables)
            candidates = [c for c in candidates if c.get("visible")]
            candidates = candidates[:max_candidates]

            write_json(run_dir / "clickables_candidates.json", candidates)
            write_text(
                run_dir / "clickables_candidates.txt",
                "\n".join(
                    [
                        f"[idx={c['index']}] tag={c['tag']} text={c['text']}"
                        for c in candidates
                    ]
                ),
            )
            logger.log(f"candidates_kept={len(candidates)}")

            expansion_results = []
            expansions_dir = run_dir / "expansions"
            ensure_dir(expansions_dir)

            for rank, candidate in enumerate(candidates, start=1):
                idx = candidate["index"]
                candidate_text = candidate.get("text", "")
                candidate_slug = slugify(candidate_text[:60])
                logger.log(f"candidate {rank}/{len(candidates)} idx={idx} text={candidate_text[:120]}")

                try:
                    page.evaluate("() => window.scrollTo(0, 0)")
                    page.wait_for_timeout(500)
                except Exception:
                    pass

                before_url = page.url
                click_result = click_candidate_by_index(page, idx, logger)
                page.wait_for_timeout(1800)
                wait_settle(page, logger, ms=1200)

                prefix = f"{rank:02d}_{candidate_slug}"
                save_snapshot(page, expansions_dir, prefix, logger)

                body_after = safe_body_text(page, logger)
                html_after = safe_html(page, logger)

                result = {
                    "rank": rank,
                    "candidate": candidate,
                    "click_result": click_result,
                    "url_before": before_url,
                    "url_after": page.url,
                    "body_after_len": len(body_after),
                    "html_after_len": len(html_after),
                }
                expansion_results.append(result)

                try:
                    page.go_back(timeout=5000)
                    wait_settle(page, logger, ms=1500)
                    logger.log(f"go_back success after candidate rank={rank}")
                except Exception as e:
                    logger.log(f"go_back failed after candidate rank={rank}: {type(e).__name__}: {e}")
                    try:
                        page.goto(event_url, wait_until="domcontentloaded", timeout=timeout_ms)
                        wait_settle(page, logger, ms=2500)
                        accept_cookies(page, logger)
                        wait_settle(page, logger, ms=1200)
                        full_page_scroll(page, logger, steps=6, wait_ms=350)
                    except Exception as e2:
                        logger.log(f"page reload after go_back failure failed: {type(e2).__name__}: {e2}")

            write_json(run_dir / "expansion_results.json", expansion_results)

            save_snapshot(page, run_dir, "final", logger)
            write_text(run_dir / "final_url_end.txt", page.url)

            summary = {
                "title": title,
                "initial_url": event_url,
                "final_url": page.url,
                "clickables_total": len(clickables),
                "candidates_total": len(candidates),
                "successful_clicks": sum(1 for x in expansion_results if x["click_result"]["success"]),
                "run_dir": str(run_dir),
            }
            write_json(run_dir / "summary.json", summary)
            logger.log(f"summary={summary}")

        except Exception as e:
            logger.log(f"fatal error: {type(e).__name__}: {e}")
            write_text(run_dir / "fatal_traceback.txt", traceback.format_exc())
            if page is not None:
                try:
                    save_snapshot(page, run_dir, "fatal_state", logger)
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
            logger.log("run finished")


if __name__ == "__main__":
    main()
