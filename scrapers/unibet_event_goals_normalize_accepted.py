import os
import re
import json
import unicodedata
from pathlib import Path
from datetime import datetime


BOOKMAKER = "unibet"
MARKET_TYPE = "goals_scorers"
NORMALIZER_VERSION = "v1"


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text.strip("-"))


def is_simple_scorer(player_name_raw: str) -> bool:
    """Filtre métier : UNIQUEMENT buteurs simples (pas de /, pas de combo)"""
    # Rejette doubles/triples chances : "Joueur1 / Joueur2"
    if "/" in player_name_raw:
        return False
    
    # Rejette "ou plus" : "Joueur 2 buts ou plus"
    if re.search(r"\bou\s+\d+", player_name_raw, re.IGNORECASE):
        return False
    
    # Accepte UNIQUEMENT : "Joueur (cote)"
    return bool(re.match(r"^\s*[^/]+\s*\([^)]+\)\s*$", player_name_raw.strip()))


def normalize_row(row: dict) -> dict:
    player_name_raw = row.get("player_name_raw", "").strip()
    
    if not is_simple_scorer(player_name_raw):
        return None  # Rejeté par filtre métier buts
    
    # Extraction nom joueur (avant parenthèses cote)
    player_match = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", player_name_raw.strip())
    if not player_match:
        return None
    
    player_display = player_match.group(1).strip()
    odds_text = player_match.group(2).strip()
    
    # Nettoyage nom
    player_slug = slugify(player_display)
    
    return {
        "bookmaker": BOOKMAKER,
        "market_type": MARKET_TYPE,
        "normalizer_version": NORMALIZER_VERSION,
        "player_slug": player_slug,
        "player_display": player_display,
        "player_name_raw": player_name_raw,
        "odds_text": odds_text,
        "decimal_odds": float(odds_text) if odds_text.replace(".", "").isdigit() else None,
        "timestamp": datetime.now().isoformat(),
    }


def process_accepted_dir(accepted_dir: Path, output_dir: Path):
    """Traite UN SEUL run_dir accepté"""
    summary_path = accepted_dir / "summary.json"
    if not summary_path.exists():
        return False
    
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not summary.get("is_complete_market"):
        return False
    
    rows_path = accepted_dir / "rows_clean.json"
    if not rows_path.exists():
        return False
    
    rows = json.loads(rows_path.read_text(encoding="utf-8"))
    normalized_rows = []
    
    for row in rows:
        normalized = normalize_row(row)
        if normalized:
            normalized_rows.append(normalized)
    
    # Écriture output
    ts_slug = accepted_dir.name
    output_run_dir = output_dir / ts_slug
    output_run_dir.mkdir(parents=True, exist_ok=True)
    
    write_json(output_run_dir / "summary.json", {
        "input_dir": str(accepted_dir),
        "input_rows": len(rows),
        "normalized_rows": len(normalized_rows),
        "rejected_rows": len(rows) - len(normalized_rows),
        "players_unique": len({r["player_slug"] for r in normalized_rows}),
    })
    
    write_json(output_run_dir / "rows_normalized.json", normalized_rows)
    print(f"✅ {ts_slug}: {len(rows)} → {len(normalized_rows)} normalisés")
    return True


def find_new_accepted_dirs(base_dir: Path, before_names: set) -> list[Path]:
    if not base_dir.exists():
        return []
    current_dirs = [p for p in base_dir.iterdir() if p.is_dir()]
    new_dirs = [p for p in current_dirs if p.name not in before_names]
    return sorted(new_dirs)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    accepted_root = Path("artifacts") / "unibet_event_goals_acceptance"
    normalized_root = Path("artifacts") / "unibet_event_goals_normalized"
    
    if not accepted_root.exists():
        print("❌ Pas de dossier acceptance trouvé")
        return
    
    before_names = set()
    if normalized_root.exists():
        before_names = {p.name for p in normalized_root.iterdir() if p.is_dir()}
    
    new_accepted_dirs = find_new_accepted_dirs(accepted_root, before_names)
    
    if not new_accepted_dirs:
        print("ℹ️ Aucun nouveau run accepté à normaliser")
        return
    
    success_count = 0
    for accepted_dir in new_accepted_dirs:
        if process_accepted_dir(accepted_dir, normalized_root):
            success_count += 1
    
    print(f"🎉 {success_count}/{len(new_accepted_dirs)} runs normalisés")
    print(f"📁 Résultats: {normalized_root}")


if __name__ == "__main__":
    main()
