"""Tests de contrat des adaptateurs sur les fixtures synthetiques (docs officielles).

# * Les fixtures de tests/fixtures/* sont SYNTHETIQUES (voir leur champ _note).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentwatch.collector.ingest import ingest_payload
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core import schema as S

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _ingest_dir(client: str, sub: str, home: Path) -> list[dict]:
    cfg = load_config(home)
    ns = 1_800_000_000_000_000_000
    for f in sorted((FIXTURES / sub).glob("*.json")):
        payload = json.loads(f.read_text(encoding="utf-8"))
        assert payload.pop("_note", "").startswith("SYNTHETIC"), f"{f} doit etre marquee synthetique"
        path, incident = ingest_payload(payload, client, home, cfg, started_ns=ns)
        ns += 100_000_000
        assert path and not incident, (f, incident)
    store = EventStore(home, cfg)
    events = []
    for c, skey, _ in store.iter_sessions():
        evs, warns = store.read_session_events(c, skey)
        assert not warns
        events += evs
    return sorted(events, key=lambda e: e["received_time_ns"])


class ClaudeCodeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.events = _ingest_dir("claude-code", "claude_code", Path(cls.tmp.name))
        cls.by_hook = {}
        for ev in cls.events:
            cls.by_hook.setdefault(ev["hook_event_name"], []).append(ev)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_schema_and_phases(self) -> None:
        for ev in self.events:
            self.assertEqual(S.validate_event(ev), [])
            self.assertEqual(ev["client"], "claude-code")
            self.assertEqual(set(ev), set(S.EVENT_FIELDS))
        self.assertEqual(self.by_hook["PreToolUse"][0]["phase"], "start")
        self.assertEqual(self.by_hook["PostToolUse"][0]["phase"], "end")
        self.assertEqual(self.by_hook["PostToolUseFailure"][0]["phase"], "failure")
        self.assertEqual(self.by_hook["SessionStart"][0]["session_meta"]["start_type"], "startup")
        self.assertEqual(self.by_hook["Stop"][0]["phase"], "turn_end")

    def test_read_response_nested_under_file(self) -> None:
        post = next(e for e in self.by_hook["PostToolUse"] if e["tool_name"] == "Read")
        self.assertEqual(post["status"], "success")
        self.assertEqual(post["target"], "README.md")
        self.assertIsNotNone(post["resource_state"]["revision"])
        self.assertEqual(post["evidence"]["totalLines"], 2)
        self.assertNotIn("API_KEY=abcdef", json.dumps(post), "le contenu lu ne doit jamais etre conserve")

    def test_failure_masks_secret_and_signature(self) -> None:
        fail = self.by_hook["PostToolUseFailure"][0]
        self.assertEqual(fail["status"], "error")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", json.dumps(fail))
        self.assertIn("<secret:", fail["target"])
        self.assertEqual(fail["error_signature"], "Exit code <n>: curl: (<n>) Failed to connect to example.com port <n>")
        self.assertIsNone(fail["exit_code"], "Claude Code n'expose pas de code de sortie : rester None")

    def test_grep_result_paths_and_mcp(self) -> None:
        grep = next(e for e in self.by_hook["PostToolUse"] if e["tool_name"] == "Grep")
        self.assertEqual(grep["result_paths"], ["agentwatch/cli.py", "agentwatch/hook_entry.py"])
        self.assertEqual(grep["params"]["pattern"], "def main")
        mcp = next(e for e in self.by_hook["PostToolUse"] if e["tool_name"].startswith("mcp__"))
        self.assertEqual((mcp["tool_category"], mcp["mcp_server"], mcp["mcp_tool"]), ("mcp", "codegraph", "codegraph_explore"))
        self.assertNotIn("hunter2", json.dumps(mcp))
        self.assertIn("secret_thing", mcp["params"]["_fp"])
        self.assertEqual(mcp["status"], "success")

    def test_subagent_fields(self) -> None:
        pre = next(e for e in self.by_hook["PreToolUse"] if e["tool_name"] == "Glob")
        self.assertEqual((pre["agent_id"], pre["agent_type"]), ("agent-77", "Explore"))
        self.assertEqual(pre["params"]["pattern"], "**/*.py")


class CodexContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.events = _ingest_dir("codex", "codex", Path(cls.tmp.name))
        cls.by_hook = {}
        for ev in cls.events:
            cls.by_hook.setdefault(ev["hook_event_name"], []).append(ev)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_phases_and_turns(self) -> None:
        for ev in self.events:
            self.assertEqual(S.validate_event(ev), [])
            self.assertEqual(ev["client"], "codex")
        self.assertEqual(self.by_hook["UserPromptSubmit"][0]["phase"], "turn_start")
        self.assertEqual(self.by_hook["UserPromptSubmit"][0]["evidence"]["prompt_chars"], 18)
        self.assertNotIn("please read README", json.dumps(self.by_hook["UserPromptSubmit"][0]))
        self.assertEqual(self.by_hook["Interrupt"][0]["phase"], "interrupt")
        self.assertEqual(self.by_hook["PreToolUse"][0]["turn_id"], "turn_1")
        self.assertEqual(self.by_hook["PreToolUse"][0]["model"], "gpt-example")

    def test_shell_exit_code_status(self) -> None:
        posts = {e["call_id"]: e for e in self.by_hook["PostToolUse"]}
        self.assertEqual((posts["call_1"]["status"], posts["call_1"]["exit_code"]), ("success", 0))
        self.assertEqual((posts["call_2"]["status"], posts["call_2"]["exit_code"]), ("error", 2))
        self.assertIn("No such file", posts["call_2"]["error_signature"])

    def test_apply_patch_paths(self) -> None:
        patch = next(e for e in self.by_hook["PostToolUse"] if e["tool_name"] == "apply_patch")
        self.assertEqual(patch["target"], "agentwatch/cli.py")
        self.assertEqual(patch["params"]["patch_paths"], ["agentwatch/cli.py"])
        self.assertNotIn("-old", json.dumps(patch), "le contenu du patch n'est pas conserve")

    def test_mcp_shape(self) -> None:
        mcp = next(e for e in self.by_hook["PostToolUse"] if e["tool_name"].startswith("mcp__"))
        self.assertEqual(mcp["status"], "success")
        self.assertEqual(mcp["target"], "docs/x.md")
        self.assertNotIn("abcdefgh12345678", json.dumps(mcp))

    def test_string_response_observed_live(self) -> None:
        posts = {e["call_id"]: e for e in self.by_hook["PostToolUse"]}
        ok, bad, mcp = posts["exec-1111"], posts["exec-2222"], posts["exec-3333"]
        self.assertEqual((ok["status"], ok["exit_code"]), ("unknown", None), "pas de code de sortie : statut inconnu, jamais un succes implicite")
        self.assertNotIn("error_hint", ok["evidence"])
        self.assertEqual(bad["status"], "unknown")
        self.assertIn("error_hint", bad["evidence"])
        self.assertIn("No such file or directory", bad["error_signature"])
        self.assertEqual(mcp["status"], "success")

    def test_unknown_response_shape_stays_unknown(self) -> None:
        from agentwatch.adapters import AdapterContext, get_adapter
        ctx = AdapterContext(load_config(Path(self.tmp.name)), lambda s: "0" * 64, None)
        ev = get_adapter("codex").parse_hook_payload({"session_id": "t", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                                                        "tool_input": {"command": "ls"}, "tool_use_id": "c", "tool_response": {"output": "x"}}, ctx)
        self.assertEqual(ev["status"], "unknown")
        self.assertTrue(any("status unknown" in w for w in ev["warnings"]))
        ev2 = get_adapter("codex").parse_hook_payload({"session_id": "t", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                                                         "tool_input": {"command": "ls"}, "tool_use_id": "c", "tool_response": {"output": "boom\nexit code: 3"}}, ctx)
        self.assertEqual((ev2["status"], ev2["exit_code"]), ("error", 3))
        self.assertIn("heuristic", ev2["evidence"]["exit_code_source"])


if __name__ == "__main__":
    unittest.main()
