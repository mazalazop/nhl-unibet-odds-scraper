import os
import json
from pathlib import Path
from datetime import datetime


BOOKMAKER = "unibet_fr"
MARKET_KEY = "player_points_including_ot"
MARKET_LABEL = "NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)"


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_rows(run_dir: Path):
    path = run_dir / "points_market_rows_clean.json"
    if not path.exists():
        return []
    return load_json(path)


def main():
    acceptance_report_path = os.getenv("ACCEPTANCE_REPORT_PATH", "").strip()
    if not acceptance_report_path:
        raise ValueError("ACCEPTANCE_REPORT_PATH is required")

    acceptance_path = Path(acceptance_report_path)
    if not acceptance_path.exists():
        raise FileNotFoundError(f"Acceptance report not found: {acceptance_report_path}")

    acceptance = load_json(acceptance_path)
    accepted = acceptance.get("accepted_for_insert", [])

    out_ts = now_ts()
    out_dir = acceptance_path.parent / f"normalized_{out_ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    normalized_rows = []
    rejected_rows = []

    for item in accepted:
        event_url = item.get("event_url")
        run_dir_raw = item.get("run_dir")
        teams = item.get("teams") or []

        if not run_dir_raw:
            rejected_rows.append({
                "event_url": event_url,
                "reason": "missing_run_dir"
            })
            continue

        run_dir = Path(run_dir_raw)
        summary_path = run_dir / "summary.json"

        if not summary_path.exists():
            rejected_rows.append({
                "event_url": event_url,
                "reason": "missing_summary_json"
            })
            continue

        summary = load_json(summary_path)
        rows = load_rows(run_dir)

        if not rows:
            rejected_rows.append({
                "event_url": event_url,
                "reason": "missing_points_rows"
            })
            continue

        home_team = teams[0] if len(teams) >= 1 else None
        away_team = teams[1] if len(teams) >= 2 else None

        for row in rows:
            normalized_rows.append({
                "scrape_batch_ts": acceptance.get("batch_ts"),
                "scrape_normalized_ts": out_ts,
                "bookmaker": BOOKMAKER,
                "market_key": MARKET_KEY,
                "market_label": MARKET_LABEL,
                "event_url": event_url,
                "event_title": summary.get("title"),
                "home_team": home_team,
                "away_team": away_team,
                "team": row.get("team"),
                "player_name_raw": row.get("player_name_raw"),
                "line_label": row.get("line_label"),
                "odds_raw": row.get("odds_raw"),
                "odds_decimal": float(str(row.get("odds_raw")).replace(",", ".")),
                "source_run_dir": run_dir_raw
            })

    final_payload = {
        "batch_ts": acceptance.get("batch_ts"),
        "normalized_ts": out_ts,
        "bookmaker": BOOKMAKER,
        "market_key": MARKET_KEY,
        "market_label": MARKET_LABEL,
        "accepted_events_count": len(accepted),
        "normalized_rows_count": len(normalized_rows),
        "rejected_rows_count": len(rejected_rows),
        "normalized_rows": normalized_rows,
        "rejected_rows": rejected_rows
    }

    write_json(out_dir / "normalized_points_odds.json", final_payload)
    print(json.dumps({
        "ok": True,
        "accepted_events_count": len(accepted),
        "normalized_rows_count": len(normalized_rows),
        "rejected_rows_count": len(rejected_rows),
        "output_dir": str(out_dir)
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
