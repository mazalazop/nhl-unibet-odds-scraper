#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scrapers/unibet_points_discover_event_urls.py

Objectif
--------
Découvrir automatiquement les URLs des matchs NHL depuis une page hub Unibet.

Entrées via variables d'environnement
-------------------------------------
- UNIBET_HUB_URL : URL hub à ouvrir
- PW_HEADLESS    : true / false

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
- filtered_candidate_urls.json
- filtered_candidate_urls.txt
- page_source.html
- hub_screenshot.png

Principe
--------
- ouvrir la page hub avec Playwright
- tenter de fermer / accepter le bandeau cookies
- laisser la page charger et scroller un peu
- récupérer tous les href présents
- récupérer aussi des URLs détectées dans le HTML via regex
- normaliser / filtrer les candidates
- écrire beaucoup de debug pour pouvoir corriger précisément si 0 URL
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_HUB_URL = (
    "https://www.unibet.fr/sport/hockey-sur-glace/etats-unis/nhl"
    "?filter=R%C3%A9sultat&subFilter=R%C3%A9sultat+du+match"
)
ARTIFACTS_ROOT = Path("artifacts") / "unibet_points_discover_event_urls"


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_url(url: str) -> str:
    url = str(url).strip()
    if not url:
        return ""

    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if path == "":
        path = "/"

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query_items = sorted(query_items)
    query = "&".join([f"{k}={v}" for k, v in query_items])

    return urlunparse((scheme, netloc, path, "", query, ""))


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_lines(path: Path, lines: List[str]) -> None:
    text = "\n".join(lines)
    if lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def try_accept_cookies(page) -> str:
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
        try:
            locator = page.get_by_role("button", name=label, exact=False)
            if locator.count() > 0:
                locator.first.click(timeout=2000)
                page.wait_for_timeout(1000)
                return f"button:{label}"
        except Exception:
            pass

        try:
            locator = page.get_by_role("link", name=label, exact=False)
            if locator.count() > 0:
                locator.first.click(timeout=2000)
                page.wait_for_timeout(1000)
                return f"link:{label}"
        except Exception:
            pass

    return "none"


def collect_anchor_links(page) -> List[Dict[str, str]]:
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
        out: List[Dict[str, str]] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    out.append(
                        {
                            "href": safe_text(item.get("href")),
                            "text": safe_text(item.get("text")),
                        }
                    )
        return out
    except Exception:
        return []


def collect_regex_urls_from_html(html: str, base_url: str) -> List[str]:
    found: List[str] = []

    abs_urls = re.findall(r'https?://[^\s"\'<>]+', html)
    rel_urls = re.findall(r'(?:(?:href|to|url)\s*[:=]\s*["\'])(/[^"\']+)', html)

    for url in abs_urls:
        found.append(url)

    for rel in rel_urls:
        found.append(urljoin(base_url, rel))

    return found


def path_depth(path: str) -> int:
    return len([p for p in (path or "").split("/") if p.strip()])


