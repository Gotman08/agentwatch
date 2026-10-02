import unittest

from agentwatch.core.correlate import Marker
from agentwatch.reports.token_coverage import allocation_coverage
from tests.test_replacements import call, view


class TokenCoverageTests(unittest.TestCase):
    def test_missing_link_counter_and_granularity_stay_distinct(self):
        base = {"source": "codex:rollout", "uncached_input_tokens": 10, "output_tokens": None}
        calls = [call(0, usage=base.copy(), evidence={"rollout_item": "McpToolCall"}),
                 call(1, usage=base | {"emitter_request_id": "empty"}),
                 call(2, usage=base | {"emitter_request_id": "measured"}),
                 call(3, usage=base | {"output_tokens": 0})]
        v = view(calls)
        v.markers = [Marker(phase="usage", time="", ns=0, agent_id="main", meta={"usage": {
            "scope": "response", "response_id": name, "output_tokens": count}}) for name, count in (("empty", None), ("measured", 55))]
        original = [c.usage.copy() for c in calls]
        d = allocation_coverage(v, calls)
        self.assertEqual(d["reasons"], {"emitter_link_missing_for_standalone_item": 1, "emitter_counter_absent": 1,
                                      "allocation_missing_despite_linked_counter": 1, "complete_allocation": 1})
        self.assertEqual([c.usage for c in calls], original)
        self.assertIsNone(d["saving_estimate"])

    def test_response_id_from_another_actor_does_not_resolve_emitter(self):
        c = call(0, usage={"source": "codex:rollout", "emitter_request_id": "r", "uncached_input_tokens": 2})
        v = view([c])
        v.markers = [Marker(phase="usage", time="", ns=0, agent_id="other", meta={"usage": {
            "scope": "response", "response_id": "r", "output_tokens": 100}})]
        self.assertEqual(allocation_coverage(v, [c])["reasons"], {"emitter_response_not_in_frozen_data": 1})
