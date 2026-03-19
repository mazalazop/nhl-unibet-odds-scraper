#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scrapers/unibet_points_discover_event_urls.py

Objectif
--------
Découvrir automatiquement les URLs des événements NHL du jour depuis une page hub Unibet,
afin d'alimenter ensuite le batch runner POINTS existant.

Entrée
------
- --hub-url : URL Unibet de la page NHL / hub
- --headless : true / false (optionnel, sinon lu via PW_HEADLESS)

Sorties
-------
- artifacts/unibet_points_discover_event_urls/<timestamp>/discovered_event_urls.json
- artifacts/unibet_points_discover_event_urls/<timestamp>/discovered_event_urls.txt

Notes
-----
- Le site Unibet est rendu côté JavaScript : Playwright est requis.
- Le script reste volontairement séparé du batch runner POINTS.
- Il déduplique les URLs et filtre autant que possible les liens non-event.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_HUB_URL = (
    "https://www.unibet.fr/sport/hockey-sur-glace/etats-unis/nhl"
    "?filter=R%C3%A9sultat&subFilter=R%C3%A9sultat+du+match"
)
DEFAULT_WAIT_MS = 45000
DEFAULT_SCROLL_ROUNDS = 8
DEFAULT_SCROLL_PAUSE_SEC = 1.2


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_bool(value: str | bool | None, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"

    allowed_query_keys = {
        "eventId",
        "eventid",
        "id",
        "marketId",
        "marketid",
        "betOfferType",
        "betoffertype",
    }
    query_pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False) if k in allowed_query_keys]
    query_pairs.sort()
    query = urlencode(query_pairs)

    return urlunparse((scheme, netloc, path, "", query, ""))


def is_same_unibet_domain(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return netloc.endswith("unibet.fr")


def slug_looks_like_event(last_segment: str) -> bool:
    if not last_segment:
        return False
    if last_segment in {"nhl", "etats-unis", "hockey-sur-glace", "sport"}:
        return False
    if re.search(r"\d", last_segment):
        return True
    if last_segment.count("-") >= 1:
        return True
    return False


def looks_like_event_url(abs_url: str, hub_url: str) -> bool:
    if not is_same_unibet_domain(abs_url):
        return False

    parsed = urlparse(abs_url)
    hub_parsed = urlparse(hub_url)

    path_low = (parsed.path or "").lower()
    hub_path_low = (hub_parsed.path or "").lower()

    if "hockey-sur-glace" not in path_low and "nhl" not in path_low:
        return False

    if canonicalize_url(abs_url) == canonicalize_url(hub_url):
        return False

    if "/competition/" in path_low or "/paris-sportifs/" in path_low:
        return False

    if path_low.rstrip("/") == hub_path_low.rstrip("/"):
        return False

    parts = [p for p in parsed.path.split("/") if p]
    last_segment = parts[-1].lower() if parts else ""

    if "filter=" in parsed.query.lower() and not re.search(r"(event|market|id)", parsed.query.lower()):
        return False

    if "/event/" in path_low or "/sports/event/" in path_low:
        return True

    if "/sport/" in path_low and "nhl" in path_low and slug_looks_like_event(last_segment):
        return True

    # Fallback plus permissif pour les SPA Unibet.
    if "nhl" in path_low and slug_looks_like_event(last_segment):
        return True

    return False


def extract_candidate_urls(page, hub_url: str) -> List[str]:
    hrefs = page.locator("a[href]").evaluate_all(
        """
        nodes => nodes
            .map(n => n.getAttribute('href'))
            .filter(Boolean)
        """
    )

    out: Set[str] = set()
    for href in hrefs:
        abs_url = urljoin(hub_url, href)
        if looks_like_event_url(abs_url, hub_url):
            out.add(canonicalize_url(abs_url))
    return sorted(out)


def try_accept_cookies(page) -> None:
    candidate_names = [
        "Accepter",
        "Tout accepter",
        "J'accepte",
        "Autoriser",
        "Continuer",
    ]
    for name in candidate_names:
        try:
            page.get_by_role("button", name=re.compile(fr"^{re.escape(name)}$", re.I)).click(timeout=2000)
            page.wait_for_timeout(1000)
            return
        except Exception:
            continue


def scroll_page(page, rounds: int, pause_sec: float) -> None:
    last_height = 0
    stable_rounds = 0

    for _ in range(rounds):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(int(pause_sec * 1000))
            new_height = page.evaluate("document.body.scrollHeight")
        except Exception:
            break

        if new_height == last_height:
            stable_rounds += 1
        else:
            stable_rounds = 0

        last_height = new_height

        if stable_rounds >= 2:
            break


def discover_event_urls(
    hub_url: str,
    headless: bool,
    wait_ms: int,
    scroll_rounds: int,
    scroll_pause_sec: float,
) -> List[str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1440, "height": 2200}, locale="fr-FR")
        page = context.new_page()

        page.goto(hub_url, wait_until="domcontentloaded", timeout=wait_ms)
        page.wait_for_timeout(2500)

        try_accept_cookies(page)

        # On tente de laisser le front charger au maximum.
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightTimeoutError:
            pass

        try:
            page.wait_for_selector("a[href]", timeout=wait_ms)
        except PlaywrightTimeoutError:
            pass

        scroll_page(page, rounds=scroll_rounds, pause_sec=scroll_pause_sec)

        urls = extract_candidate_urls(page, hub_url=hub_url)

        browser.close()

    return urls


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Découvrir automatiquement les URLs event Unibet NHL depuis une page hub.")
    parser.add_argument("--hub-url", type=str, default=os.getenv("UNIBET_HUB_URL", DEFAULT_HUB_URL))
    parser.add_argument("--headless", type=str, default=os.getenv("PW_HEADLESS", "true"))
    parser.add_argument("--wait-ms", type=int, default=DEFAULT_WAIT_MS)
    parser.add_argument("--scroll-rounds", type=int, default=DEFAULT_SCROLL_ROUNDS)
    parser.add_argument("--scroll-pause-sec", type=float, default=DEFAULT_SCROLL_PAUSE_SEC)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    hub_url = str(args.hub_url).strip()
    if not hub_url:
        raise ValueError("hub_url vide")

    headless = parse_bool(args.headless, default=True)

    artifact_dir = Path("artifacts") / "unibet_points_discover_event_urls" / now_ts()
    ensure_dir(artifact_dir)

    urls = discover_event_urls(
        hub_url=hub_url,
        headless=headless,
        wait_ms=int(args.wait_ms),
        scroll_rounds=int(args.scroll_rounds),
        scroll_pause_sec=float(args.scroll_pause_sec),
    )

    payload = {
        "status": "ok" if urls else "empty",
        "hub_url": hub_url,
        "headless": headless,
        "discovered_count": len(urls),
        "event_urls": urls,
    }

    json_path = artifact_dir / "discovered_event_urls.json"
    txt_path = artifact_dir / "discovered_event_urls.txt"

    write_json(json_path, payload)
    txt_path.write_text("\n".join(urls), encoding="utf-8")

    print("unibet_points_discover_event_urls.py")
    print(f"Hub URL         : {hub_url}")
    print(f"Headless        : {headless}")
    print(f"Discovered URLs : {len(urls)}")
    print(f"JSON output     : {json_path}")
    print(f"TXT output      : {txt_path}")

    if urls:
        print("\n=== URLS ===")
        for url in urls:
            print(url)
    else:
        print("\nAucune URL event découverte.")


if __name__ == "__main__":
    main()