def is_same_unibet_fr(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower().endswith("unibet.fr")
    except Exception:
        return False


def looks_like_listing_or_noise(url: str, hub_url: str) -> bool:
    u = urlparse(url)
    h = urlparse(hub_url)

    path = (u.path or "").lower()
    query = (u.query or "").lower()
    hub_path = (h.path or "").lower()

    if normalize_url(url) == normalize_url(hub_url):
        return True

    if path == hub_path and ("filter=" in query or "subfilter=" in query or query == ""):
        return True

    bad_parts = [
        "/compte",
        "/login",
        "/connexion",
        "/inscription",
        "/register",
        "/casino",
        "/poker",
        "/bingo",
        "/help",
        "/promotions",
        "/offres",
        "/live",
    ]
    return any(part in path for part in bad_parts)


def is_probable_event_url(url: str, hub_url: str) -> bool:
    if not is_same_unibet_fr(url):
        return False

    if looks_like_listing_or_noise(url, hub_url):
        return False

    u = urlparse(url)
    h = urlparse(hub_url)

    path = (u.path or "").lower()
    hub_path = (h.path or "").lower()

    if "/event/" in path or "/events/" in path:
        return True

    if path.startswith(hub_path):
        if path_depth(path) > path_depth(hub_path):
            return True

    q = dict(parse_qsl(u.query, keep_blank_values=True))
    query_keys = {k.lower() for k in q.keys()}
    if {"eventid", "matchid", "event"} & query_keys:
        return True

    return False


def dedupe_keep_order(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for v in values:
        n = normalize_url(v)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def main() -> None:
    hub_url = os.getenv("UNIBET_HUB_URL", DEFAULT_HUB_URL).strip()
    headless = env_bool("PW_HEADLESS", True)

    ts = now_ts()
    out_dir = ARTIFACTS_ROOT / ts
    ensure_dir(out_dir)

    print("unibet_points_discover_event_urls.py")
    print(f"Hub URL         : {hub_url}")
    print(f"Headless        : {headless}")

    final_url = ""
    page_title = ""
    cookie_action = "none"
    raw_anchor_links: List[Dict[str, str]] = []
    regex_candidates: List[str] = []
    filtered_candidates: List[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1440, "height": 2200})

        try:
            page.goto(hub_url, wait_until="domcontentloaded", timeout=90000)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(4000)

        cookie_action = try_accept_cookies(page)
        page.wait_for_timeout(2000)

        for _ in range(6):
            try:
                page.mouse.wheel(0, 2500)
            except Exception:
                pass
            page.wait_for_timeout(1200)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        page.wait_for_timeout(3000)

        try:
            final_url = safe_text(page.url)
        except Exception:
            final_url = ""

        try:
            page_title = safe_text(page.title())
        except Exception:
            page_title = ""

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
            [
                f"{safe_text(item.get('href'))}\t{safe_text(item.get('text'))}"
                for item in raw_anchor_links_sorted
            ],
        )

        regex_candidates = dedupe_keep_order(collect_regex_urls_from_html(html, final_url or hub_url))
        write_json(out_dir / "regex_url_candidates.json", regex_candidates)
        write_lines(out_dir / "regex_url_candidates.txt", regex_candidates)

        all_candidate_urls = dedupe_keep_order(
            [safe_text(item.get("href")) for item in raw_anchor_links_sorted] + regex_candidates
        )

        filtered_candidates = [
            url for url in all_candidate_urls
            if is_probable_event_url(url, hub_url=hub_url)
        ]
        filtered_candidates = dedupe_keep_order(filtered_candidates)

        write_json(out_dir / "filtered_candidate_urls.json", filtered_candidates)
        write_lines(out_dir / "filtered_candidate_urls.txt", filtered_candidates)

        payload = {
            "hub_url": hub_url,
            "final_url": final_url,
            "page_title": page_title,
            "headless": headless,
            "cookie_action": cookie_action,
            "raw_anchor_count": len(raw_anchor_links_sorted),
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

        browser.close()

    print(f"Discovered URLs : {len(filtered_candidates)}")
    print(f"JSON output     : {out_dir / 'discovered_event_urls.json'}")
    print(f"TXT output      : {out_dir / 'discovered_event_urls.txt'}")

    if len(filtered_candidates) == 0:
        print("")
        print("Aucune URL event découverte.")
        print("Consulte les fichiers de debug :")
        print(f"- {out_dir / 'page_title.txt'}")
        print(f"- {out_dir / 'final_url.txt'}")
        print(f"- {out_dir / 'cookie_action.txt'}")
        print(f"- {out_dir / 'raw_anchor_links.txt'}")
        print(f"- {out_dir / 'regex_url_candidates.txt'}")
        print(f"- {out_dir / 'filtered_candidate_urls.txt'}")
        print(f"- {out_dir / 'page_source.html'}")
        print(f"- {out_dir / 'hub_screenshot.png'}")


if __name__ == "__main__":
    main()
