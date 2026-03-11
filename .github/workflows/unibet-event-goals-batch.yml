import re
import json
import unicodedata
from pathlib import Path
from datetime import datetime


BOOKMAKER = "unibet"
MARKET_TYPE = "goals_scorers"
NORMALIZER_VERSION = "v2"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def is_simple_scorer(player_name_raw: str) -> bool:
    """
    Filtre métier :
    - accepte uniquement les buteurs simples
    - rejette les combos avec "/"
    - rejette les formulations type "2 buts ou plus"
    - attend un format du type "Nom Joueur (3.40)"
    """
    if not player_name_raw or not isinstance(player_name_raw, str):
        return False

    if "/" in player_name_raw:
        return False

    if re.search(r"\bou\s+\d+", player_name_raw, re.IGNORECASE):
        return False

    return bool(re.match(r"^\s*[^/]+\s*\([^)]+\)\s*$", player_name_raw.strip()))


def parse_decimal_odds(odds_text: str):
    if not odds_text:
        return None

    cleaned = odds_text.strip().replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_row(row: dict) -> dict | None:
    player_name_raw = (row.get("player_name_raw") or "").strip()

    if not is_simple_scorer(player_name_raw):
        return None

    match = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", player_name_raw)
    if not match:
        return None

    player_display = match.group(1).strip()
    odds_text = match.group(2).strip()
    decimal_odds = parse_decimal_odds(odds_text)

    player_slug = slugify(player_display)

    return {
        "bookmaker": BOOKMAKER,
        "market_type": MARKET_TYPE,
        "normalizer_version": NORMALIZER_VERSION,
        "player_slug": player_slug,
        "player_display": player_display,
        "player_name_raw": player_name_raw,
        "odds_text": odds_text,
        "decimal_odds": decimal_odds,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def get_latest_batch_dir(batch_root: Path) -> Path | None:
    if not batch_root.exists():
        return None

    batch_dirs = [p for p in batch_root.iterdir() if p.is_dir()]
    if not batch_dirs:
        return None

    return sorted(batch_dirs, key=lambda p: p.name, reverse=True)[0]


def process_accepted_run(run_info: dict, output_root: Path) -> bool:
    run_dir_value = run_info.get("run_dir")
    if not run_dir_value:
        return False

    run_dir = Path(run_dir_value)
    if not run_dir.exists():
        print(f"❌ run_dir introuvable: {run_dir}")
        return False

    summary_path = run_dir / "summary.json"
    rows_path = run_dir / "goals_market_rows_clean.json"

    if not summary_path.exists():
        print(f"❌ summary.json introuvable: {summary_path}")
        return False

    if not rows_path.exists():
        print(f"❌ goals_market_rows_clean.json introuvable: {rows_path}")
        return False

    summary = load_json(summary_path)
    rows = load_json(rows_path)

    normalized_rows = []
    for row in rows:
        normalized = normalize_row(row)
        if normalized:
            normalized_rows.append(normalized)

    output_run_dir = output_root / run_dir.name
    output_run_dir.mkdir(parents=True, exist_ok=True)

    write_json(
        output_run_dir / "summary.json",
        {
            "source_run_dir": str(run_dir),
            "source_event_url": run_info.get("event_url"),
            "teams": run_info.get("teams"),
            "selected_block_label": run_info.get("selected_block_label"),
            "input_rows": len(rows),
            "normalized_rows": len(normalized_rows),
            "rejected_rows": len(rows) - len(normalized_rows),
            "players_unique": len({r["player_slug"] for r in normalized_rows}),
            "is_complete_market": summary.get("is_complete_market"),
            "rows_valid": summary.get("rows_valid"),
            "parsed_rows_clean": summary.get("parsed_rows_clean"),
        },
    )

    write_json(output_run_dir / "rows_normalized.json", normalized_rows)

    print(f"✅ {run_dir.name}: {len(rows)} → {len(normalized_rows)} normalisés")
    return True


def main():
    batch_root = Path("artifacts") / "unibet_event_goals_batch_runner"
    normalized_root = Path("artifacts") / "unibet_event_goals_normalized"

    latest_batch_dir = get_latest_batch_dir(batch_root)
    if latest_batch_dir is None:
        print("❌ Aucun dossier batch goals trouvé")
        raise SystemExit(1)

    acceptance_report_path = latest_batch_dir / "acceptance_report.json"
    if not acceptance_report_path.exists():
        print(f"❌ acceptance_report.json introuvable: {acceptance_report_path}")
        raise SystemExit(1)

    acceptance_report = load_json(acceptance_report_path)
    accepted_runs = acceptance_report.get("accepted_for_insert", [])

    if not accepted_runs:
        print("ℹ️ Aucun run accepté à normaliser")
        normalized_root.mkdir(parents=True, exist_ok=True)
        write_json(
            normalized_root / "summary.json",
            {
                "batch_dir": str(latest_batch_dir),
                "accepted_runs_count": 0,
                "normalized_runs_count": 0,
                "message": "Aucun run accepté à normaliser",
            },
        )
        return

    normalized_root.mkdir(parents=True, exist_ok=True)

    success_count = 0
    for run_info in accepted_runs:
        if process_accepted_run(run_info, normalized_root):
            success_count += 1

    write_json(
        normalized_root / "summary.json",
        {
            "batch_dir": str(latest_batch_dir),
            "accepted_runs_count": len(accepted_runs),
            "normalized_runs_count": success_count,
            "normalized_at": datetime.utcnow().isoformat() + "Z",
        },
    )

    print(f"🎉 {success_count}/{len(accepted_runs)} runs normalisés")
    print(f"📁 Résultats: {normalized_root}")


if __name__ == "__main__":
    main()
