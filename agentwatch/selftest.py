"""Scenarios synthetiques et auto-test.

# * Les payloads construits ici sont SYNTHETIQUES : ecrits a la main d'apres la
#   documentation officielle des hooks, ils ne sont pas des captures reelles.
# * Rejouer = reanalyser des donnees. Aucune commande contenue n'est executee.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX
from agentwatch.collector.ingest import ingest_payload
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core import schema as S
from agentwatch.core.correlate import SessionView, build_session
from agentwatch.detectors import run_detectors
from agentwatch.detectors.base import Finding

SYNTHETIC_NOTE = "SYNTHETIC: hand-written from official hook documentation, not a live capture"


class Synth:
    """Constructeur de sessions synthetiques (Claude Code ou Codex) avec horloge controlee."""

    def __init__(self, home: Path, client: str = CLIENT_CLAUDE_CODE, session_id: str = "synthetic-session",
                 cwd: str | None = None, cfg: dict[str, Any] | None = None) -> None:
        self.home = home
        self.client = client
        self.session_id = session_id
        self.cwd = cwd or (r"C:\proj" if os.name == "nt" else "/proj")
        self.cfg = cfg or load_config(home)
        self.t = 1_800_000_000_000_000_000
        self.n = 0
        self.turn = 0
        self.agent: tuple[str | None, str | None] = (None, None)
        self.payloads: list[dict[str, Any]] = []
        # * Temps de reponse du modele entre le resultat d'un appel et le debut du suivant. Une valeur
        #   courte (50 ms) simule des appels emis dans une meme reponse et executes en serie par le client.
        self.response_gap_ms = 3000

    # ---------------------------------------------------------------- horloge
    def tick(self, ms: int) -> None:
        self.t += ms * 1_000_000

    def _base(self, event: str, **extra: Any) -> dict[str, Any]:
        p: dict[str, Any] = {"session_id": self.session_id, "cwd": self.cwd, "hook_event_name": event}
        if self.client == CLIENT_CODEX:
            p["model"] = "synthetic-model"
            if self.turn:
                p["turn_id"] = f"turn_{self.turn}"
        if self.agent[0]:
            p["agent_id"], p["agent_type"] = self.agent
        p.update(extra)
        return p

    def emit(self, payload: dict[str, Any], gap_ms: int = 40) -> None:
        self.payloads.append(payload)
        ingest_payload(payload, self.client, self.home, self.cfg, source=S.SOURCE_REPLAY, started_ns=self.t)
        self.tick(gap_ms)

    def _abs(self, path: str) -> str:
        if os.path.isabs(path) or (len(path) > 1 and path[1] == ":"):
            return path
        return os.path.join(self.cwd, path)

    def _call_id(self) -> str:
        self.n += 1
        return f"toolu_{self.n:04d}" if self.client == CLIENT_CLAUDE_CODE else f"call_{self.n:04d}"

    # ---------------------------------------------------------------- session
    def session_start(self, start_type: str = "startup") -> None:
        if self.client == CLIENT_CLAUDE_CODE:
            self.emit(self._base("SessionStart", session_start_type=start_type, model="synthetic-model"))
        else:
            self.emit(self._base("SessionStart", source=start_type))

    def user_prompt(self, text: str = "synthetic prompt") -> None:
        self.turn += 1
        if self.client == CLIENT_CLAUDE_CODE:
            self.emit(self._base("UserPromptSubmit", user_input=text))
        else:
            self.emit(self._base("UserPromptSubmit", prompt=text))

    def stop(self) -> None:
        self.emit(self._base("Stop", stop_hook_active=False, last_assistant_message="ok"))

    def compact(self) -> None:
        if self.client == CLIENT_CLAUDE_CODE:
            self.emit(self._base("PreCompact", compact_type="auto"))
            self.emit(self._base("PostCompact", compact_type="auto"))
        else:
            self.emit(self._base("PreCompact", trigger="auto"))
            self.emit(self._base("PostCompact", trigger="auto"))

    def set_agent(self, agent_id: str | None, agent_type: str | None = "Explore") -> None:
        self.agent = (agent_id, agent_type if agent_id else None)

    # ---------------------------------------------------------------- appels generiques
    def call(self, tool: str, tool_input: dict[str, Any], response: Any = None, *, fail: str | None = None,
             interrupt: bool = False, duration_ms: int = 120, exit_code: int | None = None,
             call_id: str | None = None, no_end: bool = False) -> str:
        cid = call_id or self._call_id()
        self.emit(self._base("PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=cid), gap_ms=duration_ms)
        if no_end:
            return cid
        if self.client == CLIENT_CLAUDE_CODE:
            if fail is not None or interrupt:
                self.emit(self._base("PostToolUseFailure", tool_name=tool, tool_input=tool_input, tool_use_id=cid,
                                     error=fail or "interrupted", is_interrupt=interrupt), gap_ms=self.response_gap_ms)
            else:
                self.emit(self._base("PostToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=cid, tool_response=response),
                          gap_ms=self.response_gap_ms)
        else:
            resp = response if isinstance(response, dict) else {"output": response if isinstance(response, str) else json.dumps(response)}
            if fail is not None:
                resp = {"output": fail, "exit_code": exit_code if exit_code is not None else 1}
            elif exit_code is not None:
                resp = dict(resp, exit_code=exit_code)
            self.emit(self._base("PostToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=cid, tool_response=resp),
                      gap_ms=self.response_gap_ms)
        return cid

    # ---------------------------------------------------------------- raccourcis Claude Code
    def read(self, path: str, content: str = "content", offset: int | None = None, limit: int | None = None, **kw: Any) -> str:
        ti: dict[str, Any] = {"file_path": self._abs(path)}
        if offset is not None:
            ti["offset"] = offset
        if limit is not None:
            ti["limit"] = limit
        resp = {"type": "text", "file": {"filePath": self._abs(path), "content": content, "numLines": content.count("\n") + 1,
                                          "startLine": offset or 1, "totalLines": content.count("\n") + 1}}
        return self.call("Read", ti, resp, **kw)

    def grep(self, pattern: str, path: str = ".", filenames: list[str] | None = None, **kw: Any) -> str:
        names = [self._abs(f) for f in (filenames or [])]
        return self.call("Grep", {"pattern": pattern, "path": self._abs(path), "output_mode": "files_with_matches"},
                         {"mode": "files_with_matches", "filenames": names, "numFiles": len(names)}, **kw)

    def glob(self, pattern: str, path: str = ".", filenames: list[str] | None = None, **kw: Any) -> str:
        names = [self._abs(f) for f in (filenames or [])]
        return self.call("Glob", {"pattern": pattern, "path": self._abs(path)},
                         {"filenames": names, "durationMs": 5, "numFiles": len(names), "truncated": False}, **kw)

    def bash(self, command: str, stdout: str = "", stderr: str = "", **kw: Any) -> str:
        if self.client == CLIENT_CODEX:
            return self.call("Bash", {"command": command}, {"output": stdout + stderr}, **kw)
        return self.call("Bash", {"command": command, "description": "synthetic"},
                         {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False}, **kw)

    def edit(self, path: str, old: str, new: str, **kw: Any) -> str:
        if self.client == CLIENT_CODEX:
            patch = f"*** Begin Patch\n*** Update File: {path}\n@@\n-{old}\n+{new}\n*** End Patch\n"
            return self.call("apply_patch", {"command": patch}, {"output": f"Success. Updated the following files:\nM {path}\n"}, exit_code=0, **kw)
        return self.call("Edit", {"file_path": self._abs(path), "old_string": old, "new_string": new},
                         {"filePath": self._abs(path), "oldString": old, "newString": new, "originalFile": "x", "structuredPatch": []}, **kw)

    def write(self, path: str, content: str, **kw: Any) -> str:
        return self.call("Write", {"file_path": self._abs(path), "content": content},
                         {"type": "create", "filePath": self._abs(path), "content": content, "structuredPatch": []}, **kw)

    def mcp(self, server: str, tool: str, tool_input: dict[str, Any], text: str = "ok", **kw: Any) -> str:
        return self.call(f"mcp__{server}__{tool}", tool_input, [{"type": "text", "text": text}], **kw)

    # ---------------------------------------------------------------- sous-agents (Claude Code)
    def agent_run(self, agent_type: str, body: Any, agent_id: str = "agent-synth", *, with_agent_id: bool = True,
                  usage: dict[str, Any] | None = None, start_marker: bool = True, background: bool = False) -> str:
        """Appel Agent du fil principal encadrant un sous-agent : SubagentStart, appels de `body`,
        SubagentStop, puis PostToolUse de l'outil Agent avec la forme de reponse observee sur 2.1.270."""
        cid = self._call_id()
        tool_input = {"subagent_type": agent_type, "prompt": "synthetic", "run_in_background": background}
        self.emit(self._base("PreToolUse", tool_name="Agent", tool_input=tool_input, tool_use_id=cid), gap_ms=30)
        if start_marker:
            self.emit(self._base("SubagentStart", agent_id=agent_id, agent_type=agent_type))
        previous = self.agent
        self.set_agent(agent_id, agent_type)
        body()
        self.agent = previous
        self.emit(self._base("SubagentStop", agent_id=agent_id, agent_type=agent_type, stop_hook_active=False, last_assistant_message="done"))
        resp: dict[str, Any] = {"agentType": agent_type, "content": "synthetic result", "status": "completed",
                                "resolvedModel": "synthetic-sub-model", "totalDurationMs": 900, "totalToolUseCount": 2,
                                "toolStats": {"Read": 2}, "prompt": "synthetic"}
        if with_agent_id:
            resp["agentId"] = agent_id
        if usage:
            resp["usage"] = usage
            resp["totalTokens"] = sum(v for v in usage.values() if isinstance(v, int))
        self.emit(self._base("PostToolUse", tool_name="Agent", tool_input=tool_input, tool_use_id=cid, tool_response=resp, duration_ms=1000))
        return cid

    # ---------------------------------------------------------------- vue
    def view(self) -> SessionView:
        store = EventStore(self.home, self.cfg)
        events, _ = store.read_session_events(self.client, store.session_dir(self.client, self.session_id).name)
        return build_session(events, self.cfg)

    def findings(self, only: list[str] | None = None) -> list[Finding]:
        return run_detectors(self.view(), self.cfg, only=only)


