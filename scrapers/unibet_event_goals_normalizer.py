import json
import os
from pathlib import Path
from typing import List, Dict

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def normalize_row(row: dict) -> dict:
    """Transforme 1 ligne parsed en format Supabase"""
    return
