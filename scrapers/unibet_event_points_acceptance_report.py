import os
import json
from pathlib import Path

ALLOWED_MARKET_LABELS = {
    "NOMBRE DE POINTS DU JOUEUR (PROLONGATIONS INCLUSES)",
    "NOMBRE DE POINTS DU JOUEUR",
}


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    batch_summary_path = os.getenv("BATCH_SUMMARY_PATH", "").strip()
    if not batch_summary_path:
        raise ValueError("BATCH_SUMMARY_PATH is required")

    batch_path = Path(batch_summary_path)
    if not batch_path.exists():
        raise FileNotFoundError(f"Batch summary not found: {batch_summary_path}")

    batch = load_json(batch_path)
    results = batch.get("results", [])

    accepted_for_insert = []
    rejected_for_insert = []

    for item in results:
        summary = item.get("summary") or {}
        reasons = []

        if item.get("returncode") != 0:
            reasons.append("returncode_non_zero")

        teams = summary.get("teams") or []
        if len(teams) != 2:
            reasons.append("teams_count_invalid")

        selected_block_label = (summary.get("selected_block_label") or "").strip()
        if selected_block_label not in ALLOWED_MARKET_LABELS:
            reasons.append("wrong_market_label")

        if summary.get("remaining_see_more_in_market") != 0:
            reasons.append("remaining_see_more_non_zero")

        if summary.get("is_complete_market") is not True:
            reasons.append("is_complete_market_false")

        if summary.get("rows_valid") is not True:
            reasons.append("rows_valid_false")

        parsed_rows_clean = summary.get("parsed_rows_clean", 0)
        if parsed_rows_clean <= 0:
            reasons.append("no_rows")

        players_seen = summary.get("players_seen", 0)
        players_kept_points_1_plus = summary.get("players_kept_points_1_plus", 0)

        if players_seen <= 0:
            reasons.append("no_players_seen")

        if players_kept_points_1_plus <= 0:
            reasons.append("no_points_1_plus_rows")

        result_row = {
            "event_url": item.get("event_url"),
            "run_dir": item.get("run_dir"),
            "teams": teams,
            "selected_block_label": selected_block_label,
            "parsed_rows_clean": parsed_rows_clean,
            "players_seen": players_seen,
            "players_kept_points_1_plus": players_kept_points_1_plus,
            "accepted_for_insert": len(reasons) == 0,
            "reasons": reasons,
        }

        if reasons:
            rejected_for_insert.append(result_row)
        else:
            accepted_for_insert.append(result_row)

    final_report = {
        "batch_ts": batch.get("batch_ts"),
        "input_urls_count": batch.get("input_urls_count"),
        "accepted_runs_in_batch": batch.get("accepted_runs"),
        "rejected_runs_in_batch": batch.get("rejected_runs"),
        "accepted_for_insert_count": len(accepted_for_insert),
        "rejected_for_insert_count": len(rejected_for_insert),
        "accepted_for_insert": accepted_for_insert,
        "rejected_for_insert": rejected_for_insert,
    }

    out_path = batch_path.parent / "acceptance_report.json"
    write_json(out_path, final_report)
    print(json.dumps(final_report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
