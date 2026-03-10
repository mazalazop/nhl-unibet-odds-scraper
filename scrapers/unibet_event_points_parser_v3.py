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
    "Simple",
    "Combiné",
    "Système",
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
