#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scrapers/unibet_points_discover_event_urls.py

But
---
Découvrir automatiquement les URLs des pages match Unibet depuis le hub NHL.

La stratégie n'est plus limitée aux `a[href]`.
Le script combine 4 sources, dans cet ordre :
1. URLs détectées dans les réponses réseau (XHR / fetch / JSON / texte)
2. URLs détectées dans le HTML du hub
3. URLs détectées dans les href du DOM
4. Fallback robuste par clic UI sur les blocs "+ N paris" / marketnumber

Entrées via variables d'environnement
-------------------------------------
- UNIBET_HUB_URL        : URL du hub à ouvrir
- PW_HEADLESS           : true / false
- DISCOVERY_MAX_MATCHES : limite de clics UI (0 = auto)

Sorties
-------
Dans artifacts/unibet_points_discover_event_urls/<timestamp>/ :
- discovered_event_urls.json
- discovered_event_urls.txt
- page_title.txt
- final_url.txt
- cookie_action.txt
- raw_anchor_links.json
- raw_anchor_links.txt
- regex_url_candidates.json
- regex_url_candidates.txt
- network_response_index.json
- network_event_url_candidates.json
- hub_cards_snapshot.json
- ui_click_discovery.json
- filtered_candidate_urls.json
- filtered_candidate_urls.txt
- page_source.html
- hub_screenshot.png

