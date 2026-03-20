#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scrapers/unibet_event_points_parser.py

Objectif
--------
Ouvrir une page event Unibet NHL et extraire uniquement le marché
"Nombre de Points du joueur (prolongations incluses)" pour l'issue
"1 ou plus".

Entrées via variables d'environnement
-------------------------------------
- UNIBET_EVENT_URL : URL event Unibet à parser (obligatoire)
- PW_HEADLESS      : true / false (défaut: true)
- PW_TIMEOUT_MS    : timeout Playwright global (défaut: 60000)

Sorties
-------
Dans artifacts/unibet_event_points_parser/<timestamp>/ :
- summary.json
- page_title.txt
- final_url.txt
- teams.json
- landing.png
- after_tab_click.png
- after_expand.png
- page_source.html
- points_market_only.txt
- points_market_block.html
- points_market_isolated_lines.json
- points_market_rows_clean.json
- points_market_players_debug.json

Principes
---------
- accepte le bandeau cookies si présent
- clique l'onglet adéquat (Buteurs en priorité)
- localise le bloc "Nombre de Points du joueur"
- clique tous les "Voir plus" de ce bloc
- parse uniquement l'issue "1 ou plus"
- écrit beaucoup d'artefacts de debug
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Locator, Page, sync_playwright


TAB_LABEL_CANDIDATES = [
    "Buteurs",
    "Joueurs",
    "Points",
    "Joueur",
]

BLOCK_LABEL_CANDIDATES = [
    "NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)",
    "NOMBRE DE POINTS DU JOUEUR",
]

COOKIE_LABEL_CANDIDATES = [
    "Accepter",
    "Tout accepter",
    "J'accepte",
    "J’accepte",
    "Autoriser tous les cookies",
    "Accepter les cookies",
    "Allow all",
    "Accept all",
]

HEADER_TOKENS = {
    "1 ou plus",
    "2 ou plus",
    "3 ou plus",
}


# -----------------------------
# utilitaires généraux
# -----------------------------

def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(f"[{now_ts()}] {msg}")


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def norm_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii")


