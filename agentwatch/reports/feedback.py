"""Retours locaux sur les signalements : pertinent / faux positif. Aucun apprentissage.

# * Fichier : <home>/feedback.json  { finding_id: {mark, note, time} }
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agentwatch.collector.store import atomic_write_bytes

FEEDBACK_FILENAME = "feedback.json"
MARKS = ("relevant", "false-positive", "clear")


def load_feedback(home: Path) -> dict[str, dict[str, Any]]:
    path = home / FEEDBACK_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_feedback(home: Path, finding_id: str, mark: str, note: str | None = None) -> dict[str, dict[str, Any]]:
    if mark not in MARKS:
        raise ValueError(f"marque inconnue : {mark!r} (attendu : {', '.join(MARKS)})")
    data = load_feedback(home)
    if mark == "clear":
        data.pop(finding_id, None)
    else:
        data[finding_id] = {"mark": mark, "note": note, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    home.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(home / FEEDBACK_FILENAME, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))
    return data
