"""Diagnostic numerique de l'allocation ; aucune imputation ni relecture de texte."""
from collections import Counter, defaultdict

from agentwatch.detectors.base import MEASURED_SOURCES
from agentwatch.reports.observed_graph import reference


def allocation_coverage(view, calls):
    calls = list(calls)
    responses = defaultdict(list)
    for marker in view.markers:
        usage = marker.meta.get("usage", {})
        if isinstance(usage, dict) and usage.get("scope") == "response" and usage.get("response_id"):
            responses[(marker.agent_id or "main", usage["response_id"])].append(usage)
    counts = Counter()
    rows = []
    numeric = lambda value: type(value) is int and value >= 0
    for call in calls:
        usage = call.usage if isinstance(call.usage, dict) else {}
        measured = usage.get("source") in MEASURED_SOURCES
        in_known = measured and numeric(usage.get("uncached_input_tokens"))
        out_known = measured and numeric(usage.get("output_tokens"))
        emitter = usage.get("emitter_request_id")
        matches = responses.get((call.agent_key, emitter), []) if emitter else []
        if in_known and out_known:
            reason = "complete_allocation"
        elif not measured:
            reason = "call_allocation_absent"
        elif not out_known and not emitter:
            reason = ("emitter_link_missing_for_standalone_item" if call.evidence.get("rollout_item") == "McpToolCall"
                      and not call.evidence.get("exec_call_id") else "emitter_link_missing")
        elif not out_known and not matches:
            reason = "emitter_response_not_in_frozen_data"
        elif not out_known and len(matches) != 1:
            reason = "emitter_response_ambiguous"
        elif not out_known and not numeric(matches[0].get("output_tokens")):
            reason = "emitter_counter_absent"
        elif not out_known:
            reason = "allocation_missing_despite_linked_counter"
        else:
            reason = "input_allocation_missing"
        counts[reason] += 1
        rows.append({"call_key": call.key, "input_allocation_known": in_known, "output_allocation_known": out_known,
                     "reason": reason, "emitter_request_id": emitter,
                     "consumer_request_id": usage.get("consumer_request_id"),
                     "correlation_provenance": call.evidence.get("allocation_correlation"),
                     "call_identity_basis": call.evidence.get("call_identity_basis", "not_recorded"),
                     "source_identity_start": call.evidence.get("source_identity_start"),
                     "source_identity_end": call.evidence.get("source_identity_end"),
                     "exec_call_id": call.evidence.get("exec_call_id"), "evidence": reference(call),
                     "allocation_source_refs": call.evidence.get("source_observations", [])})
    actors = {c.agent_key for c in calls}
    related = [r for (actor, _), values in responses.items() if actor in actors for r in values]
    return {"version": "1.1", "calls": len(calls), "reasons": dict(counts), "details": rows,
            "response_records_in_same_actors": len(related),
            "response_records_with_output_counter": sum(numeric(r.get("output_tokens")) for r in related),
            "scope": "same_selected_calls; response_counts_are_actor_context_not_attributable_costs",
            "inference": "No temporal nearest-neighbour assignment. A missing emitter link does not establish that its counter was absent in the source.",
            "allocation_semantics": "Numeric completeness does not establish an explicit source link. Codex rollout allocations use log-order rules; unknown provenance stays unknown. Neither marginal tool costs nor measured savings.",
            "saving_estimate": None}
