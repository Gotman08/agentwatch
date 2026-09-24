"""Fenêtres et dédoublonnage sur transcripts synthétiques, jamais les journaux actifs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentwatch.reports.claude_context import analyse


def event(kind, n, uid, parent=None, **extra):
    return {"type": kind, "uuid": uid, "parentUuid": parent,
            "timestamp": f"2026-09-24T10:00:{n:02}Z", **extra}


def response(rid, n, uid, parent, inp=100, output=10, model="model-a", blocks=None):
    return event("assistant", n, uid, parent, requestId=rid, message={
        "model": model, "id": "msg-" + rid,
        "usage": {"input_tokens": inp, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": output},
        "content": blocks or [{"type": "text", "text": "resultat"}]})


class ClaudeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = self.root / "projects" / "p"
        self.projects.mkdir(parents=True)
        self.cfg = {"transcripts": {"claude_projects_dir": str(self.projects.parent)}}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, sid, rows):
        p = self.projects / (sid + ".jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def run_analysis(self, sessions):
        return analyse(self.cfg, str(self.root / "home"), sessions)

    def test_copied_history_is_once_distinct_request_same_content_is_twice(self):
        rows = [event("user", 0, "u", message={"content": "go"}), response("r1", 1, "a", "u")]
        p = self.write("one", rows)
        self.write("two", rows + [response("r2", 2, "b", "a")])
        before = p.read_bytes()
        result = self.run_analysis(["one", "two"])
        self.assertEqual(result["usage"]["requests"], 2)
        self.assertEqual(result["usage"]["total_tokens"], 220)
        self.assertEqual(result["duplicates"]["request_observations_removed"], 1)
        self.assertEqual(len(result["requests"][0]["sources"]), 2)
        self.assertEqual(p.read_bytes(), before)

    def test_compaction_preservation_and_reparented_copy(self):
        r1 = response("r1", 1, "a", "u", blocks=[{"type": "tool_use", "id": "c", "name": "Read", "input": {"file_path": "x"}}])
        output = event("user", 2, "v", "a", message={"content": [{"type": "tool_result", "tool_use_id": "c", "content": "data"}]})
        compact = event("system", 3, "compact", subtype="compact_boundary",
                        compactMetadata={"preTokens": 900, "postTokens": 20,
                                         "preservedMessages": {"allUuids": ["v"]}})
        rows = [event("user", 0, "u"), r1, output, compact, {**output, "parentUuid": "compact"},
                response("r2", 4, "b", "v", inp=150)]
        self.write("one", rows)
        self.write("two", [compact, {**output, "parentUuid": "compact"}, response("r2", 4, "b", "v", inp=150)])
        result = self.run_analysis(["one", "two"])
        self.assertEqual(len(result["compactions"]), 1)
        self.assertEqual(result["usage"]["requests"], 2)
        by_id = {r["request_id"]: r for r in result["requests"]}
        self.assertEqual(by_id["r2"]["boundary_id"], "compact")
        self.assertNotEqual(by_id["r1"]["boundary_id"], "compact")
        self.assertIsNone(by_id["r2"]["input_delta"])
        self.assertEqual(result["compactions"][0]["preserved"][0]["uuid"], "v")
        self.assertIn("compaction", result["compactions"][0]["preservation_scope"])

    def test_compaction_metadata_missing_can_be_completed_but_conflict_stays_unknown(self):
        minimal = event("system", 1, "compact", subtype="compact_boundary")
        full = {**minimal, "compactMetadata": {"preTokens": 900, "postTokens": 50, "preservedMessages": {"allUuids": ["u"]}}}
        self.write("one", [minimal])
        self.write("two", [full])
        result = self.run_analysis(["one", "two"])
        compact = result["compactions"][0]
        self.assertEqual(compact["postTokens"], 50)
        self.assertEqual(compact["preserved_ids"], ["u"])
        self.write("three", [{**minimal, "compactMetadata": {"preTokens": 900, "postTokens": 60,
                                                           "preservedMessages": {"allUuids": ["v"]}}}])
        compact = self.run_analysis(["one", "two", "three"])["compactions"][0]
        self.assertIsNone(compact["postTokens"])
        self.assertIsNone(compact["preserved_ids"])
        self.assertIn("postTokens", compact["metadata_conflicts"])
        self.assertEqual(len(compact["observations"]), 3)

    def test_partial_usage_and_conflicts_are_not_zero_or_arbitrary(self):
        one = response("r1", 1, "a", None)
        other = response("r1", 1, "a", None, inp=200)
        del one["message"]["usage"]["output_tokens"]
        self.write("one", [one])
        self.write("two", [other])
        result = self.run_analysis(["one", "two"])
        self.assertEqual(result["usage"]["requests"], 1)
        self.assertIsNone(result["usage"]["input_tokens"])
        self.assertIsNone(result["usage"]["total_tokens"])
        self.assertEqual(result["requests"][0]["usage"]["output_tokens"], 10)
        self.assertIn("input_tokens", result["requests"][0]["usage_conflicts"])

    def test_conflicted_copy_does_not_disappear_behind_clean_copy(self):
        clean = response("r1", 1, "a", None)
        conflicting = response("r1", 1, "a", None, inp=0)
        conflicting["message"]["stop_reason"] = "end_turn"
        conflicting["message"]["usage"]["iterations"] = [{"type": "message", "input_tokens": 100}]
        self.write("one", [clean])
        self.write("two", [conflicting])
        result = self.run_analysis(["one", "two"])
        req = result["requests"][0]
        self.assertIsNone(req["usage"]["input_tokens"])
        self.assertIn("input_tokens", req["usage_conflicts"])
        self.assertEqual(req["usage_conflict_sources"][0]["source"]["file"], "two.jsonl")

    def test_preserved_segment_need_not_rewrite_original_parent(self):
        self.write("one", [response("r1", 1, "a", None),
                           event("system", 3, "compact", subtype="compact_boundary", compactMetadata={"preservedMessages": {"allUuids": ["a"]}}),
                           response("r2", 4, "b", "a", inp=150)])
        result = self.run_analysis(["one"])
        r2 = result["requests"][1]
        self.assertEqual(r2["boundary_id"], "compact")
        self.assertNotEqual(r2["boundary_observations"][0]["graph_boundary"], "compact")
        self.assertIsNone(r2["input_delta"])

    def test_model_change_and_missing_parent_do_not_make_growth_proof(self):
        self.write("one", [response("r1", 1, "a", None), response("r2", 2, "b", "a", inp=200, model="model-b"),
                           response("r3", 3, "c", "absent", inp=300)])
        result = self.run_analysis(["one"])
        self.assertIsNone(result["requests"][1]["input_delta"])
        self.assertIsNone(result["requests"][2]["boundary_id"])
        self.assertTrue(result["requests"][2]["boundary_limits"])

    def test_notifications_and_results_have_sources_not_persistent_retention(self):
        self.write("one", [response("r1", 1, "a", None, blocks=[{"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "read"}}]),
                           event("attachment", 2, "n", "a", attachment={"type": "queued_command", "prompt": "done"}),
                           event("user", 3, "v", "n", message={"content": [{"type": "tool_result", "tool_use_id": "c", "is_error": False, "content": "Traceback (most recent call last):\nTypeError: fail"}]}),
                           response("r2", 4, "b", "v", inp=250)])
        result = self.run_analysis(["one"])
        window = result["windows"][0]
        self.assertEqual(window["content_counts"]["notification"], 1)
        self.assertEqual(window["content_counts"]["tool_result"], 1)
        self.assertTrue(window["largest_results"][0]["internal_error_hint"])
        self.assertFalse(window["largest_results"][0]["is_error"])
        self.assertEqual(result["requests"][1]["input_delta"], 150)
        self.assertIn("indetermine", result["retention_limit"])

    def test_window_keeps_initial_prompt_and_output_after_first_response_block(self):
        self.write("one", [event("user", 0, "u", message={"content": "initial specification"}),
                           response("r1", 1, "a", "u"),
                           event("attachment", 2, "n", "a", attachment={"type": "queued_command", "prompt": "done"}),
                           response("r1", 3, "b", "n")])
        result = self.run_analysis(["one"])
        window = result["windows"][0]
        self.assertEqual(window["content_counts"]["user_text"], 1)
        self.assertEqual(window["content_counts"]["notification"], 1)
        self.assertEqual(window["content_counts"]["assistant_text"], 2)
        self.assertEqual(window["content_before_first_request"], 1)

    def test_unknown_or_equal_request_dates_do_not_support_delta(self):
        a = response("r1", 1, "a", None)
        b = response("r2", 1, "b", "a", inp=200)
        c = response("r3", 3, "c", "b", inp=300)
        c.pop("timestamp")
        self.write("one", [a, b, c])
        result = self.run_analysis(["one"])
        self.assertIsNone(result["requests"][1]["input_delta"])
        self.assertIsNone(result["requests"][2]["input_delta"])
        self.assertFalse(result["windows"][0]["temporal_order_complete"])
        self.assertIsNone(result["windows"][0]["growth_first_to_last"])
        self.assertIsNone(result["windows"][0]["input_last"])
        self.assertIsNone(result["windows"][0]["content_before_first_request"])
        self.assertIsNone(result["windows"][0]["first_source"])

    def test_configuration_change_separates_segments_and_temporal_content(self):
        a = response("r1", 1, "a", "u")
        b = response("r2", 3, "b", "a", inp=200)
        a["effort"], b["effort"] = "max", "xhigh"
        self.write("one", [event("user", 0, "u", message={"content": "spec"}), a,
                           event("attachment", 2, "n", "a", attachment={"type": "prompt_snapshot", "systemPrompt": "hidden"}), b])
        result = self.run_analysis(["one"])
        self.assertEqual(len(result["windows"]), 2)
        self.assertEqual(result["windows"][0]["content_counts"]["context_change"], 1)
        self.assertNotIn("hidden", json.dumps(result))
        self.assertIsNone(result["requests"][1]["input_delta"])

    def test_context_attachment_shapes_and_secrets(self):
        self.write("one", [response("r1", 1, "a", None),
                           event("attachment", 2, "i", "a", attachment={"type": "instructions", "changed": True,
                                 "files": [{"path": "rules.md", "content": "HIDDEN INSTRUCTIONS"}]}),
                           event("attachment", 3, "e", "i", attachment={"type": "environment", "changes": [{"key": "shell"}],
                                 "snapshot": {"shell": "pwsh", "unexpected": "password=do-not-export"}}),
                           event("attachment", 4, "m", "e", attachment={"type": "model", "identity": {"modelId": "model-a"}})])
        result = self.run_analysis(["one"])
        changes = {c["subtype"]: c for c in result["context_changes"]}
        self.assertEqual(set(changes), {"instructions", "environment", "model"})
        self.assertTrue(changes["instructions"]["observed_fields"]["changed"])
        self.assertEqual(changes["environment"]["observed_fields"]["changes_count"], 1)
        self.assertEqual(changes["model"]["observed_fields"]["identity"]["modelId"], "model-a")
        self.assertNotIn("HIDDEN INSTRUCTIONS", json.dumps(result))
        self.assertNotIn("do-not-export", json.dumps(result))

    def test_non_text_result_is_not_zero_text_or_empty_payload(self):
        contents = ["", [{"type": "image", "source": {"data": "PRIVATE-IMAGE"}}], {"count": 7}]
        self.write("one", [response("r1", 1, "a", None), *[
            event("user", n + 2, "v" + str(n), "a", message={"content": [{"type": "tool_result", "tool_use_id": str(n), "content": value}]})
            for n, value in enumerate(contents)]])
        result = self.run_analysis(["one"])
        window = result["windows"][0]
        self.assertEqual(window["content_bytes"]["tool_result"], 0)
        self.assertEqual(window["content_bytes_unknown_counts"]["tool_result"], 2)
        non_text = window["non_text_results"]
        self.assertEqual(len(non_text), 2)
        self.assertTrue(all(r["bytes"] is None and r["serialized_json_bytes"] > 0 for r in non_text))
        self.assertNotEqual(non_text[0]["content_fingerprint"], non_text[1]["content_fingerprint"])
        self.assertNotIn("PRIVATE-IMAGE", json.dumps(result))

    def test_cli_preserves_source_and_hardlink_and_rejects_codex_filters_before_import(self):
        from agentwatch.cli import main
        source = self.write("one", [response("r1", 1, "a", None)])
        home = self.root / "home"
        home.mkdir()
        (home / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        before = source.read_bytes()
        targets = [source]
        alias = self.root / "alias.jsonl"
        alias.hardlink_to(source)
        targets.append(alias)
        for target in targets:
            with self.assertRaisesRegex(SystemExit, "journal source"):
                main(["--home", str(home), "claude-context", "--session", "one", "--out", str(target)])
            self.assertEqual(source.read_bytes(), before)
        with patch("agentwatch.cli._auto_import_rollouts") as auto_import:
            with self.assertRaisesRegex(SystemExit, "claude-code"):
                main(["--home", str(home), "inspect", "--session", "one", "--kind", "message"])
            auto_import.assert_not_called()


if __name__ == "__main__":
    unittest.main()
