"""Tests des detecteurs et de la correlation sur scenarios synthetiques (agentwatch.selftest)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentwatch.core.correlate import build_session
from agentwatch.selftest import Synth, scenario_results


class ReferenceScenarios(unittest.TestCase):
    def test_all_reference_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for r in scenario_results(Path(tmp)):
                with self.subTest(r["name"]):
                    self.assertTrue(r["ok"], r["detail"])


class ExtraScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_transient_recovered_is_low_confidence(self) -> None:
        s = Synth(self.home, session_id="t1")
        s.session_start(); s.user_prompt()
        for _ in range(3):
            s.bash("curl http://svc/health", fail="Exit code 7: connection refused")
        s.bash("curl http://svc/health", "ok")
        f = s.findings(["error_loops"])
        self.assertEqual([x.kind for x in f], ["transient_recovered"])
        self.assertEqual(f[0].confidence, "low")

    def test_interrupts_never_counted(self) -> None:
        s = Synth(self.home, session_id="t2")
        s.session_start(); s.user_prompt()
        for _ in range(4):
            s.bash("sleep 100", interrupt=True)
        self.assertEqual(s.findings(["error_loops"]), [])

    def test_unknown_effect_call_degrades_redundancy(self) -> None:
        s = Synth(self.home, session_id="t3")
        s.session_start(); s.user_prompt()
        s.read("a.py", "x"); s.bash("python fix.py", "ok"); s.read("a.py", "x")
        f = s.findings(["redundant_reads"])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].confidence, "medium")

    def test_overlapping_calls_are_not_batchable(self) -> None:
        s = Synth(self.home, session_id="t4")
        s.session_start(); s.user_prompt()
        # trois PreToolUse avant les fins : appels deja paralleles
        ids = []
        for name in ("a", "b", "c"):
            ids.append(s._call_id())
            s.emit(s._base("PreToolUse", tool_name="Read", tool_input={"file_path": s._abs(name + ".py")}, tool_use_id=ids[-1]))
        for cid, name in zip(ids, ("a", "b", "c")):
            s.emit(s._base("PostToolUse", tool_name="Read", tool_input={"file_path": s._abs(name + ".py")}, tool_use_id=cid,
                           tool_response={"type": "text", "file": {"filePath": s._abs(name + ".py"), "content": name}}))
        self.assertEqual(s.findings(["batchable"]), [])

    def test_batchable_mcp_with_candidate_tool(self) -> None:
        s = Synth(self.home, session_id="t5")
        s.session_start(); s.user_prompt()
        s.mcp("fs", "list_dir", {"path": "docs"}, "a.md b.md c.md")
        for n in ("a", "b", "c"):
            s.mcp("fs", "read_file", {"path": f"docs/{n}.md"}, "text")
        f = s.findings(["batchable"])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].evidence["grouped_tool"]["status"], "candidate_observed")
        self.assertIn("mcp__fs__list_dir", f[0].evidence["grouped_tool"]["tools"])

    def test_events_without_call_id_are_paired_heuristically(self) -> None:
        s = Synth(self.home, session_id="t6")
        s.session_start()
        s.emit(s._base("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
        s.emit(s._base("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": "a", "stderr": "", "interrupted": False}))
        v = s.view()
        self.assertEqual(len(v.calls), 1)
        self.assertTrue(v.calls[0].ambiguous)
        self.assertEqual(v.counts["heuristic_pairs"], 1)

    def test_no_findings_on_clean_session(self) -> None:
        s = Synth(self.home, session_id="t7")
        s.session_start(); s.user_prompt()
        s.read("a.py", "1"); s.edit("a.py", "1", "2"); s.bash("pytest", "ok"); s.stop()
        self.assertEqual(s.findings(), [])

    def test_invalid_events_are_skipped(self) -> None:
        v = build_session([{"schema_version": "9.9", "event_id": "x"}, {"not": "an event"}], {})
        self.assertEqual(v.counts["invalid_events"], 2)
        self.assertEqual(v.calls, [])


if __name__ == "__main__":
    unittest.main()
