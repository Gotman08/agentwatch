"""Lecture passive des rollouts Codex : appels, statuts, durees, tokens, sous-agents, messages, incremental.

# * Les rollouts ci-dessous sont SYNTHETIQUES, ecrits a la main d'apres le format observe (codex-cli 0.153.4
#   a 0.155.0-alpha.9.2) : aucune capture reelle. Les textes "SECRET" verifient que rien n'est conserve en clair.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentwatch import cli
from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.detectors import batchable
from agentwatch.reports.stats import session_tokens

ROOT = "01a0aaaa-0000-7000-8000-000000000001"
CHILD = "01a0aaaa-0000-7000-8000-000000000002"


class RolloutBuilder:
    """Lignes de rollout au format observe, horloge controlee."""

    def __init__(self, thread_id: str, parent: str | None = None, role: str = "worker", nickname: str = "Volta") -> None:
        self.thread_id, self.parent, self.role, self.nickname = thread_id, parent, role, nickname
        self.lines: list[str] = []
        self.t = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)
        self.ordinal = 0
        self.responses = 0

    def _ts(self, ms: int = 100) -> str:
        self.t += timedelta(milliseconds=ms)
        return self.t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.t.microsecond // 1000:03d}Z"

    def ms(self) -> int:
        return int(self.t.timestamp() * 1000)

    def add(self, type_: str, payload: dict[str, Any], ms: int = 100) -> None:
        self.ordinal += 1
        self.lines.append(json.dumps({"timestamp": self._ts(ms), "ordinal": self.ordinal, "type": type_, "payload": payload}))

    def meta(self) -> RolloutBuilder:
        source: Any = "vscode"
        if self.parent:
            source = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": self.parent, "depth": 1, "agent_path": "/root/tache",
                                                                "agent_nickname": self.nickname, "agent_role": self.role}}})
            # * un sous-agent recopie d'abord le session_meta de son parent : il doit etre ignore
        self.add("session_meta", {"session_id": self.parent or self.thread_id, "id": self.thread_id, "timestamp": self._ts(0),
                                  "cwd": "\\\\?\\C:\\proj", "originator": "codex_vscode", "cli_version": "0.155.0-alpha.9.2",
                                  "source": source, "thread_source": "subagent" if self.parent else "user",
                                  "base_instructions": {"text": "SECRET BASE INSTRUCTIONS"}})
        if self.parent:
            self.add("session_meta", {"session_id": self.parent, "id": self.parent, "cwd": "C:\\proj", "source": "vscode"})
        return self

    def turn(self, turn_id: str, prompt: str | None = None) -> RolloutBuilder:
        self.add("turn_context", {"turn_id": turn_id, "cwd": "C:\\proj", "model": "gpt-synth", "effort": "high"})
        self.add("event_msg", {"type": "task_started", "turn_id": turn_id, "model_context_window": 400000})
        if prompt is not None:
            self.add("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]})
        return self

    def usage(self, inp: int, cached: int, out: int) -> None:
        self.responses += 1
        self.add("token_usage_record", {"thread_id": self.thread_id, "session_id": self.parent or self.thread_id, "turn_id": "t",
                                        "response_id": f"resp_{self.thread_id[-2:]}_{self.responses}",
                                        "usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
                                                  "reasoning_output_tokens": out // 2, "total_tokens": inp + out}})

    def exec_(self, call_id: str, actions: list[dict[str, Any]], usage: tuple[int, int, int] = (1000, 900, 50),
              output: str = "SECRET EXEC OUTPUT") -> None:
        self.add("response_item", {"type": "custom_tool_call", "id": "ctc_" + call_id, "status": "completed", "call_id": call_id,
                                   "name": "exec", "input": "SECRET JS CODE"})
        self.usage(*usage)
        for a in actions:
            start = self.ms()
            self.add("event_msg", {"type": "item_completed", "thread_id": self.thread_id, "turn_id": "t", "item": a,
                                   "started_at_ms": start, "completed_at_ms": start + 80}, ms=90)
        self.add("response_item", {"type": "custom_tool_call_output", "id": "o_" + call_id, "call_id": call_id,
                                   "output": [{"type": "input_text", "text": output}]})

    @staticmethod
    def cmd(item_id: str, script: str, output: str, code: int = 0) -> dict[str, Any]:
        return {"type": "CommandExecution", "id": item_id, "process_id": "1", "command": ["pwsh.exe", "-Command", script],
                "cwd": "file:///C:/proj", "parsed_cmd": [{"type": "unknown", "cmd": script}], "source": "unified_exec_startup",
                "status": "completed" if code == 0 else "failed", "stdout": output, "stderr": "", "aggregated_output": output,
                "exit_code": code, "duration": {"secs": 1, "nanos": 250_000_000}, "formatted_output": output}

    @staticmethod
    def mcp(item_id: str, server: str, tool: str, args: dict[str, Any], text: str, is_error: bool | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error is not None:
            result["isError"] = is_error
        return {"type": "McpToolCall", "id": item_id, "server": server, "tool": tool, "arguments": args, "readOnlyHint": True,
                "status": "completed", "result": result, "duration": {"secs": 0, "nanos": 40_000_000}}

    def fn(self, call_id: str, namespace: str | None, name: str, args: dict[str, Any], output: str,
           items: list[dict[str, Any]] | None = None) -> None:
        p = {"type": "function_call", "id": "fc_" + call_id, "name": name, "arguments": json.dumps(args), "call_id": call_id}
        if namespace:
            p["namespace"] = namespace
        self.add("response_item", p)
        self.usage(800, 700, 20)
        for it in items or []:
            start = self.ms()
            self.add("event_msg", {"type": "item_completed", "thread_id": self.thread_id, "turn_id": "t", "item": it,
                                   "started_at_ms": start, "completed_at_ms": start + 30})
        self.add("response_item", {"type": "function_call_output", "id": "fo_" + call_id, "call_id": call_id, "output": output})

    def end_turn(self, turn_id: str) -> None:
        self.usage(1200, 1100, 30)
        self.add("response_item", {"type": "message", "role": "assistant", "phase": "final_answer",
                                   "content": [{"type": "output_text", "text": "SECRET FINAL ANSWER"}]})
        self.add("event_msg", {"type": "task_complete", "turn_id": turn_id, "duration_ms": 5000, "time_to_first_token_ms": 800,
                               "last_agent_message": "SECRET LAST"})

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


class RolloutImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.sessions = root / "sessions"
        self.day = self.sessions / "2026" / "09" / "19"
        self.day.mkdir(parents=True)
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"health": {"codex_sessions_dir": str(self.sessions)},
                                                           "rollouts": {"background_priority": False}}), encoding="utf-8")
        self.cfg = load_config(self.home)
        self.store = EventStore(self.home, self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, b: RolloutBuilder, text: str | None = None) -> Path:
        p = self.day / f"rollout-2026-09-19T10-00-00-{b.thread_id}.jsonl"
        p.write_text(text if text is not None else b.text(), encoding="utf-8", newline="\n")
        return p

    def _main(self) -> RolloutBuilder:
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "Merci de verifier le build.\n\nRappel : ne jamais toucher au dossier Content, seulement Source.")
        b.exec_("call_1", [b.cmd("exec-a", "Get-Content Source/a.cpp -TotalCount 40", "SECRET FILE A"),
                           b.mcp("exec-b", "codegraph", "codegraph_explore", {"projectPath": "C:\\proj", "body": "SECRET MCP ARG"},
                                 "SECRET MCP OUT")])
        # * Un echec garde la fin de sa sortie (resume d'erreur borne, secrets masques), comme pour un hook.
        b.exec_("call_2", [b.cmd("exec-c", "cmake --build build", "error: build password=hunter2hunter2 failed", code=1)],
                usage=(2000, 1000, 80))
        b.fn("call_3", "collaboration", "send_message", {"target": "/root/tache",
                                                         "message": "Rappel : ne jamais toucher au dossier Content, seulement Source."}, "")
        b.fn("call_4", "collaboration", "spawn_agent", {"agent_type": "worker", "message": "SECRET TASK TEXT", "task_name": "x"},
             "unknown agent type: planner")
        b.fn("call_5", "mcp__cua_repl", "js", {"code": "SECRET CODE", "title": "t"}, "[]",
             items=[b.mcp("call_5", "cua_repl", "js", {"code": "SECRET CODE"}, "ok", is_error=False)])
        b.add("compacted", {"message": "SECRET SUMMARY", "window_number": 1, "replacement_history": []})
        b.end_turn("turn-1")
        return b

    def _view(self) -> Any:
        (c, k), = [(c, k) for c, k, _ in self.store.iter_sessions()]
        return load_session(self.store, c, k, self.cfg)

    def test_import_builds_calls_with_real_status_duration_and_tokens(self) -> None:
        p = self._write(self._main())
        out = R.import_rollouts(self.store, self.cfg, [str(p)])
        self.assertEqual(out["errors"], [])
        v = self._view()
        self.assertEqual(v.session_id, ROOT)
        # * debut et fin de session = heures du rollout, jamais l'heure de l'import (sinon `trends` exclut la session)
        self.assertTrue(v.first_time.startswith("2026-09-19T10:00:0"), v.first_time)
        self.assertTrue(v.last_time.startswith("2026-09-19T10:00:0"), v.last_time)
        calls = {c.call_id: c for c in v.calls}
        self.assertEqual(set(calls), {"exec-a", "exec-b", "exec-c", "call_3", "call_4", "call_5"})
        self.assertEqual(v.counts["duplicate_events"], 0)
        self.assertEqual(v.counts["open_calls"], 0)
        a, b, c = calls["exec-a"], calls["exec-b"], calls["exec-c"]
        self.assertEqual((a.tool_name, a.status, a.exit_code, a.duration_ms, a.duration_source), ("Bash", "success", 0, 1250, "client"))
        self.assertEqual(a.op, "read")                              # * Get-Content traduit en lecture
        self.assertEqual((b.tool_name, b.status, b.category), ("mcp__codegraph__codegraph_explore", "success", "mcp"))
        self.assertEqual((c.status, c.exit_code), ("error", 1))
        self.assertNotIn("hunter2", c.error_summary or "")           # * secret masque dans le resume d'erreur
        self.assertEqual(calls["call_3"].status, "success")          # * sortie vide sans indice d'erreur
        self.assertEqual(calls["call_4"].status, "error")            # * texte d'erreur court
        self.assertEqual((calls["call_5"].status, calls["call_5"].duration_source), ("success", "client"))
        # * tokens : a et b partagent la reponse emettrice de call_1 et la reponse consommatrice suivante
        ua, ub = a.usage or {}, b.usage or {}
        self.assertEqual(ua["source"], "codex:rollout")
        self.assertEqual(ua["emitter_request_id"], ub["emitter_request_id"])
        self.assertEqual(ua["consumer_request_id"], ub["consumer_request_id"])
        self.assertNotEqual(ua["emitter_request_id"], (c.usage or {}).get("emitter_request_id"))
        self.assertEqual(ua["uncached_input_tokens"] + ub["uncached_input_tokens"], 1000)   # 2000 - 1000 de la reponse suivante
        tot = session_tokens(v)
        self.assertEqual((tot["requests"], tot["source"]), (6, "codex:rollout"))
        self.assertEqual(tot["input_tokens"], 1000 + 2000 + 800 * 3 + 1200)
        # * marqueurs : debut de session, tour, compaction, messages
        phases = [m.phase for m in v.markers]
        for ph in ("session_start", "turn_start", "turn_end", "compact_end", "message", "usage"):
            self.assertIn(ph, phases)
        self.assertEqual(v.epochs, 2)

    def test_nothing_is_stored_in_clear(self) -> None:
        p = self._write(self._main())
        R.import_rollouts(self.store, self.cfg, [str(p)])
        blob = "".join(f.read_text(encoding="utf-8") for f in (self.home / "segments").rglob("*.jsonl"))
        blob += (self.home / "import" / R.STATE_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("ne jamais toucher", blob)

    def test_repeated_instruction_gets_the_same_paragraph_fingerprint(self) -> None:
        R.import_rollouts(self.store, self.cfg, [str(self._write(self._main()))])
        v = self._view()
        msgs = [m.meta for m in v.markers if m.phase == "message" and m.meta.get("role") in ("user", "agent_instruction")]
        user = next(m for m in msgs if m["role"] == "user")
        instr = next(m for m in msgs if m["role"] == "agent_instruction")
        self.assertTrue(set(user["paragraphs"]) & set(instr["paragraphs"]))   # * meme consigne, meme empreinte

    def test_subagent_is_attached_to_the_root_session(self) -> None:
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        child.exec_("call_c1", [child.cmd("exec-z", "rg foo Source", "match")])
        child.end_turn("turn-c")
        paths = [str(self._write(self._main())), str(self._write(child))]
        R.import_rollouts(self.store, self.cfg, paths)
        v = self._view()
        z = next(c for c in v.calls if c.call_id == "exec-z")
        self.assertEqual((z.agent_id, z.agent_type), (CHILD, "worker"))
        starts = [m for m in v.markers if m.phase == "subagent_start"]
        self.assertEqual(starts[0].meta.get("agent_nickname"), "Sagan")
        self.assertEqual(len([m for m in v.markers if m.phase == "session_start"]), 1)   # * copie du meta parent ignoree
        tot = session_tokens(v)
        self.assertEqual(len(tot["threads"]), 2)

    def test_incremental_import_equals_single_import_and_skips_partial_line(self) -> None:
        text = self._main().text()
        lines = text.splitlines(keepends=True)
        half = len(lines) // 2
        p = self._write(RolloutBuilder(ROOT), text="".join(lines[:half]) + lines[half].rstrip("\n"))   # ligne en cours d'ecriture
        R.import_rollouts(self.store, self.cfg, [str(p)])
        state = R.load_state(str(self.home))["files"]
        offset = next(iter(state.values()))["offset"]
        self.assertEqual(offset, len("".join(lines[:half]).encode("utf-8")))
        p.write_text(text, encoding="utf-8", newline="\n")
        R.import_rollouts(self.store, self.cfg, [str(p)])
        again = R.import_rollouts(self.store, self.cfg, [str(p)])
        self.assertEqual(again["lines"], 0)
        v = self._view()
        self.assertEqual(v.counts["duplicate_events"], 0)
        self.assertEqual(len(v.calls), 6)
        self.assertTrue(all(c.usage for c in v.calls))

    def test_batchable_uses_the_emitting_response(self) -> None:
        b = RolloutBuilder(ROOT).meta().turn("turn-1")
        # * trois lectures dans UN exec : emises ensemble, rien a signaler
        b.exec_("call_1", [b.cmd(f"exec-{n}", f"Get-Content Source/{n}.cpp", n) for n in ("a", "b", "c")])
        # * puis trois exec successifs d'une lecture chacun : trois allers-retours
        for i, n in enumerate(("d", "e", "f")):
            b.exec_(f"call_{i + 2}", [b.cmd(f"exec-{n}", f"Get-Content Source/{n}.cpp", n)])
        b.end_turn("turn-1")
        R.import_rollouts(self.store, self.cfg, [str(self._write(b))])
        found = batchable.detect(self._view(), self.cfg)
        self.assertEqual(len(found), 1)
        # * quatre reponses de lecture d'affilee (celle de a, b, c puis d, e, f) : 3 allers-retours evitables
        self.assertEqual([r["call_id"] for r in found[0].call_refs], ["exec-c", "exec-d", "exec-e", "exec-f"])
        self.assertIn("rollout", found[0].evidence["separation_basis"])
        self.assertEqual(found[0].evidence["grouped_tool"]["status"], "verified_in_session")

    def test_cli_import_follow_free_and_report(self) -> None:
        self._write(self._main())
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            self.assertEqual(cli.main(["--home", str(self.home), "import-rollouts", "--days", "0"]), 0)
        self.assertIn("rollouts Codex : 1 fichier(s) lu(s)", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", ROOT[:12], "--format", "json"]), 0)
        js = json.loads(buf.getvalue())
        self.assertIn("mesure depuis les rollouts Codex", js["stats"]["usage"]["status"])
        self.assertEqual(js["session"]["client_version_observed"], "0.155.0-alpha.9.2")
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", ROOT[:12], "--format", "markdown"]), 0)
        self.assertIn("| Fil | Agent | Reponses |", buf.getvalue())

    def test_segments_are_merged_beyond_the_limit(self) -> None:
        b = self._main()
        lines = b.text().splitlines(keepends=True)
        p = self._write(b, text="")
        cfg = dict(self.cfg, rollouts={"max_segments": 3, "background_priority": False})
        for i in range(0, len(lines), 3):          # * un segment par lecture, comme en suivi
            with p.open("a", encoding="utf-8", newline="\n") as fh:
                fh.writelines(lines[i:i + 3])
            R.import_rollouts(self.store, cfg, [str(p)])
        segs = list((self.home / "segments").rglob("*-rollout.jsonl"))
        self.assertLessEqual(len(segs), 4)
        v = self._view()
        self.assertEqual((len(v.calls), v.counts["duplicate_events"]), (6, 0))


if __name__ == "__main__":
    unittest.main()