Le format de sortie principal reste compatible avec le workflow existant :
`discovered_event_urls.json` contient `event_urls`.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_HUB_URL = (
    "https://www.unibet.fr/sport/hockey-sur-glace/etats-unis/nhl"
    "?filter=R%C3%A9sultat&subFilter=R%C3%A9sultat+du+match"
)
ARTIFACTS_ROOT = Path("artifacts") / "unibet_points_discover_event_urls"
EVENT_URL_PATTERNS = [
    re.compile(r"https?://(?:www\.)?unibet\.fr/sport/ice-hockey/event/[^\s\"'<>]+\.html", re.I),
    re.compile(r"https?://(?:www\.)?unibet\.fr/event/[^\s\"'<>]+\.html", re.I),
    re.compile(r"/(?:sport/ice-hockey/)?event/[^\s\"'<>]+\.html", re.I),
]
MARKETNUMBER_SELECTORS: Sequence[str] = (
    "section.marketnumber[title*='Voir tous les paris']",
    "section.marketnumber",
    "[id='cps-marketnumber']",
)
CLICK_WAIT_MS = 9000
NAV_WAIT_MS = 30000
SCROLL_WAIT_MS = 1000


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def normalize_url(url: str) -> str:
    raw = safe_text(url)
    if not raw:
        return ""

    parsed = urlparse(raw)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if not path:
        path = "/"

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query_items = sorted(query_items)
    query = "&".join(f"{k}={v}" for k, v in query_items)

    return urlunparse((scheme, netloc, path, "", query, ""))


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_lines(path: Path, lines: Sequence[str]) -> None:
    text = "\n".join(lines)
    if lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def dedupe_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        normalized = normalize_url(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def extract_event_urls_from_text(text: str, base_url: str) -> List[str]:
    found: List[str] = []
    if not text:
        return found

    for pattern in EVENT_URL_PATTERNS:
        for match in pattern.findall(text):
            if match.startswith("/"):
                found.append(urljoin(base_url, match))
            else:
                found.append(match)

    return dedupe_keep_order(found)


def looks_like_unibet_event_url(url: str) -> bool:
    value = normalize_url(url)
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc.endswith("unibet.fr"):
        return False
    path = (parsed.path or "").lower()
    return "/event/" in path and path.endswith(".html")


def collect_anchor_links(page: Page) -> List[Dict[str, str]]:
    try:
        raw = page.evaluate(
            """
            () => {
              return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href || "",
                text: (a.innerText || a.textContent || "").trim()
              }));
            }
            """
        )
    except Exception:
        return []

    output: List[Dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                output.append(
                    {
                        "href": safe_text(item.get("href")),
                        "text": safe_text(item.get("text")),
                    }
                )
    return output


def collect_regex_urls_from_html(html: str, base_url: str) -> List[str]:
    return extract_event_urls_from_text(html, base_url=base_url)


def try_accept_cookies(page: Page) -> str:
    candidates = [
        "Accepter",
        "Tout accepter",
        "J'accepte",
        "J’accepte",
        "Autoriser tous les cookies",
        "Accepter les cookies",
        "Allow all",
        "Accept all",
    ]

    for label in candidates:
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=label, exact=False)
                if locator.count() > 0:
                    locator.first.click(timeout=2500)
                    page.wait_for_timeout(1200)
                    return f"{role}:{label}"
            except Exception:
                continue

    return "none"


def safe_page_title(page: Page) -> str:
    try:
        return safe_text(page.title())
    except Exception:
        return ""


def safe_page_url(page: Page) -> str:
    try:
        return safe_text(page.url)
    except Exception:
        return ""


def settle_hub_page(page: Page, hub_url: str) -> Dict[str, Any]:
    try:
        page.goto(hub_url, wait_until="domcontentloaded", timeout=90000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(4000)
    cookie_action = try_accept_cookies(page)
    page.wait_for_timeout(1500)

    counts: List[int] = []
    for _ in range(12):
        counts.append(count_market_buttons(page))
        try:
            page.mouse.wheel(0, 2600)
        except Exception:
            pass
        page.wait_for_timeout(SCROLL_WAIT_MS)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2500)

    return {
        "cookie_action": cookie_action,
        "market_button_counts_during_scroll": counts,
        "final_market_button_count": count_market_buttons(page),
    }


def count_market_buttons(page: Page) -> int:
    for selector in MARKETNUMBER_SELECTORS:
        try:
            count = page.locator(selector).count()
            if count > 0:
                return count
        except Exception:
            continue
    return 0


def market_button_locator(page: Page):
    for selector in MARKETNUMBER_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return page.locator("section.marketnumber")


def snapshot_hub_cards(page: Page) -> List[Dict[str, Any]]:
    try:
        raw = page.evaluate(
            """
            () => {
              const cards = Array.from(document.querySelectorAll('.eventcard--toplight'));
              return cards.map((card, idx) => {
                const teams = Array.from(card.querySelectorAll('h2')).map(el => (el.innerText || '').trim()).filter(Boolean);
                const league = (card.querySelector('.eventcard-header-title span:last-child')?.innerText || '').trim();
                const meta = (card.querySelector('.eventcard-header-meta')?.innerText || '').trim();
                const marketText = (card.querySelector('section.marketnumber')?.innerText || '').trim();
                const title = (card.querySelector('section.marketnumber')?.getAttribute('title') || '').trim();
                return { index: idx, teams, league, meta, market_text: marketText, market_title: title };
              });
            }
            """
        )
    except Exception:
        return []

    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def harvest_from_json_like(value: Any, base_url: str, out: List[str]) -> None:
    if value is None:
        return

    if isinstance(value, dict):
        for nested in value.values():
            harvest_from_json_like(nested, base_url, out)
        return

    if isinstance(value, (list, tuple)):
        for nested in value:
            harvest_from_json_like(nested, base_url, out)
        return

    if isinstance(value, str):
        out.extend(extract_event_urls_from_text(value, base_url=base_url))
        return


def build_network_listeners(page: Page, base_url: str, out_dir: Path):
    response_records: List[Dict[str, Any]] = []
    harvested_urls: List[str] = []
    body_dir = out_dir / "network_response_bodies"
    ensure_dir(body_dir)

    def on_response(response) -> None:
        try:
            resource_type = safe_text(response.request.resource_type)
        except Exception:
            resource_type = ""

        url = safe_text(response.url)
        content_type = ""
        try:
            content_type = safe_text(response.headers.get("content-type", ""))
        except Exception:
            content_type = ""

        if not url:
            return

        relevant = (
            resource_type in {"xhr", "fetch", "document"}
            or "json" in content_type.lower()
            or "graphql" in url.lower()
            or "event" in url.lower()
        )
        if not relevant:
            return

        record: Dict[str, Any] = {
            "url": url,
            "status": None,
            "resource_type": resource_type,
            "content_type": content_type,
            "harvested_event_urls": [],
            "body_saved": False,
            "body_path": "",
        }

        try:
            record["status"] = int(response.status)
        except Exception:
            record["status"] = None

        text = ""
        try:
            text = response.text()
        except Exception:
            text = ""

        if text:
            urls_from_text = extract_event_urls_from_text(text, base_url=base_url)
            harvested_urls.extend(urls_from_text)
            record["harvested_event_urls"].extend(urls_from_text)

            body_path = body_dir / f"response_{len(response_records):04d}.txt"
            try:
                body_path.write_text(text[:500000], encoding="utf-8")
                record["body_saved"] = True
                record["body_path"] = str(body_path)
            except Exception:
                pass

            if content_type.lower().startswith("application/json") or text.lstrip().startswith(("{", "[")):
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = None
                if payload is not None:
                    extra_urls: List[str] = []
                    harvest_from_json_like(payload, base_url=base_url, out=extra_urls)
                    extra_urls = dedupe_keep_order(extra_urls)
                    if extra_urls:
                        harvested_urls.extend(extra_urls)
                        record["harvested_event_urls"].extend(extra_urls)
        response_records.append(record)

    page.on("response", on_response)
    return response_records, harvested_urls


def click_target_and_capture_event_url(context: BrowserContext, page: Page, target_index: int) -> Dict[str, Any]:
    attempt: Dict[str, Any] = {
        "target_index": target_index,
        "button_count": 0,
        "hub_url_before_click": safe_page_url(page),
        "teams": [],
        "market_text": "",
        "clicked": False,
        "navigation_mode": "none",
        "event_url": "",
        "page_title_after_click": "",
        "error": "",
    }

    locator = market_button_locator(page)
    try:
        button_count = locator.count()
    except Exception:
        button_count = 0
    attempt["button_count"] = button_count

    if target_index >= button_count:
        attempt["error"] = f"index {target_index} >= button_count {button_count}"
        return attempt

    target = locator.nth(target_index)
    try:
        attempt["teams"] = target.evaluate(
            """
            (el) => {
              const card = el.closest('.eventcard--toplight');
              if (!card) return [];
              return Array.from(card.querySelectorAll('h2')).map(x => (x.innerText || '').trim()).filter(Boolean);
            }
            """
        )
    except Exception:
        attempt["teams"] = []

    try:
        attempt["market_text"] = safe_text(target.inner_text())
    except Exception:
        attempt["market_text"] = ""

    try:
        target.scroll_into_view_if_needed(timeout=5000)
        page.wait_for_timeout(700)
    except Exception:
        pass

    existing_pages = list(context.pages)
    existing_page_ids = {id(p) for p in existing_pages}
    original_url = safe_page_url(page)

    click_errors: List[str] = []
    for mode in ("normal", "force", "dom_click"):
        try:
            if mode == "normal":
                target.click(timeout=5000)
            elif mode == "force":
                target.click(timeout=5000, force=True)
            else:
                target.evaluate("(el) => el.click()")
            attempt["clicked"] = True
            attempt["navigation_mode"] = mode
            break
        except Exception as exc:
            click_errors.append(f"{mode}:{exc}")
            page.wait_for_timeout(600)

    if not attempt["clicked"]:
        attempt["error"] = " | ".join(click_errors)[:4000]
        return attempt

    deadline = time.time() + (CLICK_WAIT_MS / 1000.0)
    captured_url = ""
    current_target_page: Optional[Page] = None

    while time.time() < deadline:
        current_url = safe_page_url(page)
        if current_url and current_url != original_url and looks_like_unibet_event_url(current_url):
            captured_url = current_url
            current_target_page = page
            break

        for opened in context.pages:
            if id(opened) in existing_page_ids:
                continue
            opened_url = safe_page_url(opened)
            if looks_like_unibet_event_url(opened_url):
                captured_url = opened_url
                current_target_page = opened
                break
        if captured_url:
            break

        page.wait_for_timeout(400)

    if current_target_page is not None:
        try:
            current_target_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        attempt["page_title_after_click"] = safe_page_title(current_target_page)

    attempt["event_url"] = normalize_url(captured_url)

    # Nettoyage : revenir au hub si navigation same-tab, fermer si popup.
    if current_target_page is page and captured_url:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=NAV_WAIT_MS)
            page.wait_for_timeout(1500)
        except Exception:
            try:
                page.goto(original_url, wait_until="domcontentloaded", timeout=NAV_WAIT_MS)
                page.wait_for_timeout(1500)
            except Exception:
                pass
    elif current_target_page is not None and current_target_page is not page:
        try:
            current_target_page.close()
        except Exception:
            pass

    return attempt


