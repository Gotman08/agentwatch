"""Replay de consigne Claude sur des transcripts synthétiques copies."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from examples.claude_spec_replay import _timed_export, run_replay


def assistant(call_id: str, name: str = "Read") -> dict:
    return {"type": "assistant", "timestamp": "2026-09-24T00:00:00Z", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": {"query": "T5"}}]}}


def result(call_id: str, text: str, error: bool = False) -> dict:
    return {"type": "user", "timestamp": "2026-09-24T00:00:01Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": error}]}}


class ClaudeSpecReplayTests(unittest.TestCase):
    def test_replay_copies_sources_and_separates_text_from_envelope_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "main-session.jsonl"
            old = root / "old-session.jsonl"
            text = "Consigne T5 — état conservé 🧭\n" + ("x" * 40)
            main_rows = [{"type": "user", "timestamp": "2026-09-24T00:00:00Z", "uuid": "ref-uuid",
                          "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
                         assistant("ignored"), result("ignored", "T5 tool result")]
            source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in main_rows), encoding="utf-8")
            old_rows: list[dict] = []
            for number in range(5):
                old_rows += [assistant(f"old-{number}"), result(f"old-{number}", f"resultat-{number} 🚧", error=number == 3)]
            old.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in old_rows), encoding="utf-8")
            before_main = source.read_bytes()
            before_old = old.read_bytes()

            report = run_replay(source=source, old_source=old, output_dir=root / "out", reference_line=1,
                                session="main-session", old_session="old-session", old_start=1, old_end=10,
                                contains="t5", expected_reference=None)

            self.assertEqual(source.read_bytes(), before_main)
            self.assertEqual(old.read_bytes(), before_old)
            self.assertEqual(report["reference"]["uuid"], "ref-uuid")
            self.assertEqual(report["reference"]["text_bytes"], len(text.encode("utf-8")))
            self.assertEqual(report["reference"]["text_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
            exact = report["new"]["exact"]
            self.assertEqual(exact["results"], 1)
            self.assertEqual(exact["truncated_results"], 0)
            self.assertTrue(exact["content_useful_identical"])
            self.assertEqual(exact["references"][0]["source"]["line"], 1)
            self.assertEqual(report["new"]["unique"]["results"], 0)
            self.assertFalse(report["new"]["unique"]["content_useful_identical"])
            self.assertEqual(report["new"]["total_workflow"]["steps"],
                             ["contains search", "contains + preknown source_line"])
            self.assertEqual(report["old"]["calls"], 5)
            self.assertEqual(report["old"]["results"], 5)
            self.assertEqual(report["old"]["last_result_source_line"], 10)
            self.assertEqual(report["old"]["error_results"], 1)
            self.assertEqual(len(report["old"]["call_references"]), 5)
            self.assertEqual(report["old"]["internal_error_hint_count"], 0)
            self.assertEqual(report["old"]["rendered_result_text_bytes"],
                             sum(len(f"resultat-{number} 🚧".encode("utf-8")) for number in range(5)))
            self.assertGreater(report["old"]["result_envelope_bytes"], report["old"]["rendered_result_text_bytes"])
            self.assertFalse(report["comparison"]["token_savings_claimed"])
            self.assertTrue(Path(report["new"]["exact"]["path"]).is_file())
            saved = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["schema"], "claude_spec_replay/1")

    def test_reference_invariants_can_reject_a_wrong_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "main-session.jsonl"
            old = root / "old-session.jsonl"
            source.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "T5"}}) + "\n",
                              encoding="utf-8")
            old.write_text("\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reference historique"):
                run_replay(source=source, old_source=old, output_dir=root / "out", reference_line=1,
                           session="main-session", old_session="old-session", old_start=1, old_end=1,
                           expected_reference={"uuid": "wrong"})

    def test_export_guard_rejects_source_alias_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "transcript source"):
                _timed_export(source, {"transcripts": {}}, Path(tmp) / "home", "source",
                              protected_paths=(source,))

if __name__ == "__main__":
    unittest.main()
