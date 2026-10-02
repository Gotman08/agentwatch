"""Contrat SYNTHETIQUE : n'etablit aucune propriete du serveur ROMEO reel."""
import copy
import unittest

from agentwatch.reports.replacements import evaluate_finding, summary_lines
from agentwatch.reports.wait_contracts import CLAIMS, scope_for
from tests.test_replacements import call, finding, select, states, view

IMPLEMENTATION = "a" * 64


def fixture():
    calls = [call(i, tool_name="mcp__fixture__wait", op="mcp", category="mcp",
                  params={"job_id": "synthetic-1", "timeout_seconds": 5, "poll_seconds": 1},
                  evidence={"read_only_hint": True, "tool_implementation_sha256": IMPLEMENTATION}) for i in range(3)]
    f = finding(calls, "G.repeated_calls")
    def proof(kind="local_source_review", **extra):
        return {"state": "established", "kind": kind, "basis": "Synthetic bounded protocol, not a ROMEO measurement.",
                "sources": [{"locator": "fixture://bounded-wait", "sha256": IMPLEMENTATION,
                             "line_start": 1, "line_end": 20}], **extra}
    facts = {name: proof("task_requirement_review" if name == "intermediate_work_preserved" else "local_source_review") for name in CLAIMS}
    facts["timeout_parameter_limit"] = proof(value_seconds=100)
    facts["end_to_end_return_bound"] = proof(value_seconds=25, includes_retries_and_locking=True)
    contract = {"schema_version": "1.0", "scope": scope_for(calls), "implementation_sha256": IMPLEMENTATION,
                "timeout_seconds": 20, "reviewed_policy": {"timeout_seconds": 20, "poll_seconds": 1}, "facts": facts,
                "control_return_requirement": {"kind": "declared_preference", "max_seconds": 30, "basis": "Synthetic task requirement."}}
    cfg = {"detectors": {"repeated_calls": {"cooldowns_s": []}}, "replacements": {"capabilities": [
        {"id": "fixture", "strategy": "wait", "rule_id": f.rule_id, "tool_name": calls[0].tool_name, "wait_contract": contract}]}}
    return calls, f, cfg, contract


