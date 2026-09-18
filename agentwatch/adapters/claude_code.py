"""Adaptateur Claude Code (hooks command, entree JSON sur stdin).

Reference verifiee : https://code.claude.com/docs/en/hooks (voir docs/compatibility.md).
Evenements consommes : PreToolUse, PostToolUse, PostToolUseFailure, SessionStart,
SessionEnd, UserPromptSubmit, Stop, PreCompact, PostCompact, SubagentStart, SubagentStop.
"""

from __future__ import annotations

import re
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE
from agentwatch.adapters import base
from agentwatch.core import normalize as N
from agentwatch.core import schema as S

_PHASES: dict[str, str] = {
    "PreToolUse": S.PHASE_START,
    "PostToolUse": S.PHASE_END,
    "PostToolUseFailure": S.PHASE_FAILURE,
    "SessionStart": S.PHASE_SESSION_START,
    "SessionEnd": S.PHASE_SESSION_END,
    "UserPromptSubmit": S.PHASE_TURN_START,
    "Stop": S.PHASE_TURN_END,
    "PreCompact": S.PHASE_COMPACT_START,
    "PostCompact": S.PHASE_COMPACT_END,
    "SubagentStart": S.PHASE_SUBAGENT_START,
    "SubagentStop": S.PHASE_SUBAGENT_STOP,
}

# * Parametres conserves en clair (apres masquage) par outil. Les autres cles ne sont
#   gardees que sous forme de nom + empreinte.
_PARAM_ALLOWLIST: dict[str, list[str]] = {
    "Read": ["offset", "limit", "pages"],
    "NotebookRead": ["cell_id"],
    "Grep": ["pattern", "path", "glob", "type", "output_mode", "-i", "-n", "-A", "-B", "-C",
             "context", "multiline", "head_limit", "offset", "-o"],
    "Glob": ["pattern", "path"],
    "LS": ["path", "ignore"],
    "Bash": ["timeout", "run_in_background"],
    "PowerShell": ["timeout", "run_in_background"],
    "Edit": ["replace_all"],
    "MultiEdit": [],
    "Write": [],
    "NotebookEdit": ["cell_id", "cell_type", "edit_mode"],
    "WebFetch": [],
    "WebSearch": ["allowed_domains", "blocked_domains"],
    "Agent": ["subagent_type", "model", "run_in_background", "isolation"],
    "Task": ["subagent_type", "model", "run_in_background"],
    "Skill": ["skill"],
    "ToolSearch": ["query", "max_results"],
    "BashOutput": ["bash_id", "shell_id"],
    "KillShell": ["shell_id"],
    "TaskOutput": ["task_id"],
    "TaskStop": ["task_id"],
}
_PATH_KEYS = ("file_path", "notebook_path", "path")
_DENIED_RE = re.compile(r"(?i)permission|denied|not allowed|rejected|refus|blocked by")
_TIMEOUT_RE = re.compile(r"(?i)\btime(?:d)?[ -]?out\b")


