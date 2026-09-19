"""Fonctions partagees par les adaptateurs : parametres autorises, cibles, resultats."""

from __future__ import annotations

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

from agentwatch.core import normalize as N
from agentwatch.core import schema as S

_SCALAR = (str, int, float, bool)


def pick_params(tool_input: Any, allowed: list[str], ctx: Any, max_chars: int = 300) -> dict[str, Any]:
    """Sous-ensemble autorise de tool_input. Les autres cles : nom + empreinte de valeur."""
    params: dict[str, Any] = {}
    if not isinstance(tool_input, dict):
        if tool_input is not None:
            params["_input_type"] = type(tool_input).__name__
        return params
    hidden: dict[str, str] = {}
    for key, value in list(tool_input.items())[:40]:
        skey = str(key)
        if skey in allowed:
            params[skey] = _bounded_value(value, max_chars)
        else:
            hidden[skey] = ctx.fp(N.canonical_json(value))[:10]
    if hidden:
        params["_fp"] = hidden
    return params


def _bounded_value(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "..."
    if isinstance(value, _SCALAR) or value is None:
        return value
    if isinstance(value, list):
        out = []
        for item in value[:20]:
            if isinstance(item, _SCALAR) or item is None:
                out.append(_bounded_value(item, max_chars))
            else:
                out.append({"_type": type(item).__name__})
        if len(value) > 20:
            out.append({"_more": len(value) - 20})
        return out
    if isinstance(value, dict):
        return {"_keys": sorted(str(k) for k in list(value)[:30])}
    return {"_type": type(value).__name__}


def first_path_like(tool_input: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for k in keys:
        v = tool_input.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def bounded_paths(items: Any, project_dir: str | None, cwd: str | None, limit: int) -> list[str] | None:
    """Liste de chemins normalises et bornee (resultats de Glob/Grep)."""
    if not isinstance(items, list):
        return None
    out: list[str] = []
    for it in items[:limit]:
        if isinstance(it, str):
            p = N.normalize_path(it, project_dir, cwd)
            if p:
                out.append(p)
    return out


def apply_shell_params(ev: dict[str, Any], command: Any, cfg: dict[str, Any]) -> None:
    """Renseigne cible/params pour une commande shell (Claude Bash, Codex shell)."""
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    if not isinstance(command, str):
        ev["warnings"].append("shell command missing or not a string")
        return
    norm = N.normalize_command(command)
    limit = int(cfg.get("max_command_chars", 2000))
    ev["target"] = norm if len(norm) <= limit else norm[:limit] + "..."
    ev["target_kind"] = "command"
    info = N.classify_shell(norm)
    ev["params"]["command"] = ev["target"]
    ev["params"]["shell_kind"] = info["kind"]
    ev["params"]["shell_heads"] = info["heads"]
    if info["paths"]:
        ev["params"]["shell_paths"] = info["paths"]
    ev["evidence"]["shell_segments"] = info["segments"]


def status_from_response(resp: Any) -> tuple[str, int | None, str | None, list[str]]:
    """Deduit (statut, code de sortie, erreur, avertissements) d'un tool_response inconnu.

    # * Conservateur : sans indice explicite, le statut reste 'unknown'.
    """
    warnings: list[str] = []
    if not isinstance(resp, dict):
        return S.STATUS_UNKNOWN, None, None, ["tool_response is not an object; status unknown"]
    code: int | None = None
    for key in ("exit_code", "exitCode", "exit_status", "status_code", "returncode"):
        v = resp.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            code = v
            break
    if resp.get("interrupted") is True:
        return S.STATUS_INTERRUPTED, code, None, warnings
    if code is not None:
        return (S.STATUS_SUCCESS if code == 0 else S.STATUS_ERROR), code, None, warnings
    for key in ("isError", "is_error"):
        if key in resp:
            if resp.get(key) is True:
                return S.STATUS_ERROR, None, _error_text(resp), warnings
            return S.STATUS_SUCCESS, None, None, warnings
    if "success" in resp and isinstance(resp.get("success"), bool):
        return (S.STATUS_SUCCESS if resp["success"] else S.STATUS_ERROR), None, _error_text(resp), warnings
    if isinstance(resp.get("error"), str) and resp["error"].strip():
        return S.STATUS_ERROR, None, resp["error"], warnings
    if "stdout" in resp or "stderr" in resp or "content" in resp or "filePath" in resp or "filenames" in resp:
        # * Claude Code : PostToolUse n'est emis qu'apres succes (les echecs passent par
        #   PostToolUseFailure). Le statut reste "success" avec cette provenance documentee.
        return S.STATUS_SUCCESS, None, None, warnings
    if "output" in resp:
        warnings.append("exit status not exposed in tool_response; status unknown")
        return S.STATUS_UNKNOWN, None, None, warnings
    warnings.append("tool_response shape unknown; status unknown")
    return S.STATUS_UNKNOWN, None, None, warnings


def _error_text(resp: dict[str, Any]) -> str | None:
    for key in ("error", "message", "stderr"):
        v = resp.get(key)
        if isinstance(v, str) and v.strip():
            return v
    content = resp.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    if isinstance(content, str):
        return content
    return None


def primary_text(resp: Any, tool_name: str | None = None) -> str | None:
    """Texte principal d'une reponse : contenu lu, stdout, resultats de recherche, blocs MCP.

    # * Sert a l'empreinte de CONTENU : deux outils differents (Read / cat) qui obtiennent
    #   le meme texte donnent la meme empreinte, contrairement a l'empreinte du JSON complet.
    """
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        parts = [b.get("text") for b in resp if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return "\n".join(parts) if parts else None
    if not isinstance(resp, dict):
        return None
    info = resp.get("file") if isinstance(resp.get("file"), dict) else resp
    for key in ("content", "stdout", "output", "aggregated_output", "text"):
        v = info.get(key) if isinstance(info, dict) else None
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            parts = [b.get("text") for b in v if isinstance(b, dict) and isinstance(b.get("text"), str)]
            if parts:
                return "\n".join(parts)
    names = resp.get("filenames")
    if isinstance(names, list):
        return "\n".join(str(n) for n in names)
    return None


def content_fingerprint(text: str | None, fp: Any) -> dict[str, Any] | None:
    """Empreinte HMAC du texte normalise (CRLF -> LF, espaces de fin supprimes)."""
    if not isinstance(text, str) or not text.strip():
        return None
    norm = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()
    return {"method": "hmac-sha256-normalized-text", "value": fp(norm), "chars": len(norm)}


import re as _re

# * Observe en direct (serveur MCP romeo) : une panne SSH renvoyee comme un resultat normal
#   (isError=false, texte "session SSH interrompue"). Sans cet indice, 22 pannes identiques
#   passent pour 22 lectures reussies, pour le client comme pour l'observateur.
MCP_ERROR_TEXT_RE = _re.compile(
    r"(?im)^.*\b(?:error|erreur|exception|traceback|failed|failure|echec|échec|timeout|timed out|refused|unreachable|"
    r"unavailable|indisponible|interrompue?|interrupted|denied|refusé|impossible|not found|introuvable|no such|"
    r"connection (?:lost|closed|reset)|session .*(?:lost|closed|interrompue))\b.*$")


def mcp_error_hint(resp: Any) -> str | None:
    """Premiere ligne d'une reponse MCP 'reussie' qui ressemble a une erreur, sinon None.

    # ! Reponse JSON : lue structurellement. Constate le 2026-09-19 : le serveur romeo repond
    #   `{"erreur": null, ...}` quand tout va bien ; le mot "erreur" de la CLE faisait passer 188 attentes
    #   d'un job SLURM en file pour 188 pannes. Il faut un champ d'erreur non vide ou un statut d'echec.
    """
    text = primary_text(resp)
    if not text:
        return None
    stripped = text.strip()
    if stripped[:1] in "{[" and len(stripped) <= 500_000:   # * borne : le hook doit rester rapide
        import json
        try:
            obj = json.loads(stripped)
        except (ValueError, RecursionError):
            obj = None
        if obj is not None:
            return _json_error_hint(obj, 0)
    m = MCP_ERROR_TEXT_RE.search(text[:2000])
    return m.group(0).strip() if m else None


_ERROR_KEYS = {"error", "errors", "erreur", "erreurs", "exception", "traceback", "failure", "echec", "stderr_error"}
_STATUS_KEYS = {"status", "state", "statut", "etat", "result", "outcome"}
_FAILED_VALUES = {"failed", "failure", "error", "erreur", "echec", "fatal", "crashed"}


def _json_error_hint(obj: Any, depth: int) -> str | None:
    """Champ d'erreur non vide ou statut d'echec dans un JSON (profondeur bornee), sinon None."""
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:60]:
            lk = str(k).lower()
            if lk in _ERROR_KEYS and v not in (None, "", [], {}, False, 0):
                return f"{k}: {str(v)[:200]}"
            if lk in _STATUS_KEYS and isinstance(v, str) and v.strip().lower() in _FAILED_VALUES:
                return f"{k}: {v}"
        for v in list(obj.values())[:60]:
            if isinstance(v, (dict, list)):
                hint = _json_error_hint(v, depth + 1)
                if hint:
                    return hint
    elif isinstance(obj, list):
        for v in obj[:30]:
            hint = _json_error_hint(v, depth + 1)
            if hint:
                return hint
    return None


def response_keys(resp: Any) -> list[str] | None:
    if isinstance(resp, dict):
        return sorted(str(k) for k in list(resp)[:40])
    if resp is None:
        return None
    return [f"<{type(resp).__name__}>"]