def discover_by_ui_clicks(context: BrowserContext, page: Page, hub_url: str, out_dir: Path, max_matches: int) -> Tuple[List[str], List[Dict[str, Any]]]:
    prep = settle_hub_page(page, hub_url)
    cards_snapshot = snapshot_hub_cards(page)
    write_json(out_dir / "hub_cards_snapshot.json", {
        "prep": prep,
        "cards": cards_snapshot,
    })

    initial_count = count_market_buttons(page)
    if max_matches <= 0:
        max_matches = initial_count
    else:
        max_matches = min(max_matches, initial_count) if initial_count > 0 else max_matches

    attempts: List[Dict[str, Any]] = []
    event_urls: List[str] = []

    for index in range(max_matches):
        settle_hub_page(page, hub_url)
        attempt = click_target_and_capture_event_url(context=context, page=page, target_index=index)
        attempts.append(attempt)
        if attempt.get("event_url"):
            event_urls.append(attempt["event_url"])

    write_json(out_dir / "ui_click_discovery.json", {
        "attempt_count": len(attempts),
        "attempts": attempts,
    })
    return dedupe_keep_order(event_urls), attempts


def main() -> None:
    hub_url = safe_text(os.getenv("UNIBET_HUB_URL", DEFAULT_HUB_URL)) or DEFAULT_HUB_URL
    headless = env_bool("PW_HEADLESS", True)
    max_matches = env_int("DISCOVERY_MAX_MATCHES", 0)

    ts = now_ts()
    out_dir = ARTIFACTS_ROOT / ts
    ensure_dir(out_dir)

    print("unibet_points_discover_event_urls.py")
    print(f"Hub URL              : {hub_url}")
    print(f"Headless             : {headless}")
    print(f"Discovery max matches: {max_matches}")

    page_title = ""
    final_url = ""
    cookie_action = "none"
    raw_anchor_links_sorted: List[Dict[str, str]] = []
    regex_candidates: List[str] = []
    network_candidates: List[str] = []
    ui_candidates: List[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1440, "height": 2200})
        page = context.new_page()

        response_records, harvested_urls_from_network = build_network_listeners(page=page, base_url=hub_url, out_dir=out_dir)

        prep = settle_hub_page(page, hub_url)
        cookie_action = safe_text(prep.get("cookie_action")) or "none"

        final_url = safe_page_url(page)
        page_title = safe_page_title(page)

        try:
            page.screenshot(path=str(out_dir / "hub_screenshot.png"), full_page=True)
        except Exception:
            pass

        try:
            html = page.content()
        except Exception:
            html = ""

        (out_dir / "page_source.html").write_text(html, encoding="utf-8")
        (out_dir / "page_title.txt").write_text(page_title + "\n", encoding="utf-8")
        (out_dir / "final_url.txt").write_text(final_url + "\n", encoding="utf-8")
        (out_dir / "cookie_action.txt").write_text(cookie_action + "\n", encoding="utf-8")

        raw_anchor_links = collect_anchor_links(page)
        raw_anchor_links_sorted = sorted(
            raw_anchor_links,
            key=lambda x: (safe_text(x.get("href")), safe_text(x.get("text"))),
        )
        write_json(out_dir / "raw_anchor_links.json", raw_anchor_links_sorted)
        write_lines(
            out_dir / "raw_anchor_links.txt",
            [f"{safe_text(item.get('href'))}\t{safe_text(item.get('text'))}" for item in raw_anchor_links_sorted],
        )

        regex_candidates = dedupe_keep_order(collect_regex_urls_from_html(html, final_url or hub_url))
        write_json(out_dir / "regex_url_candidates.json", regex_candidates)
        write_lines(out_dir / "regex_url_candidates.txt", regex_candidates)

        network_candidates = dedupe_keep_order(harvested_urls_from_network)
        write_json(out_dir / "network_response_index.json", response_records)
        write_json(out_dir / "network_event_url_candidates.json", network_candidates)

        ui_candidates, _ = discover_by_ui_clicks(
            context=context,
            page=page,
            hub_url=hub_url,
            out_dir=out_dir,
            max_matches=max_matches,
        )

        filtered_candidates = dedupe_keep_order(
            [safe_text(item.get("href")) for item in raw_anchor_links_sorted]
            + regex_candidates
            + network_candidates
            + ui_candidates
        )
        filtered_candidates = [url for url in filtered_candidates if looks_like_unibet_event_url(url)]

        write_json(out_dir / "filtered_candidate_urls.json", filtered_candidates)
        write_lines(out_dir / "filtered_candidate_urls.txt", filtered_candidates)

        payload = {
            "hub_url": hub_url,
            "final_url": final_url,
            "page_title": page_title,
            "headless": headless,
            "cookie_action": cookie_action,
            "discovery_max_matches": max_matches,
            "raw_anchor_count": len(raw_anchor_links_sorted),
            "regex_candidate_count": len(regex_candidates),
            "network_candidate_count": len(network_candidates),
            "ui_candidate_count": len(ui_candidates),
            "filtered_candidate_count": len(filtered_candidates),
            "event_urls": filtered_candidates,
            "debug_files": {
                "page_source_html": str(out_dir / "page_source.html"),
                "hub_screenshot_png": str(out_dir / "hub_screenshot.png"),
                "raw_anchor_links_json": str(out_dir / "raw_anchor_links.json"),
                "regex_url_candidates_json": str(out_dir / "regex_url_candidates.json"),
                "network_response_index_json": str(out_dir / "network_response_index.json"),
                "network_event_url_candidates_json": str(out_dir / "network_event_url_candidates.json"),
                "hub_cards_snapshot_json": str(out_dir / "hub_cards_snapshot.json"),
                "ui_click_discovery_json": str(out_dir / "ui_click_discovery.json"),
                "filtered_candidate_urls_json": str(out_dir / "filtered_candidate_urls.json"),
            },
        }

        write_json(out_dir / "discovered_event_urls.json", payload)
        write_lines(out_dir / "discovered_event_urls.txt", filtered_candidates)

        browser.close()

    print(f"Discovered URLs      : {len(filtered_candidates)}")
    print(f"JSON output          : {out_dir / 'discovered_event_urls.json'}")
    print(f"TXT output           : {out_dir / 'discovered_event_urls.txt'}")

    if not filtered_candidates:
        print("")
        print("Aucune URL event découverte.")
        print("Consulte en priorité :")
        print(f"- {out_dir / 'network_event_url_candidates.json'}")
        print(f"- {out_dir / 'hub_cards_snapshot.json'}")
        print(f"- {out_dir / 'ui_click_discovery.json'}")
        print(f"- {out_dir / 'page_source.html'}")
        print(f"- {out_dir / 'hub_screenshot.png'}")


if __name__ == "__main__":
    main()