# ---------------------------------------------------------------------- scenarios
def scenario_results(home: Path) -> list[dict[str, Any]]:
    """Execute les scenarios de reference et retourne [{name, expected, ok, detail}]."""
    results: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append({"name": name, "ok": ok, "detail": detail})

    # 1. lecture repetee sur etat inchange -> signalement A
    s = Synth(home / "s1", session_id="s1")
    s.session_start(); s.user_prompt()
    s.read("a.py", "print(1)\n"); s.bash("git status", "clean\n"); s.read("a.py", "print(1)\n")
    fa = [f for f in s.findings(["redundant_reads"])]
    check("A: relecture identique signalee", len(fa) == 1 and fa[0].confidence == "high", f"{len(fa)} signalement(s) {[f.confidence for f in fa]}")

    # 2. lecture apres modification / autre agent / apres compaction -> pas de faux doublon
    s = Synth(home / "s2", session_id="s2")
    s.session_start(); s.user_prompt()
    s.read("b.py", "v1\n"); s.edit("b.py", "v1", "v2"); s.read("b.py", "v2\n")
    s.set_agent("agent-1"); s.read("b.py", "v2\n"); s.set_agent(None)
    s.compact(); s.read("b.py", "v2\n")
    s.read("b.py", "v2\n", offset=10, limit=20)
    fa = s.findings(["redundant_reads"])
    check("A: modification/agent/compaction/plage non signales", len(fa) == 0, f"{len(fa)} signalement(s)")

    # 3. erreur persistante vs transitoire corrigee
    s = Synth(home / "s3", session_id="s3")
    s.session_start(); s.user_prompt()
    for _ in range(3):
        s.bash("python run.py", fail="Exit code 1: ModuleNotFoundError: No module named 'x'")
    fb = s.findings(["error_loops"])
    persistent = [f for f in fb if f.kind == "persistent"]
    s2 = Synth(home / "s3b", session_id="s3b")
    s2.session_start(); s2.user_prompt()
    s2.bash("python run.py", fail="Exit code 1: No such file or directory: cfg.json")
    s2.write("cfg.json", "{}")
    s2.bash("python run.py", fail="Exit code 1: No such file or directory: cfg.json")
    s2.bash("python run.py", "ok\n")
    fb2 = s2.findings(["error_loops"])
    check("B: persistante signalee, corrigee non signalee", len(persistent) == 1 and persistent[0].confidence == "high" and len(fb2) == 0,
          f"persistent={len(persistent)} corrigee={len(fb2)}")

    # 4. regroupable vs dependant
    s = Synth(home / "s4", session_id="s4")
    s.session_start(); s.user_prompt()
    s.glob("src/*.py", filenames=["src/a.py", "src/b.py", "src/c.py"])
    s.read("src/a.py", "A"); s.read("src/b.py", "B"); s.read("src/c.py", "C")
    fc = s.findings(["batchable"])
    s2 = Synth(home / "s4b", session_id="s4b")
    s2.session_start(); s2.user_prompt()
    s2.read("x.py", "X"); s2.edit("x.py", "X", "Y"); s2.read("y.py", "Y"); s2.edit("y.py", "Y", "Z"); s2.read("z.py", "Z")
    fc2 = s2.findings(["batchable"])
    check("C: lectures issues d'un listage signalees (high), lectures entrelacees d'editions non signalees",
          len(fc) == 1 and fc[0].confidence == "high" and len(fc2) == 0, f"batch={len(fc)} interleaved={len(fc2)}")

    # 5. motif mecanique recurrent vs sequence avec decisions
    s = Synth(home / "s5", session_id="s5")
    s.session_start(); s.user_prompt()
    for name in ("m1", "m2", "m3", "m4"):
        s.bash(f"python export.py data/{name}.csv", "ok\n"); s.read(f"out/{name}.json", "{}"); s.bash(f"python check.py out/{name}.json", "ok\n")
    fd = s.findings(["automation_candidates"])
    s2 = Synth(home / "s5b", session_id="s5b")
    s2.session_start(); s2.user_prompt()
    for i, name in enumerate(("m1", "m2", "m3")):
        s2.read(f"src/{name}.py", f"code{i}"); s2.edit(f"src/{name}.py", f"code{i}", f"fixed{i}"); s2.bash(f"pytest tests/test_{name}.py", "ok\n", fail=("failed" if i == 1 else None))
    fd2 = s2.findings(["automation_candidates"])
    mech = [f for f in fd if f.confidence == "high"]
    judg = [f for f in fd2 if f.confidence != "high"]
    check("D: motif mecanique en confiance haute, motif avec decisions degrade",
          len(mech) >= 1 and len(fd2) >= 1 and len(judg) == len(fd2), f"mech={[f.confidence for f in fd]} judg={[f.confidence for f in fd2]}")

    # 6. pre/post/mesure = un appel ; deux appels identiques = deux appels
    s = Synth(home / "s6", session_id="s6")
    s.session_start(); s.user_prompt()
    cid = s.read("one.py", "1")
    s.emit(s._base("PostToolUse", tool_name="Read", tool_input={"file_path": s._abs("one.py")}, tool_use_id=cid,
                   tool_response={"type": "text", "file": {"filePath": s._abs("one.py"), "content": "1"}}))  # doublon de fin
    s.read("two.py", "2"); s.read("two.py", "2")
    v = s.view()
    check("6: correlation 1 appel (3 evenements) + 2 appels identiques distincts",
          len(v.calls) == 3 and v.counts["duplicate_phases"] == 1, f"calls={len(v.calls)} counts={v.counts}")

    # 7. hors ordre, import repete, absence de fin, interruption
    s = Synth(home / "s7", session_id="s7")
    s.session_start(); s.user_prompt()
    s.read("late.py", "x", no_end=True)
    s.bash("sleep 100", interrupt=True)
    store = EventStore(s.home, s.cfg)
    skey = store.session_dir(s.client, s.session_id).name
    events, _ = store.read_session_events(s.client, skey)
    shuffled = list(reversed(events)) + events  # hors ordre + doublons
    v = build_session(shuffled, s.cfg)
    open_calls = [c for c in v.calls if c.status == "open"]
    interrupted = [c for c in v.calls if c.status == "interrupted"]
    check("7: hors ordre + doublons + appel ouvert + interruption",
          v.counts["duplicate_events"] == len(events) and len(open_calls) == 1 and len(interrupted) == 1,
          f"dup={v.counts['duplicate_events']} open={len(open_calls)} interrupted={len(interrupted)}")

    # 8. entrees malformees, payload volumineux
    s = Synth(home / "s8", session_id="s8")
    path, kind = ingest_payload(["not", "an", "object"], CLIENT_CLAUDE_CODE, s.home, s.cfg)
    big = "x" * (int(s.cfg["max_event_bytes"]) + 10)
    s.bash("echo big", stdout=big)
    v = s.view()
    check("8: payload non-objet -> incident ; sortie volumineuse -> evenement borne conserve",
          path is None and kind == "malformed" and len(v.calls) == 1 and v.calls[0].status == "success", f"kind={kind} calls={len(v.calls)}")

    # 9. secrets masques sans collision
    s = Synth(home / "s9", session_id="s9")
    s.session_start(); s.user_prompt()
    s.bash("curl -H 'Authorization: Bearer sk-AAAAAAAAAAAAAAAAAAAAAAAA' https://x", "1")
    s.bash("curl -H 'Authorization: Bearer sk-BBBBBBBBBBBBBBBBBBBBBBBB' https://x", "2")
    v = s.view()
    t1, t2 = v.calls[0].target or "", v.calls[1].target or ""
    check("9: secrets masques, empreintes distinctes, pas de fusion",
          "sk-AAAA" not in t1 and "sk-BBBB" not in t2 and "<secret:" in t1 and t1 != t2 and v.calls[0].target_key != v.calls[1].target_key,
          f"t1={t1!r} t2={t2!r}")

    # 10. pagination, polling, sous-agent
    s = Synth(home / "s10", session_id="s10")
    s.session_start(); s.user_prompt()
    s.read("big.log", "p1", offset=1, limit=100); s.read("big.log", "p2", offset=101, limit=100)
    for i in range(3):
        s.bash("curl -s http://localhost:9/status", stdout=f"pending{i}")
    fa = s.findings(["redundant_reads"])
    check("10: pagination non signalee ; polling a resultats differents non signale", len(fa) == 0, f"{len(fa)} signalement(s) {[f.title for f in fa]}")

    # 11. Codex : meme moteur, statut lu dans tool_response
    s = Synth(home / "s11", client=CLIENT_CODEX, session_id="thr_synthetic")
    s.session_start(); s.user_prompt()
    s.bash("cat a.txt", "A", exit_code=0); s.bash("cat a.txt", "A", exit_code=0)
    s.bash("python nope.py", fail="No such file: nope.py", exit_code=2)
    s.bash("python nope.py", fail="No such file: nope.py", exit_code=2)
    s.bash("python nope.py", fail="No such file: nope.py", exit_code=2)
    v = s.view()
    fa = s.findings(["redundant_reads"]); fb = s.findings(["error_loops"])
    check("11: Codex - statut depuis exit_code, A et B fonctionnent",
          len(v.calls) == 5 and v.calls[0].status == "success" and v.calls[2].status == "error" and len(fa) == 1 and len(fb) == 1,
          f"calls={[c.status for c in v.calls]} A={len(fa)} B={len(fb)}")

    # 12. G : sondage d'un job en cours signale avec une cadence ; verifications apres modification justifiees
    s = Synth(home / "s12", session_id="s12")
    s.session_start(); s.user_prompt()
    s.response_gap_ms = 20_000
    # Avec la consultation differee finale comptee, garder une serie assez longue
    # pour franchir le seuil de trois appels en moins du detecteur.
    for i in range(14):
        s.mcp("jobs", "job_status", {"job_id": 1}, json.dumps({"status": "RUNNING" if i < 13 else "COMPLETED"}))
    s.response_gap_ms = 3000
    for i in range(4):
        s.bash("git status --short", f" M f{i}.py"); s.edit(f"f{i}.py", "a", "b")
    fg = s.findings(["repeated_calls"])
    from agentwatch.detectors import repeated_calls as G
    groups = {g["tool"]: g for g in G.analyse(s.view(), s.cfg)["groups"]}
    rec = ((groups.get("mcp__jobs__job_status") or {}).get("cadence") or {}).get("recommended") or {}
    check("G: sondage signale avec cadence, verifications apres modification justifiees",
          [f.kind for f in fg] == ["polling_cadence"] and bool(rec) and (groups.get("Bash") or {}).get("verdict") == "justifie",
          f"G={[f.kind for f in fg]} cadence={rec.get('cooldown_s')} s -{rec.get('avoided')} Bash={(groups.get('Bash') or {}).get('verdict')}")
    return results


