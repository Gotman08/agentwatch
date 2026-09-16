"""Tests des sous-agents : lien exact par agentId, repli temporel, agents 'arret seul',
usage rapporte par le client, cle duration_ms, isolation des detecteurs par agent."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.selftest import Synth

USAGE = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 0}


class AgentLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_exact_link_usage_and_duration_ms(self) -> None:
        s = Synth(self.home, session_id="ag1")
        s.session_start(); s.user_prompt()
        s.read("main.py", "m")
        s.agent_run("Explore", lambda: (s.read("a.py", "A"), s.grep("x", ".", ["a.py"])), agent_id="agent-A", usage=USAGE)
        v = s.view()
        agents = {a.agent_id: a for a in v.agent_infos}
        self.assertEqual(list(agents), ["agent-A"])
        a = agents["agent-A"]
        self.assertEqual((a.classification, a.agent_type, a.calls), ("subagent", "Explore", 2))
        self.assertEqual(a.link_basis, "exact:tool_response.agentId")
        parent = next(c for c in v.calls if c.key == a.parent_call_key)
        self.assertEqual((parent.tool_name, parent.agent_id), ("Agent", None))
        self.assertEqual((parent.duration_ms, parent.duration_source), (1000, "client"), "cle duration_ms observee en direct")
        self.assertEqual(a.model, "synthetic-sub-model")
        self.assertEqual(a.client_duration_ms, 900)
        self.assertEqual((a.usage["input_tokens"], a.usage["cache_read_tokens"], a.usage["scope"]), (1200, 5000, "agent"))
        self.assertNotIn("synthetic result", json.dumps(parent.evidence), "aucun contenu de reponse conserve")

    def test_temporal_fallback_and_ambiguity(self) -> None:
        s = Synth(self.home, session_id="ag2")
        s.session_start(); s.user_prompt()
        s.agent_run("Explore", lambda: s.read("b.py", "B"), agent_id="agent-B", with_agent_id=False)
        v = s.view()
        b = next(a for a in v.agent_infos if a.agent_id == "agent-B")
        self.assertTrue(b.link_basis and b.link_basis.startswith("temporal_containment"))
        self.assertIsNotNone(b.parent_call_seq)
        # deux appels Agent paralleles ouverts : aucun lien retenu
        s2 = Synth(self.home, session_id="ag3")
        s2.session_start(); s2.user_prompt()
        for cid in ("p1", "p2"):
            s2.emit(s2._base("PreToolUse", tool_name="Agent", tool_input={"subagent_type": "Explore", "run_in_background": False}, tool_use_id=cid))
        s2.emit(s2._base("SubagentStart", agent_id="agent-C", agent_type="Explore"))
        s2.set_agent("agent-C", "Explore"); s2.read("c.py", "C"); s2.set_agent(None)
        s2.emit(s2._base("SubagentStop", agent_id="agent-C", agent_type="Explore", stop_hook_active=False))
        c = next(a for a in s2.view().agent_infos if a.agent_id == "agent-C")
        self.assertIsNone(c.parent_call_key)
        self.assertTrue(c.link_basis and c.link_basis.startswith("ambiguous"))

    def test_stop_only_agent_is_classified_not_guessed(self) -> None:
        s = Synth(self.home, session_id="ag4")
        s.session_start(); s.user_prompt()
        s.emit(s._base("SubagentStop", agent_id="internal-1", agent_type=None, stop_hook_active=False, last_assistant_message=None))
        s.read("x.py", "x")
        v = s.view()
        i = next(a for a in v.agent_infos if a.agent_id == "internal-1")
        self.assertEqual((i.classification, i.calls, i.parent_call_key, i.usage), ("stop_only", 0, None, None))
        self.assertIn("aucun appel observe", i.classification_note)

    def test_detectors_do_not_cross_agents_and_report_shows_agents(self) -> None:
        s = Synth(self.home, session_id="ag5")
        s.session_start(); s.user_prompt()
        s.read("shared.py", "S")
        s.agent_run("Explore", lambda: s.read("shared.py", "S"), agent_id="agent-D", usage=USAGE)
        s.read("shared.py", "S")   # relecture par le fil principal apres un appel a effet inconnu (Agent)
        findings = s.findings(["redundant_reads"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].evidence["agent"], "main")
        self.assertEqual(findings[0].confidence, "medium", "un appel Agent (effet inconnu) s'est intercale")
        self.assertEqual(sorted(r["seq"] for r in findings[0].call_refs), [r["seq"] for r in findings[0].call_refs])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "ag5"]), 0)
        md = buf.getvalue()
        self.assertIn("## Agents", md)
        self.assertIn("exact:tool_response.agentId", md)
        self.assertIn("in 1200 / out 300 / cache lu 5000", md)
        self.assertIn("rapporte par le client pour 1 sous-agent(s)", md)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--home", str(self.home), "report", "--session", "ag5", "--format", "json"])
        js = json.loads(buf.getvalue())
        self.assertEqual(js["agents"][0]["agent_id"], "agent-D")
        self.assertTrue(any(r["capability"] == "token_usage" and r["observed_in_session"] == "observed" for r in js["coverage"]))


if __name__ == "__main__":
    unittest.main()
