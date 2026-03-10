import os
import re
import json
import traceback
from pathlib import Path
from datetime import datetime

from playwright.sync_api import sync_playwright


START_MARKER_FULL = "NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)"
START_MARKER_SHORT = "NOMBRE DE POINTS DU JOUEUR"

COOKIE_SELECTORS = [
    "button:has-text('Accepter')",
    "button:has-text('Tout accepter')",
    "button:has-text('Tout Accepter')",
    "button:has-text('J’accepte')",
    "button:has-text('Accept')",
]

STOP_MARKETS = [
    "BUTEUR DOUBLE CHANCE",
    "LES 2 JOUEURS MARQUENT DANS LE MATCH",
    "TOTAL DE BUTS MARQUÉS PAR LE DUO",
    "TOTAL DE BUTS MARQUÉS PAR LE TRIO",
    "BUTEUR ET SON ÉQUIPE GAGNE",
    "LE JOUEUR MARQUE 2 BUTS OU PLUS",
    "BUTEUR CHANCE TRIPLE",
    "LES 3 JOUEURS MARQUENT DANS LE MATCH",
    "ECART ENTRE ÉQUIPES",
    "TOTAL DE BUTS",
    "QUI MARQUERA LE 1ER BUT",
    "COMBO RÉSULTAT DU MATCH",
    "BUT POUR LES 2 ÉQUIPES",
    "MARGE DU VAINQUEUR",
    "SCORE EXACT",
    "PÉRIODE AVEC LE PLUS DE BUTS",
    "1ER TIERS-TEMPS",
    "2E TIERS-TEMPS",
    "3E TIERS-TEMPS",
    "PROLONGATIONS OUI/NON",
    "PASSEUR (PROLONGATIONS INCLUSES)",
]

SKIP_LINES = {
    "Voir plus",
    "Voir moins",
    "Créer ma sélection",
    "1",
    "2",
    "3",
    "ou plus",
}

LINE_LABELS = ["1+", "2+", "3+"]


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def log(logs, message):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    logs.append(line)
    print(line)


def wait_settle(page, ms=1800):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def body_text(page):
    try:
        return page.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


def snapshot(page, out_dir: Path, name: str):
    try:
        write_text(out_dir / f"{name}.html", page.content())
    except Exception:
        write_text(out_dir / f"{name}.html", "")
    write_text(out_dir / f"{name}_body.txt", body_text(page))
    try:
        page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
    except Exception:
        pass


def accept_cookies(page, logs):
    for sel in COOKIE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                loc.click(timeout=3000)
                log(logs, f"cookie clicked: {sel}")
                page.wait_for_timeout(1200)
                return sel
        except Exception:
            pass
    log(logs, "cookie not clicked")
    return None


def get_match_teams(page):
    try:
        title = page.title()
        m = re.search(r"Pariez sur (.+?) - (.+?) \|", title)
        if m:
            t1 = m.group(1).strip()
            t2 = m.group(2).strip()
            if t1 and t2:
                return [t1, t2]
    except Exception:
        pass

    teams = []
    try:
        h1s = page.locator("h1")
        count = min(h1s.count(), 10)
        for i in range(count):
            txt = h1s.nth(i).inner_text(timeout=1000).strip()
            if txt and txt not in teams:
                teams.append(txt)
    except Exception:
        pass

    if len(teams) >= 2:
        return teams[:2]

    return []


def inner_text_safe(loc):
    try:
        return re.sub(r"\s+", " ", loc.inner_text(timeout=800)).strip()
    except Exception:
        return ""


def try_click_locator(loc, logs, label_hint):
    try:
        if not loc.is_visible(timeout=800):
            return False, None
        label = inner_text_safe(loc)
        if not label:
            return False, None
        if len(label) > 120:
            return False, None
        if "Tigne Point" in label or label.lower().startswith("unibet n’est pas affilié"):
            return False, None
        loc.scroll_into_view_if_needed(timeout=2000)
        loc.click(timeout=4000)
        log(logs, f"clicked {label_hint}: {label}")
        return True, label
    except Exception:
        return False, None