class WaitContractTests(unittest.TestCase):
    def test_absolute_failure_does_not_claim_introduced_regression(self):
        calls, f, cfg, contract = fixture()
        contract['facts']['results_errors_identity']['state'] = 'refuted'
        contract['identifier_requirement'] = {'mode': 'self_contained_output', 'consumer': 'fixture-reader',
                                            'basis': 'Each output must carry the job identifier.'}
        analysis = evaluate_finding(view(calls), f, cfg)
        s = select(analysis, 'wait')
        self.assertEqual(s['validity'], 'rejected')
        assessment = s['wait_contract']['assessment']
        self.assertEqual(assessment['results_errors_identity']['applied_state'], 'refuted')
        self.assertEqual(assessment['baseline_service_equivalence'], 'unknown')
        self.assertEqual(assessment['replacement_introduces_defect'], 'unknown')
        text = '\n'.join(summary_lines(analysis))
        self.assertIn('degradation introduite par l\'allongement : non etablie', text)
        self.assertIn('identifiant dans chaque sortie autonome', text)

    def test_declaring_call_association_does_not_override_an_audit(self):
        calls, f, cfg, contract = fixture()
        contract['facts']['results_errors_identity']['state'] = 'refuted'
        contract['identifier_requirement'] = {'mode': 'explicit_call_association', 'consumer': 'fixture-reader'}
        s = select(evaluate_finding(view(calls), f, cfg), 'wait')
        self.assertEqual(s['validity'], 'rejected')
        self.assertEqual(s['wait_contract']['identifier_requirement']['consumer_sufficiency'], 'not_automatically_established')
        self.assertEqual(s['capability']['equivalence'], 'unknown')
        contract['identifier_requirement']['mode'] = []
        s = select(evaluate_finding(view(calls), f, cfg), 'wait')
        self.assertEqual(s['wait_contract']['identifier_requirement']['mode'], 'unspecified')

    def test_complete_positive_mask_and_contradiction(self):
        calls, f, cfg, contract = fixture()
        evaluate = lambda: select(evaluate_finding(view(calls), f, cfg), "wait")
        self.assertEqual(evaluate()["validity"], "admissible")
        decisive = contract["facts"].pop("terminal_return")
        self.assertEqual(evaluate()["validity"], "indeterminate")
        self.assertEqual(evaluate()["sensitivity"]["unknown_preconditions"], ["wait_terminal_return"])
        contract["facts"]["terminal_return"] = dict(decisive, state="refuted")
        self.assertEqual(evaluate()["validity"], "rejected")

    def test_current_code_proof_does_not_establish_historical_version(self):
        calls, f, cfg, _ = fixture()
        for c in calls:
            c.evidence.pop("tool_implementation_sha256")
        s = select(evaluate_finding(view(calls), f, cfg), "wait")
        self.assertEqual(s["validity"], "indeterminate")
        self.assertEqual(states(s)["wait_terminal_return"], "unknown")
        self.assertEqual(s["wait_contract"]["audited_facts"]["terminal_return"]["audit_state"], "established")

    def test_sensitivity_preserves_conditional_audit_contradictions(self):
        calls, f, cfg, contract = fixture()
        for c in calls:
            c.evidence.pop("tool_implementation_sha256")
        contract["facts"]["results_errors_identity"]["state"] = "refuted"
        s = select(evaluate_finding(view(calls), f, cfg), "wait")
        self.assertEqual(s["validity"], "indeterminate")
        self.assertEqual(s["sensitivity"]["all_unknowns_assumed_established"]["validity"], "rejected")
        self.assertEqual(next(r for r in s["verification_requests"] if r["precondition"]=="wait_terminal_return")["verification_precondition"], "wait_implementation_bound")

    def test_observed_version_conflict_is_not_overridden(self):
        calls, f, cfg, _ = fixture()
        calls[-1].evidence["tool_implementation_sha256"] = "b" * 64
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "wait")["validity"], "rejected")

    def test_jobs_actors_epochs_and_changed_trace_do_not_inherit_contract(self):
        for change in ({"params": {"job_id": "another", "timeout_seconds": 5, "poll_seconds": 1}},
                       {"agent_id": "another"}, {"context_epoch": 1}, {"event_ids": ["new-event"]}):
            calls, f, cfg, _ = fixture()
            other = copy.deepcopy(calls[1])
            other.key = "other|1"
            for k, val in change.items():
                setattr(other, k, val)
            altered = calls + [other]
            a = evaluate_finding(view(altered), finding(altered, f.rule_id), cfg)
            self.assertFalse(any("wait_contract" in s for s in a["scenarios"] if other.key in s["affected_calls"]))

    def test_scoped_deployment_record_can_bind_without_overriding_conflict(self):
        calls, f, cfg, contract = fixture()
        for c in calls:
            c.evidence.pop("tool_implementation_sha256")
        contract["deployment_binding"] = dict(contract["facts"]["terminal_return"], kind="deployment_record_review",
                                               implementation_sha256=IMPLEMENTATION,
                                               calls_sha256=scope_for(calls)["calls_sha256"])
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "wait")["validity"], "admissible")
        calls[0].evidence["tool_implementation_sha256"] = "b" * 64
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "wait")["validity"], "rejected")

    def test_invalid_provenance_and_preference_do_not_become_guarantees(self):
        calls, f, cfg, contract = fixture()
        contract["facts"]["end_to_end_return_bound"]["kind"] = "declared_preference"
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "wait")["validity"], "indeterminate")
        contract["facts"]["terminal_return"]["sources"] = []
        self.assertEqual(states(select(evaluate_finding(view(calls), f, cfg), "wait"))["wait_terminal_return"], "unknown")
        contract["facts"]["terminal_return"]["kind"] = []
        self.assertEqual(states(select(evaluate_finding(view(calls), f, cfg), "wait"))["wait_terminal_return"], "unknown")

    def test_policy_change_and_observed_timing_contradiction(self):
        calls, f, cfg, contract = fixture()
        contract["timeout_seconds"] = 40
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "wait")["validity"], "rejected")
        contract["timeout_seconds"] = 20
        contract["facts"]["end_to_end_return_bound"]["value_seconds"] = 0.5
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "wait")["validity"], "rejected")

    def test_dependencies_and_missing_calls_cannot_be_overridden(self):
        calls, f, cfg, _ = fixture()
        consumer = call(5, params={"source_call_id": calls[-1].call_id})
        self.assertEqual(select(evaluate_finding(view(calls + [consumer]), f, cfg), "wait")["validity"], "rejected")
        f.calls.append("missing|call")
        self.assertEqual(select(evaluate_finding(view(calls), f, cfg), "wait")["validity"], "indeterminate")

    def test_admissibility_does_not_invent_call_or_token_savings(self):
        calls, f, cfg, _ = fixture()
        s = select(evaluate_finding(view(calls), f, cfg), "wait")
        self.assertEqual(s["validity"], "admissible")
        self.assertIsNone(s["cost"]["calls"]["replacement_estimate"])
        self.assertIsNone(s["cost"]["tokens"]["reduction_estimate"])
        self.assertEqual(s["wait_contract"]["poll_seconds_unchanged"], 1)


if __name__ == "__main__":
    unittest.main()
