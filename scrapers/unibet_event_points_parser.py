#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

POINTS_BLOCK_LABEL_CANDIDATES = [
    "Nombre de Points - Joueur - Match",
    "Nombre de Points - Joueur",
    "NOMBRE DE POINTS - JOUEUR - MATCH",
    "NOMBRE DE POINTS - JOUEUR",
    "NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)",
    "NOMBRE DE POINTS DU JOUEUR",
]
PRIMARY_TAB_LABEL_CANDIDATES = ["Joueurs", "Points", "Joueur"]
SECONDARY_TAB_LABEL_CANDIDATES = ["Points", "Joueurs"]
ARTIFACTS_ROOT = Path("artifacts") / "unibet_event_points_parser"
NAME_RE = r"[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-.]+(?:,\s*[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-.]+|(?:\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-.]+){1,2})"


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(f"[{now_ts()}] {msg}")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def norm_spaces(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def strip_accents(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    return text.encode("ascii", "ignore").decode("ascii")


def normalize_for_match(text: Any) -> str:
    text = strip_accents(text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def safe_inner_text(locator: Locator, timeout: int = 1000) -> str:
    try:
        return locator.inner_text(timeout=timeout)
    except Exception:
        return ""


def dedupe_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        clean = norm_spaces(item)
        key = normalize_for_match(clean)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def normalize_team_label(text: str) -> str:
    value = normalize_for_match(text)
    value = re.sub(r"\bvs\b", " ", value)
    value = re.sub(r"\bv\b", " ", value)
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def extract_teams_from_title(title: str) -> List[str]:
    candidates = [
        r"Cotes\s+(.*?)\s*-\s*(.*?),\s*NHL\s*-\s*Unibet",
        r"Pariez sur\s+(.*?)\s*-\s*(.*?)\s*\| Hockey sur Glace \| Unibet\.fr",
        r"(.*?)\s*-\s*(.*?)\s*\| Hockey sur Glace \| Unibet\.fr",
    ]
    for pattern in candidates:
        m = re.search(pattern, title or "", re.I)
        if m:
            return dedupe_keep_order([m.group(1), m.group(2)])
    return []


def extract_teams_from_url(event_url: str) -> List[str]:
    path = (urlparse(event_url).path or "").rstrip("/")
    m = re.search(r"/paris-hockey-sur-glace/etats-unis/nhl/\d+/([^/]+)$", path, re.I)
    if m:
        slug = m.group(1)
        if "-vs-" in slug:
            left, right = slug.split("-vs-", 1)
            return dedupe_keep_order([left.replace("-", " "), right.replace("-", " ")])
    return []


def looks_like_matchup(text: str) -> bool:
    txt = norm_spaces(text)
    if len(txt) < 7:
        return False
    return bool(re.search(r"\s*[-–—]\s*", txt) or re.search(r"\bvs\b", txt, re.I))


def split_matchup_text(text: str) -> List[str]:
    txt = norm_spaces(text)
    txt = re.sub(r"\s+vs\s+", " - ", txt, flags=re.I)
    parts = re.split(r"\s*[-–—]\s*", txt)
    parts = [norm_spaces(p) for p in parts if norm_spaces(p)]
    return dedupe_keep_order(parts) if len(parts) == 2 else []


def extract_teams_from_h1(page: Page) -> List[str]:
    selectors = ["h1", "[data-test='event-header']", "[class*='event'] h1", "[class*='Event'] h1"]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = min(loc.count(), 10)
            for i in range(count):
                txt = safe_inner_text(loc.nth(i))
                if looks_like_matchup(txt):
                    teams = split_matchup_text(txt)
                    if len(teams) == 2:
                        return teams
        except Exception:
            continue
    return []


def resolve_teams(page: Page, event_url: str, title: str) -> Tuple[List[str], str]:
    teams = extract_teams_from_title(title)
    if len(teams) == 2:
        return teams, "title"
    teams = extract_teams_from_h1(page)
    if len(teams) == 2:
        return teams, "h1"
    teams = extract_teams_from_url(event_url)
    if len(teams) == 2:
        return teams, "url_slug"
    return [], "none"


def click_cookie_if_present(page: Page) -> Optional[str]:
    selectors = [
        "button:has-text('Accepter')",
        "button:has-text('Tout accepter')",
        "button:has-text(\"J'accepte\")",
        "button:has-text('J’accepte')",
        "button:has-text('Autoriser tous les cookies')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=2000)
                log(f"cookie clicked: {sel}")
                return sel
        except Exception:
            continue
    return None


def click_label(page: Page, label: str) -> bool:
    candidates = [
        page.get_by_role("tab", name=label, exact=False),
        page.get_by_role("button", name=label, exact=False),
        page.locator(f"button:has-text('{label}')"),
        page.locator(f"[role='tab']:has-text('{label}')"),
        page.get_by_text(label, exact=False),
    ]
    for loc in candidates:
        try:
            if loc.count() == 0:
                continue
            target = loc.first
            if target.is_visible(timeout=2500):
                target.click(timeout=5000)
                log(f"clicked label: {label}")
                return True
        except Exception:
            continue
    return False


def click_first_matching_label(page: Page, labels: List[str]) -> Optional[str]:
    for label in labels:
        if click_label(page, label):
            return label
    return None


def small_scroll(page: Page, rounds: int = 3, pixels: int = 1200, wait_ms: int = 700) -> None:
    for _ in range(rounds):
        try:
            page.mouse.wheel(0, pixels)
        except Exception:
            pass
        page.wait_for_timeout(wait_ms)


def click_all_expand_on_page(page: Page, max_rounds: int = 8) -> int:
    total_clicks = 0
    for round_idx in range(1, max_rounds + 1):
        clicked = 0
        try:
            clicked = int(page.evaluate(
                r"""
                () => {
                  const normalize = (value) => String(value || '')
                    .normalize('NFD')
                    .replace(/[\u0300-\u036f]/g, '')
                    .toLowerCase()
                    .replace(/\s+/g, ' ')
                    .trim();
                  const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                  };
                  const all = Array.from(document.querySelectorAll('button, a, [role="button"], div, span'));
                  let clicked = 0;
                  for (const el of all) {
                    if (!isVisible(el)) continue;
                    const txt = normalize(el.innerText || el.textContent || '');
                    if (!txt) continue;
                    if (txt !== 'afficher plus' && txt !== 'voir plus') continue;
                    let target = el;
                    for (let hops = 0; hops < 4 && target; hops += 1) {
                      if (typeof target.click === 'function') break;
                      target = target.parentElement;
                    }
                    try { target.scrollIntoView({block: 'center'}); } catch (e) {}
                    try {
                      target.click();
                      clicked += 1;
                    } catch (e) {}
                  }
                  return clicked;
                }
                """
            ))
        except Exception:
            clicked = 0
        if clicked <= 0:
            break
        total_clicks += clicked
        log(f"global expand round {round_idx}: clicked={clicked}")
        page.wait_for_timeout(900)
    log(f"total global expand clicks: {total_clicks}")
    return total_clicks


def extract_points_market_from_page_text(page_text: str) -> str:
    raw = str(page_text or '')
    if not raw.strip():
        return ''
    start_patterns = [
        r'Nombre de Points - Joueur - Match',
        r'Nombre de Points - Joueur',
        r'NOMBRE DE POINTS - JOUEUR - MATCH',
        r'NOMBRE DE POINTS - JOUEUR',
        r'NOMBRE DE POINTS DU JOUEUR(?: \(PROLONGATIONS INCLUSES\))?',
    ]
    stop_patterns = [
        r'\n\s*1N2 Handicap',
        r'\n\s*Face à Face Handicap Buts',
        r'\n\s*Face a Face Handicap Buts',
        r'\n\s*Ecart du gagnant',
        r'\n\s*Plus / Moins Buts',
        r'\n\s*Plus / Moins But\(s\) - Equipe',
        r'\n\s*Résultat et Plus/Moins Buts',
        r'\n\s*Resultat et Plus/Moins Buts',
        r'\n\s*Score Exact',
        r'\n\s*Tiers-Temps le plus prolifique',
        r'\n\s*Les 2 équipes marqueront-elles\?',
        r'\n\s*Les 2 equipes marqueront-elles\?',
        r'\n\s*Equipe inscrivant le 1er but',
        r'\n\s*Equipe inscrivant le dernier but',
        r'\n\s*Le match ira-t-il en prolongation',
    ]
    best = ''
    best_label = ''
    for pat in start_patterns:
        for m in re.finditer(pat, raw, flags=re.I):
            start = m.start()
            end = len(raw)
            after = raw[m.end():]
            stop_pos = None
            for stop_pat in stop_patterns:
                sm = re.search(stop_pat, after, flags=re.I)
                if sm:
                    candidate = m.end() + sm.start()
                    if stop_pos is None or candidate < stop_pos:
                        stop_pos = candidate
            if stop_pos is not None:
                end = stop_pos
            segment = raw[start:end].strip()
            if len(segment) > len(best):
                best = segment
                best_label = m.group(0)
    return best


def isolate_lines(block_text: str) -> List[str]:
    raw_lines = [norm_spaces(x) for x in block_text.splitlines()]
    return [x for x in raw_lines if x]


def is_valid_player_name(player_name: str, teams: List[str]) -> bool:
    name = norm_spaces(player_name)
    if not name:
        return False
    key = normalize_for_match(name)
    if key in {"1+", "2+", "3+", "4+", "afficher plus", "voir plus", "cashout", "joueurs", "points", "buts", "combos", "score exact", "prolongation", "paris populaires", "match"}:
        return False
    if len(name.split()) < 2:
        return False
    if not re.fullmatch(NAME_RE, name):
        return False
    team_keys = {normalize_team_label(t) for t in teams if t}
    name_team_key = normalize_team_label(name)
    if name_team_key in team_keys:
        return False
    return True


def dedupe_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    deduped_rows: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        key = (
            normalize_for_match(row.get("team")),
            normalize_for_match(row.get("player_name_raw")),
            normalize_for_match(row.get("outcome_label")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped_rows.append(row)
    return deduped_rows


def parse_points_rows_from_regex(block_text: str, teams: List[str]) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]], str]:
    text = norm_spaces(block_text)
    text = re.sub(r'^.*?nombre de points(?: du joueur| - joueur(?: - match)?)\s*', '', text, flags=re.I)
    text = re.sub(r'\b(?:Cashout|Afficher plus|Voir plus)\b', ' ', text, flags=re.I)
    pattern = re.compile(rf"({NAME_RE})\s+1\+\s*([\d.,]+)", re.I)

    rows: List[Dict[str, str]] = []
    debug_players: List[Dict[str, Any]] = []
    for m in pattern.finditer(text):
        player_name = norm_spaces(m.group(1))
        odd = norm_spaces(m.group(2))
        if not is_valid_player_name(player_name, teams):
            continue
        row = {
            "team": "",
            "player_name_raw": player_name,
            "outcome_label": "1 ou plus",
            "odds_raw": odd,
        }
        rows.append(row)
        debug_players.append({
            "team": "",
            "player_name_raw": player_name,
            "odds_count_seen": 1,
            "kept_outcome_label": "1 ou plus",
            "kept_odds_values": [odd],
            "parser_mode": "regex_body_text",
        })
    return dedupe_rows(rows), debug_players, "regex_body_text"


def validate_rows(rows: List[Dict[str, str]], block_text: str) -> Tuple[bool, str]:
    if not rows:
        return False, "no_rows"
    txt = normalize_for_match(block_text)
    if "nombre de points" not in txt or "joueur" not in txt:
        return False, "wrong_block_missing_points_heading"
    for row in rows:
        if not row.get("player_name_raw"):
            return False, "missing_player"
        if row.get("outcome_label") != "1 ou plus":
            return False, "invalid_outcome_label"
        if not row.get("odds_raw"):
            return False, "missing_odds_raw"
    return True, "ok"


def main() -> None:
    event_url = os.getenv("UNIBET_EVENT_URL", "").strip()
    headless = os.getenv("PW_HEADLESS", "true").lower() == "true"
    if not event_url:
        raise ValueError("UNIBET_EVENT_URL is required")

    run_dir = ARTIFACTS_ROOT / now_ts()
    ensure_dir(run_dir)

    summary: Dict[str, Any] = {
        "title": None,
        "event_url": event_url,
        "final_url": None,
        "cookie_clicked": None,
        "teams": [],
        "teams_source": None,
        "clicked_primary_tab_label": None,
        "clicked_secondary_tab_label": None,
        "primary_tab_label_candidates": PRIMARY_TAB_LABEL_CANDIDATES,
        "secondary_tab_label_candidates": SECONDARY_TAB_LABEL_CANDIDATES,
        "selected_block_label": "Nombre de Points - Joueur - Match",
        "block_label_candidates": POINTS_BLOCK_LABEL_CANDIDATES,
        "market_block_found": False,
        "market_block_selection": None,
        "market_block_foreign_noise": [],
        "see_more_clicks": 0,
        "remaining_see_more_in_market": -1,
        "is_complete_market": False,
        "rows_valid": False,
        "rows_validation_reason": "not_run",
        "isolated_lines": 0,
        "parsed_rows_clean": 0,
        "players_seen": 0,
        "players_kept_points_1_plus": 0,
        "team_assignment_mode": None,
        "used_body_text_fallback": True,
        "run_dir": str(run_dir),
        "fatal_error": None,
    }

    isolated: List[str] = []
    rows: List[Dict[str, str]] = []
    debug_players: List[Dict[str, Any]] = []
    block_text = ""
    block_selection_debug: Dict[str, Any] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 2600})
        page = context.new_page()

        try:
            log(f"goto: {event_url}")
            page.goto(event_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2.0)

            summary["cookie_clicked"] = click_cookie_if_present(page)
            time.sleep(1.0)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except PlaywrightTimeoutError:
                pass

            page.screenshot(path=str(run_dir / "landing.png"), full_page=True)
            summary["final_url"] = page.url
            summary["title"] = page.title()
            write_text(run_dir / "page_title.txt", summary["title"] + "\n")
            write_text(run_dir / "final_url.txt", summary["final_url"] + "\n")

            teams, teams_source = resolve_teams(page, event_url, summary["title"])
            summary["teams"] = teams
            summary["teams_source"] = teams_source
            log(f"teams detected: {teams} source={teams_source}")

            summary["clicked_primary_tab_label"] = click_first_matching_label(page, PRIMARY_TAB_LABEL_CANDIDATES)
            time.sleep(1.2)
            summary["clicked_secondary_tab_label"] = click_first_matching_label(page, SECONDARY_TAB_LABEL_CANDIDATES)
            time.sleep(1.2)
            small_scroll(page, rounds=2, pixels=1200, wait_ms=800)
            page.screenshot(path=str(run_dir / "after_tab_click.png"), full_page=True)

            summary["see_more_clicks"] = click_all_expand_on_page(page)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(run_dir / "after_expand.png"), full_page=True)

            body_text = safe_inner_text(page.locator("body"), timeout=8000)
            write_text(run_dir / "body_text.txt", body_text)
            block_text = extract_points_market_from_page_text(body_text)
            summary["market_block_found"] = bool(block_text)
            summary["is_complete_market"] = "afficher plus" not in normalize_for_match(block_text) and "voir plus" not in normalize_for_match(block_text)
            summary["remaining_see_more_in_market"] = 0 if summary["is_complete_market"] else 1

            block_selection_debug = {
                "mode": "body_text_only",
                "found": bool(block_text),
                "segment_length": len(block_text),
                "preview": block_text[:1000],
            }
            summary["market_block_selection"] = block_selection_debug
            write_json(run_dir / "market_block_selection_debug.json", block_selection_debug)
            write_text(run_dir / "points_market_only.txt", block_text)
            write_text(run_dir / "points_market_only_fallback_from_body.txt", block_text)

            if not block_text:
                raise RuntimeError("market_block_not_found_in_body_text")

            isolated = isolate_lines(block_text)
            rows, debug_players, team_mode = parse_points_rows_from_regex(block_text, teams)

            rows_valid, rows_validation_reason = validate_rows(rows, block_text)
            summary["rows_valid"] = rows_valid
            summary["rows_validation_reason"] = rows_validation_reason
            summary["isolated_lines"] = len(isolated)
            summary["parsed_rows_clean"] = len(rows)
            summary["players_seen"] = len(debug_players)
            summary["players_kept_points_1_plus"] = len(rows)
            summary["team_assignment_mode"] = team_mode

            write_json(run_dir / "points_market_isolated_lines.json", isolated)
            write_json(run_dir / "points_market_rows_clean.json", rows)
            write_json(run_dir / "points_market_players_debug.json", debug_players)
            write_json(run_dir / "summary.json", summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))

            if not rows_valid:
                raise RuntimeError(f"rows_invalid: {rows_validation_reason}")

        except Exception as exc:
            summary["fatal_error"] = str(exc)
            summary["isolated_lines"] = len(isolated)
            summary["parsed_rows_clean"] = len(rows)
            summary["players_seen"] = len(debug_players)
            summary["players_kept_points_1_plus"] = len(rows)
            write_json(run_dir / "market_block_selection_debug.json", block_selection_debug)
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
