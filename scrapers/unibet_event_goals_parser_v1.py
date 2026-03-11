import os
import re
import json
import time
import unicodedata
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


TAB_LABEL_CANDIDATES = [
    "Buteurs",
    "Buteur",
    "Buts",
    "Joueurs",
]

BLOCK_LABEL_CANDIDATES = [
    "BUTEUR (PROLONGATIONS INCLUSES)",
    "BUTEUR",
]


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg):
    print(f"[{now_ts()}] {msg}")


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def norm_spaces(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def strip_accents(text):
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii")


def normalize_for_match(text):
    text = strip_accents(str(text or ""))
    text = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def safe_inner_text(locator):
    try:
        return locator.inner_text(timeout=1000)
    except Exception:
        return ""


def safe_count(locator, max_count=None):
    try:
        c = locator.count()
        if max_count is not None:
            return min(c, max_count)
        return c
    except Exception:
        return 0


def dedupe_keep_order(items):
    out = []
    seen = set()
    for item in items:
        key = normalize_for_match(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(norm_spaces(item))
    return out


def extract_teams_from_title(title):
    patterns = [
        r"Pariez sur (.*?) - (.*?) \| Hockey sur Glace \| Unibet\.fr",
        r"(.*?) - (.*?) \| Hockey sur Glace \| Unibet\.fr",
        r"(.*?) v (.*?) \| Hockey sur Glace \| Unibet\.fr",
    ]

    title = title or ""
    for pattern in patterns:
        m = re.search(pattern, title, re.I)
        if m:
            return dedupe_keep_order([
                norm_spaces(m.group(1)),
                norm_spaces(m.group(2)),
            ])
    return []


def extract_teams_from_url(event_url):
    if not event_url:
        return []

    m = re.search(r"/event/([^/]+?)-\d+_\d+\.html", event_url)
    if not m:
        m = re.search(r"/event/([^/]+)\.html", event_url)
    if not m:
        return []

    slug = m.group(1)
    parts = slug.split("-")
    if len(parts) < 4:
        return []

    half = len(parts) // 2
    team1 = " ".join(parts[:half]).replace("_", " ")
    team2 = " ".join(parts[half:]).replace("_", " ")
    teams = dedupe_keep_order([team1, team2])

    if len(teams) == 2:
        return teams
    return []


def looks_like_matchup(text):
    txt = norm_spaces(text)
    if len(txt) < 7:
        return False
    if re.search(r"\b\d+(?:[.,]\d+)?\b", txt):
        return False
    return bool(re.search(r"\s[-–—]\s", txt))


def split_matchup_text(text):
    txt = norm_spaces(text)
    parts = re.split(r"\s[-–—]\s", txt)
    parts = [norm_spaces(p) for p in parts if norm_spaces(p)]
    if len(parts) == 2:
        return dedupe_keep_order(parts)
    return []


def extract_teams_from_h1(page):
    selectors = [
        "h1",
        "[data-test='event-header']",
        "[class*='event'] h1",
        "[class*='Event'] h1",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = safe_count(loc, 10)
            for i in range(count):
                txt = safe_inner_text(loc.nth(i))
                if looks_like_matchup(txt):
                    teams = split_matchup_text(txt)
                    if len(teams) == 2:
                        return teams
        except Exception:
            continue
    return []


def resolve_teams(page, event_url, title):
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


def click_label(page, label):
    candidates = [
        page.locator(f"text={label}"),
        page.get_by_text(label, exact=False),
        page.locator(f"button:has-text('{label}')"),
        page.locator(f"[role='tab']:has-text('{label}')"),
    ]
    for loc in candidates:
        try:
            if safe_count(loc) > 0:
                target = loc.first
                if target.is_visible(timeout=2500):
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


def score_market_block(text, label):
    score = 0.0
    txt = norm_spaces(text).lower()

    if label.lower() in txt:
        score += 10.0

    score += txt.count("voir plus") * 2.0
    score += txt.count("buteur") * 0.8
    score += txt.count("2 buts ou plus") * 1.2
    score += len(re.findall(r"\b\d+(?:[.,]\d+)?\b", txt)) * 0.03

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
            count = safe_count(loc, 300)
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

    if best_block is not None and best_score >= 10.0:
        log(f"market block selected label={best_label} score={best_score:.3f}")
        return best_label, best_block

    return None, None


def click_all_see_more_in_block(block, max_rounds=8):
    total_clicks = 0

    for round_idx in range(1, max_rounds + 1):
        clicked_this_round = 0

        try:
            buttons = block.locator("button")
            count = safe_count(buttons, 100)

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


def isolate_lines(block_text):
    raw_lines = [norm_spaces(x) for x in block_text.splitlines()]
    return [x for x in raw_lines if x]


def is_decimal_odd(token):
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", token))


def is_valid_player_name(player_name, teams):
    name = norm_spaces(player_name)
    if not name:
        return False

    name_key = normalize_for_match(name)

    if name_key == "match nul":
        return False

    if " ou " in name_key:
        return False

    if "/" in name_key:
        return False

    if len(name.split()) < 2:
        return False

    team_keys = {normalize_for_match(t) for t in teams if t}
    if name_key in team_keys:
        return False

    for team_key in team_keys:
        if team_key and (team_key in name_key or name_key in team_key):
            return False

    if re.search(r"\b(match nul|prolongation|victoire|resultat|double chance|points)\b", name_key):
        return False

    return True


def parse_goals_rows(lines, teams):
    rows = []
    debug_players = []

    i = 0
    current_team = None

    header_tokens = {
        "buteur",
        "2 buts ou plus",
    }

    team_match_keys = set([normalize_for_match(t) for t in teams if t])

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
        odds = []

        while j < len(lines) and len(odds) < 2:
            token = lines[j]
            token_key = normalize_for_match(token)

            if token_key in team_match_keys:
                break

            if token_key in header_tokens or "voir plus" in token_key or "voir moins" in token_key:
                break

            if is_decimal_odd(token) or token == "-":
                odds.append(token)
                j += 1
            else:
                break

        if odds:
            player_rows = []
            has_dash = False
            odds_values = []

            first_odd = odds[0]

            if first_odd == "-":
                has_dash = True
            else:
                odds_values.append(first_odd)
                player_rows.append({
                    "team": current_team,
                    "player_name_raw": player_name,
                    "outcome_label": "Buteur",
                    "odds_raw": first_odd,
                })

            for extra_odd in odds[1:]:
                if extra_odd == "-":
                    has_dash = True

            if player_rows:
                rows.extend(player_rows)
                debug_players.append({
                    "team": current_team,
                    "player_name_raw": player_name,
                    "odds_count_seen": len(odds),
                    "kept_outcome_label": "Buteur",
                    "has_dash": has_dash,
                    "kept_odds_values": odds_values,
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
        if row.get("outcome_label") != "Buteur":
            return False, "invalid_outcome_label"
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

    summary = {
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
        "see_more_clicks": 0,
        "remaining_see_more_in_market": -1,
        "is_complete_market": False,
        "rows_valid": False,
        "rows_validation_reason": "not_run",
        "isolated_lines": 0,
        "parsed_rows_clean": 0,
        "players_seen": 0,
        "players_kept_buteur": 0,
        "run_dir": str(run_dir),
        "fatal_error": None,
    }

    isolated = []
    rows = []
    debug_players = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 2200})
        page = context.new_page()

        try:
            log(f"goto: {event_url}")
            page.goto(event_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2.0)

            summary["cookie_clicked"] = click_cookie_if_present(page)
            time.sleep(1.0)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass

            summary["final_url"] = page.url
            summary["title"] = page.title()

            teams, teams_source = resolve_teams(page, event_url, summary["title"])
            summary["teams"] = teams
            summary["teams_source"] = teams_source
            log(f"teams detected: {teams} source={teams_source}")

            selected_block_label, block = select_first_matching_market_block(page, BLOCK_LABEL_CANDIDATES)

            if block is None:
                summary["clicked_tab_label"] = click_first_matching_label(page, TAB_LABEL_CANDIDATES)
                if summary["clicked_tab_label"]:
                    time.sleep(2.0)
                    selected_block_label, block = select_first_matching_market_block(page, BLOCK_LABEL_CANDIDATES)

            summary["selected_block_label"] = selected_block_label

            if block is None:
                raise RuntimeError(
                    "market_block_not_found: tab_candidates={0} block_candidates={1}".format(
                        TAB_LABEL_CANDIDATES, BLOCK_LABEL_CANDIDATES
                    )
                )

            summary["see_more_clicks"] = click_all_see_more_in_block(block)
            time.sleep(1.5)

            selected_block_label, block = select_first_matching_market_block(page, BLOCK_LABEL_CANDIDATES)
            summary["selected_block_label"] = selected_block_label

            if block is None:
                raise RuntimeError(
                    "market_block_not_found_after_expand: block_candidates={0}".format(
                        BLOCK_LABEL_CANDIDATES
                    )
                )

            block_text = safe_inner_text(block)
            summary["remaining_see_more_in_market"] = remaining_see_more_in_block(block)
            isolated = isolate_lines(block_text)
            rows, debug_players = parse_goals_rows(isolated, teams)

            rows_valid, rows_validation_reason = validate_rows(rows)
            summary["rows_valid"] = rows_valid
            summary["rows_validation_reason"] = rows_validation_reason
            summary["isolated_lines"] = len(isolated)
            summary["parsed_rows_clean"] = len(rows)
            summary["players_seen"] = len(debug_players)
            summary["players_kept_buteur"] = len(rows)
            summary["is_complete_market"] = summary["remaining_see_more_in_market"] == 0

            write_text(run_dir / "goals_market_only.txt", block_text)
            write_json(run_dir / "goals_market_isolated_lines.json", isolated)
            write_json(run_dir / "goals_market_rows_clean.json", rows)
            write_json(run_dir / "goals_market_players_debug.json", debug_players)
            write_json(run_dir / "summary.json", summary)

            print(json.dumps(summary, ensure_ascii=False, indent=2))

        except Exception as e:
            summary["fatal_error"] = str(e)
            summary["isolated_lines"] = len(isolated)
            summary["parsed_rows_clean"] = len(rows)
            summary["players_seen"] = len(debug_players)
            summary["players_kept_buteur"] = len(rows)
            summary["is_complete_market"] = summary["remaining_see_more_in_market"] == 0

            write_json(run_dir / "goals_market_isolated_lines.json", isolated)
            write_json(run_dir / "goals_market_rows_clean.json", rows)
            write_json(run_dir / "goals_market_players_debug.json", debug_players)
            write_json(run_dir / "summary.json", summary)

            print(json.dumps(summary, ensure_ascii=False, indent=2))
            raise

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
