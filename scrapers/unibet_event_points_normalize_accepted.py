#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

BOOKMAKER_NAME = "Unibet"
MARKET_NAME = "player_points"
STAT_NAME = "points"
OUTCOME_LABEL = "1 ou plus"
OUTCOME_KEY = "1_plus"
THRESHOLD_VALUE = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.casefold().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def parse_decimal_odd(value: Any) -> float:
    raw = safe_text(value).replace(",", ".")
    return float(raw)


def extract_event_id(event_url: str) -> Optional[str]:
    path = urlparse(event_url).path or ""
    m = re.search(r"/paris-hockey-sur-glace/etats-unis/nhl/(\d+)/[^/]+/?$", path, re.I)
    if m:
        return m.group(1)
    m = re.search(r"-(\d+_\d+)\.html$", path, re.I)
    if m:
        return m.group(1)
    return None


def extract_event_slug(event_url: str) -> str:
    path = urlparse(event_url).path.rstrip("/")
    slug = path.split("/")[-1]
    return re.sub(r"\.html$", "", slug, flags=re.I)


def resolve_acceptance_report_path() -> Path:
    raw = os.getenv("ACCEPTANCE_REPORT_PATH", "").strip()
    if not raw:
        raise ValueError("ACCEPTANCE_REPORT_PATH est requis")
    path = Path(raw)
    if not path.exists():
        raise FileNotFoundError(f"ACCEPTANCE_REPORT_PATH introuvable: {path}")
    return path


def build_normalized_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows_out: List[Dict[str, Any]] = []
    events = report.get("events") or []

    for event in events:
        if not isinstance(event, dict) or not event.get("accepted"):
            continue

        rows_path_raw = safe_text(event.get("rows_path"))
        if not rows_path_raw:
            continue
        rows_path = Path(rows_path_raw)
        if not rows_path.exists():
            continue

        payload = load_json(rows_path)
        if not isinstance(payload, list):
            continue

        event_url = safe_text(event.get("event_url"))
        event_slug = safe_text(event.get("event_slug")) or extract_event_slug(event_url)
        event_id = extract_event_id(event_url)
        home_team = safe_text(event.get("home_team"))
        away_team = safe_text(event.get("away_team"))

        for row in payload:
            if not isinstance(row, dict):
                continue
            odds_decimal = parse_decimal_odd(row.get("odds_raw"))
            player_name = safe_text(row.get("player_name_raw"))
            team = safe_text(row.get("team"))

            normalized_row = {
                "bookmaker": BOOKMAKER_NAME,
                "market": MARKET_NAME,
                "stat": STAT_NAME,
                "threshold": THRESHOLD_VALUE,
                "outcome_label": OUTCOME_LABEL,
                "outcome_key": OUTCOME_KEY,
                "event_url": event_url,
                "event_id": event_id,
                "event_slug": event_slug,
                "home_team": home_team,
                "away_team": away_team,
                "team": team,
                "team_normalized": normalize_text(team),
                "player_name": player_name,
                "player_name_normalized": normalize_text(player_name),
                "odds_raw": safe_text(row.get("odds_raw")),
                "odds_decimal": odds_decimal,
                "implied_probability": 1.0 / odds_decimal,
                "source_rows_path": str(rows_path),
                "source_parser_run_dir": event.get("parser_run_dir"),
                "source_parser_summary_path": event.get("parser_summary_path"),
            }
            rows_out.append(normalized_row)

    rows_out.sort(key=lambda x: (
        safe_text(x.get("event_slug")),
        safe_text(x.get("team_normalized")),
        safe_text(x.get("player_name_normalized")),
    ))
    return rows_out


def main() -> None:
    acceptance_report_path = resolve_acceptance_report_path()
    batch_dir = acceptance_report_path.parent
    report = load_json(acceptance_report_path)
    if not isinstance(report, dict):
        raise ValueError("acceptance_report.json doit être un objet JSON")

    normalized_rows = build_normalized_rows(report)
    payload = {
        "generated_at_utc": utc_now_iso(),
        "batch_run_dir": str(batch_dir),
        "source_acceptance_report_path": str(acceptance_report_path),
        "bookmaker": BOOKMAKER_NAME,
        "market": MARKET_NAME,
        "stat": STAT_NAME,
        "threshold": THRESHOLD_VALUE,
        "outcome_label": OUTCOME_LABEL,
        "rows_count": len(normalized_rows),
        "accepted_event_count": int((report.get("totals") or {}).get("accepted_event_count") or 0),
        "rows": normalized_rows,
    }
    write_json(batch_dir / "normalized_points_odds.json", payload)

    print(json.dumps({
        "batch_run_dir": str(batch_dir),
        "accepted_event_count": payload["accepted_event_count"],
        "rows_count": payload["rows_count"],
        "output": str(batch_dir / "normalized_points_odds.json"),
    }, ensure_ascii=False, indent=2))

    if payload["rows_count"] == 0:
        raise SystemExit("normalized_points_odds.json vide")


if __name__ == "__main__":
    main()
