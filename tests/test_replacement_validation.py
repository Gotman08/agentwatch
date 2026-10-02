"""Sanity checks transversaux : preuves, delais, couts et rejeu d'import."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agentwatch import cli
from agentwatch.collector.store import EventStore
from agentwatch.config import config_warnings, load_config
from agentwatch.core.correlate import build_session
from agentwatch.detectors.base import finding_cost_union, observed_cost
from agentwatch.detectors.repeated_calls import _cadence, analyse, detect, group_key, simulate_cooldown
from agentwatch.reports.replacements import _sensitivity, evaluate_finding, summary_lines
from agentwatch.reports.trends import build_trends
from agentwatch.selftest import Synth
from tests.test_replacements import call, finding, select, states, view


class ReactionContractTests(unittest.TestCase):
    def scenario(self, **options):
        calls = [call(i) for i in range(4)]  # chaque requete dure 1 s, ce n'est pas une borne future
        cfg = {"detectors": {"repeated_calls": {"cooldowns_s": [30]}}, "replacements": options}
        return select(evaluate_finding(view(calls), finding(calls, "G.repeated_calls"), cfg), "cadence")

    def test_received_result_needs_latency_jitter_and_retry_bounds(self):
        options = dict(reaction_deadline_s=35, response_latency_bound_s=4,
                       scheduling_jitter_bound_s=1, response_bound_includes_retries=True)
        self.assertEqual(states(self.scenario(**options))["reaction_deadline"], "established")
        for missing in ("response_latency_bound_s", "scheduling_jitter_bound_s", "response_bound_includes_retries"):
            with self.subTest(missing=missing):
                changed = dict(options); changed.pop(missing)
                self.assertEqual(states(self.scenario(**changed))["reaction_deadline"], "unknown")
        self.assertEqual(self.scenario(**options)["validity"], "indeterminate")

    def test_nonzero_response_time_and_joint_bounds_refute_equal_deadline(self):
        s = self.scenario(reaction_deadline_s=30, response_latency_bound_s=1,
                          scheduling_jitter_bound_s=0, response_bound_includes_retries=True)
        self.assertEqual(states(s)["reaction_deadline"], "refuted")
        # Chaque composante est < delai ; seule leur somme contredit le contrat.
        s = self.scenario(reaction_deadline_s=60, response_latency_bound_s=25,
                          scheduling_jitter_bound_s=10, response_bound_includes_retries=True)
        self.assertEqual(s["reaction_contract"]["declared_detection_bound_s"], 65)
        self.assertEqual(s["sensitivity"]["all_unknowns_assumed_established"]["validity"], "rejected")

    def test_poll_start_is_a_different_explicit_contract(self):
        s = self.scenario(reaction_deadline_s=30, reaction_deadline_kind="poll_start", scheduling_jitter_bound_s=0)
        self.assertEqual(states(s)["reaction_deadline"], "established")
        self.assertIsNone(s["reaction_contract"]["response_latency_bound_s"])
        self.assertEqual(states(self.scenario(reaction_deadline_s=30))["reaction_deadline"], "unknown")

    def test_timeout_and_retry_cannot_disappear_from_the_scenario(self):
        calls = [call(0, status="timeout", end_ns=11 * 10**9), call(1), call(2)]
        cfg = {"detectors": {"repeated_calls": {"cooldowns_s": [30]}},
               "replacements": {"reaction_deadline_s": 60, "response_latency_bound_s": 2,
                                "scheduling_jitter_bound_s": 0, "response_bound_includes_retries": True}}
        s = select(evaluate_finding(view(calls), finding(calls, "G.repeated_calls"), cfg), "cadence")
        self.assertTrue(s["reaction_contract"]["observed_response_bound_contradiction"])
        self.assertEqual(states(s)["successful_unambiguous_calls"], "refuted")
        self.assertEqual(s["validity"], "rejected")
        self.assertIsNone(s["cost"]["calls"]["reduction_estimate"])


class EvidenceAndReferenceTests(unittest.TestCase):
    def test_hidden_distinct_search_inputs_are_not_one_polling_sequence(self):
        calls = [call(i, tool_name="web_search", category="web", op="web", target_key=None,
                      params={}, params_key="{}", input_fingerprint=f"query-{i}",
                      evidence={"result_phase": "in_progress"}) for i in range(10)]
        self.assertEqual(len({group_key(c) for c in calls}), 10)
        self.assertEqual(detect(view(calls), {}), [])
        repeated = [copy.deepcopy(c) for c in calls]
        for c in repeated: c.input_fingerprint = "same-query"
        self.assertTrue(detect(view(repeated), {}))
        other = call(20, agent_id="other", tool_name="web_search", category="web", op="web", target_key=None,
                     params={}, params_key="{}", input_fingerprint="different-query")
        self.assertTrue(all(g["other_agents"] == 0 for g in analyse(view(repeated + [other]), {})["groups"]))
        calls[1].input_fingerprint = calls[0].input_fingerprint
        self.assertEqual(group_key(calls[0]), group_key(calls[1]))
        calls[0].input_fingerprint = calls[1].input_fingerprint = None
        self.assertNotEqual(group_key(calls[0]), group_key(calls[1]))

    def test_masking_unique_content_proof_downgrades_equality(self):
        calls = [call(0), call(1)]
        first = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
        self.assertEqual(states(first)["observed_content_equal"], "established")
        calls[0].content_fingerprint = None
        masked = select(evaluate_finding(view(calls), finding(calls), {}), "reuse")
        self.assertEqual(states(masked)["observed_content_equal"], "unknown")
        check = next(x for x in masked["verification_requests"] if x["precondition"] == "observed_content_equal")
        self.assertTrue(check["verification"])
        self.assertTrue(check["decision_unlocked"])

    def test_retain_preserves_segment_and_zero_variation_on_known_costs(self):
        calls = [call(i, usage={"source": "codex:rollout", "uncached_input_tokens": 7, "output_tokens": 3}) for i in range(2)]
        original = copy.deepcopy(calls)
        s = select(evaluate_finding(view(calls), finding(calls), {}), "retain")
        self.assertEqual(calls, original)
        self.assertEqual(s["affected_calls"], [c.key for c in calls])
        self.assertEqual(s["removed_calls"], [])
        for unit in ("calls", "output_bytes", "tokens"):
            self.assertEqual(s["cost"][unit]["reduction_estimate"], 0)
        self.assertIsNone(s["cost"]["wall_time_reduction_ms"])

    def test_joint_hypothesis_is_neither_a_real_verdict_nor_an_upper_bound(self):
        facts = [{"name": n, "state": "unknown"} for n in ("available", "equivalent")]
        original = copy.deepcopy(facts)
        result = _sensitivity(facts, [call(0), call(1)], 1)
        self.assertEqual(facts, original)
        self.assertTrue(all(x["validity"] != "admissible" for x in result["one_at_a_time"]))
        self.assertTrue(result["all_unknowns_assumed_established"]["hypothetical_only"])
        self.assertEqual(result["joint_assumptions_consistency"], "not_verified")
        self.assertEqual(result["interaction_coverage"], "not_exhaustive")

    def test_same_past_different_futures_preserve_policy_and_prefix_actions(self):
        prefix = [call(i) for i in range(4)]
        futures = [prefix + [call(4)], prefix + [call(i) for i in range(4, 12)]]
        policy = {"cooldowns_s": [10, 30, 60], "min_tolerated_delay_s": 30}
        results = []
        for calls in futures:
            reps = [{"phase": "done" if i == len(calls)-1 else "in_progress", "round_trip": True} for i in range(1, len(calls))]
            selected = _cadence(calls, reps, [(0, len(calls)-1)], policy)["recommended"]
            results.append((selected["cooldown_s"], [t for t in selected["scheduled_times_ns"] if t <= prefix[-1].start_ns]))
        self.assertEqual(results[0], results[1])
        prefix_replay = simulate_cooldown(prefix, ["in_progress"] * 4, [(0, 3)], 30)
        self.assertEqual(results[0][1], prefix_replay["scheduled_times_ns"])


class CostAndReplayTests(unittest.TestCase):
    def test_overlapping_a_e_g_counts_each_call_and_allocation_once(self):
        calls = [call(i, usage={"source": "codex:rollout", "uncached_input_tokens": 7, "output_tokens": 3}) for i in range(3)]
        fs = [finding(calls[:2], "A.redundant_reads"), finding(calls[1:], "E.tool_gap"), finding(calls, "G.repeated_calls")]
        union = finding_cost_union(calls, fs)
        self.assertEqual((union["call_references"], union["unique_calls"], union["overlapping_references"]), (7, 3, 4))
        self.assertEqual(union["observed_cost"]["tokens"]["total"], 30)
        self.assertEqual(union["observed_cost"]["output_bytes_sum"], 300)
        self.assertIsNone(union["savings_estimate"])

    def test_trends_scopes_costs_and_sensitivity_and_survives_duplicate_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); cfg = load_config(home); cfg["auto_compact_threshold"] = 0
            # Meme identifiant de session et d'appel dans DEUX clients.
            for client in ("codex", "claude-code"):
                s = Synth(home, client=client, session_id="same-id", cfg=cfg)
                s.session_start(); s.user_prompt(); s.read("a.py", "same"); s.read("a.py", "same"); s.stop()
            store = EventStore(home, cfg)
            def duplicate_detector(v, cfg):
                f = finding(v.calls)
                f.observed_cost = observed_cost(v.calls)
                f.evidence = {"operation": "read", "target": "a.py"}
                return [f, copy.deepcopy(f)]  # deux signalements superposes du meme motif
            with mock.patch("agentwatch.reports.trends.run_detectors", side_effect=duplicate_detector):
                before = build_trends(store, cfg, {}, days=0, now_ns=1800000000000000000)
                for client, skey, _ in list(store.iter_sessions()):
                    events, _ = store.read_session_events(client, skey)
                    for event in events: store.write_event(event)  # rejeu des memes event_id
                after = build_trends(store, cfg, {}, days=0, now_ns=1800000000000000000)
            self.assertEqual(before, after)
            self.assertEqual(before["calls"], 4)
            row = before["recurring"][0]
            self.assertEqual((row["sessions"], row["calls"], row["call_references"]), (2, 4, 8))
            self.assertEqual(before["finding_cost_union"]["observed_cost"]["calls"], 4)
            a = row["replacement_analysis_example"]
            self.assertEqual(a["admissible_alternative_ids"], [])
            self.assertIsNone(a["calls_reduction_range"])
            self.assertTrue(a["sensitivity_summary"]["hypothetical_only"])
            for group in before["by_client"]:
                self.assertEqual(group["recurring"], [])


class LegacyConfigTests(unittest.TestCase):
    def test_legacy_ratio_warns_in_json_markdown_trends_and_loaded_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.json").write_text(json.dumps({"auto_compact_threshold": 0,
                "rollouts": {"auto_import": False}, "health": {"enabled": False},
                "detectors": {"repeated_calls": {"tolerated_delay_ratio": 100, "min_tolerated_delay_s": 17}}}), encoding="utf-8")
            cfg = load_config(home)
            self.assertEqual(len(config_warnings(cfg)), 1)
            self.assertIn("17 s", cfg["_config_warnings"][0])
            s = Synth(home, session_id="fixture", cfg=cfg)
            s.session_start(); s.read("a.py", "same"); s.read("a.py", "same"); s.stop()
            for command in (("report", "--session", "fixture"), ("trends", "--days", "0")):
                for fmt in ("json", "markdown"):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(cli.main(["--home", str(home), *command, "--format", fmt]), 0)
                    self.assertIn("tolerated_delay_ratio est ignore", out.getvalue())
                    self.assertIn("17 s", out.getvalue())
                    self.assertIn("fixed_configuration_before_replay", out.getvalue())

    def test_invalid_budget_warning_reports_applied_fallback(self):
        for value in (-5, True, "bad", None, float("nan")):
            cfg = {"detectors": {"repeated_calls": {"tolerated_delay_ratio": 1, "min_tolerated_delay_s": value}}}
            self.assertIn("30 s", config_warnings(cfg)[0])


if __name__ == '__main__':
    unittest.main()
