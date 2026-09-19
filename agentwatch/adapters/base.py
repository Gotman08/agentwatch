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
        if skey in allowed or N.is_timing_param(skey, value):
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


# --------------------------------------------------------------------------- faits sur un resultat (detecteur G)
# * Pourquoi un appel est-il refait ? L'agent reessaie apres une indisponibilite, attend un traitement en cours,
#   ou redemande sans raison. Deux faits par resultat, sans jamais conserver le texte :
#   - `state_fp` : empreinte de l'ETAT, calculee apres avoir neutralise heures, durees et compteurs de temps ecoule
#     (une reponse d'attente change a chaque appel par son horodatage, pas par son etat) ;
#   - `result_phase` : unavailable | in_progress | failed | done | unknown.
_TS_RE = _re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
                     r"|\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b|\b\d{10,13}(?:\.\d+)?\b")
_DUR_RE = _re.compile(r"(?i)\b\d+(?:[.,]\d+)?\s?(?:ms|us|ns|s|sec|secs|second|seconds|seconde|secondes|min|mins|minute|minutes"
                      r"|h|hr|hrs|hour|hours|heure|heures|d|day|days|jour|jours)\b")
_VOLATILE_KEY_RE = _re.compile(r"(?i)(?:^|[_\-.])(?:time|timestamp|ts|date|elapsed|duration|uptime|age|since|until|updated|created|"
                               r"modified|now|eta|ttl|expires?|expiry|checked|polled|heartbeat|waited|latency|"
                               r"ms|secs?|seconds|minutes|at)(?:$|[_\-.])")
_STATUS_KEYS = {"status", "state", "etat", "statut", "job_state", "jobstate", "phase", "result"}
_PROGRESS_VALUES = {"pending", "queued", "running", "in_progress", "inprogress", "configuring", "starting", "completing",
                    "requeued", "suspended", "waiting", "building", "compiling", "en_cours", "en_attente"}
_DONE_VALUES = {"completed", "complete", "done", "success", "succeeded", "finished", "ok", "ready", "available", "active"}
_FAILED_STATE_VALUES = {"failed", "failure", "error", "cancelled", "canceled", "timeout", "node_fail", "out_of_memory",
                        "oom", "preempted", "boot_fail", "deadline", "echec", "erreur"}
_UNAVAILABLE_FLAGS = {"available", "active", "reachable", "connected", "online", "up", "alive", "running_service"}
_MESSAGE_KEYS = {"error", "erreur", "message", "detail", "details", "reason", "raison", "status", "state", "etat", "statut"}
_UNAVAILABLE_RE = _re.compile(
    r"(?i)(connection (?:refused|aborted|reset|closed)|actively refused|no running|not (?:active|available|reachable|connected|"
    r"started|up)\b|\binactive\b|\bunavailable\b|indisponible|injoignable|\bunreachable\b|\boffline\b|hors ligne|econnrefused|"
    r"winerror 1006[01]|winerror 10053|n'est pas (?:actif|active|lanc|disponible|d[ée]marr|joignable|ouvert)|"
    r"pas encore (?:actif|active|disponible|pr[eê]t|lanc|d[ée]marr)|no .{0,20}answered|failed to (?:connect|attempt))")
_PROGRESS_RE = _re.compile(
    r"(?i)\b(pending|queued|in[ _-]progress|en cours|en file|en attente|configuring|compiling|building|"
    r"not (?:yet )?(?:finished|done|complete|completed|ready)|pas (?:encore )?(?:fini|termin[ée]e?|pr[eê]t)|"
    r"still (?:running|waiting|compiling|building|pending|in progress)|"
    r"(?:script|process|job|task|build|command|cell|compilation) (?:is )?(?:still )?running|running with (?:cell|session|pid))\b")
# * Outil dont le resultat decrit un etat : son nom le dit. Un ticket Linear "In Progress" relu n'est pas un job
#   en cours (constate le 2026-09-19 sur get_issue) : hors de ces noms, seule l'indisponibilite est lue.
_STATUS_TOOL_RE = _re.compile(r"(?i)(status|state|wait|poll|progress|watch|health|ready|check|job|task|queue|monitor|"
                              r"ping|alive|sleep|result)")
_WAIT_TOOL_RE = _re.compile(r"(?i)(wait|poll|status|progress|watch)")
_STATE_TEXT_CHARS = 50_000     # * au-dela, le debut suffit a distinguer deux etats ; borne le cout sur le chemin du hook
_PROBE_CHARS = 600             # * un message d'etat ou d'indisponibilite tient en tete (ou en fin) de sortie


def _state_value(v: Any, key: str) -> Any:
    if isinstance(v, str):
        return _DUR_RE.sub("<d>", _TS_RE.sub("<t>", v[:2000]))
    if isinstance(v, (int, float)) and not isinstance(v, bool) and _VOLATILE_KEY_RE.search(key):
        return "<n>"
    return v


def _state_json(o: Any, depth: int = 0, key: str = "") -> Any:
    if depth > 8:
        return "<...>"
    if isinstance(o, dict):
        return {str(k): _state_json(v, depth + 1, str(k)) for k, v in list(o.items())[:200]
                if not (_VOLATILE_KEY_RE.search(str(k)) and not isinstance(v, (dict, list, bool)))}
    if isinstance(o, list):
        return [_state_json(v, depth + 1, key) for v in o[:500]]
    return _state_value(o, key)


