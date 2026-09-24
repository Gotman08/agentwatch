"""Replay evidence must remain available at its decision, including bad cases."""
import json
import copy
import os
import subprocess
import sys
import unittest

from examples.claude_run_replay import replay
from tests import test_inspect_runs as fixtures


class ReplayTests(unittest.TestCase):
    def test_projection_keeps_error_arguments_source_and_missing_future(self):
        fixture = fixtures.RunViewTests()
        fixture.setUp()
        try:
            fixture.call("bad", "python t5_view.py Run1")
            fixture.result("bad", "<tool_use_error>blocked</tool_use_error>", is_error=True)
            fixture.call("read", "python t5_view.py Run1")
            fixture.result("read", "======================== host\n10.0 STREAM observation MB=5014")
            fixture.write()
            before = fixture.source.read_bytes()
            cases = [
                {"id": "missing", "question": "metric unavailable", "run": "Run1", "start_line": 7,
                 "end_line": 9, "fields": ["stream.MB"], "expected_missing": ["stream.MB"], "discovery": True},
                {"id": "present", "question": "observed metric", "run": "Run1", "start_line": 7,
                 "end_line": 11, "fields": ["stream.MB"], "expected": [{"field": "stream.MB", "value": 5014}]},
            ]
            result = replay(fixture.cfg, str(fixture.home), "session", "session", cases)
            self.assertEqual(before, fixture.source.read_bytes())
            missing, present = result["cases"]
            self.assertTrue(missing["all_checks_pass"])
            self.assertTrue(present["all_checks_pass"])
            self.assertTrue(missing["adaptive"]["all_checks_pass"])
            self.assertTrue(present["adaptive"]["all_checks_pass"])
            self.assertIn("blocked", json.dumps(present["proposed"]))
            self.assertEqual(present["acquisitions_preserved_in_proposal"], 2)
            self.assertEqual(present["acquisitions_executed_in_replay"], 0)
            self.assertGreater(missing["rendered_utf8_bytes"]["difference"], 0)
            self.assertTrue(missing["proposed"]["discovery"])
            self.assertFalse(present["historical_artifact_version_available"])
        finally:
            fixture.tearDown()

    def test_adaptation_keeps_short_notification_without_projection(self):
        fixture = fixtures.RunViewTests()
        fixture.setUp()
        try:
            fixture.write()
            case = {"id": "short", "question": "hitch?", "run": "Run1", "start_line": 7,
                    "end_line": 7, "fields": ["hitch_ms"], "expected": [{"field": "hitch_ms", "value": 6719, "role": "host"}]}
            data = replay(fixture.cfg, str(fixture.home), "session", "session", [case])["cases"][0]["adaptive"]
            self.assertTrue(data["all_checks_pass"])
            self.assertEqual(data["steps"]["projection_calls"], 0)
            self.assertEqual(data["choices"][0]["route"], "direct")
            self.assertIn("6719", data["answer"]["path"][0]["content"])
            proof = next(iter(data["answer"]["context"]["evidence"].values()))
            self.assertTrue(proof["version"])
            self.assertEqual(proof["observed_at"], "2026-09-24T12:00:07Z")
            cmd = next(iter(data["detail_commands"].values()))
            self.assertEqual(cmd[cmd.index("--source-line") + 1], "7")
        finally:
            fixture.tearDown()

    def test_projection_uses_requested_fields_not_expected_values_or_future(self):
        fixture = fixtures.RunViewTests()
        fixture.setUp()
        try:
            fixture.call("read", "python t5_view.py Run1 | tail -100")
            text = "\n".join(f"{i}.0 STREAM observation MB={5000 + i} lv=35 filler=" + "x" * 100 for i in range(100))
            fixture.result("read", text)
            fixture.notification("monitor1", "host 200.0 END 0 echec(s)")
            fixture.write()
            original = fixture.source.read_bytes()
            case = {"id": "wide", "question": "recent memory, end?", "run": "Run1", "start_line": 8,
                    "end_line": 9, "fields": ["stream.MB", "run_end"],
                    "expected": [{"field": "stream.MB", "value": 5099, "at": 99.0, "role": "unspecified"}],
                    "expected_missing": ["run_end"]}
            other = copy.deepcopy(case)
            other["expected"][0]["value"] = -1
            results = replay(fixture.cfg, str(fixture.home), "session", "session", [case, other])["cases"]
            a, b = [r["adaptive"] for r in results]
            self.assertEqual(a["answer"], b["answer"])
            self.assertTrue(a["all_checks_pass"])
            self.assertFalse(b["all_checks_pass"])
            self.assertEqual(a["steps"]["projection_calls"], 1)
            self.assertEqual(a["steps"]["necessary_recorded_complements"], [9])
            self.assertEqual(a["steps"]["historical_acquisitions_avoided"], 0)
            self.assertLess(a["rendered_utf8_bytes"]["difference_same_context"], 0)
            context = a["answer"]["context"]
            self.assertEqual(context["missing_in_known_prefix"], ["run_end"])
            self.assertTrue(all(p["partial"] for p in context["evidence"].values()))
            self.assertEqual(original, fixture.source.read_bytes())
        finally:
            fixture.tearDown()

    def test_unflagged_traceback_failure_and_opaque_fragment_stay_visible(self):
        fixture = fixtures.RunViewTests()
        fixture.setUp()
        try:
            wide = "======================== host\n" + "\n".join(f"{i}.0 STREAM observation MB={i}" for i in range(100))
            for cid, suffix in [("trace", "Traceback (most recent call last):\nFileNotFoundError: missing"),
                                ("fail", "100.0 FAIL walkto: still far"),
                                ("opaque", '100.0 LIFE phase {"incomplete":')]:
                fixture.call(cid, "python t5_view.py Run1 | cut -c1-100")
                fixture.result(cid, wide + "\n" + suffix)
            fixture.write()
            case = {"id": "unsafe", "question": "memory?", "run": "Run1", "start_line": 8,
                    "end_line": 13, "fields": ["stream.MB"], "expected": [{"field": "stream.MB", "value": 99}]}
            r = replay(fixture.cfg, str(fixture.home), "session", "session", [case])["cases"][0]
            self.assertEqual(r["adaptive"]["steps"]["projection_calls"], 0)
            for needle in ("FileNotFoundError", "FAIL walkto", "incomplete"):
                self.assertIn(needle, json.dumps(r["adaptive"]["answer"]))
                self.assertIn(needle, json.dumps(r["proposed"]["common"]))
        finally:
            fixture.tearDown()

    def test_known_prefix_fact_is_not_silently_injected_into_direct_answer(self):
        fixture = fixtures.RunViewTests()
        fixture.setUp()
        try:
            fixture.call("read", "python t5_view.py Run1")
            fixture.result("read", "host 100.0 STREAM observation MB=4000")
            fixture.write()
            case = {"id": "scope", "question": "hitch in this result?", "run": "Run1", "start_line": 8,
                    "end_line": 9, "fields": ["hitch_ms"], "expected": [{"field": "hitch_ms", "value": 6719}]}
            r = replay(fixture.cfg, str(fixture.home), "session", "session", [case])["cases"][0]
            self.assertTrue(r["all_checks_pass"])
            self.assertFalse(r["adaptive"]["all_checks_pass"])
            ctx = r["adaptive"]["answer"]["context"]
            self.assertEqual(ctx["missing_in_sequence"], ["hitch_ms"])
            self.assertEqual(ctx["missing_in_known_prefix"], [])
        finally:
            fixture.tearDown()

    def test_complement_does_not_repeat_exact_terminal_fact_already_notified(self):
        fixture = fixtures.RunViewTests()
        fixture.setUp()
        try:
            fixture.notification("monitor1", "host 100.0 END 0 echec(s)")
            fixture.call("read", "python t5_view.py Run1")
            fixture.result("read", "======================== host\n" +
                           "\n".join(f"{i}.0 STREAM observation MB={i} extra=" + "x" * 100 for i in range(100)) +
                           "\n100.0 END 0 echec(s)")
            fixture.write()
            case = {"id": "complement", "question": "memory and count?", "run": "Run1", "start_line": 8,
                    "end_line": 10, "fields": ["stream.MB", "tests_failed"],
                    "expected": [{"field": "tests_failed", "value": 0, "role": "host"}, {"field": "stream.MB", "value": 99}]}
            r = replay(fixture.cfg, str(fixture.home), "session", "session", [case])["cases"][0]["adaptive"]
            self.assertTrue(r["all_checks_pass"])
            projection = next(e for e in r["answer"]["path"] if e["kind"] == "projection")
            self.assertEqual(projection["input"]["fields"], ["stream.MB"])
        finally:
            fixture.tearDown()

    def test_printed_answer_is_utf8_even_with_legacy_pipe_encoding(self):
        fixture = fixtures.RunViewTests()
        fixture.setUp()
        try:
            fixture.notification("monitor1", "host 10.0 ok arrivee admise apres 0.3 s — arrivée 日本")
            fixture.write()
            manifest = fixture.root / "cases.json"
            manifest.write_text(json.dumps([{"id": "utf8", "question": "arrival?", "run": "Run1", "start_line": 8,
                                           "end_line": 8, "fields": ["arrival"],
                                           "expected": [{"field": "arrival", "value": {"kind": "arrival_admitted", "after_s": 0.3}}]}]), encoding="utf-8")
            cmd = [sys.executable, "examples/claude_run_replay.py", "--home", str(fixture.home), "--session", "session",
                   "--thread", "session", "--cases", str(manifest), "--out", str(fixture.root / "audit.json"), "--answer", "utf8"]
            p = subprocess.run(cmd, capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252"})
            self.assertEqual(p.returncode, 0, p.stderr)
            result = json.loads(p.stdout.decode("utf-8"))
            self.assertIn("arrivée 日本", result["path"][0]["content"])
        finally:
            fixture.tearDown()

    def test_unassociated_required_result_is_rejected_until_manual_link_is_declared(self):
        fixture = fixtures.RunViewTests()
        fixture.setUp()
        try:
            fixture.call("opaque", "sh custom-launcher.sh Run1")
            fixture.result("opaque", '{"host":{"ended":false,"exit_code":4294967295}}')
            fixture.rows[-1]["message"]["content"].append({"type": "tool_result", "tool_use_id": "other",
                                                          "content": '{"client":{"ended":false}}'})
            fixture.notification("monitor1", "run finished", status="completed")
            fixture.write()
            case = {"id": "manual", "question": "did it end?", "run": "Run1", "start_line": 8, "end_line": 10,
                    "fields": ["watcher_end", "run_end"], "retain_result_lines": [9],
                    "expected": [{"field": "watcher_end", "value": True}], "expected_missing": ["run_end"]}
            with self.assertRaisesRegex(ValueError, "sans resultat associe"):
                replay(fixture.cfg, str(fixture.home), "session", "session", [case])
            case["common_lines"] = [8, 9]
            data = replay(fixture.cfg, str(fixture.home), "session", "session", [case])["cases"][0]["adaptive"]
            self.assertTrue(data["all_checks_pass"])
            self.assertIn("4294967295", json.dumps(data["answer"]))
            self.assertIn('client', json.dumps(data["answer"]))
            refs = data["answer"]["context"]["evidence"]
            self.assertEqual(sum(k.startswith("9.") for k in refs), 2)
            self.assertEqual(next(v for k, v in refs.items() if k.startswith("9."))["association"], "manifest_common_line")
            case["common_lines"] = [8, 9, 11]
            with self.assertRaisesRegex(ValueError, "hors de la sequence"):
                replay(fixture.cfg, str(fixture.home), "session", "session", [case])
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
