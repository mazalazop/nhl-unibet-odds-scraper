#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qsl, urlparse, urlunparse

DEFAULT_MIN_ROWS_PER_EVENT = 7


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


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def normalize_url(url: str) -> str:
    raw = safe_text(url)
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if not path:
        path = "/"
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query_items = sorted(query_items)
    query = "&".join(f"{k}={v}" for k, v in query_items)
    return urlunparse((scheme, netloc, path, "", query, ""))


def looks_like_event_page(url: str) -> bool:
    parsed = urlparse(normalize_url(url))
    path = (parsed.path or "").lower().rstrip("/")
    if "/event/" in path and path.endswith(".html"):
        return True
    return bool(re.search(r"/paris-hockey-sur-glace/etats-unis/nhl/\d+/[^/]+$", path))


def normalize_key(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.casefold().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def parse_decimal_odd(value: Any) -> float:
    raw = safe_text(value).replace(",", ".")
    return float(raw)


def has_polluted_player_prefix(player_name: Any) -> bool:
    value = normalize_key(player_name)
    return bool(value) and (
        value.startswith("match ")
        or value.startswith("info ")
        or value.startswith("informations ")
    )


def resolve_batch_summary_path() -> Path:
    raw = os.getenv("BATCH_SUMMARY_PATH", "").strip()
    if not raw:
        raise ValueError("BATCH_SUMMARY_PATH est requis")
    path = Path(raw)
    if not path.exists():
        raise FileNotFoundError(f"BATCH_SUMMARY_PATH introuvable: {path}")
    return path


def evaluate_event(event: Dict[str, Any], min_rows: int) -> Tuple[bool, List[str], List[Dict[str, Any]]]:
    reasons: List[str] = []
    summary = event.get("parser_summary") or {}
    rows_path_raw = safe_text(event.get("rows_path"))
    rows: List[Dict[str, Any]] = []

    if int(event.get("parser_exit_code") or 0) != 0:
        reasons.append("parser_exit_nonzero")
    if safe_text(summary.get("fatal_error")):
        reasons.append("fatal_error")

    final_url = normalize_url(summary.get("final_url") or event.get("final_url"))
    expected_url = normalize_url(event.get("event_url"))
    if not final_url:
        reasons.append("missing_final_url")
    elif not looks_like_event_page(final_url):
        reasons.append("final_url_not_event_page")
    elif expected_url and final_url != expected_url:
        reasons.append("final_url_mismatch")

    if not bool(summary.get("market_block_found")):
        reasons.append("market_block_not_found")
    if bool(summary.get("used_body_text_fallback")):
        reasons.append("body_text_fallback_used")
    team_mode = safe_text(summary.get("team_assignment_mode"))
    if team_mode and team_mode != "line_based_with_team":
        reasons.append(f"unsafe_team_assignment_mode:{team_mode}")
    if not bool(summary.get("rows_valid")):
        reasons.append(f"rows_invalid:{safe_text(summary.get('rows_validation_reason')) or 'unknown'}")

    if not rows_path_raw:
        reasons.append("missing_rows_path")
    else:
        rows_path = Path(rows_path_raw)
        if not rows_path.exists():
            reasons.append("rows_path_not_found")
        else:
            payload = load_json(rows_path)
            if not isinstance(payload, list):
                reasons.append("rows_payload_not_list")
            else:
                rows = [row for row in payload if isinstance(row, dict)]

    if len(rows) < min_rows:
        reasons.append(f"rows_below_min:{len(rows)}<{min_rows}")

    seen = set()
    for row in rows:
        if safe_text(row.get("outcome_label")) != "1 ou plus":
            reasons.append("unexpected_outcome_label")
            break

    for row in rows:
        key = (
            normalize_key(row.get("team")),
            normalize_key(row.get("player_name_raw")),
            normalize_key(row.get("outcome_label")),
        )
        if not key[1] or not key[2]:
            reasons.append("missing_row_key_fields")
            break
        if not key[0]:
            reasons.append("missing_team")
            break
        if has_polluted_player_prefix(row.get("player_name_raw")):
            reasons.append("polluted_player_name")
            break
        if key in seen:
            reasons.append("duplicate_player_rows")
            break
        seen.add(key)
        try:
            odd = parse_decimal_odd(row.get("odds_raw"))
        except Exception:
            reasons.append("invalid_odds_decimal")
            break
        if odd <= 1.0:
            reasons.append("odds_not_gt_1")
            break

    accepted = len(reasons) == 0
    return accepted, reasons, rows


def main() -> None:
    batch_summary_path = resolve_batch_summary_path()
    batch_dir = batch_summary_path.parent
    min_rows = env_int("MIN_ROWS_PER_EVENT", DEFAULT_MIN_ROWS_PER_EVENT)

    batch_summary = load_json(batch_summary_path)
    if not isinstance(batch_summary, dict):
        raise ValueError("batch_summary.json doit être un objet JSON")

    event_results = batch_summary.get("event_results")
    if not isinstance(event_results, list):
        raise ValueError("batch_summary.json ne contient pas event_results")

    accepted_rows: List[Dict[str, Any]] = []
    report_events: List[Dict[str, Any]] = []

    for event in event_results:
        if not isinstance(event, dict):
            continue
        accepted, reasons, rows = evaluate_event(event, min_rows=min_rows)
        summary = event.get("parser_summary") or {}
        teams = event.get("teams") or summary.get("teams") or []
        home_team = teams[0] if len(teams) >= 1 else None
        away_team = teams[1] if len(teams) >= 2 else None

        report_event = {
            "event_index": event.get("event_index"),
            "event_url": event.get("event_url"),
            "event_slug": event.get("event_slug"),
            "accepted": accepted,
            "reasons": reasons,
            "rows_count": len(rows),
            "parser_exit_code": event.get("parser_exit_code"),
            "title": event.get("title") or summary.get("title"),
            "final_url": summary.get("final_url") or event.get("final_url"),
            "teams": teams,
            "home_team": home_team,
            "away_team": away_team,
            "parser_run_dir": event.get("parser_run_dir"),
            "parser_summary_path": event.get("parser_summary_path"),
            "rows_path": event.get("rows_path"),
            "summary_checks": {
                "market_block_found": summary.get("market_block_found"),
                "rows_valid": summary.get("rows_valid"),
                "rows_validation_reason": summary.get("rows_validation_reason"),
                "is_complete_market": summary.get("is_complete_market"),
                "fatal_error": summary.get("fatal_error"),
                "players_kept_points_1_plus": summary.get("players_kept_points_1_plus"),
                "team_assignment_mode": summary.get("team_assignment_mode"),
            },
            "sample_rows": rows[:3],
        }
        report_events.append(report_event)

        if accepted:
            for row in rows:
                enriched = dict(row)
                enriched.update({
                    "event_url": event.get("event_url"),
                    "event_slug": event.get("event_slug"),
                    "home_team": home_team,
                    "away_team": away_team,
                    "parser_run_dir": event.get("parser_run_dir"),
                    "parser_summary_path": event.get("parser_summary_path"),
                    "rows_path": event.get("rows_path"),
                })
                accepted_rows.append(enriched)

    reason_counts: Dict[str, int] = {}
    for event in report_events:
        for reason in event.get("reasons") or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    report = {
        "generated_at_utc": utc_now_iso(),
        "batch_run_dir": str(batch_dir),
        "source_batch_summary_path": str(batch_summary_path),
        "thresholds": {
            "min_rows_per_event": min_rows,
            "expected_outcome_label": "1 ou plus",
            "require_complete_market": False,
            "require_event_page_final_url": True,
        },
        "totals": {
            "input_event_count": len(report_events),
            "accepted_event_count": sum(1 for x in report_events if x.get("accepted")),
            "rejected_event_count": sum(1 for x in report_events if not x.get("accepted")),
            "accepted_rows_count": len(accepted_rows),
        },
        "reason_counts": dict(sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "accepted_event_urls": [x.get("event_url") for x in report_events if x.get("accepted")],
        "rejected_event_urls": [x.get("event_url") for x in report_events if not x.get("accepted")],
        "artifacts": {
            "accepted_points_rows_raw_json": str(batch_dir / "accepted_points_rows_raw.json"),
            "acceptance_report_json": str(batch_dir / "acceptance_report.json"),
        },
        "events": report_events,
    }

    write_json(batch_dir / "accepted_points_rows_raw.json", {
        "generated_at_utc": utc_now_iso(),
        "batch_run_dir": str(batch_dir),
        "rows_count": len(accepted_rows),
        "rows": accepted_rows,
    })
    write_json(batch_dir / "acceptance_report.json", report)

    print(json.dumps({
        "batch_run_dir": str(batch_dir),
        "accepted_event_count": report["totals"]["accepted_event_count"],
        "rejected_event_count": report["totals"]["rejected_event_count"],
        "accepted_rows_count": report["totals"]["accepted_rows_count"],
    }, ensure_ascii=False, indent=2))

    if report["totals"]["accepted_event_count"] == 0:
        raise SystemExit("Aucun match accepté. Voir acceptance_report.json")


if __name__ == "__main__":
    main()
