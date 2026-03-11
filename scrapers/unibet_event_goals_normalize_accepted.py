import os
import re
import json
import unicodedata
from pathlib import Path
from datetime import datetime


BOOKMAKER = "unibet_fr"
MARKET_KEY = "player_to_score_including_ot"
MARKET_LABEL = "BUTEUR (PROLONGATIONS INCLUSES)"
ROWS_FILENAME = "goals_market_rows_clean.json"
OUTPUT_FILENAME = "normalized_goals_odds.json"


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_rows(run_dir):
    path = run_dir / ROWS_FILENAME
    if not path.exists():
        return []
    return load_json(path)


def slugify(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def normalize_name(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_record_key(bookmaker, market_key, home_team_norm, away_team_norm, player_name_norm):
    return "|".join([
        bookmaker or "",
        market_key or "",
        home_team_norm or "",
        away_team_norm or "",
        player_name_norm or "",
    ])


def build_dedupe_key(event_slug, team_norm, player_name_norm, outcome_label):
    return "|".join([
        event_slug or "",
        team_norm or "",
        player_name_norm or "",
        outcome_label or "",
    ])


def main():
    acceptance_report_path = os.getenv("ACCEPTANCE_REPORT_PATH", "").strip()
    if not acceptance_report_path:
        raise ValueError("ACCEPTANCE_REPORT_PATH is required")

    acceptance_path = Path(acceptance_report_path)
    if not acceptance_path.exists():
        raise FileNotFoundError("Acceptance report not found: {0}".format(acceptance_report_path))

    acceptance = load_json(acceptance_path)
    accepted = acceptance.get("accepted_for_insert", [])

    out_ts = now_ts()
    out_dir = acceptance_path.parent / ("normalized_{0}".format(out_ts))
    out_dir.mkdir(parents=True, exist_ok=True)

    normalized_rows = []
    rejected_rows = []
    duplicate_rows = []

    seen_keys = {}

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
                "run_dir": run_dir_raw,
                "reason": "missing_summary_json"
            })
            continue

        try:
            summary = load_json(summary_path)
            rows = load_rows(run_dir)
        except Exception as e:
            rejected_rows.append({
                "event_url": event_url,
                "run_dir": run_dir_raw,
                "reason": "json_read_error",
                "error": str(e)
            })
            continue

        if not rows:
            rejected_rows.append({
                "event_url": event_url,
                "run_dir": run_dir_raw,
                "reason": "missing_market_rows"
            })
            continue

        home_team = teams[0] if len(teams) >= 1 else None
        away_team = teams[1] if len(teams) >= 2 else None
        home_team_norm = normalize_name(home_team)
        away_team_norm = normalize_name(away_team)
        event_slug = slugify(summary.get("title") or event_url)

        for row in rows:
            try:
                team = row.get("team")
                player_name_raw = row.get("player_name_raw")
                outcome_label = row.get("outcome_label")
                odds_raw = row.get("odds_raw")

                if player_name_raw is None or str(player_name_raw).strip() == "":
                    raise ValueError("player_name_raw_missing")

                if outcome_label != "Buteur":
                    raise ValueError("invalid_outcome_label")

                if odds_raw is None or str(odds_raw).strip() == "":
                    raise ValueError("odds_raw_missing")

                odds_decimal = float(str(odds_raw).replace(",", "."))
                player_name_norm = normalize_name(player_name_raw)
                team_norm = normalize_name(team)

                record_key = build_record_key(
                    BOOKMAKER,
                    MARKET_KEY,
                    home_team_norm,
                    away_team_norm,
                    player_name_norm
                )

                dedupe_key = build_dedupe_key(
                    event_slug,
                    team_norm,
                    player_name_norm,
                    outcome_label
                )

                normalized_row = {
                    "record_key": record_key,
                    "scrape_batch_ts": acceptance.get("batch_ts"),
                    "scrape_normalized_ts": out_ts,
                    "bookmaker": BOOKMAKER,
                    "market_key": MARKET_KEY,
                    "market_label": MARKET_LABEL,
                    "event_url": event_url,
                    "event_slug": event_slug,
                    "event_title": summary.get("title"),
                    "home_team": home_team,
                    "home_team_norm": home_team_norm,
                    "away_team": away_team,
                    "away_team_norm": away_team_norm,
                    "team": team,
                    "team_norm": team_norm,
                    "player_name_raw": player_name_raw,
                    "player_name_norm": player_name_norm,
                    "outcome_label": outcome_label,
                    "odds_raw": str(odds_raw),
                    "odds_decimal": odds_decimal,
                    "source_run_dir": run_dir_raw
                }

                if dedupe_key in seen_keys:
                    duplicate_rows.append({
                        "dedupe_key": dedupe_key,
                        "kept_record_key": seen_keys[dedupe_key]["record_key"],
                        "dropped_record_key": normalized_row["record_key"],
                        "kept_odds_decimal": seen_keys[dedupe_key]["odds_decimal"],
                        "dropped_odds_decimal": normalized_row["odds_decimal"],
                        "row": normalized_row
                    })
                    continue

                seen_keys[dedupe_key] = normalized_row
                normalized_rows.append(normalized_row)

            except Exception as e:
                rejected_rows.append({
                    "event_url": event_url,
                    "run_dir": run_dir_raw,
                    "reason": "row_normalization_error",
                    "row": row,
                    "error": str(e)
                })

    final_payload = {
        "batch_ts": acceptance.get("batch_ts"),
        "normalized_ts": out_ts,
        "bookmaker": BOOKMAKER,
        "market_key": MARKET_KEY,
        "market_label": MARKET_LABEL,
        "accepted_events_count": len(accepted),
        "normalized_rows_count": len(normalized_rows),
        "duplicate_rows_count": len(duplicate_rows),
        "rejected_rows_count": len(rejected_rows),
        "normalized_rows": normalized_rows,
        "duplicate_rows": duplicate_rows,
        "rejected_rows": rejected_rows
    }

    write_json(out_dir / OUTPUT_FILENAME, final_payload)

    print(json.dumps({
        "ok": True,
        "accepted_events_count": len(accepted),
        "normalized_rows_count": len(normalized_rows),
        "duplicate_rows_count": len(duplicate_rows),
        "rejected_rows_count": len(rejected_rows),
        "output_dir": str(out_dir),
        "output_file": str(out_dir / OUTPUT_FILENAME)
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