def click_points_market(page, logs):
    exact_targets = ["Points"]
    strict_targets = [
        START_MARKER_FULL,
        START_MARKER_SHORT,
    ]

    for text in exact_targets:
        candidates = [
            page.get_by_text(text, exact=True),
            page.locator(f"button:has-text('{text}')"),
            page.locator(f"a:has-text('{text}')"),
            page.locator(f"span:has-text('{text}')"),
            page.locator(f"div:has-text('{text}')"),
        ]
        for base in candidates:
            try:
                count = min(base.count(), 10)
            except Exception:
                count = 0
            for i in range(count):
                ok, label = try_click_locator(base.nth(i), logs, "points tab")
                if ok:
                    wait_settle(page, 1200)
                    return label

    for text in strict_targets:
        candidates = [
            page.get_by_text(text, exact=True),
            page.locator(f"button:has-text('{text}')"),
            page.locator(f"a:has-text('{text}')"),
            page.locator(f"span:has-text('{text}')"),
            page.locator(f"div:has-text('{text}')"),
        ]
        for base in candidates:
            try:
                count = min(base.count(), 10)
            except Exception:
                count = 0
            for i in range(count):
                ok, label = try_click_locator(base.nth(i), logs, "points market")
                if ok:
                    wait_settle(page, 1200)
                    return label

    raise RuntimeError("Points market not clickable")


def find_points_block(page, logs):
    patterns = [
        re.compile(r"NOMBRE DE POINTS DU JOUEUR \(PROLONGATIONS INCLUSES\)", re.I),
        re.compile(r"NOMBRE DE POINTS DU JOUEUR", re.I),
    ]
    candidates = []

    for pattern in patterns:
        try:
            loc = page.locator("section, article, div").filter(has_text=pattern)
            count = min(loc.count(), 40)
        except Exception:
            count = 0

        for i in range(count):
            item = loc.nth(i)
            try:
                if not item.is_visible(timeout=500):
                    continue
                txt = item.inner_text(timeout=1000)
                txt_clean = re.sub(r"\s+", " ", txt).strip()
                if len(txt_clean) < 40 or len(txt_clean) > 2500:
                    continue

                score = 0
                if START_MARKER_FULL in txt_clean.upper():
                    score += 5
                if START_MARKER_SHORT in txt_clean.upper():
                    score += 4
                if "VOIR PLUS" in txt_clean.upper() or "VOIR MOINS" in txt_clean.upper():
                    score += 3
                if "1 OU PLUS" in txt_clean.upper():
                    score += 2
                if "2 OU PLUS" in txt_clean.upper():
                    score += 2
                if "3 OU PLUS" in txt_clean.upper():
                    score += 2
                score += min(len(txt_clean), 1200) / 1000

                candidates.append((score, item, txt_clean))
            except Exception:
                continue

    if not candidates:
        log(logs, "points block not found")
        return None, ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0]
    log(logs, f"points block selected score={best[0]}")
    return best[1], best[2]


def click_see_more_in_points_block(block, page, logs):
    if block is not None:
        for sel in [
            "button:has-text('Voir plus')",
            "a:has-text('Voir plus')",
            "text=Voir plus",
        ]:
            try:
                loc = block.locator(sel)
                count = min(loc.count(), 10)
            except Exception:
                count = 0

            for i in range(count):
                item = loc.nth(i)
                try:
                    if not item.is_visible(timeout=500):
                        continue
                    label = inner_text_safe(item)
                    if label != "Voir plus":
                        continue
                    item.scroll_into_view_if_needed(timeout=2000)
                    item.click(timeout=4000)
                    log(logs, f"clicked see more in points block: {label}")
                    wait_settle(page, 1500)
                    return True
                except Exception:
                    continue

    try:
        loc = page.get_by_text("Voir plus", exact=True)
        count = min(loc.count(), 10)
    except Exception:
        count = 0

    for i in range(count):
        item = loc.nth(i)
        try:
            if not item.is_visible(timeout=500):
                continue
            label = inner_text_safe(item)
            item.scroll_into_view_if_needed(timeout=2000)
            item.click(timeout=4000)
            log(logs, f"clicked fallback see more: {label}")
            wait_settle(page, 1500)
            return True
        except Exception:
            continue

    log(logs, "see more not clicked")
    return False


def normalize_lines(text):
    lines = []
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if clean:
            lines.append(clean)
    return lines


def is_upper_market_heading(text):
    if text in SKIP_LINES:
        return False
    if len(text) < 8:
        return False
    for m in STOP_MARKETS:
        if text.upper().startswith(m):
            return True
    return False


def extract_points_market_only(full_body_text, logs):
    lines = normalize_lines(full_body_text)
    start_idx = None

    for i, line in enumerate(lines):
        up = line.upper()
        if START_MARKER_FULL in up or START_MARKER_SHORT in up:
            start_idx = i
            break

    if start_idx is None:
        raise RuntimeError("Points market start marker not found in body")

    selected = [lines[start_idx]]

    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        if i > start_idx + 1 and is_upper_market_heading(line):
            break
        selected.append(line)

    log(logs, f"points market lines isolated: {len(selected)}")
    return selected


