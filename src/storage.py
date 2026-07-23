from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .config import RAW_DIR, STATE_DIR


def write_jsonl(source: str, rows: list[dict[str, Any]], run_date: date | None = None) -> Path:
    run_date = run_date or date.today()
    out_dir = RAW_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_date.isoformat()}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def source_glob(source: str) -> str:
    return str(RAW_DIR / source / "*.jsonl")


def has_data(source: str) -> bool:
    d = RAW_DIR / source
    return d.exists() and any(d.glob("*.jsonl"))


def load_state(name: str, default: Any) -> Any:
    path = STATE_DIR / f"{name}.json"
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(name: str, value: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{name}.json"
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
