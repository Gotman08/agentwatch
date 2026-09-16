"""Adaptateur Codex (hooks command, entree JSON sur stdin).

Reference verifiee : https://developers.openai.com/codex/hooks (redirige vers
https://learn.chatgpt.com/docs/hooks) ; voir docs/compatibility.md.
Evenements consommes : PreToolUse, PostToolUse, SessionStart, SessionEnd,
UserPromptSubmit, Stop, Interrupt, PreCompact, PostCompact, SubagentStart, SubagentStop.
# ! Codex n'a pas de PostToolUseFailure : l'echec doit etre lu dans tool_response,
#   dont la forme n'est pas entierement documentee. Le statut reste 'unknown' sans indice.
"""

from __future__ import annotations

import re
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

from agentwatch import CLIENT_CODEX
from agentwatch.adapters import base
from agentwatch.core import normalize as N
from agentwatch.core import schema as S

_PHASES: dict[str, str] = {
    "PreToolUse": S.PHASE_START,
    "PostToolUse": S.PHASE_END,
    "SessionStart": S.PHASE_SESSION_START,
    "SessionEnd": S.PHASE_SESSION_END,
    "UserPromptSubmit": S.PHASE_TURN_START,
    "Stop": S.PHASE_TURN_END,
    "Interrupt": S.PHASE_INTERRUPT,
    "PreCompact": S.PHASE_COMPACT_START,
    "PostCompact": S.PHASE_COMPACT_END,
    "SubagentStart": S.PHASE_SUBAGENT_START,
    "SubagentStop": S.PHASE_SUBAGENT_STOP,
}
_PARAM_ALLOWLIST: dict[str, list[str]] = {
    "Bash": ["timeout_ms", "workdir", "cwd", "with_escalated_permissions", "login", "yield_time_ms", "max_output_tokens"],
    "shell": ["timeout_ms", "workdir", "cwd", "with_escalated_permissions"],
    "local_shell": ["timeout_ms", "workdir", "cwd"],
    "exec_command": ["timeout_ms", "workdir", "cwd", "yield_time_ms", "max_output_tokens", "shell", "login"],
    "shell_command": ["timeout_ms", "workdir", "cwd"],
    "apply_patch": [],
    "Edit": [], "Write": [],
    "Read": ["offset", "limit"], "read_file": ["offset", "limit"],
    "view_image": [],
    "Grep": ["pattern", "path", "glob", "include"], "Glob": ["pattern", "path"], "list_dir": ["path"],
    "update_plan": [], "web_search": [], "spawn_agent": ["agent_type", "model"],
    "write_stdin": ["session_id", "yield_time_ms"],
}
_SHELL_CMD_KEYS = ("command", "cmd", "commands")
_PATH_KEYS = ("file_path", "path", "filePath", "file")
_EXIT_IN_OUTPUT_RE = re.compile(r"(?im)^\s*(?:process\s+)?exit(?:ed)?(?:\s+with)?(?:\s+code)?\s*[:=]?\s*(-?\d+)\s*$")
# * Verifie en direct (codex-cli 0.154.0-alpha.6.2) : tool_response d'une commande shell est une
#   simple chaine (sortie) sans code de sortie. On ne fabrique pas de statut ; on pose seulement
#   un indice textuel d'erreur, exploite avec une confiance degradee par le detecteur B.
_ERROR_HINT_RE = re.compile(
    r"(?im)^.*\b(?:Traceback \(most recent call last\)|[A-Za-z]*Error\b|error:|exception|No such file or directory|"
    r"not found|is not recognized as|cannot (?:open|find|access|stat)|permission denied|command not found|failed)\b.*$")


