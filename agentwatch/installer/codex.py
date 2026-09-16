"""Installation des hooks AgentWatch pour Codex (hooks.json).

Format verifie (docs officielles, voir docs/compatibility.md) : ~/.codex/hooks.json ou
<projet>/.codex/hooks.json -> "hooks" -> <Event> -> [ { "matcher", "hooks": [ {type: command,
command, commandWindows, timeout} ] } ].
# ! Codex execute `command` via un shell : sous Windows la chaine est passee a PowerShell
#   (d'ou l'operateur d'appel `&` et les guillemets simples), sur POSIX a `sh`.
# ! Codex exige que l'utilisateur fasse confiance a chaque hook (commande /hooks) avant
#   qu'il ne s'execute ; AgentWatch ne peut pas accorder cette confiance a sa place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentwatch.installer import common as C

EVENTS: tuple[str, ...] = (
    "PreToolUse", "PostToolUse", "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop",
    "Interrupt", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop",
)
HOOK_TIMEOUT_SECONDS = 10


def hooks_path(scope: str, project_dir: Path | None = None, codex_home: Path | None = None) -> Path:
    if scope == "user":
        return (codex_home or Path.home() / ".codex") / "hooks.json"
    if scope == "project":
        return (project_dir or Path.cwd()) / ".codex" / "hooks.json"
    raise ValueError(f"scope inconnu : {scope!r} (user|project)")


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def hook_spec(python: str, entry: Path, home: Path, posix_python: str = "python3") -> dict[str, Any]:
    posix_entry = str(entry).replace("\\", "/")
    posix_home = str(home).replace("\\", "/")
    return {
        "type": "command",
        "command": f"{_sh_quote(posix_python)} -I {_sh_quote(posix_entry)} ingest --client codex --home {_sh_quote(posix_home)}",
        "commandWindows": f"& {_ps_quote(python)} -I {_ps_quote(str(entry))} ingest --client codex --home {_ps_quote(str(home))}",
        "timeout": HOOK_TIMEOUT_SECONDS,
        "statusMessage": "AgentWatch",
    }


def _strip_ours(obj: dict[str, Any]) -> tuple[dict[str, Any], int]:
    removed = 0
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        return obj, 0
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if isinstance(inner, list) and inner and all(C.is_our_hook(h) for h in inner):
                removed += 1
                continue
            if isinstance(inner, list) and any(C.is_our_hook(h) for h in inner):
                g = dict(g, hooks=[h for h in inner if not C.is_our_hook(h)])
                removed += 1
            kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return obj, removed


def plan_install(current: dict[str, Any], python: str, entry: Path, home: Path,
                 posix_python: str = "python3") -> tuple[dict[str, Any], dict[str, Any]]:
    import copy
    obj = copy.deepcopy(current)
    obj, replaced = _strip_ours(obj)
    hooks = obj.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("la cle 'hooks' existante n'est pas un objet : intervention manuelle requise")
    foreign = 0
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} n'est pas une liste : intervention manuelle requise")
        foreign += len(groups)
        groups.append({"matcher": "*", "hooks": [hook_spec(python, entry, home, posix_python)]})
    return obj, {"events": list(EVENTS), "replaced_groups": replaced, "foreign_groups_preserved": foreign}


def plan_uninstall(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    import copy
    obj, removed = _strip_ours(copy.deepcopy(current))
    return obj, {"removed_groups": removed}


def status(current: dict[str, Any]) -> dict[str, Any]:
    raw_hooks = current.get("hooks")
    hooks: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
    ours: dict[str, int] = {}
    foreign: dict[str, int] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if isinstance(inner, list) and any(C.is_our_hook(h) for h in inner):
                ours[event] = ours.get(event, 0) + 1
            else:
                foreign[event] = foreign.get(event, 0) + 1
    missing = [e for e in EVENTS if e not in ours]
    return {"installed_events": sorted(ours), "missing_events": missing, "foreign_groups": foreign, "complete": not missing}


def config_toml_status(codex_home: Path | None = None) -> dict[str, Any]:
    """Lecture seule de config.toml : drapeau features.hooks et hooks inline eventuels."""
    import tomllib
    path = (codex_home or Path.home() / ".codex") / "config.toml"
    out: dict[str, Any] = {"path": str(path), "exists": path.is_file(), "features_hooks": None, "inline_hooks_events": []}
    if not path.is_file():
        return out
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    raw_features = data.get("features")
    features: dict[str, Any] = raw_features if isinstance(raw_features, dict) else {}
    out["features_hooks"] = features.get("hooks", features.get("codex_hooks"))
    raw_hooks = data.get("hooks")
    inline: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
    out["inline_hooks_events"] = sorted(inline)
    return out
