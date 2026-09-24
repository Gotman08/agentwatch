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
from agentwatch.reports.stats import session_tokens
from agentwatch.detectors.base import observed_cost, tokens_of
from agentwatch.reports.labels import tokens_cost_text
from agentwatch.core import schema as S


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

    def test_replayed_request_drains_pending_results_without_leaking_or_duplicates(self) -> None:
        usage = {"input_tokens": 2, "cache_creation_input_tokens": 3,
                 "cache_read_input_tokens": 5, "output_tokens": 7}
        for copies in (1, 2):
            with self.subTest(copies=copies):
                rows = _assistant("replayed", usage, [])
                for _ in range(copies):
                    rows.extend([_result("c"), *_assistant("replayed", usage, [])])
                rows.extend([_result("d"), *_assistant("next", usage, [])])
                self.main.write_text("\n".join(rows) + "\n", encoding="utf-8")
                parsed = parse_transcript(self.main)
                self.assertEqual(len(parsed["requests"]), 2)
                replayed, following = parsed["requests"]
                self.assertEqual(replayed["consumed"], ["c"])
                self.assertEqual(following["consumed"], ["d"])
                self.assertEqual(totals(parsed)["requests"], 2)

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
        # * Les tokens d'un signalement sont une part CALCULEE a partir des releves par reponse : le libelle ne dit
        #   plus « mesures » (correction du 2026-09-20, voir tests/test_report_accuracy.py).
        self.assertIn("tokens repartis par calcul sur ces appels (a partir des releves par reponse)", js["ranking_criteria"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "tx"]), 0)
        self.assertIn("tokens repartis par calcul", buf.getvalue())
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

    def test_missing_usage_stays_unknown_through_import_report_and_trends(self) -> None:
        # Missing readings must not become the measured value zero at any consumer.
        self.main.write_text("\n".join([
            *_assistant("missing1", {}, [{"type": "tool_use", "id": "toolu_0001"}]),
            _result("toolu_0001"),
            *_assistant("missing2", {}, []),
        ]) + "\n", encoding="utf-8")
        parsed = parse_transcript(self.main)
        attributed = attribute_calls(parsed)["toolu_0001"]
        self.assertIsNone(attributed["output_tokens"])
        self.assertIsNone(attributed["uncached_input_tokens"])
        total = totals(parsed)
        self.assertIsNone(total["total_tokens"])
        self.assertIsNone(total["observed_tokens"])
        self.assertEqual(total["usage_coverage"]["status"], "missing")
        self.assertEqual(total["usage_coverage"]["missing_requests"], 2)
        store = EventStore(self.home, self.cfg)
        view = load_session(store, "claude-code", "tx", self.cfg)
        summary = import_session(store, self.cfg, "claude-code", "tx", view, set())
        self.assertIsNone(summary["total_tokens"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "tx", "--format", "json"]), 0)
        report = json.loads(buf.getvalue())
        self.assertIsNone(report["stats"]["usage"]["session"]["total_tokens"])
        self.assertIn("non releve", report["stats"]["usage"]["status"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "trends", "--days", "0", "--format", "json"]), 0)
        trends = json.loads(buf.getvalue())
        self.assertIsNone(trends["tokens"])
        self.assertIsNone(trends["by_session"][0]["tokens"])

    def test_partial_usage_keeps_observed_sums_separate_from_totals(self) -> None:
        self.main.write_text("\n".join([
            *_assistant("partial1", {"input_tokens": 2, "output_tokens": 4}, [{"type": "tool_use", "id": "toolu_0001"}]),
            _result("toolu_0001"),
            *_assistant("partial2", {"input_tokens": 3, "cache_creation_input_tokens": 5,
                                        "cache_read_input_tokens": 7}, []),
        ]) + "\n", encoding="utf-8")
        parsed = parse_transcript(self.main)
        total = totals(parsed)
        self.assertEqual(total["input_tokens"], 5)
        self.assertIsNone(total["output_tokens"])
        self.assertIsNone(total["total_tokens"])
        self.assertEqual(total["observed_tokens"], 21)
        self.assertEqual(total["observed_totals"]["output_tokens"], 4)
        self.assertEqual(total["usage_coverage"]["status"], "partial")
        self.assertEqual(total["usage_coverage"]["field_requests"]["output_tokens"], 1)
        attributed = attribute_calls(parsed)["toolu_0001"]
        self.assertEqual(attributed["output_tokens"], 4)
        self.assertEqual(attributed["uncached_input_tokens"], 8)

    def test_explicit_zero_remains_measured_zero(self) -> None:
        usage = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
        self.main.write_text("\n".join(_assistant("zero", usage, [{"type": "tool_use", "id": "toolu_0001"}])) + "\n", encoding="utf-8")
        parsed = parse_transcript(self.main)
        total = totals(parsed)
        self.assertEqual(total["total_tokens"], 0)
        self.assertEqual(total["observed_tokens"], 0)
        self.assertEqual(total["usage_coverage"]["status"], "complete")
        self.assertEqual(attribute_calls(parsed)["toolu_0001"]["output_tokens"], 0)

    def test_synthetic_assistant_is_not_api_usage_but_real_zero_and_fallback_are(self) -> None:
        zero = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0}
        synthetic = json.loads(_assistant("synthetic", zero, [])[0])
        synthetic.pop("requestId")
        synthetic["message"]["model"] = "<synthetic>"
        real = json.loads(_assistant("real-zero", zero, [])[0])
        fallback = json.loads(_assistant("fallback", {**zero, "output_tokens": 7}, [])[0])
        fallback.pop("requestId")
        self.main.write_text("\n".join([
            _result("pending-tool"), json.dumps(synthetic), json.dumps(real), json.dumps(fallback),
        ]) + "\n", encoding="utf-8")
        parsed = parse_transcript(self.main)
        self.assertEqual([r["request_id"] for r in parsed["requests"]], ["real-zero", "msg_fallback"])
        self.assertEqual(parsed["requests"][0]["usage"]["output_tokens"], 0)
        self.assertEqual(parsed["requests"][0]["consumed"], ["pending-tool"])
        self.assertEqual(parsed["requests"][1]["usage"]["output_tokens"], 7)
        self.assertEqual(parsed["coverage"]["synthetic_assistant_lines"], 1)
        self.assertEqual(parsed["coverage"]["line_types"]["assistant"], 3)
        self.assertEqual(totals(parsed)["requests"], 2)
        from agentwatch.reports.inspect_claude import export_session
        exported = io.StringIO()
        export_session(self.cfg, str(self.home), "tx", exported)
        self.assertIn("msg_synthetic", exported.getvalue(), "le message local reste visible dans l'export")

    def _usage_rows(self, observations: list[tuple[dict, str | None]]) -> dict:
        rows = []
        for line, (usage, stop_reason) in enumerate(observations):
            row = json.loads(_assistant("stream", usage, [{"type": "tool_use", "id": "toolu_0001"}])[0])
            row["uuid"] = f"stream-{line}"
            row["message"]["stop_reason"] = stop_reason
            rows.append(json.dumps(row))
        self.main.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return parse_transcript(self.main)

    def test_terminal_usage_replaces_streaming_counter_with_provenance(self) -> None:
        initial = {"input_tokens": 2, "cache_creation_input_tokens": 30,
                   "cache_read_input_tokens": 35, "output_tokens": 8}
        final = {**initial, "output_tokens": 242}
        final["iterations"] = [{"type": "message", **final}]
        parsed = self._usage_rows([(initial, None), (final, "tool_use")])
        req = parsed["requests"][0]
        self.assertEqual(req["usage"]["output_tokens"], 242)
        self.assertEqual(req["usage_source_lines"], [2])
        self.assertEqual(req["usage_basis"], "terminal_with_iterations")
        self.assertEqual(req["usage_conflicts"], [])
        self.assertEqual([o["line"] for o in req["usage_observations"]], [1, 2])
        self.assertEqual(attribute_calls(parsed)["toolu_0001"]["output_tokens"], 242)
        self.assertNotIn("SECRET", json.dumps(parsed))

    def test_terminal_usage_is_not_selected_by_numeric_maximum(self) -> None:
        initial = {"input_tokens": 2, "cache_creation_input_tokens": 30,
                   "cache_read_input_tokens": 35, "output_tokens": 999}
        final = {**initial, "output_tokens": 242}
        final["iterations"] = [{"type": "message", **final}]
        parsed = self._usage_rows([(initial, None), (final, "tool_use")])
        self.assertEqual(parsed["requests"][0]["usage"]["output_tokens"], 242)

    def test_conflicting_terminal_usage_stays_unknown(self) -> None:
        first = {"input_tokens": 2, "cache_creation_input_tokens": 30,
                 "cache_read_input_tokens": 35, "output_tokens": 242}
        second = {**first, "output_tokens": 300}
        for usage in (first, second):
            usage["iterations"] = [{"type": "message", **usage}]
        parsed = self._usage_rows([(first, "tool_use"), (second, "tool_use")])
        req = parsed["requests"][0]
        self.assertIsNone(req["usage"]["output_tokens"])
        self.assertEqual(req["usage"]["input_tokens"], 2)
        self.assertEqual(req["usage_source_lines"], [1, 2])
        self.assertTrue(any(c["field"] == "output_tokens" for c in req["usage_conflicts"]))
        self.assertIsNone(totals(parsed)["total_tokens"])

    def test_zeroed_history_copy_does_not_reconstruct_usage_from_iterations(self) -> None:
        zero = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0,
                "iterations": [{"type": "message", "input_tokens": 2,
                                "cache_creation_input_tokens": 634,
                                "cache_read_input_tokens": 962681, "output_tokens": 4899}]}
        parsed = self._usage_rows([(zero, "tool_use")])
        req = parsed["requests"][0]
        self.assertTrue(all(v is None for v in req["usage"].values()))
        self.assertEqual(len(req["usage_conflicts"]), 4)
        self.assertEqual(req["usage_observations"][0]["usage"]["output_tokens"], 0)
        self.assertIsNone(totals(parsed)["total_tokens"])

    def test_disagreement_without_terminal_evidence_is_not_arbitrated_by_order(self) -> None:
        first = {"input_tokens": 2, "output_tokens": 8}
        second = {"input_tokens": 2, "output_tokens": 42}
        for observations in ([(first, None), (second, None)], [(second, None), (first, None)]):
            req = self._usage_rows(observations)["requests"][0]
            self.assertIsNone(req["usage"]["output_tokens"])
            self.assertEqual(req["usage"]["input_tokens"], 2)
            self.assertTrue(req["usage_conflicts"])

    def test_parser_coverage_and_request_line_references_keep_no_text(self) -> None:
        rows = [*_assistant("refs", {}, [], n_lines=2),
                json.dumps({"type": "system", "subtype": "compact_boundary", "content": "SECRET SUMMARY"}),
                json.dumps({"type": "progress", "data": "SECRET PROGRESS"}), "{bad-json", "[]"]
        self.main.write_text("\n".join(rows) + "\n", encoding="utf-8")
        parsed = parse_transcript(self.main)
        self.assertEqual(parsed["requests"][0]["source_lines"], [1, 2])
        self.assertEqual((parsed["requests"][0]["first_line"], parsed["requests"][0]["last_line"]), (1, 2))
        self.assertEqual(parsed["coverage"]["line_types"], {"assistant": 2, "system": 1, "progress": 1})
        self.assertEqual(parsed["coverage"]["ignored_types"], {"system": 1, "progress": 1})
        self.assertEqual(parsed["coverage"]["invalid_json_lines"], 1)
        self.assertEqual(parsed["coverage"]["non_object_lines"], 1)
        self.assertEqual(parsed["coverage"]["compaction_lines"], [3])
        self.assertNotIn("SECRET", json.dumps(parsed))

    def test_reimport_same_request_count_replaces_older_missing_usage(self) -> None:
        self.main.write_text("\n".join(_assistant("same", {}, [])) + "\n", encoding="utf-8")
        store = EventStore(self.home, self.cfg)
        view = load_session(store, "claude-code", "tx", self.cfg)
        import_session(store, self.cfg, "claude-code", "tx", view, set())
        self.main.write_text("\n".join(_assistant("same", {"input_tokens": 1, "cache_creation_input_tokens": 0,
                                                           "cache_read_input_tokens": 0, "output_tokens": 2}, [])) + "\n", encoding="utf-8")
        view = load_session(store, "claude-code", "tx", self.cfg)
        existing = {e.get("event_id") for e in store.read_session_events("claude-code", "tx")[0]}
        import_session(store, self.cfg, "claude-code", "tx", view, existing)
        current = session_tokens(load_session(store, "claude-code", "tx", self.cfg))
        self.assertEqual(current["total_tokens"], 3)

    def test_invalid_token_counts_do_not_become_measured_values(self) -> None:
        self.main.write_text("\n".join(_assistant("invalid", {"input_tokens": -1, "cache_creation_input_tokens": True,
                                                              "cache_read_input_tokens": float("inf"), "output_tokens": 1.5}, [])) + "\n", encoding="utf-8")
        total = totals(parse_transcript(self.main))
        self.assertIsNone(total["total_tokens"])
        self.assertEqual(total["usage_coverage"]["status"], "missing")

    def test_incomplete_last_line_is_not_an_observed_request(self) -> None:
        self.main.write_text("\n".join(_assistant("partial-write", {"input_tokens": 2, "output_tokens": 3}, [])), encoding="utf-8")
        parsed = parse_transcript(self.main)
        self.assertEqual(parsed["requests"], [])
        self.assertEqual(parsed["coverage"]["incomplete_lines"], 1)
        self.assertTrue(any("incomplete" in w for w in parsed["warnings"]))

    def test_partial_call_cost_does_not_invent_its_missing_component(self) -> None:
        store = EventStore(self.home, self.cfg)
        call = load_session(store, "claude-code", "tx", self.cfg).calls[0]
        call.usage = {"source": "claude-code:transcript", "uncached_input_tokens": None, "output_tokens": 5}
        cost = observed_cost([call])
        self.assertIsNone(tokens_of(call))
        self.assertIsNone(cost["tokens"]["total"])
        self.assertIsNone(cost["tokens"]["uncached_input"])
        self.assertEqual(cost["tokens"]["observed_total"], 5)
        self.assertEqual(cost["tokens"]["complete_for"], 0)
        self.assertIn("partielle", tokens_cost_text(cost))
        self.assertNotIn("None", tokens_cost_text(cost))

    def test_reimport_prefers_coverage_over_legacy_false_zero(self) -> None:
        self.main.write_text("\n".join(_assistant("legacy", {}, [])) + "\n", encoding="utf-8")
        store = EventStore(self.home, self.cfg)
        old = S.empty_event()
        old.update({"source": S.SOURCE_IMPORT, "client": "claude-code", "session_id": "tx", "phase": S.PHASE_USAGE,
                    "event_id": "legacy-usage", "usage": {"scope": "session", "source": "claude-code:transcript",
                    "requests": 1, "input_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0,
                    "output_tokens": 0, "total_tokens": 0}})
        store.write_event(old)
        view = load_session(store, "claude-code", "tx", self.cfg)
        self.assertEqual(session_tokens(view)["total_tokens"], 0, "ancien import avant correction")
        import_session(store, self.cfg, "claude-code", "tx", view, {"legacy-usage"})
        current = session_tokens(load_session(store, "claude-code", "tx", self.cfg))
        self.assertIsNone(current["total_tokens"])
        self.assertEqual(current["usage_coverage"]["status"], "missing")
        self.assertTrue(any(e.get("event_id") == "legacy-usage" for e in store.read_session_events("claude-code", "tx")[0]),
                        "la correction ajoute une observation sans effacer l'historique")


if __name__ == "__main__":
    unittest.main()
