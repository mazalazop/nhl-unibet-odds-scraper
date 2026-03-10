import os
import re
import json
import time
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


TAB_LABEL_CANDIDATES = [
    "Buteurs",
    "Buteur",
]

BLOCK_LABEL_CANDIDATES = [
    "BUTEUR (PROLONGATIONS INCLUSES)",
    "BUTEUR",
]


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H:%M:%S")


def log(msg: str):
    print(f"[{now_ts()}] {msg}")


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def extract_teams_from_title(title: str):
    m = re.search(r"Pariez sur (.*?) - (.*?) \| Hockey sur Glace \| Unibet\.fr", title or "", re.I)
    if not m:
        return []
    return [norm_spaces(m.group(1)), norm_spaces(m.group(2))]


def safe_inner_text(locator):
    try:
        return locator.inner_text(timeout=1000)
    except Exception:
        return ""


def click_cookie_if_present(page):
    selectors = [
        "button:has-text('Accepter')",
        "button:has-text('Tout accepter')",
        "button:has-text(\"J'accepte\")",
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


def click_label(page, label: str):
    candidates = [
        page.locator(f"text={label}"),
        page.get_by_text(label, exact=False),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                target = loc.first
                if target.is_visible(timeout=3000):
                    target.click(timeout=5000)
                    log(f"clicked label: {label}")
                    return True
        except Exception:
            continue
    return False


def click_first_matching_label(page, labels):
    for label in labels:
        if click_label(page, label):
            return label
    return None


def score_market_block(text: str, label: str):
    score = 0.0
    txt = norm_spaces(text).lower()
    if label.lower() in txt:
        score += 10.0
    score += txt.count("voir plus") * 2.0
    score += len(re.findall(r"\b\d+(?:[.,]\d+)?\b", txt)) * 0.03
    score += txt.count("buteur") * 0.5
    score += txt.count("+") * 0.2
    return score


def select_first_matching_market_block(page, labels):
    best_block = None
    best_label = None
    best_score = -1.0

    selectors = [
        "section",
        "div",
        "[class*='market']",
        "[class*='Market']",
        "[data-test]",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = min(loc.count(), 250)
            for i in range(count):
                item = loc.nth(i)
                txt = safe_inner_text(item)
                if not txt:
                    continue
                for label in labels:
                    score = score_market_block(txt, label)
                    if score > best_score:
                        best_score = score
                        best_block = item
                        best_label = label
        except Exception:
            continue

    if best_block is not None:
        log(f"market block selected label={best_label} score={best_score:.3f}")
    return best_label, best_block


def click_all_see_more_in_block(block, max_rounds=6):
    total_clicks = 0
    for round_idx in range(1, max_rounds + 1):
        clicked_this_round = 0
        try:
            buttons = block.locator("button")
            count = buttons.count()
            for i in range(count):
                btn = buttons.nth(i)
                txt = norm_spaces(safe_inner_text(btn)).lower()
                if "voir plus" not in txt:
                    continue
                try:
                    btn.click(timeout=3000)
                    clicked_this_round += 1
                    total_clicks += 1
                    log(f"clicked see more in goals block #{total_clicks}")
                    time.sleep(0.8)
                except Exception:
                    continue
        except Exception:
            pass

        log(f"see more round {round_idx}: clicked={clicked_this_round}")
        if clicked_this_round == 0:
            break
        time.sleep(1.0)

    log(f"total see more clicks in goals block: {total_clicks}")
    return total_clicks


def remaining_see_more_in_block(block):
    try:
        txt = safe_inner_text(block).lower()
        return txt.count("voir plus")
    except Exception:
        return -1


def isolate_lines(block_text: str):
    raw_lines = [norm_spaces(x) for x in block_text.splitlines()]
    return [x for x in raw_lines if x]


def is_decimal_odd(token: str):
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", token))


def parse_goals_rows(lines, teams):
    """
    Bloc BUTEUR (PROLONGATIONS INCLUSES) d'après l'exemple :
    - <Team>
    - 'Buteur'
    - '2 buts' / '2 buts ou plus'
    - '3 buts' / '3 buts ou plus'
    - puis, pour chaque joueur :
      - Nom
      - 1 à 3 cotes successives (buteur, 2 buts+, 3 buts+), avec éventuellement '-'
    """
    rows = []
    debug_players = []

    i = 0
    current_team = None

    header_tokens = {
        "buteur",
        "2 buts",
        "2 buts ou plus",
        "3 buts",
        "3 buts ou plus",
    }

    while i < len(lines):
        line = lines[i]

        # Détection équipe
        if line in teams:
            current_team = line
            i += 1
            # Sauter les lignes d'en-tête colonnes
            while i < len(lines) and lines[i].lower() in header_tokens:
                i += 1
            continue

        if not current_team:
            i += 1
            continue

        player_name = line
        j = i + 1
        odds = []

        # On lit jusqu'à 3 cotes max après le joueur
        while j < len(lines) and len(odds) < 3:
            token = lines[j]
            if token in teams:
                break
            tl = token.lower()
            if tl in header_tokens or "voir plus" in tl or "voir moins" in tl:
                break
            if is_decimal_odd(token) or token == "-":
                odds.append(token)
                j += 1
            else:
                break

        if odds:
            labels = ["1+", "2+", "3+"]
            player_rows = []
            has_dash = False
            odds_values = []

            for idx, odd in enumerate(odds):
                if idx >= len(labels):
                    break
                line_label = labels[idx]
                if odd == "-":
                    has_dash = True
                    continue
                odds_values.append(odd)
                player_rows.append({
                    "team": current_team,
                    "player_name_raw": player_name,
                    "line_label": line_label,
                    "odds_raw": odd,
                })

            if player_rows:
                rows.extend(player_rows)
                debug_players.append({
                    "team": current_team,
                    "player_name_raw": player_name,
                    "odds_count": len(odds_values),
                    "has_dash": has_dash,
                    "odds_values": odds_values,
                })
            i = j
        else:
            i += 1

    return rows, debug_players


def validate_rows(rows):
    if not rows:
        return False, "no_rows"
    for row in rows:
        if not row.get("team"):
            return False, "missing_team"
        if not row.get("player_name_raw"):
            return False, "missing_player"
        if not row.get("line_label"):
            return False, "missing_line_label"
        if not row.get("odds_raw"):
            return False, "missing_odds_raw"
    return True, "ok"


def main():
    event_url = os.getenv("UNIBET_EVENT_URL", "").strip()
    headless = os.getenv("PW_HEADLESS", "true").lower() == "true"

    if not event_url:
        raise ValueError("UNIBET_EVENT_URL is required")

    run_dir = Path("artifacts") / "unibet_event_goals_parser_v1" / now_ts()
    ensure_dir(run_dir)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 2200})
        page = context.new_page()

        cookie_clicked = None
        final_url = None
        title = None
        teams = []
        clicked_tab_label = None
        selected_block_label = None
        see_more_clicks = 0
        remaining_see_more = -1
        isolated = []
        rows = []
        debug_players = []

        try:
            log(f"goto: {event_url}")
            page.goto(event_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2.0)

            cookie_clicked = click_cookie_if_present(page)
            time.sleep(1.0)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass

            final_url = page.url
            title = page.title()
            teams = extract_teams_from_title(title)
            log(f"teams detected: {teams}")

            clicked_tab_label = click_first_matching_label(page, TAB_LABEL_CANDIDATES)
            if not clicked_tab_label:
                raise RuntimeError(f"market_tab_not_found: candidates={TAB_LABEL_CANDIDATES}")

            time.sleep(2.0)

            selected_block_label, block = select_first_matching_market_block(page, BLOCK_LABEL_CANDIDATES)
            if block is None:
                raise RuntimeError(f"market_block_not_found: candidates={BLOCK_LABEL_CANDIDATES}")

            see_more_clicks = click_all_see_more_in_block(block)

            time.sleep(1.5)
            selected_block_label, block = select_first_matching_market_block(page, BLOCK_LABEL_CANDIDATES)
            if block is None:
                raise RuntimeError(f"market_block_not_found_after_expand: candidates={BLOCK_LABEL_CANDIDATES}")

            block_text = safe_inner_text(block)
            remaining_see_more = remaining_see_more_in_block(block)

            isolated = isolate_lines(block_text)
            rows, debug_players = parse_goals_rows(isolated, teams)

            rows_valid, rows_validation_reason = validate_rows(rows)

            write_text(run_dir / "goals_market_only.txt", block_text)
            write_json(run_dir / "goals_market_isolated_lines.json", isolated)
            write_json(run_dir / "goals_market_rows_clean.json", rows)
            write_json(run_dir / "goals_market_players_debug.json", debug_players)

            summary = {
                "title": title,
                "event_url": event_url,
                "final_url": final_url,
                "cookie_clicked": cookie_clicked,
                "teams": teams,
                "clicked_tab_label": clicked_tab_label,
                "tab_label_candidates": TAB_LABEL_CANDIDATES,
                "selected_block_label": selected_block_label,
                "block_label_candidates": BLOCK_LABEL_CANDIDATES,
                "see_more_clicks": see_more_clicks,
                "remaining_see_more_in_market": remaining_see_more,
                "is_complete_market": remaining_see_more == 0,
                "rows_valid": rows_valid,
                "rows_validation_reason": rows_validation_reason,
                "isolated_lines": len(isolated),
                "parsed_rows_clean": len(rows),
                "players_seen": len(debug_players),
                "players_with_3_odds": sum(1 for x in debug_players if x["odds_count"] == 3),
                "players_with_less_than_3_odds": sum(1 for x in debug_players if x["odds_count"] < 3),
                "players_with_dash": sum(1 for x in debug_players if x["has_dash"]),
                "run_dir": str(run_dir),
            }
            write_json(run_dir / "summary.json", summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
