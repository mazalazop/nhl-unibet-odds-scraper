#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scrapers/unibet_event_points_batch_runner.py

Objectif
--------
Exécuter le parseur de marché POINTS 1+ sur une liste d'event URLs Unibet,
agréger les sorties, et produire un batch exploitable par les étapes suivantes.

Entrées via variables d'environnement
-------------------------------------
- DISCOVERY_JSON_PATH         : chemin vers discovered_event_urls.json
- UNIBET_EVENT_URLS_JSON_PATH : chemin vers un JSON contenant event_urls
- UNIBET_EVENT_URLS_JSON      : JSON brut contenant event_urls ou liste d'URLs
- UNIBET_EVENT_URLS           : CSV d'URLs
- PW_HEADLESS                 : true / false
- PARSER_SCRIPT_PATH          : chemin du parseur, défaut = scrapers/unibet_event_points_parser.py
- PARSER_TIMEOUT_SECONDS      : timeout par match, défaut = 240
- FAIL_FAST                   : true / false, défaut = false

Sorties
-------
Dans artifacts/unibet_event_points_batch_runner/<timestamp>/ :
- batch_inputs.json
- batch_event_results.json
- batch_summary.json
- raw_points_markets.json
- logs/*.stdout.log
- logs/*.stderr.log

Notes
-----
- Le script n'échoue pas sur un match isolé.
- Le script écrit toujours les artefacts batch avant de quitter.
- Le jugement final accepté / rejeté est délégué à l'étape acceptance_report.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qsl, urlparse, urlunparse

ARTIFACTS_ROOT = Path("artifacts") / "unibet_event_points_batch_runner"
PARSER_ARTIFACTS_ROOT = Path("artifacts") / "unibet_event_points_parser"
DEFAULT_PARSER_SCRIPT = "scrapers/unibet_event_points_parser.py"
DEFAULT_TIMEOUT_SECONDS = 240


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    print(f"[{now_ts()}] {message}")


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


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


def dedupe_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        normalized = normalize_url(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def extract_event_slug(event_url: str) -> str:
    path = urlparse(event_url).path or ""
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.html$", "", slug, flags=re.I)
    if slug:
        return slug
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", event_url).strip("-")[:120] or "event"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_event_urls_from_payload(payload: Any) -> List[str]:
    if isinstance(payload, dict):
        urls = payload.get("event_urls")
        if isinstance(urls, list):
            return [safe_text(x) for x in urls if safe_text(x)]
        if isinstance(urls, str):
            return [safe_text(urls)]
        return []

    if isinstance(payload, list):
        return [safe_text(x) for x in payload if safe_text(x)]

    if isinstance(payload, str):
        raw = payload.strip()
        if raw.startswith("[") or raw.startswith("{"):
            try:
                return parse_event_urls_from_payload(json.loads(raw))
            except Exception:
                pass
        return [part.strip() for part in raw.split(",") if part.strip()]

    return []


def resolve_event_urls() -> Dict[str, Any]:
    sources_checked: List[Dict[str, Any]] = []

    candidate_paths = [
        os.getenv("DISCOVERY_JSON_PATH", "").strip(),
        os.getenv("UNIBET_EVENT_URLS_JSON_PATH", "").strip(),
    ]
    for raw_path in candidate_paths:
        if not raw_path:
            continue
        path = Path(raw_path)
        exists = path.exists()
        sources_checked.append({"type": "json_path", "path": str(path), "exists": exists})
        if not exists:
            continue
        payload = load_json(path)
        urls = dedupe_keep_order(parse_event_urls_from_payload(payload))
        if urls:
            return {
                "source_type": "json_path",
                "source_value": str(path),
                "event_urls": urls,
                "sources_checked": sources_checked,
            }

    raw_json_env = os.getenv("UNIBET_EVENT_URLS_JSON", "").strip()
    if raw_json_env:
        sources_checked.append({"type": "json_env", "present": True})
        urls = dedupe_keep_order(parse_event_urls_from_payload(raw_json_env))
        if urls:
            return {
                "source_type": "json_env",
                "source_value": "UNIBET_EVENT_URLS_JSON",
                "event_urls": urls,
                "sources_checked": sources_checked,
            }

    raw_csv_env = os.getenv("UNIBET_EVENT_URLS", "").strip()
    if raw_csv_env:
        sources_checked.append({"type": "csv_env", "present": True})
        urls = dedupe_keep_order([part.strip() for part in raw_csv_env.split(",") if part.strip()])
        if urls:
            return {
                "source_type": "csv_env",
                "source_value": "UNIBET_EVENT_URLS",
                "event_urls": urls,
                "sources_checked": sources_checked,
            }

    raise ValueError(
        "Aucune source d'event URLs fournie. Utilise DISCOVERY_JSON_PATH, "
        "UNIBET_EVENT_URLS_JSON_PATH, UNIBET_EVENT_URLS_JSON ou UNIBET_EVENT_URLS."
    )


def list_run_dirs(root: Path) -> List[Path]:
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()])


def resolve_new_parser_run_dir(before: Sequence[Path], after: Sequence[Path]) -> Optional[Path]:
    before_set = {str(p.resolve()) for p in before}
    new_dirs = [p for p in after if str(p.resolve()) not in before_set]
    if new_dirs:
        return sorted(new_dirs)[-1]
    if after:
        return after[-1]
    return None


def read_parser_artifacts(run_dir: Optional[Path]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []

    if run_dir is None:
        return {
            "parser_run_dir": None,
            "summary_path": None,
            "rows_path": None,
            "summary": summary,
            "rows": rows,
        }

    summary_path = run_dir / "summary.json"
    rows_path = run_dir / "points_market_rows_clean.json"

    if summary_path.exists():
        try:
            summary = load_json(summary_path)
        except Exception as exc:
            summary = {"fatal_error": f"summary_read_error: {exc}"}

    if rows_path.exists():
        try:
            payload = load_json(rows_path)
            if isinstance(payload, list):
                rows = payload
        except Exception:
            rows = []

    return {
        "parser_run_dir": str(run_dir),
        "summary_path": str(summary_path) if summary_path.exists() else None,
        "rows_path": str(rows_path) if rows_path.exists() else None,
        "summary": summary,
        "rows": rows,
    }


def basic_prelim_ok(event_result: Dict[str, Any]) -> bool:
    summary = event_result.get("parser_summary") or {}
    rows_count = int(event_result.get("rows_count") or 0)
    return (
        int(event_result.get("parser_exit_code") or 0) == 0
        and not safe_text(summary.get("fatal_error"))
        and bool(summary.get("market_block_found"))
        and bool(summary.get("rows_valid"))
        and rows_count > 0
    )


def run_parser_for_event(
    event_url: str,
    event_index: int,
    total_events: int,
    out_dir: Path,
    parser_script: str,
    headless: bool,
    timeout_seconds: int,
) -> Dict[str, Any]:
    slug = extract_event_slug(event_url)
    logs_dir = out_dir / "logs"
    ensure_dir(logs_dir)

    before_dirs = list_run_dirs(PARSER_ARTIFACTS_ROOT)
    started_at = utc_now_iso()

    env = os.environ.copy()
    env["UNIBET_EVENT_URL"] = event_url
    env["PW_HEADLESS"] = "true" if headless else "false"

    log(f"[{event_index}/{total_events}] parser start: {slug}")
    try:
        proc = subprocess.run(
            [sys.executable, parser_script],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        parser_exit_code = int(proc.returncode)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        timeout_hit = False
    except subprocess.TimeoutExpired as exc:
        parser_exit_code = 124
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\nTIMEOUT after {timeout_seconds}s"
        timeout_hit = True

    finished_at = utc_now_iso()
    after_dirs = list_run_dirs(PARSER_ARTIFACTS_ROOT)
    run_dir = resolve_new_parser_run_dir(before_dirs, after_dirs)
    artifacts = read_parser_artifacts(run_dir)

    stdout_path = logs_dir / f"{event_index:03d}_{slug}.stdout.log"
    stderr_path = logs_dir / f"{event_index:03d}_{slug}.stderr.log"
    write_text(stdout_path, stdout)
    write_text(stderr_path, stderr)

    summary = artifacts["summary"] or {}
    rows = artifacts["rows"] or []

    event_result: Dict[str, Any] = {
        "event_index": event_index,
        "event_url": event_url,
        "event_slug": slug,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "timeout_seconds": timeout_seconds,
        "timeout_hit": timeout_hit,
        "parser_script": parser_script,
        "parser_exit_code": parser_exit_code,
        "parser_stdout_path": str(stdout_path),
        "parser_stderr_path": str(stderr_path),
        "parser_run_dir": artifacts["parser_run_dir"],
        "parser_summary_path": artifacts["summary_path"],
        "rows_path": artifacts["rows_path"],
        "rows_count": len(rows),
        "parser_summary": summary,
        "prelim_ok": False,
    }

    if summary:
        event_result["title"] = summary.get("title")
        event_result["final_url"] = summary.get("final_url")
        event_result["teams"] = summary.get("teams") or []
        event_result["rows_valid"] = summary.get("rows_valid")
        event_result["rows_validation_reason"] = summary.get("rows_validation_reason")
        event_result["market_block_found"] = summary.get("market_block_found")
        event_result["is_complete_market"] = summary.get("is_complete_market")
        event_result["fatal_error"] = summary.get("fatal_error")
        event_result["players_kept_points_1_plus"] = summary.get("players_kept_points_1_plus")
    else:
        event_result["fatal_error"] = "missing_parser_summary"

    event_result["prelim_ok"] = basic_prelim_ok(event_result)
    log(
        f"[{event_index}/{total_events}] parser done: exit={parser_exit_code} rows={len(rows)} "
        f"prelim_ok={event_result['prelim_ok']} slug={slug}"
    )
    return event_result


def aggregate_raw_rows(event_results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for event in event_results:
        rows_path = safe_text(event.get("rows_path"))
        if not rows_path:
            continue
        path = Path(rows_path)
        if not path.exists():
            continue

        try:
            rows = load_json(path)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue

        teams = event.get("teams") or []
        home_team = teams[0] if len(teams) >= 1 else None
        away_team = teams[1] if len(teams) >= 2 else None

        for row in rows:
            if not isinstance(row, dict):
                continue
            enriched = dict(row)
            enriched.update(
                {
                    "event_url": event.get("event_url"),
                    "event_slug": event.get("event_slug"),
                    "home_team": home_team,
                    "away_team": away_team,
                    "parser_run_dir": event.get("parser_run_dir"),
                    "parser_summary_path": event.get("parser_summary_path"),
                    "rows_path": event.get("rows_path"),
                }
            )
            out.append(enriched)
    return out


def main() -> None:
    resolved = resolve_event_urls()
    event_urls = resolved["event_urls"]
    if not event_urls:
        raise ValueError("Liste d'event URLs vide après résolution.")

    parser_script = os.getenv("PARSER_SCRIPT_PATH", DEFAULT_PARSER_SCRIPT).strip() or DEFAULT_PARSER_SCRIPT
    headless = env_bool("PW_HEADLESS", True)
    timeout_seconds = env_int("PARSER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    fail_fast = env_bool("FAIL_FAST", False)

    out_dir = ARTIFACTS_ROOT / now_ts()
    ensure_dir(out_dir)

    batch_inputs = {
        "generated_at_utc": utc_now_iso(),
        "batch_run_dir": str(out_dir),
        "source_type": resolved["source_type"],
        "source_value": resolved["source_value"],
        "sources_checked": resolved["sources_checked"],
        "event_urls": event_urls,
        "event_count": len(event_urls),
        "headless": headless,
        "parser_script": parser_script,
        "parser_timeout_seconds": timeout_seconds,
        "fail_fast": fail_fast,
    }
    write_json(out_dir / "batch_inputs.json", batch_inputs)

    event_results: List[Dict[str, Any]] = []
    raw_points_rows: List[Dict[str, Any]] = []

    for idx, event_url in enumerate(event_urls, start=1):
        event_result = run_parser_for_event(
            event_url=event_url,
            event_index=idx,
            total_events=len(event_urls),
            out_dir=out_dir,
            parser_script=parser_script,
            headless=headless,
            timeout_seconds=timeout_seconds,
        )
        event_results.append(event_result)
        if fail_fast and not event_result.get("prelim_ok"):
            log("FAIL_FAST actif: arrêt après premier échec parser.")
            break

    raw_points_rows = aggregate_raw_rows(event_results)

    batch_summary = {
        "generated_at_utc": utc_now_iso(),
        "batch_run_dir": str(out_dir),
        "source_type": resolved["source_type"],
        "source_value": resolved["source_value"],
        "headless": headless,
        "parser_script": parser_script,
        "parser_timeout_seconds": timeout_seconds,
        "input_event_count": len(event_urls),
        "processed_event_count": len(event_results),
        "prelim_ok_event_count": sum(1 for x in event_results if x.get("prelim_ok")),
        "parser_success_exit_count": sum(1 for x in event_results if int(x.get("parser_exit_code") or 0) == 0),
        "parser_nonzero_exit_count": sum(1 for x in event_results if int(x.get("parser_exit_code") or 0) != 0),
        "raw_points_rows_count": len(raw_points_rows),
        "event_results": event_results,
        "artifacts": {
            "batch_inputs_json": str(out_dir / "batch_inputs.json"),
            "batch_event_results_json": str(out_dir / "batch_event_results.json"),
            "batch_summary_json": str(out_dir / "batch_summary.json"),
            "raw_points_markets_json": str(out_dir / "raw_points_markets.json"),
            "logs_dir": str(out_dir / "logs"),
        },
    }

    raw_points_payload = {
        "generated_at_utc": utc_now_iso(),
        "batch_run_dir": str(out_dir),
        "source_type": resolved["source_type"],
        "source_value": resolved["source_value"],
        "rows_count": len(raw_points_rows),
        "rows": raw_points_rows,
    }

    write_json(out_dir / "batch_event_results.json", event_results)
    write_json(out_dir / "batch_summary.json", batch_summary)
    write_json(out_dir / "raw_points_markets.json", raw_points_payload)

    print(json.dumps({
        "batch_run_dir": str(out_dir),
        "input_event_count": batch_summary["input_event_count"],
        "processed_event_count": batch_summary["processed_event_count"],
        "prelim_ok_event_count": batch_summary["prelim_ok_event_count"],
        "raw_points_rows_count": batch_summary["raw_points_rows_count"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
