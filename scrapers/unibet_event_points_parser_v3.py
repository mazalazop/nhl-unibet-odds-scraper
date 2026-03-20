#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scrapers/unibet_event_points_parser.py

Objectif
--------
Parser proprement le marché Unibet :
- NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)

Règle métier
------------
- garder uniquement l'issue : "1 ou plus"
- ignorer complètement : "2 ou plus" et "3 ou plus"
- éviter toute pollution par le bloc BUTEUR ou par les marchés situés plus bas

Entrées via variables d'environnement
-------------------------------------
- UNIBET_EVENT_URL : URL d'un match Unibet
- PW_HEADLESS      : true / false

Sorties
-------
Dans artifacts/unibet_event_points_parser/<timestamp>/ :
- landing.png
- after_tab_click.png
- after_expand.png
- page_title.txt
- final_url.txt
- points_market_only.txt
- points_market_block.html
- market_block_selection_debug.json
- points_market_isolated_lines.json
- points_market_rows_clean.json
- points_market_players_debug.json
- summary.json

Principe
--------
1. ouvrir la page match
2. accepter les cookies si présents
3. cliquer l'onglet Buteurs en priorité
4. identifier le conteneur exact du marché POINTS avec un marquage JS robuste
5. cliquer les "Voir plus" dans ce conteneur seulement
6. isoler les lignes du bloc
7. garder uniquement les cotes "1 ou plus"
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

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


POINTS_BLOCK_LABEL_CANDIDATES = [
    "NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)",
    "NOMBRE DE POINTS DU JOUEUR",
]

TAB_LABEL_CANDIDATES = [
    "Buteurs",
    "Joueurs",
    "Points",
    "Joueur",
]

MARKET_MARKER_ATTR = "data-oai-points-market-target"
MARKET_MARKER_VALUE = "1"
ARTIFACTS_ROOT = Path("artifacts") / "unibet_event_points_parser"


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


def extract_teams_from_title(title: str) -> List[str]:
    patterns = [
        r"Pariez sur (.*?) - (.*?) \| Hockey sur Glace \| Unibet\.fr",
        r"(.*?) - (.*?) \| Hockey sur Glace \| Unibet\.fr",
        r"(.*?) v (.*?) \| Hockey sur Glace \| Unibet\.fr",
    ]

    for pattern in patterns:
        m = re.search(pattern, title or "", re.I)
        if m:
            return dedupe_keep_order([m.group(1), m.group(2)])
    return []


def extract_teams_from_url(event_url: str) -> List[str]:
    if not event_url:
        return []

    m = re.search(r"/event/([^/]+?)-\d+_\d+\.html", event_url)
    if not m:
        m = re.search(r"/event/([^/]+)\.html", event_url)
    if not m:
        return []

    slug = m.group(1)
    parts = [p for p in slug.split("-") if p]
    if len(parts) < 4:
        return []

    half = len(parts) // 2
    team1 = " ".join(parts[:half]).replace("_", " ")
    team2 = " ".join(parts[half:]).replace("_", " ")
    teams = dedupe_keep_order([team1, team2])
    return teams if len(teams) == 2 else []


def looks_like_matchup(text: str) -> bool:
    txt = norm_spaces(text)
    if len(txt) < 7:
        return False
    if re.search(r"\b\d+(?:[.,]\d+)?\b", txt):
        return False
    return bool(re.search(r"\s[-–—]\s", txt))


def split_matchup_text(text: str) -> List[str]:
    parts = re.split(r"\s[-–—]\s", norm_spaces(text))
    parts = [norm_spaces(p) for p in parts if norm_spaces(p)]
    return dedupe_keep_order(parts) if len(parts) == 2 else []


def extract_teams_from_h1(page: Page) -> List[str]:
    selectors = [
        "h1",
        "[data-test='event-header']",
        "[class*='event'] h1",
        "[class*='Event'] h1",
    ]
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


