"""Smoke test reel : lance Claude Code ou Codex dans un dossier temporaire avec les hooks
AgentWatch, puis resume ce qui a ete observe.

# ! Ce script utilise le compte deja connecte du client (aucune connexion n'est faite a
#   sa place) et consomme une petite quantite d'usage. Il ne modifie PAS la configuration
#   globale : Claude Code lit un settings.json de projet cree dans le dossier temporaire ;
#   Codex recoit ses hooks par des surcharges `-c` en memoire.

Usage : python tests/live_smoke.py --client claude-code|codex [--workdir DIR] [--home DIR] [--timeout 300]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentwatch.collector.store import EventStore  # noqa: E402
from agentwatch.config import load_config  # noqa: E402
from agentwatch.core.session import load_session  # noqa: E402
from agentwatch.installer import claude_code as IC  # noqa: E402
from agentwatch.installer import codex as IX  # noqa: E402

PROMPT = ("Do exactly these steps with tools, in order, then reply with the single word DONE. "
          "1) Read the file README.txt. "
          "2) Run the shell command: cat README.txt . "
          "3) Run the shell command: python does_not_exist.py  (it is expected to fail; do not retry, do not fix). "
          "4) Call the MCP tool echo_path from the server named awtest with path=README.txt. "
          "Do not do anything else.")


def find_codex() -> str | None:
    exe = shutil.which("codex")
    if exe:
        return exe
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    if base.is_dir():
        cands = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        if cands:
            return str(cands[0])
    return None


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run(client: str, workdir: Path, home: Path, timeout: int) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    # * Smoke test uniquement : extraits bornes actives pour documenter la forme reelle des reponses.
    (home / "config.json").write_text(json.dumps({"detailed_excerpts": True, "detailed_excerpt_chars": 600}), encoding="utf-8")
    (workdir / "README.txt").write_text("AgentWatch smoke test file.\n", encoding="utf-8")
    python = sys.executable
    entry = ROOT / "agentwatch" / "hook_entry.py"
    mcp_server = ROOT / "tests" / "mcp_test_server.py"
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SIMPLE"):
        env.pop(k, None)
    log_dir = workdir / "_logs"
    log_dir.mkdir(exist_ok=True)
    t0 = time.time()
    if client == "claude-code":
        settings = {"hooks": {}}
        for event in IC.EVENTS:
            settings["hooks"][event] = [{"matcher": "*", "hooks": [IC.hook_spec(python, entry, home)]}]
        (workdir / ".claude").mkdir(exist_ok=True)
        (workdir / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        mcp_cfg = workdir / "mcp.json"
        mcp_cfg.write_text(json.dumps({"mcpServers": {"awtest": {"command": python, "args": [str(mcp_server)]}}}), encoding="utf-8")
        cmd = ["claude", "-p", PROMPT, "--model", "haiku", "--max-turns", "8", "--output-format", "json",
               "--no-session-persistence", "--mcp-config", str(mcp_cfg),
               "--allowedTools", "Read,Bash(cat:*),Bash(python:*),mcp__awtest__echo_path"]
        exe = shutil.which("claude")
        if exe is None:
            return {"client": client, "status": "unavailable", "reason": "claude introuvable sur le PATH"}
        cmd[0] = exe
    else:
        exe = find_codex()
        if exe is None:
            return {"client": client, "status": "unavailable", "reason": "codex introuvable"}
        spec = IX.hook_spec(python, entry, home)
        hook_toml = ("[{matcher=\"*\",hooks=[{type=\"command\",command=" + toml_str(spec["command"]) +
                     ",commandWindows=" + toml_str(spec["commandWindows"]) + ",timeout=10}]}]")
        overrides = []
        for event in ("PreToolUse", "PostToolUse", "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "Interrupt"):
            overrides += ["-c", f"hooks.{event}={hook_toml}"]
        overrides += ["-c", f"mcp_servers.awtest.command={toml_str(python)}",
                      "-c", f"mcp_servers.awtest.args=[{toml_str(str(mcp_server))}]",
                      # * approbation automatique de l'outil de test (sinon refus en mode exec, approval_policy=never)
                      "-c", 'mcp_servers.awtest.default_tools_approval_mode="approve"',
                      "-c", 'model_reasoning_effort="low"']
        cmd = [exe, "exec", "--cd", str(workdir), "-s", "read-only", "--dangerously-bypass-hook-trust", *overrides, PROMPT]
        subprocess.run(["git", "init", "-q"], cwd=workdir, capture_output=True)
    (log_dir / "command.txt").write_text(" ".join(cmd), encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        # * Tuer tout l'arbre (serveur MCP, shells) sinon les tubes restent ouverts.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()
        out, err = proc.communicate()
        code, err = -1, (err or "") + f"\nTIMEOUT after {timeout}s"
    elapsed = round(time.time() - t0, 1)
    (log_dir / "stdout.txt").write_text(out or "", encoding="utf-8")
    (log_dir / "stderr.txt").write_text(err or "", encoding="utf-8")
    cfg = load_config(home)
    store = EventStore(home, cfg)
    sessions = list(store.iter_sessions())
    summary = {"client": client, "status": "ran", "exit_code": code, "elapsed_s": elapsed, "sessions": len(sessions),
               "stdout_tail": (out or "")[-600:], "stderr_tail": (err or "")[-600:], "diagnostics": store.read_diagnostics(20)}
    for c, skey, _ in sessions:
        view = load_session(store, c, skey, cfg)
        events, _w = store.read_session_events(c, skey)
        by_hook: dict[str, int] = {}
        keys_by_tool: dict[str, list] = {}
        for ev in events:
            by_hook[ev.get("hook_event_name") or "?"] = by_hook.get(ev.get("hook_event_name") or "?", 0) + 1
            if ev.get("phase") == "end":
                keys_by_tool.setdefault(ev.get("tool_name") or "?", ev.get("evidence", {}).get("tool_response_keys"))
                summary.setdefault("excerpts", []).append({"tool": ev.get("tool_name"), "status": ev.get("status"),
                                                           "excerpt": ev.get("evidence", {}).get("excerpt")})
        summary.setdefault("session_views", []).append({
            "session_id": view.session_id, "model": view.model, "events_by_hook": by_hook,
            "payload_keys_example": next((ev.get("evidence", {}).get("payload_keys") for ev in events if ev.get("phase") == "end"), None),
            "tool_response_keys_by_tool": keys_by_tool,
            "calls": [c2.summary() | {"exit_code": c2.exit_code, "warnings": c2.warnings} for c2 in view.calls],
            "hook_ms": sorted(view.hook_ms_samples),
        })
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True, choices=("claude-code", "codex"))
    ap.add_argument("--workdir")
    ap.add_argument("--home")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    base = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix=f"agentwatch-smoke-{args.client}-"))
    home = Path(args.home) if args.home else base / "_agentwatch_home"
    result = run(args.client, base, home, args.timeout)
    result["workdir"] = str(base)
    (base / "_logs" / "summary.json").parent.mkdir(exist_ok=True, parents=True)
    (base / "_logs" / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