def _json_phase(o: Any, tool: str, depth: int = 0) -> str | None:
    """Phase lue dans un champ d'etat JSON (status, state, etat...), sinon None."""
    if depth > 4:
        return None
    if isinstance(o, dict):
        for k, v in list(o.items())[:80]:
            lk = str(k).lower()
            if lk in _STATUS_KEYS and isinstance(v, str):
                val = v.strip().lower().replace(" ", "_").replace("-", "_")
                if val in _FAILED_STATE_VALUES:
                    return "failed"
                if val in _PROGRESS_VALUES:
                    return "in_progress"
                if val in _DONE_VALUES:
                    return "done"
            if lk in ("timed_out", "timedout") and v is True and _WAIT_TOOL_RE.search(tool or ""):
                return "in_progress"   # * attente arrivee a echeance : ce qu'on attend n'est pas encore la
        for v in list(o.values())[:80]:
            if isinstance(v, (dict, list)):
                ph = _json_phase(v, tool, depth + 1)
                if ph:
                    return ph
    elif isinstance(o, list):
        for v in o[:30]:
            ph = _json_phase(v, tool, depth + 1)
            if ph:
                return ph
    return None


def _json_unavailable(o: Any, depth: int = 0) -> bool:
    """Indisponibilite declaree par un champ, jamais par un mot perdu dans le contenu : drapeau `available`,
    `connected`... a faux, ou message d'erreur/d'etat qui la decrit."""
    if depth > 3 or not isinstance(o, dict):
        return False
    for k, v in list(o.items())[:80]:
        lk = str(k).lower()
        if lk in _UNAVAILABLE_FLAGS and v is False:
            return True
        if lk in _MESSAGE_KEYS and isinstance(v, str) and _UNAVAILABLE_RE.search(v[:_PROBE_CHARS]):
            return True
        if isinstance(v, dict) and _json_unavailable(v, depth + 1):
            return True
    return False


_STATUS_HEADS = {"squeue", "sacct", "scontrol", "sinfo", "qstat", "docker", "kubectl", "get-process", "tasklist", "curl", "wget",
                 "invoke-webrequest", "invoke-restmethod", "iwr", "irm", "gh", "test-netconnection", "ping", "systemctl", "sc"}


def facts_kind(category: str | None, params: dict[str, Any] | None, tool: str | None = None) -> str | None:
    """Comment lire la phase d'un resultat : `status` (etat d'un service ou d'un traitement), `run` (execution :
    seule l'indisponibilite compte), `content` (contenu lu : phase tiree du seul statut), None (edition : rien)."""
    params = params or {}
    if category in (S.CAT_EDIT, S.CAT_WRITE):
        return None
    if category in (S.CAT_READ, S.CAT_SEARCH, S.CAT_LIST, S.CAT_WEB):
        return "content"
    if category == S.CAT_SHELL:
        heads = {str(h).lower() for h in params.get("shell_heads") or []}
        if heads & _STATUS_HEADS:
            return "status"
        return "content" if params.get("shell_kind") == "read" else "run"
    # * MCP, fonctions (attente d'un sous-agent), autres : etat si le nom de l'outil l'annonce.
    return "status" if _STATUS_TOOL_RE.search(tool or "") else "run"


def result_facts(text: str | None, status: str | None, fp: Any, tool: str | None = None, kind: str = "status") -> dict[str, Any]:
    """{"state_fp", "result_phase"} d'un resultat ; aucun texte conserve.

    # ! La phase n'est lue dans le texte que pour un resultat qui decrit un etat (`status`) ou une execution
    #   (`run`, indisponibilite seulement) : un fichier lu qui contient le mot "indisponible" n'est pas un service
    #   indisponible ; un journal de build termine qui contient "building" n'est pas un traitement en cours.
    #   Un contenu lu (`content`) n'a pas d'empreinte d'etat : son empreinte de contenu fait deja foi.
    """
    out: dict[str, Any] = {"state_fp": None, "result_phase": "unknown"}
    by_status = {S.STATUS_ERROR: "failed", S.STATUS_TIMEOUT: "failed", S.STATUS_DENIED: "failed",
                 S.STATUS_INTERRUPTED: "failed", S.STATUS_SUCCESS: "done"}.get(status or "", "unknown")
    if not isinstance(text, str) or kind == "content":
        out["result_phase"] = by_status
        return out
    head = text[:_STATE_TEXT_CHARS]
    obj: Any = None
    stripped = head.strip()
    if stripped[:1] in ("{", "[") and len(text) <= 200_000:
        import json
        try:
            obj = json.loads(stripped)
        except (ValueError, RecursionError):
            obj = None
    if obj is not None:
        norm = N.canonical_json(_state_json(obj))
        phase = _json_phase(obj, tool or "") if kind == "status" else None
        unavailable = _json_unavailable(obj)
    else:
        norm = " ".join(_DUR_RE.sub("<d>", _TS_RE.sub("<t>", head)).split())
        phase = None
        probe = head[:_PROBE_CHARS] + "\n" + text[-_PROBE_CHARS:]
        unavailable = bool(_UNAVAILABLE_RE.search(probe))
        if kind == "status" and _PROGRESS_RE.search(head[:_PROBE_CHARS]):
            phase = "in_progress"
    # * Sortie vide : un etat comme un autre (`git apply --check` reussi, attente sans sortie).
    out["state_fp"] = fp(norm)[:16] if norm.strip() else "empty"
    if unavailable:
        out["result_phase"] = "unavailable"
    elif phase == "failed" or by_status == "failed":
        out["result_phase"] = "failed"
    elif phase:
        out["result_phase"] = phase
    else:
        out["result_phase"] = by_status
    return out


def response_keys(resp: Any) -> list[str] | None:
    if isinstance(resp, dict):
        return sorted(str(k) for k in list(resp)[:40])
    if resp is None:
        return None
    return [f"<{type(resp).__name__}>"]
