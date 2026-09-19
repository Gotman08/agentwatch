"""Tests de la lecture des transcripts Claude Code : usage en tokens par requete et par appel, sans texte."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.collector.store import EventStore
from agentwatch.collector.transcripts import (attribute_calls, find_transcripts, import_session, parse_transcript, project_slug,
                                              totals)
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.detectors import run_detectors
from agentwatch.selftest import Synth


def _assistant(rid: str, usage: dict, blocks: list[dict], n_lines: int = 1, agent: str | None = None) -> list[str]:
    """Une reponse API = plusieurs lignes `assistant` (un bloc par ligne) avec le meme requestId et le meme usage."""
    out = []
    for i in range(n_lines):
        o = {"type": "assistant", "requestId": rid, "uuid": f"{rid}-{i}", "timestamp": f"2026-09-18T10:00:0{i}.000Z",
             "message": {"id": f"msg_{rid}", "model": "synthetic-model", "usage": usage, "content": [blocks[i]] if i < len(blocks) else [{"type": "text", "text": "SECRET TEXT"}]}}
        if agent:
            o["agentId"], o["isSidechain"] = agent, True
        out.append(json.dumps(o))
    return out


def _result(tid: str) -> str:
    return json.dumps({"type": "user", "uuid": f"u-{tid}", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "SECRET OUTPUT"}]}})


class TranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.projects = root / "projects"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"transcripts": {"claude_projects_dir": str(self.projects)}}), encoding="utf-8")
        self.cfg = load_config(self.home)
        # * Session synthetique : Read a.py (toolu_0001), Read a.py (toolu_0002) et Grep (toolu_0003) en parallele,
        #   puis un sous-agent qui lit b.py (toolu_0005 ; l'appel Agent lui-meme est toolu_0004).
        self.s = Synth(self.home, session_id="tx")
        s = self.s
        s.session_start(); s.user_prompt()
        s.read("a.py", "x"); s.read("a.py", "x"); s.grep("x", ".", ["a.py"])
        s.agent_run("Explore", lambda: s.read("b.py", "B"), agent_id="agent-abc")
        s.stop()
        slug_dir = self.projects / project_slug(s.cwd)
        (slug_dir / "tx" / "subagents").mkdir(parents=True)
        main = [
            json.dumps({"type": "user", "uuid": "u0", "message": {"role": "user", "content": "SECRET PROMPT"}}),
            *_assistant("r1", {"input_tokens": 5, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 50},
                        [{"type": "tool_use", "id": "toolu_0001", "name": "Read", "input": {}}]),
            _result("toolu_0001"),
            *_assistant("r2", {"input_tokens": 10, "cache_creation_input_tokens": 400, "cache_read_input_tokens": 5000, "output_tokens": 20},
                        [{"type": "tool_use", "id": "toolu_0002", "name": "Read", "input": {}}, {"type": "tool_use", "id": "toolu_0003", "name": "Grep", "input": {}}], n_lines=2),
            _result("toolu_0002"), _result("toolu_0003"),
            *_assistant("r3", {"input_tokens": 0, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 5400, "output_tokens": 10},
                        [{"type": "tool_use", "id": "toolu_0004", "name": "Agent", "input": {}}], n_lines=3),
            _result("toolu_0004"),
            *_assistant("r4", {"input_tokens": 0, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5700, "output_tokens": 30}, []),
            json.dumps({"type": "custom-title", "customTitle": "SECRET TITLE"}),
            "not json at all",
        ]
        (slug_dir / "tx.jsonl").write_text("\n".join(main) + "\n", encoding="utf-8")
        sub = [
            *_assistant("s1", {"input_tokens": 3, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 0, "output_tokens": 8},
                        [{"type": "tool_use", "id": "toolu_0005", "name": "Read", "input": {}}], agent="abc"),
            _result("toolu_0005"),
            *_assistant("s2", {"input_tokens": 0, "cache_creation_input_tokens": 60, "cache_read_input_tokens": 200, "output_tokens": 12}, [], agent="abc"),
        ]
        (slug_dir / "tx" / "subagents" / "agent-abc.jsonl").write_text("\n".join(sub) + "\n", encoding="utf-8")
        self.main = slug_dir / "tx.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_slug_and_discovery(self) -> None:
        self.assertEqual(project_slug(r"G:\UnrealEngine\Unearthed"), "G--UnrealEngine-Unearthed")
        self.assertEqual(project_slug("/home/x/proj"), "-home-x-proj")
        main, subs = find_transcripts(self.s.cwd, "tx", self.cfg)
        self.assertEqual(main, self.main)
        self.assertEqual([p.name for p in subs], ["agent-abc.jsonl"])
        stored, _ = find_transcripts(None, None, self.cfg, stored_path=str(self.main))
        self.assertEqual(stored, self.main)
        self.assertEqual(find_transcripts(self.s.cwd, "inconnue", self.cfg), (None, []))

    def test_parse_dedupes_requests_and_keeps_no_text(self) -> None:
        parsed = parse_transcript(self.main)
        self.assertEqual([r["request_id"] for r in parsed["requests"]], ["r1", "r2", "r3", "r4"])
        self.assertEqual(parsed["requests"][1]["tool_uses"], ["toolu_0002", "toolu_0003"], "blocs repartis sur deux lignes, meme requete")
        self.assertEqual(parsed["requests"][2]["consumed"], ["toolu_0002", "toolu_0003"], "deux resultats consommes par la meme requete")
        self.assertEqual(len(parsed["warnings"]), 1, "ligne non JSON signalee une fois")
        self.assertNotIn("SECRET", json.dumps(parsed))
        per = attribute_calls(parsed)
        self.assertEqual(per["toolu_0001"]["uncached_input_tokens"], 410, "input 10 + cache_creation 400 de la requete consommatrice r2")
        self.assertEqual(per["toolu_0001"]["output_tokens"], 50)
        self.assertEqual((per["toolu_0002"]["uncached_input_tokens"], per["toolu_0002"]["consumers"]), (150, 2), "300 partages entre deux resultats")
        self.assertEqual((per["toolu_0002"]["output_tokens"], per["toolu_0002"]["emitters"]), (10, 2), "sortie 20 partagee entre deux appels")
        t = totals(parsed)
        self.assertEqual((t["requests"], t["output_tokens"], t["cache_read_tokens"]), (4, 110, 16100))
        self.assertEqual(t["total_tokens"], 15 + 1800 + 16100 + 110)
        self.assertEqual(parse_transcript(self.main, max_bytes=10)["requests"], [], "fichier trop gros : ignore avec avertissement")

    def test_import_attaches_usage_to_calls_and_session(self) -> None:
        store = EventStore(self.home, self.cfg)
        view = load_session(store, "claude-code", "tx", self.cfg)
        summary = import_session(store, self.cfg, "claude-code", "tx", view, set())
        self.assertEqual((summary["calls_matched"], summary["calls_without_hook_events"], summary["subagent_transcripts"]), (5, 0, 1))
        self.assertEqual(summary["written"], 6, "5 appels + 1 usage de session")
        view2 = load_session(store, "claude-code", "tx", self.cfg)
        by_id = {c.call_id: c for c in view2.calls}
        self.assertEqual(len(view2.calls), len(view.calls), "aucun appel fictif cree par l'import")
        self.assertEqual(by_id["toolu_0001"].usage["uncached_input_tokens"], 410)
        self.assertEqual(by_id["toolu_0005"].usage["output_tokens"], 8, "appel du sous-agent relie par tool_use_id")
        self.assertEqual(by_id["toolu_0005"].agent_id, "agent-abc")
        usage_markers = [m for m in view2.markers if m.phase == "usage"]
        self.assertEqual(len(usage_markers), 1)
        self.assertEqual(usage_markers[0].meta["usage"]["requests"], 4)
        self.assertEqual(usage_markers[0].meta["agents"]["abc"]["output_tokens"], 20)
        again = import_session(store, self.cfg, "claude-code", "tx", view2, {e.get("event_id") for e in store.read_session_events("claude-code", "tx")[0]})
        self.assertEqual((again["written"], again["skipped"]), (0, 6), "reimport idempotent")
        # * Le detecteur A voit maintenant un cout en tokens ; le classement et le rapport le reprennent.
        fa = [f for f in run_detectors(view2, self.cfg) if f.rule_id.startswith("A.")]
        self.assertEqual(len(fa), 1)
        self.assertEqual(fa[0].observed_cost["tokens"]["total"], 150 + 10, "second Read : part non mise en cache 150 + sortie 10")

    def test_cli_import_report_and_trends(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "import-transcripts", "--session", "tx"]), 0)
        self.assertIn("4 requetes API", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "tx", "--format", "json"]), 0)
        js = json.loads(buf.getvalue())
        self.assertIn("4 requetes API", js["stats"]["usage"]["status"])
        self.assertEqual(js["stats"]["usage"]["transcript_calls"], 5)
        self.assertIn("tokens mesures (transcripts ou rollouts importes)", js["ranking_criteria"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "tx"]), 0)
        self.assertIn("tokens mesures", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "trends", "--days", "0", "--format", "json"]), 0)
        tr = json.loads(buf.getvalue())
        self.assertEqual(tr["tokens"], 15 + 1800 + 16100 + 110)
        self.assertEqual(tr["by_session"][0]["tokens"], tr["tokens"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "import-transcripts", "--all"]), 0)
        self.assertIn("deja presente(s)", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
