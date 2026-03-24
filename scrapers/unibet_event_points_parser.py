#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scrapers/unibet_event_points_parser.py

Objectif
--------
Parser proprement le marché Unibet :
- NOMBRE DE POINTS - JOUEUR

Règle métier
------------
- garder uniquement l'issue : "1 ou plus"
- ignorer complètement : "2+", "3+", "4+"
- cliquer les boutons "Afficher plus" / "Voir plus" dans le bloc ciblé seulement
"""

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
    "Nombre de Points - Joueur",
    "NOMBRE DE POINTS - JOUEUR",
    "NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)",
    "NOMBRE DE POINTS DU JOUEUR",
]
PRIMARY_TAB_LABEL_CANDIDATES = ["Joueurs", "Points", "Joueur"]
SECONDARY_TAB_LABEL_CANDIDATES = ["Points", "Joueurs"]
MARKET_MARKER_ATTR = "data-oai-points-market-target"
MARKET_MARKER_VALUE = "1"
ARTIFACTS_ROOT = Path("artifacts") / "unibet_event_points_parser"
NAME_RE = r"[A-Za-zÀ-ÿ'’\-\. ]+,\s*[A-Za-zÀ-ÿ'’\-\. ]+"


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


def safe_count(locator: Locator, max_count: Optional[int] = None) -> int:
    try:
        c = locator.count()
        if max_count is not None:
            return min(c, max_count)
        return c
    except Exception:
        return 0


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
            return dedupe_keep_order([
                left.replace("-", " "),
                right.replace("-", " "),
            ])
    m = re.search(r"/event/([^/]+?)-\d+_\d+\.html$", path, re.I)
    if not m:
        m = re.search(r"/event/([^/]+)\.html$", path, re.I)
    if m:
        slug = m.group(1)
        parts = [p for p in slug.split("-") if p]
        if len(parts) >= 4:
            half = len(parts) // 2
            return dedupe_keep_order([
                " ".join(parts[:half]).replace("_", " "),
                " ".join(parts[half:]).replace("_", " "),
            ])
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
            for i in range(safe_count(loc, 10)):
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
            if safe_count(loc) == 0:
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


def select_exact_points_market_block(page: Page, teams: List[str]) -> Dict[str, Any]:
    payload = {
        "labels": POINTS_BLOCK_LABEL_CANDIDATES,
        "team_names": teams,
        "marker_attr": MARKET_MARKER_ATTR,
        "marker_value": MARKET_MARKER_VALUE,
    }
    result = page.evaluate(
        """
        (cfg) => {
          const normalize = (value) => String(value || '')
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .toLowerCase()
            .replace(/\s+/g, ' ')
            .trim();

          const cleanOwnText = (el) => {
            const chunks = [];
            for (const node of Array.from(el.childNodes || [])) {
              if (node && node.nodeType === Node.TEXT_NODE) chunks.push(node.textContent || '');
            }
            return normalize(chunks.join(' '));
          };

          const countMatches = (text, re) => (text.match(re) || []).length;
          const targetLabels = (cfg.labels || []).map(normalize).filter(Boolean);
          const markerAttr = cfg.marker_attr;
          const markerValue = cfg.marker_value;
          const teamNames = (cfg.team_names || []).map(normalize).filter(Boolean);
          const all = Array.from(document.querySelectorAll('div, section, article, li'));

          for (const prev of Array.from(document.querySelectorAll(`[${markerAttr}]`))) {
            prev.removeAttribute(markerAttr);
          }

          const candidates = [];
          for (const el of all) {
            const raw = el.innerText || '';
            const text = normalize(raw);
            if (!text) continue;
            if (!text.includes('nombre de points')) continue;
            if (!text.includes('joueur')) continue;

            const ownText = cleanOwnText(el);
            const startsWithPoints = text.startsWith('nombre de points');
            const ownStartsWithPoints = ownText.startsWith('nombre de points');
            const onePlusHits = countMatches(text, /(?:^|\s)1\+(?:\s|$)/g);
            const showMoreHits = countMatches(text, /afficher plus|voir plus/g);
            const butsHits = countMatches(text, /nombre de buts - joueur/g);
            const passesHits = countMatches(text, /nombre de passes decisives - joueur/g);
            const teamHits = teamNames.filter(x => text.includes(x)).length;
            const lineCount = raw.split(/\n+/).map(x => x.trim()).filter(Boolean).length;
            const textLength = text.length;
            const oddCount = countMatches(text, /\b\d+(?:[.,]\d+)?\b/g);
            const headerMatch = targetLabels.some(lbl => text.startsWith(lbl));

            let score = 0;
            if (headerMatch) score += 180;
            if (startsWithPoints) score += 120;
            if (ownStartsWithPoints) score += 140;
            if (onePlusHits >= 4) score += 60;
            if (teamHits >= 1) score += 20;
            if (oddCount >= 8) score += 20;
            if (lineCount >= 6 && lineCount <= 120) score += 20;
            if (textLength >= 100 && textLength <= 3500) score += 20;
            if (showMoreHits <= 4) score += 15;
            score -= butsHits * 120;
            score -= passesHits * 120;
            if (textLength > 6000) score -= 150;
            if (lineCount > 180) score -= 120;

            candidates.push({
              tag: el.tagName,
              text_length: textLength,
              line_count: lineCount,
              odd_count: oddCount,
              one_plus_hits: onePlusHits,
              show_more_hits: showMoreHits,
              team_hits: teamHits,
              buts_hits: butsHits,
              passes_hits: passesHits,
              starts_with_points: startsWithPoints,
              own_starts_with_points: ownStartsWithPoints,
              score,
              preview: raw.slice(0, 700),
              element: el,
            });
          }

          candidates.sort((a, b) => {
            if (b.score !== a.score) return b.score - a.score;
            if (a.text_length !== b.text_length) return a.text_length - b.text_length;
            return a.line_count - b.line_count;
          });

          const top = candidates.slice(0, 10).map(c => ({
            tag: c.tag,
            text_length: c.text_length,
            line_count: c.line_count,
            odd_count: c.odd_count,
            one_plus_hits: c.one_plus_hits,
            show_more_hits: c.show_more_hits,
            team_hits: c.team_hits,
            buts_hits: c.buts_hits,
            passes_hits: c.passes_hits,
            starts_with_points: c.starts_with_points,
            own_starts_with_points: c.own_starts_with_points,
            score: c.score,
            preview: c.preview,
          }));

          if (!candidates.length) {
            return {found: false, selected: null, top_candidates: top};
          }

          const best = candidates[0];
          best.element.setAttribute(markerAttr, markerValue);
          return {
            found: true,
            selected: {
              tag: best.tag,
              text_length: best.text_length,
              line_count: best.line_count,
              odd_count: best.odd_count,
              one_plus_hits: best.one_plus_hits,
              show_more_hits: best.show_more_hits,
              team_hits: best.team_hits,
              buts_hits: best.buts_hits,
              passes_hits: best.passes_hits,
              starts_with_points: best.starts_with_points,
              own_starts_with_points: best.own_starts_with_points,
              score: best.score,
              preview: best.preview,
            },
            top_candidates: top,
          };
        }
        """,
        payload,
    )
    return result


def get_marked_market_block(page: Page) -> Optional[Locator]:
    loc = page.locator(f"[{MARKET_MARKER_ATTR}='{MARKET_MARKER_VALUE}']")
    if safe_count(loc) == 0:
        return None
    return loc.first


def click_all_expand_in_block(block: Locator, max_rounds: int = 10) -> int:
    total_clicks = 0
    for round_idx in range(1, max_rounds + 1):
        clicked_this_round = 0
        try:
            buttons = block.locator("button, a, [role='button']")
            for i in range(safe_count(buttons, 200)):
                btn = buttons.nth(i)
                txt = normalize_for_match(safe_inner_text(btn, timeout=800))
                if "afficher plus" not in txt and "voir plus" not in txt:
                    continue
                try:
                    btn.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                try:
                    btn.click(timeout=3000)
                    clicked_this_round += 1
                    total_clicks += 1
                    log(f"clicked expand in points block #{total_clicks}")
                    time.sleep(0.8)
                except Exception:
                    continue
        except Exception:
            pass

        log(f"expand round {round_idx}: clicked={clicked_this_round}")
        if clicked_this_round == 0:
            break
        time.sleep(0.8)
    log(f"total expand clicks in points block: {total_clicks}")
    return total_clicks


def remaining_expand_in_block(block: Locator) -> int:
    txt = normalize_for_match(safe_inner_text(block, timeout=1200))
    if not txt:
        return -1
    return txt.count("afficher plus") + txt.count("voir plus")


def contains_foreign_market_noise(block_text: str) -> List[str]:
    txt = normalize_for_match(block_text)
    banned = [
        "nombre de buts - joueur",
        "nombre de passes decisives - joueur",
        "buteur (prolongations incluses)",
        "buteur double chance",
    ]
    return [label for label in banned if label in txt]


def isolate_lines(block_text: str) -> List[str]:
    raw_lines = [norm_spaces(x) for x in block_text.splitlines()]
    return [x for x in raw_lines if x]


def is_decimal_odd(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", norm_spaces(token)))


def is_valid_player_name(player_name: str, teams: List[str]) -> bool:
    name = norm_spaces(player_name)
    if not name:
        return False
    key = normalize_for_match(name)
    if key in {"1+", "2+", "3+", "4+", "afficher plus", "voir plus", "cashout"}:
        return False
    if len(name.split()) < 2:
        return False
    if "," not in name:
        return False
    team_keys = {normalize_team_label(t) for t in teams if t}
    name_team_key = normalize_team_label(name)
    if name_team_key in team_keys:
        return False
    return True


def assign_team_from_context(current_team: Optional[str]) -> str:
    return current_team or ""


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


def parse_points_rows_from_lines(lines: List[str], teams: List[str]) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]], str]:
    rows: List[Dict[str, str]] = []
    debug_players: List[Dict[str, Any]] = []
    i = 0
    current_team: Optional[str] = None
    header_tokens = {"1+", "2+", "3+", "4+", "1 ou plus", "2 ou plus", "3 ou plus", "4 ou plus"}
    team_match_keys = {normalize_team_label(t): t for t in teams if t}

    while i < len(lines):
        line = lines[i]
        line_key = normalize_team_label(line)
        if line_key in team_match_keys:
            current_team = team_match_keys[line_key]
            i += 1
            while i < len(lines) and normalize_for_match(lines[i]) in header_tokens:
                i += 1
            continue

        player_name = line
        if not is_valid_player_name(player_name, teams):
            i += 1
            continue

        j = i + 1
        odds: List[str] = []
        while j < len(lines) and len(odds) < 4:
            token = norm_spaces(lines[j])
            token_key = normalize_for_match(token)
            if normalize_team_label(token) in team_match_keys:
                break
            if token_key in header_tokens:
                j += 1
                continue
            if "afficher plus" in token_key or "voir plus" in token_key:
                break
            if is_decimal_odd(token) or token == "-":
                odds.append(token)
                j += 1
            else:
                break

        if odds:
            first_odd = odds[0]
            team = assign_team_from_context(current_team)
            debug_players.append({
                "team": team,
                "player_name_raw": player_name,
                "odds_count_seen": len(odds),
                "kept_outcome_label": "1 ou plus",
                "kept_odds_values": [first_odd] if first_odd != "-" else [],
                "parser_mode": "line_based",
            })
            if first_odd != "-":
                rows.append({
                    "team": team,
                    "player_name_raw": player_name,
                    "outcome_label": "1 ou plus",
                    "odds_raw": first_odd,
                })
            i = j
            continue
        i += 1

    mode = "line_based_with_team" if any(r.get("team") for r in rows) else "line_based_no_team"
    return dedupe_rows(rows), debug_players, mode


def parse_points_rows_from_regex(block_text: str) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]], str]:
    text = norm_spaces(block_text)
    parts = re.split(r"nombre de points(?: du joueur| - joueur)", text, flags=re.I)
    if parts:
        text = norm_spaces(parts[-1])
    text = re.sub(r"^.*?\|\s*", "", text)
    pattern = re.compile(rf"({NAME_RE})\s+1\+\s*([\d.,]+)", re.I)

    rows: List[Dict[str, str]] = []
    debug_players: List[Dict[str, Any]] = []
    for m in pattern.finditer(text):
        player_name = norm_spaces(m.group(1))
        odd = norm_spaces(m.group(2))
        rows.append({
            "team": "",
            "player_name_raw": player_name,
            "outcome_label": "1 ou plus",
            "odds_raw": odd,
        })
        debug_players.append({
            "team": "",
            "player_name_raw": player_name,
            "odds_count_seen": 1,
            "kept_outcome_label": "1 ou plus",
            "kept_odds_values": [odd],
            "parser_mode": "regex_flattened",
        })
    return dedupe_rows(rows), debug_players, "regex_flattened_no_team"


def parse_points_rows(lines: List[str], block_text: str, teams: List[str]) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]], str]:
    rows_line, debug_line, mode_line = parse_points_rows_from_lines(lines, teams)
    if rows_line:
        return rows_line, debug_line, mode_line
    return parse_points_rows_from_regex(block_text)


def validate_rows(rows: List[Dict[str, str]], block_text: str) -> Tuple[bool, str]:
    if not rows:
        return False, "no_rows"
    txt = normalize_for_match(block_text)
    if "nombre de points" not in txt or "joueur" not in txt:
        return False, "wrong_block_missing_points_heading"
    foreign_noise = contains_foreign_market_noise(block_text)
    if foreign_noise and len(rows) < 3:
        return False, "wrong_block_foreign_market_noise"
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
        "selected_block_label": "Nombre de Points - Joueur",
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

            block_selection_debug = select_exact_points_market_block(page, teams)
            write_json(run_dir / "market_block_selection_debug.json", block_selection_debug)
            summary["market_block_selection"] = block_selection_debug.get("selected")
            summary["market_block_found"] = bool(block_selection_debug.get("found"))

            block = get_marked_market_block(page)
            if block is None:
                raise RuntimeError("market_block_not_found: impossible de cibler le bloc exact 'Nombre de Points - Joueur'")

            try:
                block.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass
            time.sleep(1.0)

            summary["see_more_clicks"] = click_all_expand_in_block(block)
            time.sleep(1.2)

            block_selection_debug = select_exact_points_market_block(page, teams)
            write_json(run_dir / "market_block_selection_debug.json", block_selection_debug)
            summary["market_block_selection"] = block_selection_debug.get("selected")
            summary["market_block_found"] = bool(block_selection_debug.get("found"))

            block = get_marked_market_block(page)
            if block is None:
                raise RuntimeError("market_block_not_found_after_expand: le bloc points n'a plus pu être retrouvé après expansion")

            try:
                block.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass
            page.screenshot(path=str(run_dir / "after_expand.png"), full_page=True)

            block_text = safe_inner_text(block, timeout=2500)
            summary["remaining_see_more_in_market"] = remaining_expand_in_block(block)
            summary["market_block_foreign_noise"] = contains_foreign_market_noise(block_text)
            summary["is_complete_market"] = summary["remaining_see_more_in_market"] == 0

            try:
                outer_html = block.evaluate("el => el.outerHTML")
            except Exception:
                outer_html = ""

            write_text(run_dir / "points_market_only.txt", block_text)
            write_text(run_dir / "points_market_block.html", outer_html)

            isolated = isolate_lines(block_text)
            rows, debug_players, team_mode = parse_points_rows(isolated, block_text, teams)

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
