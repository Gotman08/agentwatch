"""Selection des enregistrements Claude avant bornage du rendu."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from agentwatch.reports import inspect_claude as INS


SESSION = "22222222-2222-4222-8222-222222222222"


def event(kind: str, line: int, content: object, *, timestamp: str | None = None) -> dict:
    return {
        "type": kind,
        "timestamp": timestamp or f"2026-09-24T12:00:{line:02}.000Z",
        "uuid": f"u-{line}",
        "message": {"role": kind, "content": content},
    }


class InspectClaudeFiltersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.projects = root / "projects"
        self.source = self.projects / "fixture" / f"{SESSION}.jsonl"
        self.source.parent.mkdir(parents=True)
        self.cfg = {"transcripts": {"claude_projects_dir": str(self.projects)}}

        long_user = "debut " + ("x" * 80) + " German " + ("y" * 80)
        self.rows = [
            event("user", 1, "Bonjour"),
            event("assistant", 2, [{"type": "tool_use", "id": "call-german", "name": "Bash",
                                    "input": {"command": "echo German"}}]),
            event("user", 3, [{"type": "tool_result", "tool_use_id": "call-german",
                               "content": "German from a command result"}]),
            event("assistant", 4, [{"type": "text", "text": "German assistant text"}]),
            event("user", 5, long_user),
            event("user", 6, "Straße déjà vu"),
        ]
        self.rows[3]["requestId"] = "request-german"
        self.rows[3]["message"]["usage"] = {"input_tokens": 2, "cache_creation_input_tokens": 3,
                                               "cache_read_input_tokens": 4, "output_tokens": 1}
        self.rows[1]["message"]["usage"] = dict(self.rows[3]["message"]["usage"])
        self.write()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self) -> None:
        self.source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows), encoding="utf-8")

    def export(self, **kwargs: object) -> tuple[list[dict], dict]:
        out = io.StringIO()
        summary = INS.export_session(self.cfg, str(self.home), SESSION, out, fmt="jsonl", **kwargs)
        return [json.loads(line) for line in out.getvalue().splitlines()], summary

    def test_user_message_contains_is_before_bound_and_excludes_tools(self) -> None:
        rows, summary = self.export(kinds="message", role="user", contains="gErMaN", max_chars=24)
        messages = [row for row in rows if row["kind"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["source"]["line"], 5)
        self.assertEqual(messages[0]["data"]["role"], "user")
        self.assertTrue(any("tronque" in flag for flag in messages[0]["flags"]))
        self.assertNotIn("German", messages[0]["data"]["text"])
        self.assertEqual(summary["selection"]["selected"], 1)
        self.assertEqual(summary["selection"]["excluded"], summary["selection"]["examined"] - 1)
        self.assertFalse(any(row["kind"] in {"appel", "resultat"} for row in rows))
        self.assertFalse(any(row["kind"] in {"requete", "totaux"} for row in rows))
        self.assertEqual(summary["threads"][0]["measured"]["total_input_tokens"], 18)

    def test_unicode_casefold_and_list_or_null_filters(self) -> None:
        rows, summary = self.export(kinds=["message"], role="user", contains="STRASSE")
        selected = [row for row in rows if row["kind"] == "message"]
        self.assertEqual([row["source"]["line"] for row in selected], [6])
        self.assertEqual(summary["selection"]["filters"]["kinds"], ["message"])

        all_rows, all_summary = self.export(kinds=None, role=None, contains=None)
        self.assertTrue(any(row["kind"] == "appel" for row in all_rows))
        self.assertTrue(any(row["kind"] == "resultat" for row in all_rows))
        self.assertEqual(all_summary["selection"]["excluded"], 0)
        self.assertEqual(all_summary["selection"]["selected"], all_summary["selection"]["examined"])

    def test_source_line_filter_keeps_each_record_provenance(self) -> None:
        rows, summary = self.export(source_line=4)
        selected = [row for row in rows if row["source"]["line"] == 4]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["data"]["text"], "German assistant text")
        self.assertEqual(summary["selection"]["selected"], 1)

    def test_markdown_and_jsonl_keep_secret_masking_with_filter(self) -> None:
        self.rows[0]["message"]["content"] = "password=hunter2hunter2"
        self.write()
        for fmt in ("markdown", "jsonl"):
            out = io.StringIO()
            INS.export_session(self.cfg, str(self.home), SESSION, out, fmt=fmt,
                               kinds="message", role="user", contains="password")
            self.assertNotIn("hunter2hunter2", out.getvalue())
            self.assertIn("<secret:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