def normalize_for_match(text: Any) -> str:
    text = strip_accents(str(text or ""))
    text = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def dedupe_keep_order(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        clean = norm_spaces(value)
        key = normalize_for_match(clean)
        if not clean or not key or key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def as_decimal_string(value: str) -> Optional[str]:
    raw = norm_spaces(value)
    if not raw or raw == "-":
        return None
    raw = raw.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return None
    return raw


# -----------------------------
# wrappers Playwright sûrs
# -----------------------------

def safe_count(locator: Locator, max_count: Optional[int] = None) -> int:
    try:
        count = locator.count()
        return min(count, max_count) if max_count is not None else count
    except Exception:
        return 0


def safe_inner_text(locator: Locator, timeout: int = 1000) -> str:
    try:
        return locator.inner_text(timeout=timeout)
    except Exception:
        return ""


def safe_text_content(locator: Locator, timeout: int = 1000) -> str:
    try:
        return locator.text_content(timeout=timeout) or ""
    except Exception:
        return ""


def safe_is_visible(locator: Locator, timeout: int = 1000) -> bool:
    try:
        return locator.is_visible(timeout=timeout)
    except Exception:
        return False


def safe_click(locator: Locator, timeout: int = 3000, force: bool = False) -> bool:
    try:
        locator.click(timeout=timeout, force=force)
        return True
    except Exception:
        return False


def wait_small(page: Page, ms: int = 800) -> None:
    try:
        page.wait_for_timeout(ms)
    except Exception:
        time.sleep(ms / 1000)


# -----------------------------
# extraction équipes
# -----------------------------

def extract_teams_from_title(title: str) -> List[str]:
    patterns = [
        r"Pariez sur (.*?) - (.*?) \| Hockey sur Glace \| Unibet\.fr",
        r"(.*?) - (.*?) \| Hockey sur Glace \| Unibet\.fr",
        r"(.*?) v (.*?) \| Hockey sur Glace \| Unibet\.fr",
    ]
    title = title or ""
    for pattern in patterns:
        match = re.search(pattern, title, re.I)
        if match:
            return dedupe_keep_order([match.group(1), match.group(2)])
    return []


def extract_teams_from_url(event_url: str) -> List[str]:
    if not event_url:
        return []

    match = re.search(r"/event/([^/]+?)-\d+_\d+\.html", event_url)
    if not match:
        match = re.search(r"/event/([^/]+)\.html", event_url)
    if not match:
        return []

    slug = match.group(1).replace("_", " ")
    parts = [p for p in slug.split("-") if p]
    if len(parts) < 4:
        return []

    for idx in range(2, len(parts) - 1):
        left = " ".join(parts[:idx])
        right = " ".join(parts[idx:])
        if len(left.split()) >= 2 and len(right.split()) >= 2:
            return dedupe_keep_order([left, right])

    half = len(parts) // 2
    return dedupe_keep_order([" ".join(parts[:half]), " ".join(parts[half:])])


def looks_like_matchup(text: str) -> bool:
    txt = norm_spaces(text)
    if len(txt) < 7:
        return False
    if re.search(r"\b\d+(?:[.,]\d+)?\b", txt):
        return False
    return bool(re.search(r"\s[-–—]\s", txt))


def split_matchup_text(text: str) -> List[str]:
    parts = re.split(r"\s[-–—]\s", norm_spaces(text))
    return dedupe_keep_order([p for p in parts if norm_spaces(p)]) if len(parts) == 2 else []


def extract_teams_from_h1(page: Page) -> List[str]:
    selectors = [
        "h1",
        "[data-test='event-header']",
        "[class*='event'] h1",
        "[class*='Event'] h1",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = safe_count(loc, 12)
            for idx in range(count):
                txt = safe_inner_text(loc.nth(idx))
                if looks_like_matchup(txt):
                    teams = split_matchup_text(txt)
                    if len(teams) == 2:
                        return teams
        except Exception:
            continue
    return []


def extract_teams_from_visible_text(page: Page) -> List[str]:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return []

    lines = [norm_spaces(line) for line in body_text.splitlines() if norm_spaces(line)]
    for line in lines[:50]:
        if looks_like_matchup(line):
            teams = split_matchup_text(line)
            if len(teams) == 2:
                return teams
    return []


def resolve_teams(page: Page, event_url: str, title: str) -> Tuple[List[str], str]:
    methods = [
        (extract_teams_from_title, "title", [title]),
        (extract_teams_from_h1, "h1", [page]),
        (extract_teams_from_visible_text, "visible_text", [page]),
        (extract_teams_from_url, "url_slug", [event_url]),
    ]
    for fn, source, args in methods:
        try:
            teams = fn(*args)
            if len(teams) == 2:
                return teams, source
        except Exception:
            continue
    return [], "none"


# -----------------------------
# interactions UI
# -----------------------------

def click_cookie_if_present(page: Page) -> Optional[str]:
    for label in COOKIE_LABEL_CANDIDATES:
        candidates = [
            page.get_by_role("button", name=label, exact=False),
            page.get_by_role("link", name=label, exact=False),
            page.locator(f"button:has-text('{label}')"),
            page.locator(f"text={label}"),
        ]
        for locator in candidates:
            try:
                if safe_count(locator) == 0:
                    continue
                target = locator.first
                if not safe_is_visible(target, timeout=1500):
                    continue
                if safe_click(target, timeout=2500):
                    log(f"cookie clicked: {label}")
                    wait_small(page, 1000)
                    return label
            except Exception:
                continue
    return None


def click_label(page: Page, label: str) -> bool:
    label = norm_spaces(label)
    if not label:
        return False

    candidates = [
        page.get_by_role("button", name=label, exact=False),
        page.get_by_role("tab", name=label, exact=False),
        page.get_by_role("link", name=label, exact=False),
        page.get_by_text(label, exact=False),
        page.locator(f"button:has-text('{label}')"),
        page.locator(f"[role='tab']:has-text('{label}')"),
        page.locator(f"text={label}"),
    ]

    for locator in candidates:
        try:
            count = safe_count(locator, 8)
            for idx in range(count):
                target = locator.nth(idx)
                if not safe_is_visible(target, timeout=1500):
                    continue
                try:
                    target.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                if safe_click(target, timeout=4000):
                    log(f"clicked label: {label}")
                    wait_small(page, 1200)
                    return True
        except Exception:
            continue

    return False


def click_first_matching_label(page: Page, labels: Sequence[str]) -> Optional[str]:
    for label in labels:
        if click_label(page, label):
            return label
    return None


def gentle_scroll(page: Page, rounds: int = 4, delta: int = 1800) -> None:
    for _ in range(rounds):
        try:
            page.mouse.wheel(0, delta)
        except Exception:
            pass
        wait_small(page, 900)


def _locate_heading_candidates(page: Page, label: str) -> List[Locator]:
    candidates: List[Locator] = []
    pools = [
        page.get_by_text(label, exact=True),
        page.get_by_text(label, exact=False),
        page.locator(f"text={label}"),
        page.locator(f"h1:has-text('{label}'), h2:has-text('{label}'), h3:has-text('{label}'), h4:has-text('{label}')"),
    ]
    for pool in pools:
        count = safe_count(pool, 12)
        for idx in range(count):
            candidates.append(pool.nth(idx))
    return candidates


def _score_block_text(block_text: str) -> int:
    text = normalize_for_match(block_text)
    score = 0
    if "nombre de points du joueur" in text:
        score += 10
    if "prolongations incluses" in text:
        score += 5
    if "1 ou plus" in text:
        score += 3
    if "voir plus" in text:
        score += 1
    if "buteur" in text and "nombre de points du joueur" not in text:
        score -= 5
    return score


def _candidate_ancestor_blocks(heading: Locator) -> List[Locator]:
    selectors = [
        "xpath=ancestor::section[1]",
        "xpath=ancestor::article[1]",
        "xpath=ancestor::div[@role='region'][1]",
        "xpath=ancestor::div[contains(@class, 'market')][1]",
        "xpath=ancestor::div[contains(@class, 'Market')][1]",
        "xpath=ancestor::div[1]",
        "xpath=ancestor::div[2]",
        "xpath=ancestor::div[3]",
        "xpath=ancestor::div[4]",
    ]
    out: List[Locator] = []
    for selector in selectors:
        try:
            loc = heading.locator(selector)
            if safe_count(loc) > 0:
                out.append(loc.first)
        except Exception:
            continue
    return out


def select_first_matching_market_block(page: Page, labels: Sequence[str]) -> Tuple[Optional[str], Optional[Locator], List[Dict[str, Any]]]:
    debug_candidates: List[Dict[str, Any]] = []
    best_label: Optional[str] = None
    best_block: Optional[Locator] = None
    best_score = -999

    for label in labels:
        for heading in _locate_heading_candidates(page, label):
            try:
                if not safe_is_visible(heading, timeout=1200):
                    continue
                for block in _candidate_ancestor_blocks(heading):
                    block_text = safe_inner_text(block, timeout=1500)
                    if not block_text:
                        continue
                    score = _score_block_text(block_text)
                    preview = norm_spaces(block_text)[:300]
                    debug_candidates.append({
                        "label": label,
                        "score": score,
                        "preview": preview,
                    })
                    if score > best_score:
                        best_score = score
                        best_label = label
                        best_block = block
            except Exception:
                continue

    if best_block is not None and best_score >= 8:
        log(f"market block selected by heading: {best_label} score={best_score}")
        return best_label, best_block, debug_candidates

    # fallback text global scan
    try:
        containers = page.locator("section, article, div")
        count = safe_count(containers, 250)
        for idx in range(count):
            block = containers.nth(idx)
            if not safe_is_visible(block, timeout=100):
                continue
            block_text = safe_inner_text(block, timeout=500)
            if not block_text:
                continue
            score = _score_block_text(block_text)
            if score > best_score:
                best_score = score
                best_label = "fallback_text_scan"
                best_block = block
    except Exception:
        pass

    if best_block is not None and best_score >= 8:
        log(f"market block selected by fallback scan: {best_label} score={best_score}")
        return best_label, best_block, debug_candidates

    return None, None, debug_candidates


def click_all_see_more_in_block(page: Page, block: Locator, max_rounds: int = 8) -> int:
    total_clicks = 0

    for round_idx in range(1, max_rounds + 1):
        clicked_this_round = 0
        candidates = [
            block.get_by_role("button", name=re.compile(r"voir plus", re.I)),
            block.get_by_text(re.compile(r"voir plus", re.I)),
            block.locator("button"),
            block.locator("[role='button']"),
        ]

        visited_keys = set()
        for locator in candidates:
            count = safe_count(locator, 30)
            for idx in range(count):
                btn = locator.nth(idx)
                txt = norm_spaces(safe_inner_text(btn, timeout=400) or safe_text_content(btn, timeout=400)).lower()
                if "voir plus" not in txt:
                    continue
                key = f"{txt}|{idx}"
                if key in visited_keys:
                    continue
                visited_keys.add(key)
                try:
                    btn.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                if safe_click(btn, timeout=3000):
                    clicked_this_round += 1
                    total_clicks += 1
                    log(f"clicked see more in points block #{total_clicks}")
                    wait_small(page, 900)

        log(f"see more round {round_idx}: clicked={clicked_this_round}")
        if clicked_this_round == 0:
            break
        wait_small(page, 1200)

    log(f"total see more clicks in points block: {total_clicks}")
    return total_clicks


def remaining_see_more_in_block(block: Locator) -> int:
    text = safe_inner_text(block).lower()
    return text.count("voir plus") if text else -1


# -----------------------------
# parsing du marché points
# -----------------------------

def isolate_lines(block_text: str) -> List[str]:
    raw_lines = [norm_spaces(x) for x in block_text.splitlines()]
    return [x for x in raw_lines if x]


def is_decimal_odd(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", norm_spaces(token)))


def is_valid_player_name(player_name: str, teams: Sequence[str]) -> bool:
    name = norm_spaces(player_name)
    if not name:
        return False

    key = normalize_for_match(name)
    if key in {"ou plus", "1 ou plus", "2 ou plus", "3 ou plus", "match nul"}:
        return False
    if re.search(r"^\d+\s+ou\s+plus$", key):
        return False
    if " ou " in key:
        return False
    if "/" in key:
        return False
    if len(name.split()) < 2:
        return False

    team_keys = {normalize_for_match(t) for t in teams if t}
    if key in team_keys:
        return False
    for team_key in team_keys:
        if team_key and (team_key in key or key in team_key):
            return False

    if re.search(r"\b(match nul|prolongation|victoire|resultat|résultat|double chance|buteur|passeur)\b", key):
        return False

    return True


def parse_points_rows(lines: Sequence[str], teams: Sequence[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    debug_players: List[Dict[str, Any]] = []

    i = 0
    current_team: Optional[str] = None
    team_match_keys = {normalize_for_match(team) for team in teams if team}

    while i < len(lines):
        line = lines[i]
        line_key = normalize_for_match(line)

        if line_key in team_match_keys:
            current_team = line
            i += 1
            while i < len(lines) and normalize_for_match(lines[i]) in HEADER_TOKENS:
                i += 1
            continue

        if not current_team:
            i += 1
            continue

        player_name = line
        if not is_valid_player_name(player_name, teams):
            i += 1
            continue

        odds_seen: List[str] = []
        j = i + 1
        while j < len(lines) and len(odds_seen) < 3:
            token = lines[j]
            token_key = normalize_for_match(token)
            if token_key in team_match_keys:
                break
            if token_key in HEADER_TOKENS:
                break
            if "voir plus" in token_key or "voir moins" in token_key:
                break
            if is_decimal_odd(token) or token == "-":
                odds_seen.append(token)
                j += 1
            else:
                break

        if odds_seen:
            first_odd_raw = odds_seen[0]
            first_odd_decimal = as_decimal_string(first_odd_raw)
            has_dash = any(token == "-" for token in odds_seen)

            debug_players.append({
                "team": current_team,
                "player_name_raw": player_name,
                "odds_count_seen": len(odds_seen),
                "odds_seen": odds_seen,
                "kept_outcome_label": "1 ou plus" if first_odd_decimal else None,
                "has_dash": has_dash,
            })

            if first_odd_decimal is not None:
                rows.append({
                    "team": current_team,
                    "player_name_raw": player_name,
                    "outcome_label": "1 ou plus",
                    "odds_raw": first_odd_raw,
                    "odds_decimal": first_odd_decimal,
                })
            i = j
        else:
            i += 1

    return rows, debug_players


def validate_rows(rows: Sequence[Dict[str, Any]]) -> Tuple[bool, str]:
    if not rows:
        return False, "no_rows"

    for row in rows:
        if not row.get("team"):
            return False, "missing_team"
        if not row.get("player_name_raw"):
            return False, "missing_player"
        if row.get("outcome_label") != "1 ou plus":
            return False, "invalid_outcome_label"
        if not row.get("odds_raw"):
            return False, "missing_odds_raw"
        if not row.get("odds_decimal"):
            return False, "missing_odds_decimal"

    return True, "ok"


# -----------------------------
# programme principal
# -----------------------------

def main() -> None:
    event_url = os.getenv("UNIBET_EVENT_URL", "").strip()
    headless = env_bool("PW_HEADLESS", True)
    timeout_ms = env_int("PW_TIMEOUT_MS", 60000)

    if not event_url:
        raise ValueError("UNIBET_EVENT_URL is required")

    run_dir = Path("artifacts") / "unibet_event_points_parser" / now_ts()
    ensure_dir(run_dir)

    summary: Dict[str, Any] = {
        "title": None,
        "event_url": event_url,
        "final_url": None,
        "cookie_clicked": None,
        "teams": [],
        "teams_source": None,
        "clicked_tab_label": None,
        "tab_label_candidates": TAB_LABEL_CANDIDATES,
        "selected_block_label": None,
        "block_label_candidates": BLOCK_LABEL_CANDIDATES,
        "block_locator_debug_candidates": [],
        "see_more_clicks": 0,
        "remaining_see_more_in_market": -1,
        "is_complete_market": False,
        "rows_valid": False,
        "rows_validation_reason": "not_run",
        "isolated_lines": 0,
        "parsed_rows_clean": 0,
        "players_seen": 0,
        "players_kept_points_1_plus": 0,
        "run_dir": str(run_dir),
        "fatal_error": None,
    }

    isolated: List[str] = []
    rows: List[Dict[str, Any]] = []
    debug_players: List[Dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1600, "height": 2200},
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            log(f"goto: {event_url}")
            page.goto(event_url, wait_until="domcontentloaded", timeout=timeout_ms)
            wait_small(page, 2000)

            summary["cookie_clicked"] = click_cookie_if_present(page)
            wait_small(page, 1000)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass

            gentle_scroll(page, rounds=2, delta=1200)

            summary["final_url"] = page.url
            summary["title"] = page.title()
            write_text(run_dir / "page_title.txt", (summary["title"] or "") + "\n")
            write_text(run_dir / "final_url.txt", (summary["final_url"] or "") + "\n")
            write_text(run_dir / "page_source.html", page.content())
            page.screenshot(path=str(run_dir / "landing.png"), full_page=True)

            teams, teams_source = resolve_teams(page, event_url, summary["title"] or "")
            summary["teams"] = teams
            summary["teams_source"] = teams_source
            write_json(run_dir / "teams.json", {"teams": teams, "source": teams_source})
            log(f"teams detected: {teams} source={teams_source}")

            clicked_tab = click_first_matching_label(page, TAB_LABEL_CANDIDATES)
            summary["clicked_tab_label"] = clicked_tab
            wait_small(page, 1200)
            gentle_scroll(page, rounds=3, delta=1500)
            page.screenshot(path=str(run_dir / "after_tab_click.png"), full_page=True)

            selected_block_label, block, block_debug = select_first_matching_market_block(page, BLOCK_LABEL_CANDIDATES)
            summary["selected_block_label"] = selected_block_label
            summary["block_locator_debug_candidates"] = block_debug[:100]

            if block is None:
                raise RuntimeError(
                    "market_block_not_found: "
                    f"tab_candidates={TAB_LABEL_CANDIDATES} block_candidates={BLOCK_LABEL_CANDIDATES}"
                )

            try:
                block.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            wait_small(page, 800)

            summary["see_more_clicks"] = click_all_see_more_in_block(page, block)
            wait_small(page, 1200)

            selected_block_label, block, block_debug_after = select_first_matching_market_block(page, BLOCK_LABEL_CANDIDATES)
            summary["selected_block_label"] = selected_block_label
            summary["block_locator_debug_candidates"] = (block_debug_after or block_debug)[:100]

            if block is None:
                raise RuntimeError(
                    "market_block_not_found_after_expand: "
                    f"block_candidates={BLOCK_LABEL_CANDIDATES}"
                )

            try:
                block.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            wait_small(page, 500)
            page.screenshot(path=str(run_dir / "after_expand.png"), full_page=True)

            block_text = safe_inner_text(block, timeout=2500)
            block_html = ""
            try:
                block_html = block.evaluate("el => el.outerHTML")
            except Exception:
                block_html = ""

            summary["remaining_see_more_in_market"] = remaining_see_more_in_block(block)
            isolated = isolate_lines(block_text)
            rows, debug_players = parse_points_rows(isolated, teams)

            rows_valid, rows_validation_reason = validate_rows(rows)
            summary["rows_valid"] = rows_valid
            summary["rows_validation_reason"] = rows_validation_reason
            summary["isolated_lines"] = len(isolated)
            summary["parsed_rows_clean"] = len(rows)
            summary["players_seen"] = len(debug_players)
            summary["players_kept_points_1_plus"] = len(rows)
            summary["is_complete_market"] = summary["remaining_see_more_in_market"] == 0

            write_text(run_dir / "points_market_only.txt", block_text)
            write_text(run_dir / "points_market_block.html", block_html)
            write_json(run_dir / "points_market_isolated_lines.json", isolated)
            write_json(run_dir / "points_market_rows_clean.json", rows)
            write_json(run_dir / "points_market_players_debug.json", debug_players)
            write_json(run_dir / "summary.json", summary)

            print(json.dumps(summary, ensure_ascii=False, indent=2))

        except Exception as exc:
            summary["fatal_error"] = str(exc)
            summary["isolated_lines"] = len(isolated)
            summary["parsed_rows_clean"] = len(rows)
            summary["players_seen"] = len(debug_players)
            summary["players_kept_points_1_plus"] = len(rows)
            summary["is_complete_market"] = summary["remaining_see_more_in_market"] == 0

            try:
                page.screenshot(path=str(run_dir / "fatal.png"), full_page=True)
            except Exception:
                pass
            try:
                write_text(run_dir / "fatal_page_source.html", page.content())
            except Exception:
                pass

            write_json(run_dir / "points_market_isolated_lines.json", isolated)
            write_json(run_dir / "points_market_rows_clean.json", rows)
            write_json(run_dir / "points_market_players_debug.json", debug_players)
            write_json(run_dir / "summary.json", summary)

            print(json.dumps(summary, ensure_ascii=False, indent=2))
            raise

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
