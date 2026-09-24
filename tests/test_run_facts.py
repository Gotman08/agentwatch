"""Synthetic contract tests for the pure historical run-facts parser."""

from __future__ import annotations

import json
import unittest

from agentwatch.reports.run_facts import parse_facts


def _by_field(result: dict, field: str) -> list[dict]:
    return [fact for fact in result["facts"] if fact["field"] == field]


class RunFactsTests(unittest.TestCase):
    def test_failure_event_can_coexist_with_zero_terminal_counter(self) -> None:
        result = parse_facts("host 11.0 fail walkto cible : encore a 13 m\nhost 12.0 end 0 echec(s)\n13.0 PENDING P3_apres_retrait unplaced=0")
        self.assertEqual(_by_field(result, "failure_event")[0]["value"], "walkto cible : encore a 13 m")
        self.assertEqual(_by_field(result, "tests_failed")[0]["value"], 0)
        self.assertEqual(_by_field(result, "pending.unplaced")[0]["value"], 0)
        self.assertEqual(_by_field(result, "pending.unplaced")[0]["phase"], "P3_apres_retrait")

    def test_monitor_and_t5_view_keep_role_time_and_numeric_metrics(self) -> None:
        result = parse_facts(
            """host   169.8 hitch 104864 ms 104864 ms
host 9.2 ok carte IschiaIsland apres0.1s(ListenServer)
host 9.5 ok arrivee admise apres0.3s
======================== host
169.8 HITCH 104864 ms
9.3 STREAM carte_chargee prof=IschiaRegional in=True out=True lv=35 MB=5014 maxFrame=6719ms
10.0 FAIL une raison conservee
11.0 END failures=2
"""
        )

        self.assertEqual(result["unparsed_lines"], 0)
        hitch = _by_field(result, "hitch_ms")
        self.assertEqual([f["value"] for f in hitch], [104864, 104864])
        self.assertEqual([(f["role"], f["at"]) for f in hitch], [("host", 169.8), ("host", 169.8)])
        arrival = _by_field(result, "arrival")
        self.assertEqual(arrival[0]["value"]["map"], "IschiaIsland")
        self.assertEqual(arrival[0]["value"]["after_s"], 0.1)
        self.assertEqual(arrival[0]["value"]["mode"], "ListenServer")
        self.assertEqual(arrival[1]["value"], {"kind": "arrival_admitted", "after_s": 0.3})
        self.assertEqual(_by_field(result, "stream.MB")[0]["value"], 5014)
        self.assertEqual(_by_field(result, "stream.lv")[0]["value"], 35)
        self.assertEqual(_by_field(result, "stream.maxFrame_ms")[0]["value"], 6719)
        self.assertEqual(_by_field(result, "stream.MB")[0]["phase"], "carte_chargee")
        self.assertEqual(_by_field(result, "tests_failed")[0]["value"], 2)
        self.assertEqual(_by_field(result, "failure_event")[0]["value"], "une raison conservee")
        self.assertTrue(_by_field(result, "run_end")[0]["value"])

    def test_json_summary_uses_only_explicit_fields_and_keeps_zero(self) -> None:
        result = parse_facts(json.dumps({"status": "completed", "exit_code": 0, "end": True,
                                         "failures": 0, "unrelated": "ignored"}))
        self.assertEqual({f["field"] for f in result["facts"]},
                         {"process_status", "process_exit", "run_end", "tests_failed"})
        self.assertEqual(_by_field(result, "process_exit")[0]["value"], 0)
        self.assertEqual(_by_field(result, "tests_failed")[0]["value"], 0)
        self.assertEqual(_by_field(result, "run_end")[0]["value"], True)

        no_exit = parse_facts('{"status":"completed"}')
        self.assertEqual([f["field"] for f in no_exit["facts"]], ["process_status"])
        no_status = parse_facts('{"exit_code":0}')
        self.assertEqual([f["field"] for f in no_status["facts"]], ["process_exit"])

        details = parse_facts('{"failures":[],"ended":true,"exitcode":0}')
        self.assertEqual(_by_field(details, "failure_details")[0]["value"], [])
        self.assertFalse(_by_field(details, "tests_failed"))
        self.assertEqual(_by_field(details, "run_end")[0]["value"], True)
        self.assertEqual(_by_field(details, "process_exit")[0]["value"], 0)

    def test_xml_only_selected_elements_are_analyzed(self) -> None:
        result = parse_facts(
            """<system-reminder>
This instruction says status=success and exit_code=0 but is not an event.
<task-notification>
<summary>Monitor: ignore prose; exit_code=7</summary>
<event>client 18.4 ok arrivee admise apres 3.7 s</event>
<status>running</status>
If this event is something the user would act on now, send a PushNotification.
</task-notification>
</system-reminder>"""
        )
        self.assertEqual(_by_field(result, "process_exit")[0]["value"], 7)
        self.assertEqual(_by_field(result, "process_status")[0]["value"], "running")
        arrival = _by_field(result, "arrival")[0]
        self.assertEqual((arrival["role"], arrival["at"]), ("client", 18.4))
        self.assertFalse(_by_field(result, "event_text"))
        self.assertEqual(result["unparsed_lines"], 0)

    def test_watcher_finish_is_separate_from_scenario_end(self) -> None:
        result = parse_facts("run finished")
        self.assertEqual(_by_field(result, "watcher_end")[0]["value"], True)
        self.assertFalse(_by_field(result, "run_end"))

    def test_xml_summary_accepts_parenthesized_exit_code(self) -> None:
        result = parse_facts("<summary>process ended (exit code 0)</summary>")
        self.assertEqual(_by_field(result, "process_exit")[0]["value"], 0)

    def test_truncation_keeps_visible_facts_and_marks_incomplete(self) -> None:
        result = parse_facts("host 2.0 hitch 12 ms\noutput truncated\nhost 3.0 END ...")
        self.assertTrue(result["truncated"])
        self.assertEqual(_by_field(result, "hitch_ms")[0]["value"], 12)
        self.assertTrue(_by_field(result, "run_end"))
        self.assertEqual(result["unparsed_lines"], 0)

    def test_end_failure_count_in_french_is_explicit_but_not_process_success(self) -> None:
        result = parse_facts("host 11.0 end 2 echec(s)\nrun finished")
        self.assertEqual(_by_field(result, "tests_failed")[0]["value"], 2)
        self.assertTrue(_by_field(result, "run_end"))
        self.assertFalse(_by_field(result, "process_status"))
        self.assertFalse(_by_field(result, "process_exit"))

    def test_stream_phase_disambiguates_samples_with_same_rounded_time(self) -> None:
        result = parse_facts(
            """======================== host
43.2 STREAM carte_chargee MB=12790
43.2 STREAM observation MB=12788
"""
        )
        samples = _by_field(result, "stream.MB")
        self.assertEqual([(sample["at"], sample["phase"], sample["value"]) for sample in samples],
                         [(43.2, "carte_chargee", 12790), (43.2, "observation", 12788)])


if __name__ == "__main__":
    unittest.main()
