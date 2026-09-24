"""Exports Claude Code: fixtures synthetiques uniquement, sans hooks ni collecte."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from agentwatch.collector.rollouts import iso_to_ns
from agentwatch.reports import inspect_claude as INS

ROOT = "11111111-1111-4111-8111-111111111111"
SECRET = "hunter2hunter2hunter2"
USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 100, "output_tokens": 8}


def row(kind: str, sec: int | None, **fields: object) -> dict:
    return {"type": kind, "timestamp": f"2026-09-24T10:00:{sec:02}.000Z" if sec is not None else None, **fields}


def assistant(rid: str, sec: int | None, blocks: list[dict], **fields: object) -> dict:
    return row("assistant", sec, requestId=rid, uuid=f"uuid-{rid}-{sec}",
               message={"id": f"msg-{rid}", "model": "synthetic-model", "usage": dict(USAGE), "content": blocks}, **fields)


def call(cid: str, name: str = "Read", **args: object) -> dict:
    return {"type": "tool_use", "id": cid, "name": name, "input": args}


def result(cid: str, sec: int, content: object = "source line", error: bool = False) -> dict:
    return row("user", sec, uuid=f"result-{cid}", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": cid, "content": content, "is_error": error}]})


class InspectClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.projects = root / "projects"
        self.main = self.projects / "synthetic-project" / f"{ROOT}.jsonl"
        self.main.parent.mkdir(parents=True)
        self.cfg = {"transcripts": {"claude_projects_dir": str(self.projects)}}
        self.rows = [row("user", 0, uuid="u0", message={"role": "user", "content": "Inspecte la sequence"}),
                     assistant("r1", 1, [call("c1", file_path="a.py"), call("c2", "Bash", command="python probe.py")], parentUuid="u0"),
                     result("c1", 2), result("c2", 3, "Traceback: useful error evidence", False),
                     assistant("r2", 4, [{"type": "text", "text": "Resultat utile"}]),
                     row("system", 5, subtype="compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 1000})]
        self.write()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, rows: list[dict] | None = None, suffix: str = "") -> None:
        self.main.write_text("".join(json.dumps(r) + "\n" for r in (rows if rows is not None else self.rows)) + suffix, encoding="utf-8")

    def export(self, **kwargs: object) -> tuple[str, dict]:
        out = io.StringIO()
        summary = INS.export_session(self.cfg, str(self.home), ROOT[:13], out, **kwargs)
        return out.getvalue(), summary

    def json_export(self, **kwargs: object) -> tuple[list[dict], dict]:
        text, summary = self.export(fmt="jsonl", **kwargs)
        return [json.loads(s) for s in text.splitlines()], summary

    def test_sequence_sources_tool_results_compaction_and_tokens(self) -> None:
        before = self.main.read_bytes()
        rows, summary = self.json_export()
        calls = [r for r in rows if r["kind"] == "appel"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["data"]["result_source"]["line"], 3)
        self.assertEqual(calls[0]["data"]["parent_source"]["line"], 1)
        self.assertEqual(json.loads(calls[0]["data"]["input"]), {"file_path": "a.py"})
        results = [r for r in rows if r["kind"] == "resultat"]
        self.assertEqual(results[1]["data"]["is_error"], False)
        self.assertIn("Traceback", results[1]["data"]["content"])
        self.assertEqual(results[1]["data"]["elapsed_from_timestamps_ms"], 2000)
        self.assertEqual(results[0]["data"]["attribution"]["consumers"], 2)
        self.assertEqual(summary["kinds"]["compaction"], 1)
        self.assertTrue(all({"file", "path", "line", "ordinal", "ts"} <= set(r["source"]) for r in rows))
        measured = summary["threads"][0]["measured"]
        self.assertEqual((measured["requests"], measured["total_input_tokens"], measured["output_tokens"]), (2, 260, 16))
        self.assertEqual(self.main.read_bytes(), before)
        self.assertFalse((self.home / "spool").exists())

    def test_request_deduplication_and_subagent_links_are_observed(self) -> None:
        self.rows.insert(2, assistant("r1", 1, [{"type": "text", "text": "Meme reponse en fragments"}]))
        self.write()
        sub = self.main.with_suffix("") / "subagents" / "agent-abc.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps(assistant("s1", 2, [call("s-c1")], agentId="abc", parentToolUseID="c2", isSidechain=True)) + "\n", encoding="utf-8")
        rows, summary = self.json_export()
        self.assertEqual([r["thread_id"] for r in summary["threads"]], [ROOT, "abc"])
        self.assertEqual(summary["threads"][0]["responses"], 2)
        child_call = next(r for r in rows if r["kind"] == "appel" and r["thread"] == "abc")
        self.assertEqual(child_call["data"]["parentToolUseID"], "c2")
        self.assertEqual(child_call["data"]["parent_tool_sources"][0]["line"], 2)
        _, selected = self.export(thread="abc")
        self.assertEqual(len(selected["threads"]), 1)

    def test_secrets_masked_in_markdown_jsonl_and_structured_inputs(self) -> None:
        self.rows[0]["message"]["content"] = f"password={SECRET}"
        self.rows[1]["message"]["content"][0]["input"] = {"password": SECRET, "nested": [{"token": SECRET}]}
        self.rows[2]["message"]["content"][0]["content"] = json.dumps({"password": SECRET})
        self.rows += [row("future-event", 6, detail={"secret": SECRET})]
        self.write()
        for fmt in ("markdown", "jsonl"):
            text, summary = self.export(fmt=fmt)
            self.assertNotIn(SECRET, text)
            self.assertIn("<secret:", text)
            self.assertGreater(summary["flags"].get("[masque]", 0), 0)

    def test_reasoning_is_opt_in_and_unknown_media_is_flagged(self) -> None:
        self.rows[1]["message"]["content"] += [{"type": "thinking", "thinking": "PRIVATE REASONING"},
                                              {"type": "redacted_thinking", "data": "encrypted-content"},
                                              {"type": "image", "source": {"data": "IMAGE BASE64"}},
                                              {"type": "future-block", "detail": "UNKNOWN BLOCK"}]
        self.write()
        text, summary = self.export(fmt="jsonl")
        self.assertNotIn("PRIVATE REASONING", text)
        self.assertNotIn("encrypted-content", text)
        self.assertNotIn("IMAGE BASE64", text)
        self.assertIn("UNKNOWN BLOCK", text)
        self.assertGreater(summary["flags"].get("[non compris]", 0), 0)
        text, _ = self.export(reasoning=True)
        self.assertIn("PRIVATE REASONING", text)

    def test_progress_omits_nested_reasoning_and_keeps_notifications(self) -> None:
        self.rows.append(row("progress", 6, data={"message": {"content": [
            {"type": "thinking", "thinking": "NESTED PRIVATE REASONING"}]}}))
        self.rows.append(row("attachment", 7, attachment={"type": "queued_command", "prompt":
            f"<task-notification>Tests termines password={SECRET}</task-notification>"}, rendered="Tests termines"))
        self.write()
        text, summary = self.export(fmt="jsonl")
        self.assertNotIn("NESTED PRIVATE REASONING", text)
        self.assertNotIn(SECRET, text)
        self.assertIn("task-notification", text)
        self.assertIn("Tests termines", text)
        self.assertEqual(summary["kinds"]["notification"], 1)
        self.assertEqual(summary["kinds"]["message"], 2)
        self.assertGreater(summary["flags"].get("[omis]", 0), 0)

    def test_period_filters_metrics_and_keeps_outside_references(self) -> None:
        since, until = iso_to_ns("2026-09-24T10:00:02.000Z"), iso_to_ns("2026-09-24T10:00:05.000Z")
        rows, summary = self.json_export(since=since, until=until)
        self.assertFalse(any(r["kind"] == "appel" for r in rows))
        result_row = next(r for r in rows if r["kind"] == "resultat")
        self.assertEqual(result_row["data"]["call_source"]["line"], 2)
        self.assertTrue(any("hors periode" in f for f in result_row["flags"]))
        self.assertEqual(result_row["data"]["attribution"]["uncached_input_tokens"], 15)
        self.assertNotIn("output_tokens", result_row["data"]["attribution"])
        self.assertEqual(summary["threads"][0]["measured"]["total_input_tokens"], 130)
        self.assertEqual(summary["threads"][0]["responses"], 1)
        self.assertFalse(any(r["kind"] == "compaction" for r in rows))

    def test_unknown_dates_visible_but_not_in_period_totals(self) -> None:
        self.rows += [assistant("no-date", None, [{"type": "text", "text": "Date inconnue"}])]
        self.write()
        rows, summary = self.json_export(since=iso_to_ns("2026-09-24T10:00:04.000Z"))
        self.assertTrue(any(r["data"].get("text") == "Date inconnue" for r in rows))
        self.assertEqual(summary["threads"][0]["requests_unknown_timestamp"], 1)
        self.assertEqual(summary["threads"][0]["responses"], 1)
        self.assertGreater(summary["flags"].get("[horodatage non fiable]", 0), 0)

    def test_missing_usage_and_results_are_not_zero(self) -> None:
        self.rows[1]["message"].pop("usage")
        self.rows[1]["message"]["content"].append(call("never-returned"))
        self.rows.append(result("unseen-call", 7, "Error", True))
        self.write()
        rows, summary = self.json_export()
        self.assertIsNone(summary["threads"][0]["measured"]["total_tokens"])
        missing = next(r for r in rows if r["kind"] == "appel" and r["data"]["call_id"] == "never-returned")
        self.assertTrue(any("resultat absent" in f for f in missing["flags"]))
        unseen = next(r for r in rows if r["kind"] == "resultat" and r["data"]["call_id"] == "unseen-call")
        self.assertTrue(any("appel absent" in f for f in unseen["flags"]))
        self.assertTrue(unseen["data"]["is_error"])

    def test_unknown_invalid_and_partial_lines_survive_as_flags(self) -> None:
        self.rows += [row("future-event", 8, something="useful evidence")]
        self.write(suffix="not JSON\n" + json.dumps(assistant("partial", 9, [{"type": "text", "text": "NOT COMPLETE"}])))
        text, summary = self.export(fmt="jsonl")
        self.assertIn("useful evidence", text)
        self.assertNotIn("NOT COMPLETE", text)
        self.assertEqual(summary["kinds"]["invalid_json"], 1)
        self.assertEqual(summary["kinds"]["incomplete"], 1)
        self.assertEqual(summary["threads"][0]["responses"], 2)

    def test_ambiguous_unknown_thread_and_bounds_fail_explicitly(self) -> None:
        with self.assertRaises(ValueError):
            self.export(thread="missing")
        with self.assertRaises(ValueError):
            self.export(since=2, until=1)
        second = self.main.with_name(ROOT[:-1] + "2.jsonl")
        second.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ambigu"):
            self.export()

    def test_bounded_text(self) -> None:
        self.rows[0]["message"]["content"] = "long input " * 100
        self.write()
        rows, summary = self.json_export(max_chars=50)
        message = next(r for r in rows if r["kind"] == "message")
        self.assertLessEqual(len(message["data"]["text"]), 50)
        self.assertGreater(summary["flags"].get("[tronque]", 0), 0)

    def test_numeric_secret_and_malformed_timestamp_do_not_leak_or_crash(self) -> None:
        self.rows[1]["message"]["content"][0]["input"] = {"password": 123456789, "secret": ["sensitive value"]}
        self.rows[0]["timestamp"] = 42
        self.rows.append({"type": ["invalid-type"], "timestamp": {"invalid": True}, "value": "unknown event"})
        self.write()
        for fmt in ("markdown", "jsonl"):
            text, summary = self.export(fmt=fmt)
            self.assertNotIn("123456789", text)
            self.assertNotIn("sensitive value", text)
            self.assertIn("Inspecte la sequence", text)
            self.assertIn("unknown event", text)
            self.assertGreater(summary["flags"].get("[horodatage non fiable]", 0), 0)

    def test_malformed_identifiers_stay_inspectable(self) -> None:
        self.rows[1]["parentUuid"] = {"future": "shape"}
        self.rows[1]["parentToolUseID"] = ["future"]
        self.rows[1]["message"]["content"][0]["id"] = []
        self.rows[2]["message"]["content"][0]["tool_use_id"] = {"future": "shape"}
        self.rows.append({"type": {"future": 1}})
        self.write()
        rows, summary = self.json_export()
        self.assertEqual(summary["kinds"]["appel"], 2)
        self.assertEqual(summary["kinds"]["resultat"], 2)
        self.assertGreater(summary["flags"].get("[non compris]", 0), 0)
        self.assertTrue(any(r["data"].get("call_id") == [] for r in rows))


if __name__ == "__main__":
    unittest.main()
