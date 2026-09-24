"""Replay evidence must remain available at its decision, including bad cases."""
import json
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
                {"id": "missing", "question": "metric unavailable", "run": "Run1", "start_line": 5,
                 "end_line": 7, "fields": ["stream.MB"], "expected_missing": ["stream.MB"], "discovery": True},
                {"id": "present", "question": "observed metric", "run": "Run1", "start_line": 5,
                 "end_line": 9, "fields": ["stream.MB"], "expected": [{"field": "stream.MB", "value": 5014}]},
            ]
            result = replay(fixture.cfg, str(fixture.home), "session", "session", cases)
            self.assertEqual(before, fixture.source.read_bytes())
            missing, present = result["cases"]
            self.assertTrue(missing["all_checks_pass"])
            self.assertTrue(present["all_checks_pass"])
            self.assertIn("blocked", json.dumps(present["proposed"]))
            self.assertEqual(present["acquisitions_preserved_in_proposal"], 2)
            self.assertEqual(present["acquisitions_executed_in_replay"], 0)
            self.assertGreater(missing["rendered_utf8_bytes"]["difference"], 0)
            self.assertTrue(missing["proposed"]["discovery"])
            self.assertFalse(present["historical_artifact_version_available"])
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