def hook_subprocess_check(home: Path, runs: int = 5) -> dict[str, Any]:
    """Execute le vrai point d'entree en sous-processus et mesure le temps mural."""
    entry = Path(__file__).resolve().parent / "hook_entry.py"
    payload = {"session_id": "selftest-subprocess", "cwd": str(home), "hook_event_name": "PreToolUse",
               "tool_name": "Read", "tool_input": {"file_path": str(home / "x.txt")}, "tool_use_id": "toolu_selftest"}
    data = json.dumps(payload).encode("utf-8")
    times: list[float] = []
    stdout_empty = True
    codes: list[int] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, "-I", str(entry), "ingest", "--client", "claude-code", "--home", str(home)],
                           input=data, capture_output=True)
        times.append((time.perf_counter() - t0) * 1000)
        codes.append(r.returncode)
        stdout_empty = stdout_empty and r.stdout == b""
    base: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-I", "-c", "pass"], capture_output=True)
        base.append((time.perf_counter() - t0) * 1000)
    store = EventStore(home, load_config(home))
    written = sum(1 for _ in store.session_dir("claude-code", "selftest-subprocess").iterdir()) if store.session_dir("claude-code", "selftest-subprocess").is_dir() else 0
    times.sort(); base.sort()
    return {"runs": runs, "exit_codes": sorted(set(codes)), "stdout_empty": stdout_empty, "events_written": written,
            "wall_ms_median": round(times[len(times) // 2], 1), "wall_ms_max": round(times[-1], 1),
            "interpreter_only_ms_median": round(base[len(base) // 2], 1),
            "python": sys.version.split()[0], "platform": sys.platform,
            "note": "temps mural du sous-processus complet (demarrage de l'interpreteur inclus), sur cette machine, echantillon indique"}


def run_selftest(runs: int = 5) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok_all = True
    with tempfile.TemporaryDirectory(prefix="agentwatch-selftest-") as tmp:
        home = Path(tmp)
        for r in scenario_results(home):
            ok_all = ok_all and r["ok"]
            lines.append(f"[{'OK' if r['ok'] else 'FAIL'}] {r['name']} - {r['detail']}")
        sub = hook_subprocess_check(home / "hook", runs=runs)
        hook_ok = sub["exit_codes"] == [0] and sub["stdout_empty"] and sub["events_written"] == runs
        ok_all = ok_all and hook_ok
        lines.append(f"[{'OK' if hook_ok else 'FAIL'}] hook en sous-processus : codes={sub['exit_codes']} stdout vide={sub['stdout_empty']} "
                     f"evenements={sub['events_written']}/{runs} ; temps mural median {sub['wall_ms_median']} ms "
                     f"(interpreteur seul {sub['interpreter_only_ms_median']} ms) ; {sub['note']}")
    return ok_all, lines
