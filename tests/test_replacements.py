"""Contre-exemples : une baisse du compteur n'est jamais une preuve de validite."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agentwatch import cli
from agentwatch.config import load_config
from agentwatch.core.correlate import Call, SessionView
from agentwatch.detectors.base import Finding
from agentwatch.detectors.repeated_calls import _cadence, simulate_cooldown
from agentwatch.reports.observed_graph import build_observed_graph, check_removal
from agentwatch.reports.replacements import attach_replacements, evaluate_finding, summary_lines
from agentwatch.selftest import Synth


def call(i: int, **changes) -> Call:
    values = dict(key=f"main|c{i}", call_id=f"c{i}", client="codex", session_id="fixture", seq=i,
                  agent_id="main", context_epoch=0, tool_name="Read", category="read",
                  op="read", op_key="read:a.py:{}", op_target="a.py", target="a.py", target_key="a.py", target_kind="path",
                  project_dir="/project", params={}, status="success",
                  content_fingerprint="same-content", resource_revision="same-observed-content",
                  start_ns=(i * 10 + 1) * 10**9, end_ns=(i * 10 + 2) * 10**9,
                  has_start=True, has_end=True, event_ids=[f"e{i}-start", f"e{i}-end"],
                  output_size_bytes=100, duration_ms=1000)
    values.update(changes)
    return Call(**values)


def finding(calls: list[Call], rule: str = "A.redundant_reads") -> Finding:
    return Finding(rule_id=rule, rule_version="fixture", kind="fixture", title="fixture", confidence="high",
                   confidence_rationale="fixture", calls=[c.key for c in calls], call_refs=[],
                   evidence={}, explanation="fixture", counter_indications=[], missing_data=[],
                   observed_cost={}, proposal={}, validation_protocol=[])


def view(calls: list[Call]) -> SessionView:
    return SessionView(client="codex", session_id="fixture", calls=calls)


def select(analysis: dict, strategy: str) -> dict:
    return next(s for s in analysis["scenarios"] if s["strategy"] == strategy)


def states(scenario: dict) -> dict:
    return {f["name"]: f["state"] for f in scenario["preconditions"]}


class ObservedGraphTests(unittest.TestCase):
    def test_shared_job_fingerprint_is_an_observation_not_a_submission_link(self):
        calls = [call(i, params={"_fp": {"job_id": "local-hmac"}}, mcp_server="jobs") for i in range(4)]
        calls[2].context_epoch = 1
        calls[3].mcp_server = "other-jobs"
        graph = build_observed_graph(view(calls))
        edges = [e for e in graph["edges"] if e["kind"] == "shared_resource_reference"]
        self.assertEqual(len(edges), 1)
        self.assertFalse(edges[0]["requires_source"])
        self.assertEqual(edges[0]["basis"]["representation"], "fingerprint")

    def test_equality_and_temporal_proximity_are_not_dependencies(self):
        calls = [call(0), call(1), call(2, content_fingerprint="different")]
        graph = build_observed_graph(view(calls))
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual([e["kind"] for e in graph["edges"]], ["observed_content_equality"])
        self.assertFalse(graph["edges"][0]["requires_source"])
        self.assertEqual(check_removal(graph, [calls[0].key])["state"], "established")
        self.assertEqual(graph["dependency_coverage"], "partial")

    def test_explicit_producer_cannot_be_removed_while_consumer_remains(self):
        producer = call(0, op="mcp", category="mcp", tool_name="submit_job")
        consumer = call(1, params={"source_call_id": "c0"}, tool_name="job_status")
        graph = build_observed_graph(view([producer, consumer]))
        edges = [e for e in graph["edges"] if e["requires_source"]]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["evidence"][0]["event_ids"], producer.event_ids)
        self.assertEqual(check_removal(graph, [producer.key])["state"], "refuted")

    def test_future_failed_ambiguous_cross_actor_and_compacted_sources_are_unresolved(self):
        for change in ({"end_ns": 100 * 10**9}, {"status": "error"}, {"ambiguous": True},
                       {"agent_id": "worker"}, {"context_epoch": 1}, {"session_id": "another"}):
            with self.subTest(change=change):
                producer = call(0, **change)
                consumer = call(1, params={"source_call_id": "c0"})
                graph = build_observed_graph(view([producer, consumer]))
                self.assertFalse(any(e["requires_source"] for e in graph["edges"]))
                self.assertEqual(len(graph["unresolved"]), 1)

    def test_colliding_call_ids_do_not_choose_a_producer(self):
        calls = [call(0), call(1, call_id="c0"), call(2, params={"source_call_id": "c0"})]
        graph = build_observed_graph(view(calls))
        self.assertFalse(any(e["requires_source"] for e in graph["edges"]))
        self.assertEqual(len(graph["unresolved"]), 1)


class ReplacementTests(unittest.TestCase):
    def test_identical_reads_do_not_establish_availability_stability_or_savings(self):
        calls = [call(0), call(1), call(2)]
        result = evaluate_finding(view(calls), finding(calls), {})
        reuse = select(result, "reuse")
        fs = states(reuse)
        self.assertEqual(reuse["validity"], "indeterminate")
        self.assertEqual(fs["observed_content_equal"], "established")
        self.assertEqual(fs["information_still_available"], "unknown")
        self.assertEqual(fs["version_stable_before_reuse"], "unknown")
        self.assertIsNone(reuse["cost"]["calls"]["reduction_estimate"])
        self.assertIsNone(reuse["cost"]["tokens"]["reduction_estimate"])
        self.assertIsNone(reuse["cost"]["wall_time_reduction_ms"])
        optimistic = reuse["sensitivity"]["all_unknowns_assumed_established"]
        self.assertTrue(optimistic["hypothetical_only"])
        self.assertEqual(optimistic["calls_reduction_estimate"], 2)
        self.assertIsNone(result["calls_reduction_range"])
        self.assertEqual(result["recommendation"], "retain_until_preconditions_verified")
        self.assertIn("2/3", "\n".join(summary_lines(result)))

    def test_different_ranges_are_rejected_even_with_equal_output(self):
        calls = [call(0), call(1, op_key="read:a.py:1-10", op_params={"range": [1, 10]})]
        reuse = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
        self.assertEqual(states(reuse)["same_scope"], "refuted")
        self.assertEqual(reuse["validity"], "rejected")

    def test_cross_actor_is_rejected_and_compaction_is_unknown(self):
        for changes, name, expected in (({"agent_id": "worker"}, "same_actor", "refuted"),
                                        ({"context_epoch": 1}, "same_context_epoch", "unknown")):
            with self.subTest(changes=changes):
                calls = [call(0), call(1, **changes)]
                reuse = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
                self.assertEqual(states(reuse)[name], expected)
                self.assertNotEqual(reuse["validity"], "admissible")

    def test_resource_revision_observed_later_is_not_a_prior_version_guarantee(self):
        calls = [call(0), call(1)]
        result = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
        self.assertEqual(states(result)["version_stable_before_reuse"], "unknown")
        calls[1].content_fingerprint = "external-edit"
        result = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
        self.assertEqual(result["validity"], "rejected")

    def test_incomplete_acquisition_missing_ids_and_failures_cannot_be_removed(self):
        for changes in ({"end_ns": 50 * 10**9}, {"status": "open"}, {"ambiguous": True}):
            with self.subTest(changes=changes):
                calls = [call(0, **changes), call(1)]
                result = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
                self.assertNotEqual(result["validity"], "admissible")
        calls = [call(0), call(1, status="error")]
        result = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
        self.assertEqual(result["validity"], "rejected")
        f = finding(calls)
        f.calls.append("missing")
        self.assertEqual(states(select(evaluate_finding(view(calls), f, {}), "reuse"))["complete_call_scope"], "unknown")

    def test_tests_builds_scripts_and_mutations_are_preserved(self):
        for op in ("run_tests", "build", "run_script", "write", "edit", "agent"):
            with self.subTest(op=op):
                calls = [call(0, op=op), call(1, op=op)]
                reuse = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
                self.assertEqual(reuse["validity"], "rejected")
                self.assertIsNone(reuse["cost"]["calls"]["reduction_estimate"])
                self.assertIsNone(reuse["sensitivity"]["all_unknowns_assumed_established"]["calls_reduction_estimate"])

    def test_removing_a_read_that_supplies_another_call_is_rejected(self):
        calls = [call(0), call(1), call(2, params={"source_call_id": "c1"})]
        result = select(evaluate_finding(view(calls), finding(calls[:2]), {}), "reuse")
        self.assertEqual(states(result)["observed_dependencies_preserved"], "refuted")

    def test_mcp_read_name_is_not_a_side_effect_contract(self):
        calls = [call(i, category="mcp", op="mcp_read", tool_name="mcp__server__get_status") for i in range(2)]
        result = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
        self.assertEqual(states(result)["read_only_operations"], "unknown")

    def test_skill_and_project_rule_have_no_automatic_call_reduction(self):
        for rule in ("B.error_loops", "D.automation_candidates", "E.tool_gap", "F.repeated_guidance"):
            with self.subTest(rule=rule):
                calls = [call(0), call(1)]
                guide = select(evaluate_finding(view(calls), finding(calls, rule), {}), "guidance")
                self.assertIsNone(guide["cost"]["calls"]["reduction_estimate"])
                self.assertIsNone(guide["sensitivity"]["all_unknowns_assumed_established"]["calls_reduction_estimate"])

    def test_catalogue_classifies_availability_without_inferring_equivalence(self):
        exact = call(0, tool_name="mcp__cluster__wait")
        calls = [exact, call(1), call(2)]
        f = finding(calls[1:], "E.tool_gap")
        entry = {"id": "cluster", "rule_id": f.rule_id, "strategy": "structured", "tool_name": exact.tool_name,
                 "contract": "return id, status, errors"}
        cfg = {"replacements": {"capabilities": [entry]}}
        scenario = select(evaluate_finding(view(calls), f, cfg), "structured")
        self.assertEqual(scenario["capability"]["status"], "observed_available")
        self.assertEqual(scenario["capability"]["equivalence"], "unknown")
        self.assertEqual(scenario["validity"], "indeterminate")
        exact.start_ns, exact.end_ns = 90 * 10**9, 91 * 10**9
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "structured")["capability"]["status"],
                         "declared_unverified")
        entry["status"] = "to_create"
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "structured")["validity"], "rejected")
        self.assertEqual(select(evaluate_finding(view(calls), f, {}), "structured")["capability"]["status"], "unknown")

    def test_catalogue_does_not_match_related_server_or_wrong_client(self):
        calls = [call(0, tool_name="mcp__cluster__status"), call(1), call(2)]
        f = finding(calls[1:], "E.tool_gap")
        entry = {"rule_id": f.rule_id, "strategy": "structured", "tool_name": "mcp__cluster__wait"}
        cfg = {"replacements": {"capabilities": [entry]}}
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "structured")["capability"]["status"],
                         "declared_unverified")
        entry["client"] = "claude-code"
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "structured")["capability"]["status"], "unknown")

    def test_batch_parameters_and_volume_are_checked(self):
        calls = [call(0), call(1, op_key="read:b.py:{}", op_target="b.py")]
        f = finding(calls, "C.batchable")
        cfg = {"replacements": {"capabilities": [{"rule_id": f.rule_id, "strategy": "batch",
                                                 "tool_name": "batch_read", "max_items": 1}]}}
        batch = select(evaluate_finding(view(calls), f, cfg), "batch")
        self.assertEqual(states(batch)["parameters_known_at_start"], "unknown")
        self.assertEqual(states(batch)["no_intermediate_dependency"], "unknown")
        self.assertEqual(states(batch)["batch_volume"], "refuted")

    def test_explicit_intermediate_dependency_blocks_batch(self):
        calls = [call(0), call(1, params={"source_call_id": "c0"})]
        batch = select(evaluate_finding(view(calls), finding(calls, "C.batchable"), {}), "batch")
        self.assertEqual(states(batch)["no_intermediate_dependency"], "refuted")

    def test_passive_deterministic_and_does_not_mutate_view_or_detector_evidence(self):
        calls = [call(0), call(1)]
        v, f = view(calls), finding(calls)
        original = copy.deepcopy((v, f))
        with mock.patch("subprocess.run", side_effect=AssertionError("no execution")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("no network")), \
             mock.patch("builtins.open", side_effect=AssertionError("no source access")):
            result = evaluate_finding(v, f, {})
            self.assertEqual(result, evaluate_finding(v, f, {}))
        self.assertEqual((v, f), original)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_disabled_analysis_removes_old_enrichment(self):
        calls = [call(0), call(1)]
        v, f = view(calls), finding(calls)
        self.assertIsNotNone(attach_replacements(v, [f], {}))
        self.assertIsNotNone(f.replacement_analysis)
        self.assertIsNone(attach_replacements(v, [f], {"replacements": {"enabled": False}}))
        self.assertIsNone(f.replacement_analysis)


class CausalCadenceTests(unittest.TestCase):
    def test_invalid_cooldowns_are_ignored_and_zero_direct_cooldown_is_rejected(self):
        calls = [call(0), call(1)]
        d = {"cooldowns_s": [0, -1, True, "60", float("inf"), float("nan"), 30]}
        cad = _cadence(calls, [{"phase": "done", "round_trip": True}], [(0, 1)], d)
        self.assertEqual([s["cooldown_s"] for s in cad["simulation"]], [30])
        with self.assertRaises(ValueError):
            simulate_cooldown(calls, ["in_progress", "done"], [(0, 1)], 0)

    def test_last_deferred_terminal_request_is_retained_and_counted(self):
        calls = [call(i) for i in range(3)]
        sim = simulate_cooldown(calls, ["in_progress", "in_progress", "done"], [(0, 2)], 30)
        self.assertEqual(sim["calls"], 2)
        self.assertEqual(sim["avoided"], 1)
        self.assertEqual(sim["retained_indices"], [0, 2])
        self.assertEqual(sim["scheduled_times_ns"], [10**9, 31 * 10**9])
        self.assertEqual(sim["max_delay_s"], 10)

    def test_fixed_policy_does_not_depend_on_trace_length_or_completion_time(self):
        d = {"cooldowns_s": [10, 30, 60, 120], "min_tolerated_delay_s": 30, "tolerated_delay_ratio": 100}
        for n in (3, 20, 100):
            calls = [call(i) for i in range(n)]
            reps = [{"phase": "done" if i == n - 1 else "in_progress", "round_trip": True} for i in range(1, n)]
            cad = _cadence(calls, reps, [(0, n - 1)], d)
            self.assertEqual(cad["recommended"]["cooldown_s"], 30)
            self.assertEqual(cad["policy_selection"], "fixed_configuration_before_replay")

    def test_exact_deadline_and_episode_end_do_not_double_count(self):
        calls = [call(i, start_ns=t * 10**9, end_ns=(t + 1) * 10**9) for i, t in enumerate((1, 11, 31, 200, 205))]
        sim = simulate_cooldown(calls, ["in_progress"] * 5, [(0, 2), (3, 4)], 30)
        self.assertEqual(sim["retained_indices"], [0, 2, 3, 4])
        self.assertEqual(sim["scheduled_times_ns"], [10**9, 31 * 10**9, 200 * 10**9, 230 * 10**9])
        self.assertIsNone(sim["max_delay_s"])

    def test_default_delay_budget_is_not_treated_as_user_deadline(self):
        calls = [call(i, evidence={"result_phase": "in_progress"}) for i in range(4)]
        f = finding(calls, "G.repeated_calls")
        cfg = {"detectors": {"repeated_calls": {"cooldowns_s": [10, 30]}}}
        result = evaluate_finding(view(calls), f, cfg)
        self.assertEqual(states(select(result, "cadence"))["reaction_deadline"], "unknown")
        cfg["replacements"] = {"reaction_deadline_s": 20}
        result = evaluate_finding(view(calls), f, cfg)
        cadences = [s for s in result["scenarios"] if s["strategy"] == "cadence"]
        self.assertEqual([states(s)["reaction_deadline"] for s in cadences], ["unknown", "refuted"])
        self.assertTrue(all(s["cost"]["calls"]["reduction_estimate"] is None for s in cadences))

    def test_unreliable_timestamps_prevent_a_numeric_replay(self):
        calls = [call(0), call(1)]
        v = view(calls)
        v.timing_unreliable_agents = ["main"]
        s = select(evaluate_finding(v, finding(calls, "G.repeated_calls"), {}), "cadence")
        self.assertIsNone(s["replay"])
        self.assertEqual(s["validity"], "indeterminate")


class ReportIntegrationTests(unittest.TestCase):
    def test_short_polling_series_stays_below_threshold_with_final_request_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Synth(Path(tmp))
            s.session_start()
            s.user_prompt()
            s.response_gap_ms = 20_000
            for i in range(10):
                s.mcp("jobs", "job_status", {"job_id": 1}, json.dumps({"status": "RUNNING" if i < 9 else "COMPLETED"}))
            from agentwatch.detectors.repeated_calls import analyse
            group = next(g for g in analyse(s.view(), s.cfg)["groups"] if g["tool"] == "mcp__jobs__job_status")
            self.assertEqual(group["cadence"]["recommended"]["avoided"], 2)
            self.assertEqual(s.findings(["repeated_calls"]), [])

    def test_cli_reports_and_trends_expose_analysis_on_synthetic_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = load_config(home)
            cfg["rollouts"]["auto_import"] = False
            cfg["health"]["enabled"] = False
            (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            for sid in ("first", "second"):
                s = Synth(home, session_id=sid, cfg=cfg)
                s.session_start()
                s.user_prompt()
                s.read("a.py", "same")
                s.read("a.py", "same")
                s.stop()

            def run(*args):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(cli.main(["--home", str(home), *args]), 0)
                return buf.getvalue()

            report = json.loads(run("report", "--session", "first", "--format", "json"))
            from agentwatch.reports.shared_json import expand_report
            compact = json.loads(run("report", "--session", "first", "--format", "json-refs"))
            self.assertEqual(compact["report_version"], "2.0")
            self.assertEqual(expand_report(compact), report)
            self.assertEqual(report["report_version"], "1.1")
            self.assertEqual(len(report["observed_dependency_graph"]["nodes"]), 2)
            self.assertTrue(report["findings"][0]["replacement_analysis"])
            self.assertIn("Remplacements sous conditions", run("report", "--session", "first", "--format", "markdown"))
            trends = json.loads(run("trends", "--days", "0", "--format", "json"))
            self.assertTrue(trends["recurring"][0]["replacement_analysis_example"])
            self.assertIn("non additionnes", trends["recurring"][0]["replacement_note"])
            self.assertEqual(trends["findings_by_rule"].get("H"), None)


if __name__ == "__main__":
    unittest.main()