def split_by_teams(lines, teams):
    sections = {team: [] for team in teams}
    current_team = None

    for line in lines:
        if line in teams:
            current_team = line
            continue
        if current_team:
            sections[current_team].append(line)

    return sections


def is_decimal_odds(text):
    return bool(re.fullmatch(r"\d{1,3}(?:[.,]\d{1,2})?", text))


def clean_team_lines(lines):
    out = []
    for line in lines:
        if line in SKIP_LINES:
            continue
        out.append(line)
    return out


def parse_team_rows(team, lines):
    tokens = clean_team_lines(lines)
    rows = []
    i = 0

    while i < len(tokens):
        token = tokens[i]

        if token in SKIP_LINES:
            i += 1
            continue

        if is_upper_market_heading(token):
            break

        if is_decimal_odds(token):
            i += 1
            continue

        player = token
        odds = []
        j = i + 1

        while j < len(tokens):
            nxt = tokens[j]
            if is_decimal_odds(nxt):
                odds.append(nxt.replace(",", "."))
                j += 1
                if len(odds) == 3:
                    break
            else:
                break

        if odds:
            for idx, odd in enumerate(odds):
                if idx >= len(LINE_LABELS):
                    break
                rows.append({
                    "team": team,
                    "player_name_raw": player,
                    "line_label": LINE_LABELS[idx],
                    "odds_raw": odd,
                })
            i = j
        else:
            i += 1

    dedup = []
    seen = set()
    for row in rows:
        key = (row["team"], row["player_name_raw"], row["line_label"], row["odds_raw"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)

    return dedup


def main():
    event_url = os.getenv("UNIBET_EVENT_URL", "").strip()
    if not event_url:
        raise ValueError("UNIBET_EVENT_URL is required")

    headless = os.getenv("PW_HEADLESS", "true").lower() == "true"

    ts = now_ts()
    out_dir = Path("artifacts") / "unibet_event_points_parser_v2" / ts
    ensure_dir(out_dir)

    logs = []

    with sync_playwright() as p:
        browser = None
        context = None
        page = None

        try:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                viewport={"width": 1600, "height": 2200},
                locale="fr-FR",
                timezone_id="Europe/Paris",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.set_default_timeout(45000)

            log(logs, f"goto: {event_url}")
            page.goto(event_url, wait_until="domcontentloaded", timeout=45000)
            wait_settle(page, 2500)

            cookie_clicked = accept_cookies(page, logs)
            wait_settle(page, 1200)

            snapshot(page, out_dir, "initial")

            teams = get_match_teams(page)
            log(logs, f"teams detected: {teams}")

            if len(teams) != 2:
                raise RuntimeError(f"Could not detect exactly 2 teams, got: {teams}")

            clicked_market_label = click_points_market(page, logs)
            snapshot(page, out_dir, "after_points_click")

            points_block_before, points_block_before_txt = find_points_block(page, logs)
            write_text(out_dir / "points_block_before.txt", points_block_before_txt)

            see_more_clicked = click_see_more_in_points_block(points_block_before, page, logs)
            snapshot(page, out_dir, "after_see_more")

            full_body = body_text(page)
            write_text(out_dir / "full_body_after_see_more.txt", full_body)

            isolated_lines = extract_points_market_only(full_body, logs)
            write_text(out_dir / "points_market_only.txt", "\n".join(isolated_lines))
            write_json(out_dir / "points_market_only_lines.json", isolated_lines)

            sections = split_by_teams(isolated_lines, teams)
            write_json(out_dir / "points_market_sections_by_team.json", sections)

            parsed_rows = []
            for team in teams:
                parsed_rows.extend(parse_team_rows(team, sections.get(team, [])))

            write_json(out_dir / "points_market_rows_clean.json", parsed_rows)

            summary = {
                "title": page.title(),
                "event_url": event_url,
                "final_url": page.url,
                "cookie_clicked": cookie_clicked,
                "teams": teams,
                "clicked_market_label": clicked_market_label,
                "see_more_clicked": see_more_clicked,
                "isolated_lines": len(isolated_lines),
                "parsed_rows_clean": len(parsed_rows),
                "run_dir": str(out_dir),
            }

            write_json(out_dir / "summary.json", summary)
            write_text(out_dir / "events.log", "\n".join(logs))

        except Exception as e:
            write_text(out_dir / "fatal_traceback.txt", traceback.format_exc())
            write_text(out_dir / "events.log", "\n".join(logs + [f"fatal: {type(e).__name__}: {e}"]))
            if page is not None:
                try:
                    snapshot(page, out_dir, "fatal_state")
                except Exception:
                    pass
            raise
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
