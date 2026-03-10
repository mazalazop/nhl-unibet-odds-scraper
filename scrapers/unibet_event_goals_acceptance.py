import os
import json
from pathlib import Path


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def is_result_accepted(result: dict) -> bool:
    summary = result.get("summary") or {}

    return (
        result.get("returncode") == 0
        and summary.get("is_complete_market") is True
        and summary.get("rows_valid") is True
        and summary.get("remaining_see_more_in_market") == 0
        and summary.get("parsed_rows_clean", 0) > 0
    )


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
    rejected = []

    for result in results:
        summary = result.get("summary") or {}

        record = {
            "batch_index": result.get("batch_index"),
            "event_url": result.get("event_url"),
            "returncode": result.get("returncode"),
            "run_dir": result.get("run_dir"),
            "teams": summary.get("teams") or [],
            "title": summary.get("title"),
            "parsed_rows_clean": summary.get("parsed_rows_clean", 0),
            "is_complete_market": summary.get("is_complete_market"),
            "rows_valid": summary.get("rows_valid"),
            "remaining_see_more_in_market": summary.get("remaining_see_more_in_market"),
        }

        if is_result_accepted(result):
            accepted_for_insert.append(record)
        else:
            rejected.append({
                **record,
                "stdout_tail": result.get("stdout_tail", ""),
                "stderr_tail": result.get("stderr_tail", ""),
                "summary": summary,
            })

    acceptance_report = {
        "batch_ts": batch.get("batch_ts"),
        "input_urls_count": batch.get("input_urls_count", 0),
        "accepted_count": len(accepted_for_insert),
        "rejected_count": len(rejected),
        "accepted_for_insert": accepted_for_insert,
        "rejected": rejected,
    }

    out_path = batch_path.parent / "acceptance_report.json"
    write_json(out_path, acceptance_report)

    print(json.dumps({
        "ok": True,
        "accepted_count": len(accepted_for_insert),
        "rejected_count": len(rejected),
        "output_file": str(out_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
