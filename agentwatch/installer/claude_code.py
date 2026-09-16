"""Installation des hooks AgentWatch dans les settings de Claude Code.

Format verifie (docs officielles, voir docs/compatibility.md) : settings.json ->
"hooks" -> <Event> -> [ { "matcher": "*", "hooks": [ {type: command, command, args, timeout} ] } ].
# * Forme "exec" (command + args) : aucun shell, chaque argument est passe tel quel,
#   donc espaces et accents dans les chemins sont sans risque.
# * Idempotent : les groupes AgentWatch existants sont remplaces ; le reste est intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentwatch.installer import common as C

EVENTS: tuple[str, ...] = (
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionStart", "SessionEnd",
    "UserPromptSubmit", "Stop", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop",
)
HOOK_TIMEOUT_SECONDS = 10


def settings_path(scope: str, project_dir: Path | None = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    base = project_dir or Path.cwd()
    if scope == "project":
        return base / ".claude" / "settings.json"
    if scope == "local":
        return base / ".claude" / "settings.local.json"
    raise ValueError(f"scope inconnu : {scope!r} (user|project|local)")


def hook_spec(python: str, entry: Path, home: Path) -> dict[str, Any]:
    return {
        "type": "command",
        "command": python,
        "args": ["-I", str(entry), "ingest", "--client", "claude-code", "--home", str(home)],
        "timeout": HOOK_TIMEOUT_SECONDS,
    }


def _strip_ours(obj: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Retire les groupes AgentWatch. Retourne (objet, nombre de groupes retires)."""
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
                # ? Groupe mixte (edite a la main) : on ne retire que nos entrees.
                g = dict(g, hooks=[h for h in inner if not C.is_our_hook(h)])
                removed += 1
            kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return obj, removed


def plan_install(current: dict[str, Any], python: str, entry: Path, home: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retourne (objet propose, resume)."""
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
        groups.append({"matcher": "*", "hooks": [hook_spec(python, entry, home)]})
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
    return {"installed_events": sorted(ours), "missing_events": missing, "foreign_groups": foreign,
            "complete": not missing, "disable_all_hooks": bool(current.get("disableAllHooks"))}
