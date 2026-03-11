import os
import json
from pathlib import Path


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def is_event_accepted(result: dict) -> tuple[bool, str]:
    if not isinstance(result, dict):
        return False, "result_not_dict"

    if result.get("returncode") != 0:
        return False, "returncode_not_zero"

    summary = result.get("summary") or {}
    if not isinstance(summary, dict):
        return False, "missing_summary"

    if summary.get("is_complete_market") is not True:
        return False, "market_not_complete"

    if summary.get("rows_valid") is not True:
        return False, "rows_not_valid"

    if summary.get("remaining_see_more_in_market") != 0:
        return False, "remaining_see_more_not_zero"

    if (summary.get("parsed_rows_clean") or 0) <= 0:
        return False, "no_clean_rows"

    run_dir = result.get("run_dir")
    if not run_dir:
        return False, "missing_run_dir"

    return True, "accepted"


def build_base_record(result: dict) -> dict:
    summary = result.get("summary") or {}
    return {
        "batch_index": result.get("batch_index"),
        "event_url": result.get("event_url"),
        "returncode": result.get("returncode"),
        "run_dir": result.get("run_dir"),
        "teams": summary.get("teams") or [],
        "title": summary.get("title"),
        "parsed_rows_clean": summary.get("parsed_rows_clean", 0),
        "is_complete_market": summary.get("is_complete_market"),
        "rows_valid": summary.get("rows_valid"),
        "rows_validation_reason": summary.get("rows_validation_reason"),
        "remaining_see_more_in_market": summary.get("remaining_see_more_in_market"),
        "players_seen": summary.get("players_seen"),
        "players_with_3_odds": summary.get("players_with_3_odds"),
        "players_with_less_than_3_odds": summary.get("players_with_less_than_3_odds"),
        "players_with_dash": summary.get("players_with_dash"),
    }


def main():
    batch_summary_path = os.getenv("BATCH_SUMMARY_PATH", "").strip()
    if not batch_summary_path:
        raise ValueError("BATCH_SUMMARY_PATH is required")

    batch_path = Path(batch_summary_path)
    if not batch_path.exists():
        raise FileNotFoundError(f"Batch summary not found: {batch_summary_path}")

    batch = load_json(batch_path)
    results = batch.get("results", [])
    if not isinstance(results, list):
        raise ValueError("Invalid batch_summary.json: 'results' must be a list")

    accepted_for_insert = []
    rejected = []

    for result in results:
        base_record = build_base_record(result)
        accepted, reason = is_event_accepted(result)

        if accepted:
            accepted_for_insert.append({
                **base_record,
                "acceptance_reason": reason
            })
        else:
            rejected.append({
                **base_record,
                "rejection_reason": reason,
                "stdout_tail": result.get("stdout_tail", ""),
                "stderr_tail": result.get("stderr_tail", ""),
                "summary": result.get("summary") or {}
            })

    acceptance_report = {
        "batch_ts": batch.get("batch_ts"),
        "input_urls_count": batch.get("input_urls_count", 0),
        "accepted_count": len(accepted_for_insert),
        "rejected_count": len(rejected),
        "accepted_for_insert": accepted_for_insert,
        "rejected": rejected
    }

    out_path = batch_path.parent / "acceptance_report.json"
    write_json(out_path, acceptance_report)

    print(json.dumps({
        "ok": True,
        "accepted_count": len(accepted_for_insert),
        "rejected_count": len(rejected),
        "output_file": str(out_path)
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
