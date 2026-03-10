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
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


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
        "button:has-text('J'accepte')",
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
    best_score = -1

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
    total_clicks =
