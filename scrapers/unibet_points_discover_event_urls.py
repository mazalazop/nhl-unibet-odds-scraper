#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_HUB_URL = "https://www.unibet.fr/paris-hockey-sur-glace/etats-unis/nhl"
ARTIFACTS_ROOT = Path("artifacts") / "unibet_points_discover_event_urls"
CURRENT_EVENT_URL_RE = re.compile(
    r"https?://(?:www\.)?unibet\.fr/paris-hockey-sur-glace/etats-unis/nhl/\d+/[^\s\"'<>/?#]+/?",
    re.I,
)
RELATIVE_CURRENT_EVENT_URL_RE = re.compile(
    r"/(?:paris-hockey-sur-glace|sport/hockey-sur-glace)/etats-unis/nhl/\d+/[^\s\"'<>/?#]+/?",
    re.I,
)
SCROLL_WAIT_MS = 900
SCROLL_ROUNDS = 8


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


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


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
    query_items = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    query = "&".join(f"{k}={v}" for k, v in query_items)
    return urlunparse((scheme, netloc, path, "", query, ""))


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


def looks_like_unibet_event_url(url: str) -> bool:
    value = normalize_url(url)
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc.endswith("unibet.fr"):
        return False
    path = (parsed.path or "").lower().rstrip("/")
    return bool(re.search(r"/paris-hockey-sur-glace/etats-unis/nhl/\d+/[^/]+$", path))


def extract_event_urls_from_text(text: str, base_url: str) -> List[str]:
    found: List[str] = []
    if not text:
        return found
    found.extend(CURRENT_EVENT_URL_RE.findall(text))
    for match in RELATIVE_CURRENT_EVENT_URL_RE.findall(text):
        found.append(urljoin(base_url, match))
    return dedupe_keep_order(found)


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
                loc = page.get_by_role(role, name=label, exact=False)
                if loc.count() > 0:
                    loc.first.click(timeout=2500)
                    page.wait_for_timeout(1200)
                    return f"{role}:{label}"
            except Exception:
                continue
    return "none"


def settle_hub_page(page: Page, hub_url: str) -> Dict[str, Any]:
    try:
        page.goto(hub_url, wait_until="domcontentloaded", timeout=90000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(2500)
    cookie_action = try_accept_cookies(page)
    page.wait_for_timeout(1000)

    counts: List[int] = []
    for _ in range(SCROLL_ROUNDS):
        counts.append(count_event_links(page))
        try:
            page.mouse.wheel(0, 2600)
        except Exception:
            pass
        page.wait_for_timeout(SCROLL_WAIT_MS)

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1200)

    return {
        "cookie_action": cookie_action,
        "event_link_counts_during_scroll": counts,
        "final_event_link_count": count_event_links(page),
    }


def count_event_links(page: Page) -> int:
    try:
        return int(page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a[href]'))
              .filter(a => /\/paris-hockey-sur-glace\/etats-unis\/nhl\/\d+\//i.test(a.href || ''))
              .length
            """
        ))
    except Exception:
        return 0


def collect_anchor_links(page: Page) -> List[Dict[str, str]]:
    try:
        raw = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
              href: a.href || '',
              text: (a.innerText || a.textContent || '').trim(),
              title: (a.getAttribute('title') || '').trim(),
              aria_label: (a.getAttribute('aria-label') || '').trim(),
            }))
            """
        )
    except Exception:
        return []

    out: List[Dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "href": safe_text(item.get("href")),
                    "text": safe_text(item.get("text")),
                    "title": safe_text(item.get("title")),
                    "aria_label": safe_text(item.get("aria_label")),
                }
            )
    return out


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

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1440, "height": 2400})
        page = context.new_page()
        page.set_default_timeout(15000)

        prep = settle_hub_page(page, hub_url)
        final_url = safe_text(page.url)
        page_title = safe_text(page.title())

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
        (out_dir / "cookie_action.txt").write_text(safe_text(prep.get("cookie_action")) + "\n", encoding="utf-8")

        raw_anchor_links = collect_anchor_links(page)
        write_json(out_dir / "raw_anchor_links.json", raw_anchor_links)
        write_lines(
            out_dir / "raw_anchor_links.txt",
            [f"{x['href']}\t{x['text']}\t{x['title']}\t{x['aria_label']}" for x in raw_anchor_links],
        )

        anchor_candidates = dedupe_keep_order([x.get("href", "") for x in raw_anchor_links])
        regex_candidates = dedupe_keep_order(extract_event_urls_from_text(html, final_url or hub_url))

        filtered_candidates = dedupe_keep_order(anchor_candidates + regex_candidates)
        filtered_candidates = [x for x in filtered_candidates if looks_like_unibet_event_url(x)]

        if max_matches > 0:
            filtered_candidates = filtered_candidates[:max_matches]

        write_json(out_dir / "regex_url_candidates.json", regex_candidates)
        write_lines(out_dir / "regex_url_candidates.txt", regex_candidates)
        write_json(out_dir / "filtered_candidate_urls.json", filtered_candidates)
        write_lines(out_dir / "filtered_candidate_urls.txt", filtered_candidates)

        payload = {
            "hub_url": hub_url,
            "final_url": final_url,
            "page_title": page_title,
            "headless": headless,
            "cookie_action": safe_text(prep.get("cookie_action")) or "none",
            "discovery_max_matches": max_matches,
            "scroll_prep": prep,
            "raw_anchor_count": len(raw_anchor_links),
            "regex_candidate_count": len(regex_candidates),
            "filtered_candidate_count": len(filtered_candidates),
            "event_urls": filtered_candidates,
            "debug_files": {
                "page_source_html": str(out_dir / "page_source.html"),
                "hub_screenshot_png": str(out_dir / "hub_screenshot.png"),
                "raw_anchor_links_json": str(out_dir / "raw_anchor_links.json"),
                "regex_url_candidates_json": str(out_dir / "regex_url_candidates.json"),
                "filtered_candidate_urls_json": str(out_dir / "filtered_candidate_urls.json"),
            },
        }
        write_json(out_dir / "discovered_event_urls.json", payload)
        write_lines(out_dir / "discovered_event_urls.txt", filtered_candidates)

        context.close()
        browser.close()

    print(f"Discovered URLs      : {len(filtered_candidates)}")
    print(f"JSON output          : {out_dir / 'discovered_event_urls.json'}")
    print(f"TXT output           : {out_dir / 'discovered_event_urls.txt'}")

    if not filtered_candidates:
        print("")
        print("Aucune URL event découverte.")
        print("Consulte en priorité :")
        print(f"- {out_dir / 'raw_anchor_links.json'}")
        print(f"- {out_dir / 'filtered_candidate_urls.json'}")
        print(f"- {out_dir / 'page_source.html'}")
        print(f"- {out_dir / 'hub_screenshot.png'}")


if __name__ == "__main__":
    main()
