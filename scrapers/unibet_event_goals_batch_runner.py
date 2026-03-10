import os
import re
import json
import subprocess
from pathlib import Path
from datetime import datetime


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_urls(raw_text: str):
    if not raw_text:
        return []
    parts = re.split(r"[\n,;]+", raw_text)
    urls = []
    for part in parts:
        url = part.strip()
        if not url:
            continue
        if not url.startswith("http"):
            continue
        urls.append(url)
    dedup = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        dedup.append(url)
    return dedup


def find_new_run_dirs(base_dir: Path, before_names: set):
    if not base_dir.exists():
        return []
    current = [p for p in base_dir.iterdir() if p.is_dir()]
    new_dirs = [p for p in current if p.name not in before_names]
    new_dirs.sort(key=lambda p: p.name)
    return new_dirs


def read_summary(run_dir: Path):
    path = run_dir / "summary.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main():
    raw_urls = os.getenv("UNIBET_EVENT_URLS", "").strip()
    headless = os.getenv("PW_HEADLESS", "true").lower() == "true"

    urls = parse_urls(raw_urls)
    if not urls:
        raise ValueError("UNIBET_EVENT_URLS is required and must contain at least one valid URL")

    batch_ts = now_ts()
    batch_dir = Path("artifacts") / "unibet_event_goals_batch_runner" / batch_ts
    ensure_dir(batch_dir)

    parser_output_root = Path("artifacts") / "unibet_event_goals_parser_v1"
    ensure_dir(parser_output_root)

    batch_results = []

    for idx, url in enumerate(urls, start=1):
        before_names = set()
        if parser_output_root.exists():
            before_names = {p.name for p in parser_output_root.iterdir() if p.is_dir()}

        env = os.environ.copy()
        env["UNIBET_EVENT_URL"] = url
        env["PW_HEADLESS"] = "true" if headless else "false"

        proc = subprocess.run(
            ["python3", "scrapers/unibet_event_goals_parser_v1.py"],
            env=env,
            capture_output=True,
            text=True,
        )

        new_dirs = find_new_run_dirs(parser_output_root, before_names)
        run_dir = str(new_dirs[-1]) if new_dirs else None
        summary = read_summary(Path(run_dir)) if run_dir else None

        result = {
            "batch_index": idx,
            "event_url": url,
            "returncode": proc.returncode,
            "run_dir": run_dir,
            "summary": summary,
            "stdout_tail": proc.stdout[-3000:] if proc.stdout else "",
            "stderr_tail": proc.stderr[-3000:] if proc.stderr else "",
        }
        batch_results.append(result)

    accepted_runs = 0
    rejected_runs = 0
    for item in batch_results:
        summary = item.get("summary") or {}
        if (
            item.get("returncode") == 0
            and summary.get("is_complete_market") is True
            and summary.get("rows_valid") is True
            and summary.get("remaining_see_more_in_market") == 0
            and summary.get("parsed_rows_clean", 0) > 0
        ):
            accepted_runs += 1
        else:
            rejected_runs += 1

    batch_summary = {
        "batch_ts": batch_ts,
        "input_urls_count": len(urls),
        "accepted_runs": accepted_runs,
        "rejected_runs": rejected_runs,
        "results": batch_results,
    }

    write_json(batch_dir / "batch_summary.json", batch_summary)
    print(json.dumps(batch_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