class ClaudeCodeAdapter:
    client = CLIENT_CLAUDE_CODE

    def parse_hook_payload(self, payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        ev = S.empty_event()
        ev["client"] = self.client
        name = payload.get("hook_event_name")
        ev["hook_event_name"] = name if isinstance(name, str) else None
        ev["phase"] = _PHASES.get(ev["hook_event_name"] or "", S.PHASE_UNKNOWN)
        ev["session_id"] = _str(payload.get("session_id"))
        ev["turn_id"] = _str(payload.get("prompt_id"))         # v2.1.196+ ; sinon None
        ev["agent_id"] = _str(payload.get("agent_id"))
        ev["agent_type"] = _str(payload.get("agent_type"))
        ev["cwd"] = _str(payload.get("cwd"))
        ev["project_dir"] = ctx.project_dir_env or ev["cwd"]
        ev["model"] = _str(payload.get("model"))
        ev["evidence"]["payload_keys"] = sorted(str(k) for k in list(payload)[:40])
        if "permission_mode" in payload:
            ev["evidence"]["permission_mode"] = _str(payload.get("permission_mode"))

        phase = ev["phase"]
        if phase in (S.PHASE_START, S.PHASE_END, S.PHASE_FAILURE):
            self._parse_tool(ev, payload, ctx)
        elif phase == S.PHASE_SESSION_START:
            ev["session_meta"] = {
                "start_type": _str(payload.get("session_start_type") or payload.get("source")),
                "model": ev["model"],
                "permission_mode": _str(payload.get("permission_mode")),
                # * Chemin du transcript (masque par ~ a l'ecriture) : sert a `import-transcripts` pour
                #   lire l'usage en tokens ; le transcript lui-meme n'est jamais lu par le hook.
                "transcript_path": _str(payload.get("transcript_path")),
            }
        elif phase == S.PHASE_SESSION_END:
            ev["session_meta"] = {"end_type": _str(payload.get("session_end_type") or payload.get("reason"))}
        elif phase == S.PHASE_TURN_START:
            prompt = payload.get("user_input", payload.get("prompt"))
            # ! Le contenu du prompt n'est jamais conserve : seulement sa longueur.
            ev["evidence"]["prompt_chars"] = len(prompt) if isinstance(prompt, str) else None
        elif phase in (S.PHASE_COMPACT_START, S.PHASE_COMPACT_END):
            ev["session_meta"] = {"compact_type": _str(payload.get("compact_type") or payload.get("trigger"))}
        elif phase in (S.PHASE_SUBAGENT_START, S.PHASE_SUBAGENT_STOP):
            ev["session_meta"] = {"agent_id": ev["agent_id"], "agent_type": ev["agent_type"]}
        elif phase == S.PHASE_UNKNOWN:
            ev["warnings"].append(f"hook event not mapped: {ev['hook_event_name']!r}")
        return ev

    # ------------------------------------------------------------------ outils
    def _parse_tool(self, ev: dict[str, Any], payload: dict[str, Any], ctx: Any) -> None:
        cfg = ctx.config
        tool = _str(payload.get("tool_name"))
        ev["tool_name"] = tool
        ev["call_id"] = _str(payload.get("tool_use_id"))
        if ev["call_id"] is None:
            ev["warnings"].append("tool_use_id missing; correlation will be heuristic")
        cat, server, mcp_tool = N.categorize_tool(self.client, tool)
        ev["tool_category"], ev["mcp_server"], ev["mcp_tool"] = cat, server, mcp_tool
        tool_input = payload.get("tool_input")
        if tool_input is None:
            ev["warnings"].append("tool_input missing")
        ev["input_fingerprint"] = {"method": S.FINGERPRINT_METHOD_HMAC,
                                   "value": ctx.fp(N.canonical_json({"t": tool, "i": tool_input}))}
        project, cwd = ev["project_dir"], ev["cwd"]

        if cat == S.CAT_SHELL:
            cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
            base.apply_shell_params(ev, cmd, cfg)
            ev["params"].update(base.pick_params(_without(tool_input, ("command", "description")),
                                                 _PARAM_ALLOWLIST.get(tool or "", []), ctx))
            if cfg.get("store_tool_descriptions") and isinstance(tool_input, dict):
                ev["params"]["description"] = _str(tool_input.get("description"))
        elif cat == S.CAT_MCP:
            ev["params"] = base.pick_params(tool_input, list(cfg.get("mcp_param_allowlist", [])), ctx,
                                            int(cfg.get("mcp_param_value_max_chars", 300)))
            p = base.first_path_like(tool_input, ("path", "file_path", "filePath", "file", "directory", "dir", "projectPath"))
            if p:
                ev["target"], ev["target_kind"] = N.normalize_path(p, project, cwd), "path"
            else:
                ev["target"], ev["target_kind"] = f"mcp:{server}/{mcp_tool}", "mcp"
        elif cat == S.CAT_WEB:
            url = base.first_path_like(tool_input, ("url",))
            ev["target"], ev["target_kind"] = N.strip_url(url), "url"
            if tool == "WebSearch" and isinstance(tool_input, dict):
                q = tool_input.get("query")
                ev["params"]["query_fp"] = ctx.fp(str(q))[:10] if q is not None else None
            ev["params"].update(base.pick_params(_without(tool_input, ("url", "query", "prompt")),
                                                 _PARAM_ALLOWLIST.get(tool or "", []), ctx))
        else:
            p = base.first_path_like(tool_input, _PATH_KEYS)
            if p:
                ev["target"], ev["target_kind"] = N.normalize_path(p, project, cwd), "path"
            elif tool in ("Grep", "Glob") and isinstance(tool_input, dict):
                ev["target"] = _str(tool_input.get("pattern"))
                ev["target_kind"] = "pattern"
            ev["params"].update(base.pick_params(_without(tool_input, _PATH_KEYS + ("old_string", "new_string", "content", "edits", "prompt", "description", "new_source")),
                                                 _PARAM_ALLOWLIST.get(tool or "", []), ctx))
            if tool == "Grep" and isinstance(tool_input, dict):
                # * Pour Grep, la cible est le chemin de recherche ; le motif reste un parametre.
                ev["params"]["pattern"] = _str(tool_input.get("pattern"))
                search_path = tool_input.get("path")
                ev["target"] = N.normalize_path(search_path, project, cwd) if isinstance(search_path, str) else "."
                ev["target_kind"] = "path"
            if tool == "Glob" and isinstance(tool_input, dict):
                ev["params"]["pattern"] = _str(tool_input.get("pattern"))
                search_path = tool_input.get("path")
                ev["target"] = N.normalize_path(search_path, project, cwd) if isinstance(search_path, str) else "."
                ev["target_kind"] = "path"
            self._edit_fingerprints(ev, tool, tool_input, ctx)

        if ev["target"] is None and ev["target_kind"] is None:
            ev["target_kind"] = "unknown"

        if ev["phase"] == S.PHASE_END:
            self._parse_response(ev, payload, ctx)
        elif ev["phase"] == S.PHASE_FAILURE:
            err = payload.get("error")
            ev["error_summary"] = err if isinstance(err, str) else (N.canonical_json(err) if err is not None else None)
            if payload.get("is_interrupt") is True:
                ev["status"] = S.STATUS_INTERRUPTED
            elif isinstance(err, str) and _TIMEOUT_RE.search(err):
                ev["status"] = S.STATUS_TIMEOUT
                ev["evidence"]["status_basis"] = "error text matched timeout pattern (heuristic)"
            elif isinstance(err, str) and _DENIED_RE.search(err):
                ev["status"] = S.STATUS_DENIED
                ev["evidence"]["status_basis"] = "error text matched permission pattern (heuristic)"
            else:
                ev["status"] = S.STATUS_ERROR
            ev["evidence"]["is_interrupt"] = payload.get("is_interrupt")

    def _edit_fingerprints(self, ev: dict[str, Any], tool: str | None, tool_input: Any, ctx: Any) -> None:
        if not isinstance(tool_input, dict):
            return
        if tool == "Edit":
            for key, label in (("old_string", "old"), ("new_string", "new")):
                v = tool_input.get(key)
                if isinstance(v, str):
                    ev["params"][f"{label}_chars"] = len(v)
                    ev["params"][f"{label}_fp"] = ctx.fp(v)[:10]
        elif tool == "Write":
            v = tool_input.get("content")
            if isinstance(v, str):
                ev["params"]["content_chars"] = len(v)
                ev["params"]["content_fp"] = ctx.fp(v)[:10]
        elif tool == "MultiEdit":
            edits = tool_input.get("edits")
            ev["params"]["edit_count"] = len(edits) if isinstance(edits, list) else None

    def _parse_response(self, ev: dict[str, Any], payload: dict[str, Any], ctx: Any) -> None:
        cfg = ctx.config
        resp = payload.get("tool_response")
        ev["evidence"]["tool_response_keys"] = base.response_keys(resp)
        if resp is None:
            ev["warnings"].append("tool_response missing")
            ev["status"] = S.STATUS_UNKNOWN
            return
        ev["output_size_bytes"] = N.size_of(resp)
        ev["output_size_source"] = "serialized_tool_response"
        if isinstance(resp, dict) and ev["tool_category"] in (S.CAT_EDIT, S.CAT_WRITE):
            # * Observe en direct (2.1.270) : la reponse d'Edit/Write recopie le fichier entier
            #   (originalFile, content, structuredPatch). Ce n'est pas une sortie lue par le
            #   modele : on la retranche du volume et on la consigne a part.
            echoed = sum(N.size_of(resp.get(k)) for k in ("originalFile", "content", "structuredPatch", "oldString", "newString") if k in resp)
            ev["evidence"]["echoed_file_bytes"] = echoed
            ev["output_size_bytes"] = max(0, ev["output_size_bytes"] - echoed)
            ev["output_size_source"] = "serialized_tool_response_minus_echoed_file"
        status, code, err, warns = base.status_from_response(resp)
        if status == S.STATUS_UNKNOWN:
            # * Documente : PostToolUse n'est emis qu'apres un appel reussi ; les echecs
            #   passent par PostToolUseFailure. On ne garde 'unknown' que si la reponse
            #   porte un indice contraire (isError, interrupted), traite ci-dessus.
            status, warns = S.STATUS_SUCCESS, []
            ev["evidence"]["status_basis"] = "PostToolUse implies success (documented); response shape not recognized"
        ev["status"], ev["exit_code"] = status, code
        ev["warnings"].extend(warns)
        if err:
            ev["error_summary"] = err
        if ev["tool_category"] == S.CAT_MCP and status == S.STATUS_SUCCESS and not err:
            hint = base.mcp_error_hint(resp)
            if hint:
                # * Le statut reste celui du client (succes) ; l'indice permet a l'analyse de
                #   traiter l'appel comme un echec probable et d'en tirer une signature.
                ev["evidence"]["error_hint"] = "mcp response text looks like an error although isError=false (heuristic)"
                ev["error_summary"] = hint[: int(cfg.get("max_error_chars", 400))]
        # * Observe en direct (2.1.270) : la cle est `duration_ms` ; la documentation cite `duration`.
        dur = payload.get("duration_ms", payload.get("duration"))
        if isinstance(dur, (int, float)) and not isinstance(dur, bool):
            ev["duration_ms"], ev["duration_source"] = int(dur), "client"
        elif isinstance(resp, dict) and isinstance(resp.get("durationMs"), (int, float)):
            # * Observe en direct sur Glob (2.1.270) ; documente aussi pour certains outils MCP.
            ev["duration_ms"], ev["duration_source"] = int(resp["durationMs"]), "client_durationMs"
        if isinstance(resp, dict) and ev["tool_name"] in ("Agent", "Task"):
            self._parse_agent_response(ev, resp)
        if isinstance(resp, dict):
            ev["output_truncated"] = resp.get("truncated") if isinstance(resp.get("truncated"), bool) else None
            if ev["tool_category"] == S.CAT_SHELL:
                so, se = resp.get("stdout"), resp.get("stderr")
                ev["evidence"]["stdout_chars"] = len(so) if isinstance(so, str) else None
                ev["evidence"]["stderr_chars"] = len(se) if isinstance(se, str) else None
            if ev["tool_name"] == "Read":
                # ? Deux formes observees/documentees : champs a plat, ou imbriques sous "file".
                info = resp.get("file") if isinstance(resp.get("file"), dict) else resp
                for k in ("numLines", "startLine", "totalLines"):
                    if isinstance(info.get(k), int):
                        ev["evidence"][k] = info[k]
                content = info.get("content")
                ev["resource_state"] = {
                    "revision": ctx.fp(content)[:16] if isinstance(content, str) else None,
                    "source": "tool_response_content_hash",
                    "freshness": "at_call",
                }
            names = resp.get("filenames")
            if isinstance(names, list):
                ev["result_paths"] = base.bounded_paths(names, ev["project_dir"], ev["cwd"], int(cfg.get("max_result_paths", 200)))
                ev["evidence"]["result_count"] = len(names)
            elif isinstance(resp.get("numFiles"), int):
                ev["evidence"]["result_count"] = resp["numFiles"]
        # * Empreinte du resultat : sur le JSON canonique complet (comparaison d'egalite).
        ev["result_fingerprint"] = {"method": S.FINGERPRINT_METHOD_HMAC, "value": ctx.fp(N.canonical_json(resp))}
        if ev["tool_category"] in (S.CAT_READ, S.CAT_SEARCH, S.CAT_LIST, S.CAT_SHELL, S.CAT_MCP):
            ev["content_fingerprint"] = base.content_fingerprint(base.primary_text(resp, ev["tool_name"]), ctx.fp)
        if cfg.get("detailed_excerpts"):
            text = _excerpt_source(resp)
            if text:
                ev["evidence"]["excerpt"] = text[: int(cfg.get("detailed_excerpt_chars", 240))]


    def _parse_agent_response(self, ev: dict[str, Any], resp: dict[str, Any]) -> None:
        """Reponse de l'outil Agent, observee en direct sur 2.1.270 : identifiant et type du
        sous-agent lance, modele resolu, statut, duree et usage rapportes par le client.

        # * Ni `prompt` ni `content` ne sont conserves : uniquement des identifiants et des nombres.
        # * L'usage est etiquete scope=agent, source=reponse de l'outil : ce n'est pas une mesure
        #   d'AgentWatch et il ne couvre pas le fil principal.
        """
        aid = resp.get("agentId")
        ev["evidence"]["spawned_agent_id"] = aid if isinstance(aid, str) else None
        ev["evidence"]["spawned_agent_type"] = _str(resp.get("agentType"))
        ev["evidence"]["spawned_agent_model"] = _str(resp.get("resolvedModel"))
        ev["evidence"]["spawned_agent_status"] = _str(resp.get("status"))
        for key, label in (("totalDurationMs", "spawned_agent_duration_ms"), ("totalToolUseCount", "spawned_agent_tool_calls")):
            v = resp.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                ev["evidence"][label] = int(v)
        stats = resp.get("toolStats")
        if isinstance(stats, dict):
            ev["evidence"]["spawned_agent_tool_stats"] = {str(k): v for k, v in list(stats.items())[:30]
                                                          if isinstance(v, (int, float)) and not isinstance(v, bool)}
        raw_usage = resp.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        total = resp.get("totalTokens")
        if usage or isinstance(total, int):
            ev["usage"] = {
                "scope": "agent", "source": "claude-code:Agent.tool_response", "agent_id": ev["evidence"]["spawned_agent_id"],
                "input_tokens": _first_int(usage, "input_tokens", "inputTokens"),
                "output_tokens": _first_int(usage, "output_tokens", "outputTokens"),
                "cache_read_tokens": _first_int(usage, "cache_read_input_tokens", "cache_read_tokens", "cacheReadInputTokens"),
                "cache_creation_tokens": _first_int(usage, "cache_creation_input_tokens", "cache_creation_tokens", "cacheCreationInputTokens"),
                "total_tokens": total if isinstance(total, int) and not isinstance(total, bool) else None,
                "raw_keys": sorted(str(k) for k in usage)[:20],
            }


def _first_int(d: dict[str, Any], *keys: str) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return None


def _excerpt_source(resp: Any) -> str | None:
    if isinstance(resp, dict):
        for k in ("stdout", "content", "text", "output"):
            v = resp.get(k)
            if isinstance(v, str):
                return v
    if isinstance(resp, str):
        return resp
    return None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v != "" else None


def _without(d: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if k not in keys}
