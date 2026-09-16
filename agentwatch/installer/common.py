"""Utilitaires communs aux installateurs : lecture tolerante, sauvegarde, diff, ecriture atomique."""

from __future__ import annotations

import difflib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from agentwatch.collector.store import atomic_write_bytes

MARKER_FILENAME = "hook_entry.py"


def hook_entry_path() -> Path:
    return Path(__file__).resolve().parent.parent / MARKER_FILENAME


def python_executable() -> str:
    return sys.executable


def hook_python_executable() -> str:
    """Interpreteur pour les hooks : sous Windows, pythonw.exe (meme dossier) evite qu'une
    fenetre console apparaisse a chaque hook lance par une application de bureau (Codex,
    issue publique #44768). Verifie : pythonw lit stdin redirige et coute le meme temps (~39 ms)."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        if pyw.is_file():
            return str(pyw)
    return str(exe)


def read_json_file(path: Path) -> tuple[dict[str, Any], str]:
    """(objet, texte brut). Fichier absent -> ({}, ""). JSON invalide -> ValueError explicite."""
    if not path.is_file():
        return {}, ""
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}, raw
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} : un objet JSON etait attendu")
    return obj, raw


def dump_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def unified_diff(old: str, new: str, path: Path) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"{path} (actuel)", tofile=f"{path} (propose)"))


def backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + f".bak-agentwatch-{stamp}")
    shutil.copy2(path, dest)
    return dest


def write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, dump_json(obj).encode("utf-8"))


def is_our_hook(hook: Any) -> bool:
    """Reconnait une entree AgentWatch : la commande ou ses arguments citent hook_entry.py + agentwatch."""
    if not isinstance(hook, dict):
        return False
    parts: list[str] = []
    for key in ("command", "commandWindows", "command_windows"):
        if isinstance(hook.get(key), str):
            parts.append(hook[key])
    args = hook.get("args")
    if isinstance(args, list):
        parts.extend(str(a) for a in args)
    joined = " ".join(parts).replace("\\", "/").lower()
    return MARKER_FILENAME in joined and "agentwatch" in joined
