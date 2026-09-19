"""D.automation_candidates v1.1 : contenu compose par le modele = jugement ; coordination hors des sequences.

Faux positifs reels (session Codex du 2026-09-19) : 30 motifs sur 32 portaient sur des messages entre agents
ou sur des appels MCP dont le modele ecrit le code a chaque fois, classes "mecaniques".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentwatch import CLIENT_CODEX
from agentwatch.selftest import Synth


class JudgmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _codex(self, sid: str) -> Synth:
        s = Synth(self.home / sid, client=CLIENT_CODEX, session_id=sid)
        s.session_start(); s.user_prompt()
        return s

    def test_messages_between_agents_are_not_automation_steps(self) -> None:
        s = self._codex("coord")
        for i in range(5):
            s.call("collaboration.send_message", {"target": "/root/a", "message": f"rapport numero {i} tres detaille"}, "")
            s.call("mcp__codegraph__codegraph_explore", {"projectPath": "C:/proj", "query": f"question {i}"}, "ok")
            s.call("collaboration.wait_agent", {"timeout_ms": 30000}, '{"message": "", "timed_out": true}')
        self.assertEqual(s.findings(["automation_candidates"]), [])

    def test_code_written_each_time_is_judgment(self) -> None:
        s = self._codex("code")
        for i in range(5):
            s.call("mcp__node_repl__js", {"code": f"const x = {i}; compute(x)"}, "ok")
            s.call("mcp__unreal__unreal_editor_py", {"script": f"import unreal\nstep({i})"}, "ok")
        self.assertEqual(s.findings(["automation_candidates"]), [])

    def test_inline_script_in_shell_is_judgment(self) -> None:
        s = self._codex("inline")
        for i in range(5):
            s.bash(f"@'\nprint({i} * 2)\n'@ | python -", "ok")
            s.call("mcp__unreal__unreal_editor_status", {}, "ok")
        f = s.findings(["automation_candidates"])
        # * seule l'etape de controle reste mecanique : candidat partiel, jamais en confiance haute
        self.assertEqual(len(f), 1)
        kinds = {st["tool"]: st["kind"] for st in f[0].proposal["recipe"]["steps"]}
        self.assertEqual(kinds, {"Bash": "judgment", "mcp__unreal__unreal_editor_status": "mechanical"})
        self.assertNotEqual(f[0].confidence, "high")

    def test_real_mechanical_sequence_is_reported_once(self) -> None:
        s = self._codex("mech")
        for i in range(5):
            s.call("mcp__linear__get_issue", {"id": f"UNE-{i}"}, "ok")
            s.bash(f"Get-Content Saved/Logs/run{i}.log", "ok")
        f = s.findings(["automation_candidates"])
        self.assertEqual(len(f), 1)                  # * A -> B et B -> A : un seul cycle
        self.assertEqual([st["kind"] for st in f[0].proposal["recipe"]["steps"]], ["mechanical", "mechanical"])


if __name__ == "__main__":
    unittest.main()