def small_scroll(page: Page, rounds: int = 4, pixels: int = 1200, wait_ms: int = 800) -> None:
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
          const normalize = (value) => {
            return String(value || "")
              .normalize("NFD")
              .replace(/[\u0300-\u036f]/g, "")
              .toLowerCase()
              .replace(/\s+/g, " ")
              .trim();
          };

          const cleanOwnText = (el) => {
            const chunks = [];
            for (const node of Array.from(el.childNodes || [])) {
              if (node && node.nodeType === Node.TEXT_NODE) {
                chunks.push(node.textContent || "");
              }
            }
            return normalize(chunks.join(" "));
          };

          const countMatches = (text, needle) => {
            if (!needle) return 0;
            const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
            const re = new RegExp(escaped, "g");
            const m = text.match(re);
            return m ? m.length : 0;
          };

          const targetLabels = (cfg.labels || []).map(normalize).filter(Boolean);
          const targetLabel = targetLabels[0] || "nombre de points du joueur (prolongations incluses)";
          const markerAttr = cfg.marker_attr;
          const markerValue = cfg.marker_value;
          const teamNames = (cfg.team_names || []).map(normalize).filter(Boolean);

          const navNoise = [
            "tous les paris",
            "resultat",
            "buteurs",
            "passeurs",
            "buts",
            "combo",
            "scores",
            "1er tiers-temps",
            "2e tiers-temps",
            "3e tiers-temps"
          ];

          const foreignMarketLabels = [
            "buteur (prolongations incluses)",
            "buteur double chance",
            "buteur chance triple",
            "les 2 joueurs marquent dans le match",
            "les 3 joueurs marquent dans le match",
            "total de buts marques par le duo",
            "total de buts marques par le trio",
            "1 joueur marque 2 buts ou plus",
            "1 joueur marque 2 buts ou plus - double chance"
          ].map(normalize);

          for (const prev of Array.from(document.querySelectorAll(`[${markerAttr}]`))) {
            prev.removeAttribute(markerAttr);
          }

          const all = Array.from(document.querySelectorAll("div, section, article, li"));
          const headingLike = Array.from(document.querySelectorAll("div, span, h1, h2, h3, h4, strong, b"));

          const candidates = [];
          const seen = new Set();

          const maybeAddCandidate = (originLabel, el, depth) => {
            if (!el || !(el instanceof Element)) return;
            const textRaw = el.innerText || "";
            const text = normalize(textRaw);
            if (!text) return;
            if (!text.includes("nombre de points du joueur")) return;
            if (!text.includes("1 ou plus")) return;

            const key = text.slice(0, 400) + "|" + depth + "|" + (el.tagName || "");
            if (seen.has(key)) return;
            seen.add(key);

            const textLength = text.length;
            const lineCount = (textRaw || "").split(/\n+/).map(x => x.trim()).filter(Boolean).length;
            const ownText = cleanOwnText(el);
            const pointsIdx = text.indexOf("nombre de points du joueur");
            const buteurIdx = text.indexOf("buteur (prolongations incluses)");
            const navHits = navNoise.filter(x => text.includes(x)).length;
            const foreignHits = foreignMarketLabels.filter(x => text.includes(x)).length;
            const ownStartsWithPoints = ownText.startsWith("nombre de points du joueur");
            const startsWithPoints = text.startsWith("nombre de points du joueur");
            const voirPlusCount = countMatches(text, "voir plus");
            const voirMoinsCount = countMatches(text, "voir moins");
            const oddCount = (text.match(/\b\d+(?:[.,]\d+)?\b/g) || []).length;
            const teamHits = teamNames.filter(x => text.includes(x)).length;

            let score = 0;
            score += 100;
            if (startsWithPoints) score += 120;
            if (ownStartsWithPoints) score += 140;
            if (pointsIdx >= 0 && pointsIdx < 40) score += 80;
            if (pointsIdx >= 0 && pointsIdx < 120) score += 40;
            if (teamHits >= 2) score += 40;
            if (oddCount >= 6) score += 20;
            if (lineCount >= 10 && lineCount <= 80) score += 30;
            if (textLength >= 150 && textLength <= 2500) score += 30;
            if (voirPlusCount + voirMoinsCount <= 4) score += 25;
            if (depth <= 4) score += 30;

            if (navHits > 0) score -= navHits * 80;
            if (foreignHits > 0) score -= foreignHits * 120;
            if (buteurIdx >= 0) score -= 200;
            if (textLength > 3500) score -= 180;
            if (lineCount > 120) score -= 180;
            if (voirPlusCount + voirMoinsCount > 6) score -= 120;
            if (!startsWithPoints && !ownStartsWithPoints) score -= 80;

            candidates.push({
              origin_label: originLabel,
              depth,
              tag: el.tagName,
              text_length: textLength,
              line_count: lineCount,
              team_hits: teamHits,
              odd_count: oddCount,
              nav_hits: navHits,
              foreign_hits: foreignHits,
              points_idx: pointsIdx,
              buteur_idx: buteurIdx,
              voir_plus_count: voirPlusCount,
              voir_moins_count: voirMoinsCount,
              starts_with_points: startsWithPoints,
              own_starts_with_points: ownStartsWithPoints,
              score,
              preview: textRaw.slice(0, 500),
              element: el,
            });
          };

          for (const el of headingLike) {
            const txt = normalize(el.innerText || "");
            if (!txt) continue;
            const matched = targetLabels.find(label => txt === label || txt.startsWith(label));
            if (!matched) continue;

            let depth = 0;
            let node = el;
            while (node && node instanceof Element && depth <= 8) {
              maybeAddCandidate(matched, node, depth);
              node = node.parentElement;
              depth += 1;
            }
          }

          for (const el of all) {
            const txt = normalize(el.innerText || "");
            if (!txt) continue;
            if (!txt.startsWith("nombre de points du joueur")) continue;
            maybeAddCandidate("direct_scan", el, 99);
          }

          candidates.sort((a, b) => {
            if (b.score !== a.score) return b.score - a.score;
            if (a.text_length !== b.text_length) return a.text_length - b.text_length;
            if (a.line_count !== b.line_count) return a.line_count - b.line_count;
            return a.depth - b.depth;
          });

          const top = candidates.slice(0, 10).map(c => ({
            origin_label: c.origin_label,
            depth: c.depth,
            tag: c.tag,
            text_length: c.text_length,
            line_count: c.line_count,
            team_hits: c.team_hits,
            odd_count: c.odd_count,
            nav_hits: c.nav_hits,
            foreign_hits: c.foreign_hits,
            points_idx: c.points_idx,
            buteur_idx: c.buteur_idx,
            voir_plus_count: c.voir_plus_count,
            voir_moins_count: c.voir_moins_count,
            starts_with_points: c.starts_with_points,
            own_starts_with_points: c.own_starts_with_points,
            score: c.score,
            preview: c.preview,
          }));

          if (!candidates.length) {
            return {
              found: false,
              selected: null,
              top_candidates: top,
            };
          }

          const best = candidates[0];
          best.element.setAttribute(markerAttr, markerValue);

          return {
            found: true,
            selected: {
              origin_label: best.origin_label,
              depth: best.depth,
              tag: best.tag,
              text_length: best.text_length,
              line_count: best.line_count,
              team_hits: best.team_hits,
              odd_count: best.odd_count,
              nav_hits: best.nav_hits,
              foreign_hits: best.foreign_hits,
              points_idx: best.points_idx,
              buteur_idx: best.buteur_idx,
              voir_plus_count: best.voir_plus_count,
              voir_moins_count: best.voir_moins_count,
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


def click_all_see_more_in_block(block: Locator, max_rounds: int = 8) -> int:
    total_clicks = 0

    for round_idx in range(1, max_rounds + 1):
        clicked_this_round = 0
        try:
            buttons = block.locator("button")
            for i in range(safe_count(buttons, 100)):
                btn = buttons.nth(i)
                txt = normalize_for_match(safe_inner_text(btn, timeout=800))
                if "voir plus" not in txt:
                    continue
                try:
                    btn.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                try:
                    btn.click(timeout=3000)
                    clicked_this_round += 1
                    total_clicks += 1
                    log(f"clicked see more in points block #{total_clicks}")
                    time.sleep(0.8)
                except Exception:
                    continue
        except Exception:
            pass

        log(f"see more round {round_idx}: clicked={clicked_this_round}")
        if clicked_this_round == 0:
            break
        time.sleep(1.0)

    log(f"total see more clicks in points block: {total_clicks}")
    return total_clicks


def remaining_see_more_in_block(block: Locator) -> int:
    try:
        txt = normalize_for_match(safe_inner_text(block, timeout=1200))
        return txt.count("voir plus")
    except Exception:
        return -1


def contains_foreign_market_noise(block_text: str) -> List[str]:
    txt = normalize_for_match(block_text)
    banned = [
        "buteur (prolongations incluses)",
        "buteur double chance",
        "buteur chance triple",
        "les 2 joueurs marquent dans le match",
        "les 3 joueurs marquent dans le match",
        "total de buts marques par le duo",
        "total de buts marques par le trio",
        "1 joueur marque 2 buts ou plus",
        "tous les paris resultat buteurs passeurs buts combo scores",
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

    name_key = normalize_for_match(name)
    if name_key in {"ou plus", "1 ou plus", "2 ou plus", "3 ou plus", "voir plus", "voir moins"}:
        return False
    if re.search(r"^\d+\s+ou\s+plus$", name_key):
        return False
    if name_key == "match nul":
        return False
    if " / " in name or "/" in name_key:
        return False
    if len(name.split()) < 2:
        return False

    team_keys = {normalize_for_match(t) for t in teams if t}
    if name_key in team_keys:
        return False
    for team_key in team_keys:
        if team_key and (team_key in name_key or name_key in team_key):
            return False

    if re.search(
        r"\b(match nul|prolongation|victoire|resultat|double chance|buteur|chance triple|duo|trio)\b",
        name_key,
    ):
        return False

    return True


def parse_points_rows(lines: List[str], teams: List[str]) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    rows: List[Dict[str, str]] = []
    debug_players: List[Dict[str, Any]] = []

    i = 0
    current_team: Optional[str] = None
    header_tokens = {"1 ou plus", "2 ou plus", "3 ou plus"}
    team_match_keys = {normalize_for_match(t) for t in teams if t}

    while i < len(lines):
        line = lines[i]
        line_key = normalize_for_match(line)

        if line_key in team_match_keys:
            current_team = line
            i += 1
            while i < len(lines) and normalize_for_match(lines[i]) in header_tokens:
                i += 1
            continue

        if not current_team:
            i += 1
            continue

        player_name = line
        if not is_valid_player_name(player_name, teams):
            i += 1
            continue

        j = i + 1
        odds: List[str] = []
        while j < len(lines) and len(odds) < 3:
            token = lines[j]
            token_key = normalize_for_match(token)
            if token_key in team_match_keys:
                break
            if token_key in header_tokens:
                break
            if "voir plus" in token_key or "voir moins" in token_key:
                break
            if is_decimal_odd(token) or token == "-":
                odds.append(norm_spaces(token))
                j += 1
            else:
                break

        if not odds:
            i += 1
            continue

        first_odd = odds[0]
        has_dash = any(x == "-" for x in odds)
        kept_odds_values = [x for x in odds[:1] if x != "-"]

        debug_players.append(
            {
                "team": current_team,
                "player_name_raw": player_name,
                "odds_count_seen": len(odds),
                "kept_outcome_label": "1 ou plus",
                "has_dash": has_dash,
                "kept_odds_values": kept_odds_values,
            }
        )

        if first_odd != "-":
            rows.append(
                {
                    "team": current_team,
                    "player_name_raw": player_name,
                    "outcome_label": "1 ou plus",
                    "odds_raw": first_odd,
                }
            )

        i = j

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

    return deduped_rows, debug_players


def validate_rows(rows: List[Dict[str, str]], block_text: str) -> Tuple[bool, str]:
    if not rows:
        return False, "no_rows"

    txt = normalize_for_match(block_text)
    if "nombre de points du joueur" not in txt:
        return False, "wrong_block_missing_points_heading"

    foreign_noise = contains_foreign_market_noise(block_text)
    if foreign_noise:
        return False, "wrong_block_foreign_market_noise"

    for row in rows:
        if not row.get("team"):
            return False, "missing_team"
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
        "clicked_tab_label": None,
        "tab_label_candidates": TAB_LABEL_CANDIDATES,
        "selected_block_label": "NOMBRE DE POINTS DU JOUEUR",
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

            summary["clicked_tab_label"] = click_first_matching_label(page, TAB_LABEL_CANDIDATES)
            time.sleep(1.8)
            small_scroll(page, rounds=2, pixels=1200, wait_ms=800)
            page.screenshot(path=str(run_dir / "after_tab_click.png"), full_page=True)

            block_selection_debug = select_exact_points_market_block(page, teams)
            write_json(run_dir / "market_block_selection_debug.json", block_selection_debug)
            summary["market_block_selection"] = block_selection_debug.get("selected")
            summary["market_block_found"] = bool(block_selection_debug.get("found"))

            block = get_marked_market_block(page)
            if block is None:
                raise RuntimeError(
                    "market_block_not_found: impossible de cibler le bloc exact 'Nombre de points du joueur'"
                )

            try:
                block.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass
            time.sleep(1.0)

            summary["see_more_clicks"] = click_all_see_more_in_block(block)
            time.sleep(1.5)

            block_selection_debug = select_exact_points_market_block(page, teams)
            write_json(run_dir / "market_block_selection_debug.json", block_selection_debug)
            summary["market_block_selection"] = block_selection_debug.get("selected")
            summary["market_block_found"] = bool(block_selection_debug.get("found"))

            block = get_marked_market_block(page)
            if block is None:
                raise RuntimeError(
                    "market_block_not_found_after_expand: le bloc points n'a plus pu être retrouvé après expansion"
                )

            try:
                block.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass
            page.screenshot(path=str(run_dir / "after_expand.png"), full_page=True)

            block_text = safe_inner_text(block, timeout=2500)
            summary["remaining_see_more_in_market"] = remaining_see_more_in_block(block)
            summary["market_block_foreign_noise"] = contains_foreign_market_noise(block_text)
            summary["is_complete_market"] = summary["remaining_see_more_in_market"] == 0

            try:
                outer_html = block.evaluate("el => el.outerHTML")
            except Exception:
                outer_html = ""

            write_text(run_dir / "points_market_only.txt", block_text)
            write_text(run_dir / "points_market_block.html", outer_html)

            isolated = isolate_lines(block_text)
            rows, debug_players = parse_points_rows(isolated, teams)

            rows_valid, rows_validation_reason = validate_rows(rows, block_text)
            summary["rows_valid"] = rows_valid
            summary["rows_validation_reason"] = rows_validation_reason
            summary["isolated_lines"] = len(isolated)
            summary["parsed_rows_clean"] = len(rows)
            summary["players_seen"] = len(debug_players)
            summary["players_kept_points_1_plus"] = len(rows)

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