class CodexAdapter:
    client = CLIENT_CODEX

    def parse_hook_payload(self, payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        ev = S.empty_event()
        ev["client"] = self.client
        name = payload.get("hook_event_name")
        ev["hook_event_name"] = name if isinstance(name, str) else None
        ev["phase"] = _PHASES.get(ev["hook_event_name"] or "", S.PHASE_UNKNOWN)
        ev["session_id"] = _str(payload.get("session_id") or payload.get("thread_id"))
        ev["turn_id"] = _str(payload.get("turn_id"))
        ev["agent_id"] = _str(payload.get("agent_id"))
        ev["agent_type"] = _str(payload.get("agent_type"))
        ev["cwd"] = _str(payload.get("cwd"))
        ev["project_dir"] = ctx.project_dir_env or ev["cwd"]   # ? Codex n'expose pas de racine projet
        ev["model"] = _str(payload.get("model"))
        ev["evidence"]["payload_keys"] = sorted(str(k) for k in list(payload)[:40])
        if "permission_mode" in payload:
            ev["evidence"]["permission_mode"] = _str(payload.get("permission_mode"))

        phase = ev["phase"]
        if phase in (S.PHASE_START, S.PHASE_END):
            self._parse_tool(ev, payload, ctx)
        elif phase == S.PHASE_SESSION_START:
            ev["session_meta"] = {"start_type": _str(payload.get("source")), "model": ev["model"],
                                  "permission_mode": _str(payload.get("permission_mode"))}
        elif phase == S.PHASE_SESSION_END:
            ev["session_meta"] = {"end_type": _str(payload.get("reason") or payload.get("source"))}
        elif phase == S.PHASE_TURN_START:
            prompt = payload.get("prompt")
            ev["evidence"]["prompt_chars"] = len(prompt) if isinstance(prompt, str) else None
        elif phase in (S.PHASE_TURN_END, S.PHASE_INTERRUPT):
            ev["evidence"]["stop_hook_active"] = payload.get("stop_hook_active")
        elif phase in (S.PHASE_COMPACT_START, S.PHASE_COMPACT_END):
            ev["session_meta"] = {"compact_type": _str(payload.get("trigger") or payload.get("compact_type"))}
        elif phase in (S.PHASE_SUBAGENT_START, S.PHASE_SUBAGENT_STOP):
            ev["session_meta"] = {"agent_id": ev["agent_id"], "agent_type": ev["agent_type"]}
        elif phase == S.PHASE_UNKNOWN:
            ev["warnings"].append(f"hook event not mapped: {ev['hook_event_name']!r}")
        return ev

    def _parse_tool(self, ev: dict[str, Any], payload: dict[str, Any], ctx: Any) -> None:
        cfg = ctx.config
        tool = _str(payload.get("tool_name"))
        ev["tool_name"] = tool
        ev["call_id"] = _str(payload.get("tool_use_id") or payload.get("call_id"))
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
        allowed = _PARAM_ALLOWLIST.get(tool or "", [])

        if cat == S.CAT_SHELL:
            cmd = None
            if isinstance(tool_input, dict):
                for k in _SHELL_CMD_KEYS:
                    if k in tool_input:
                        cmd = tool_input[k]
                        break
            base.apply_shell_params(ev, cmd, cfg)
            ev["params"].update(base.pick_params(_without(tool_input, _SHELL_CMD_KEYS + ("justification",)), allowed, ctx))
        elif tool == "apply_patch":
            patch = None
            if isinstance(tool_input, dict):
                for k in ("command", "patch", "input"):
                    if isinstance(tool_input.get(k), str):
                        patch = tool_input[k]
                        break
            paths = [N.normalize_path(p, project, cwd) for p in N.extract_patch_paths(patch)]
            paths = [p for p in paths if p]
            ev["params"]["patch_paths"] = paths
            ev["params"]["patch_chars"] = len(patch) if isinstance(patch, str) else None
            ev["params"]["patch_fp"] = ctx.fp(patch)[:10] if isinstance(patch, str) else None
            ev["target"] = paths[0] if len(paths) == 1 else (f"patch:{len(paths)} files" if paths else None)
            ev["target_kind"] = "path" if len(paths) == 1 else "patch"
        elif cat == S.CAT_MCP:
            ev["params"] = base.pick_params(tool_input, list(cfg.get("mcp_param_allowlist", [])), ctx,
                                            int(cfg.get("mcp_param_value_max_chars", 300)))
            p = base.first_path_like(tool_input, ("path", "file_path", "filePath", "file", "directory", "dir", "projectPath"))
            if p:
                ev["target"], ev["target_kind"] = N.normalize_path(p, project, cwd), "path"
            else:
                ev["target"], ev["target_kind"] = f"mcp:{server}/{mcp_tool}", "mcp"
        else:
            p = base.first_path_like(tool_input, _PATH_KEYS)
            if p:
                ev["target"], ev["target_kind"] = N.normalize_path(p, project, cwd), "path"
            ev["params"].update(base.pick_params(_without(tool_input, _PATH_KEYS + ("content", "old_string", "new_string", "query")), allowed, ctx))
            if isinstance(tool_input, dict):
                for key, label in (("content", "content"), ("old_string", "old"), ("new_string", "new")):
                    v = tool_input.get(key)
                    if isinstance(v, str):
                        ev["params"][f"{label}_chars"] = len(v)
                        ev["params"][f"{label}_fp"] = ctx.fp(v)[:10]
        if ev["target"] is None and ev["target_kind"] is None:
            ev["target_kind"] = "unknown"

        if ev["phase"] == S.PHASE_END:
            self._parse_response(ev, payload, ctx)

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
        status, code, err, warns = base.status_from_response(resp)
        if status == S.STATUS_UNKNOWN and ev["tool_category"] == S.CAT_SHELL:
            # ? Repli : certains formats placent le code de sortie dans le texte de sortie.
            text = _output_text(resp)
            m = _EXIT_IN_OUTPUT_RE.search(text[-400:]) if text else None
            if m:
                code = int(m.group(1))
                status = S.STATUS_SUCCESS if code == 0 else S.STATUS_ERROR
                ev["evidence"]["exit_code_source"] = "parsed_from_output_text (heuristic)"
                warns = [w for w in warns if "status unknown" not in w]
        ev["status"], ev["exit_code"] = status, code
        ev["warnings"].extend(warns)
        if err:
            ev["error_summary"] = err
        elif status == S.STATUS_ERROR:
            text = _output_text(resp)
            if text:
                ev["error_summary"] = text[-int(cfg.get("max_error_chars", 400)):]
        elif status == S.STATUS_UNKNOWN and ev["tool_category"] == S.CAT_SHELL:
            text = _output_text(resp)
            m = _ERROR_HINT_RE.search(text[-2000:]) if text else None
            if m:
                ev["evidence"]["error_hint"] = "output text matched an error pattern (heuristic; exit status not exposed by client)"
                ev["error_summary"] = m.group(0).strip()[-int(cfg.get("max_error_chars", 400)):]
        if isinstance(resp, dict):
            dur = None
            for k in ("duration_ms", "durationMs", "duration"):
                if isinstance(resp.get(k), (int, float)) and not isinstance(resp.get(k), bool):
                    dur = int(resp[k])
                    break
            if dur is not None:
                ev["duration_ms"], ev["duration_source"] = dur, "client"
            ev["output_truncated"] = resp.get("truncated") if isinstance(resp.get("truncated"), bool) else None
        ev["result_fingerprint"] = {"method": S.FINGERPRINT_METHOD_HMAC, "value": ctx.fp(N.canonical_json(resp))}
        if cfg.get("detailed_excerpts"):
            text = _output_text(resp)
            if text:
                ev["evidence"]["excerpt"] = text[: int(cfg.get("detailed_excerpt_chars", 240))]


def _output_text(resp: Any) -> str | None:
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        for k in ("output", "stdout", "aggregated_output", "text", "content", "result"):
            v = resp.get(k)
            if isinstance(v, str):
                return v
            if isinstance(v, list):
                parts = [i.get("text") for i in v if isinstance(i, dict) and isinstance(i.get("text"), str)]
                if parts:
                    return "\n".join(parts)
    return None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v != "" else None


def _without(d: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if k not in keys}
