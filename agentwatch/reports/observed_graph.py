"""Graphe de preuves locales, construit sans relire les sources ni executer d'outil.

Une egalite observee n'est pas une dependance ni une garantie de disponibilite.
Les aretes obligatoires exigent une reference explicite et une portee non ambigue.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from agentwatch.core.correlate import Call, SessionView


def reference(call: Call) -> dict[str, Any]:
    return {"call_key": call.key, "event_ids": list(call.event_ids),
            "source_start": call.evidence.get("source_start"),
            "source_end": call.evidence.get("source_end")}


def before(source: Call, target: Call) -> bool:
    """Un resultat doit etre acquis avant l'action qui le consomme."""
    return (source.seq < target.seq and source.end_ns is not None and target.start_ns is not None
            and source.end_ns <= target.start_ns and not source.ambiguous and not target.ambiguous)


def build_observed_graph(view: SessionView) -> dict[str, Any]:
    nodes = []
    edges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    by_id: dict[tuple[Any, ...], list[Call]] = defaultdict(list)
    previous: dict[tuple[Any, ...], Call] = {}
    input_refs: dict[tuple[Any, ...], Call] = {}
    by_key = {c.key: c for c in view.calls}
    for c in view.calls:
        if c.call_id:
            by_id[(c.client, c.session_id, c.agent_key, c.call_id)].append(c)
        nodes.append({
            "id": c.key, "call_id": c.call_id, "seq": c.seq, "actor": c.agent_key,
            "client": c.client, "session_id": c.session_id, "context_epoch": c.context_epoch,
            "operation": c.op, "resource": c.op_target, "scope": c.op_params,
            "observed_revision": c.resource_revision, "content_fingerprint": c.content_fingerprint,
            "result_fingerprint": c.result_fingerprint, "status": c.status, "evidence": reference(c),
            "information_available_now": "unknown",
        })
        # Un meme op_key contient le perimetre (plage, filtres, parametres).
        identity = (c.client, c.session_id, c.project_dir, c.op_key)
        prev = previous.get(identity)
        if c.op_key and prev and c.content_fingerprint and c.content_fingerprint == prev.content_fingerprint:
            edges.append({"source": prev.key, "target": c.key, "kind": "observed_content_equality",
                          "requires_source": False, "state": "established",
                          "evidence": [reference(prev), reference(c)],
                          "basis": "same_work_scope_and_content_fingerprint"})
        previous[identity] = c
        # Les identifiants de jobs/taches conserves (en clair ou en empreinte) peuvent
        # relier des observations du meme objet, jamais inventer sa soumission.
        for name in ("job_id", "task_id", "cell_id"):
            hidden = c.params.get("_fp") if isinstance(c.params.get("_fp"), dict) else {}
            value = c.params.get(name, hidden.get(name))
            if type(value) not in (str, int) or value == "":
                continue
            mode = "value" if name in c.params else "fingerprint"
            scope = (c.client, c.session_id, c.agent_key, c.context_epoch, c.mcp_server or c.tool_name, name, mode, value)
            prior = input_refs.get(scope)
            if prior:
                edges.append({"source": prior.key, "target": c.key, "kind": "shared_resource_reference",
                              "requires_source": False, "state": "established",
                              "evidence": [reference(prior), reference(c)],
                              "basis": {"parameter": name, "representation": mode, "value": value}})
            input_refs[scope] = c

    for c in view.calls:
        # Champs explicites uniquement ; un id de job commun ou la proximite temporelle
        # ne prouvent pas quelle acquisition a fourni l'information.
        ids: list[str] = []
        if isinstance(c.params.get("source_call_id"), str):
            ids.append(c.params["source_call_id"])
        if isinstance(c.params.get("depends_on_call_ids"), list):
            ids.extend(x for x in c.params["depends_on_call_ids"] if isinstance(x, str))
        for source_id in sorted(set(ids)):
            candidates = by_id.get((c.client, c.session_id, c.agent_key, source_id), [])
            src = candidates[0] if len(candidates) == 1 else None
            valid = (src is not None and before(src, c) and src.status == "success"
                     and src.context_epoch == c.context_epoch
                     and c.agent_key not in view.timing_unreliable_agents)
            if valid:
                edges.append({"source": src.key, "target": c.key, "kind": "explicit_call_dependency",
                              "requires_source": True, "state": "established",
                              "evidence": [reference(src), reference(c)], "basis": "explicit_input_call_id"})
            else:
                unresolved.append({"target": c.key, "reference": source_id, "state": "unknown",
                                   "reason": "producer_missing_ambiguous_future_failed_or_outside_context",
                                   "evidence": reference(c)})

    for agent in view.agent_infos:
        parent = by_key.get(agent.parent_call_key or "")
        if parent is None or not (agent.link_basis or "").startswith("exact:"):
            continue
        for child in view.calls:
            if child.agent_key != agent.agent_id or parent.ambiguous or child.ambiguous:
                continue
            if parent.start_ns is None or child.start_ns is None or parent.start_ns > child.start_ns:
                continue
            edges.append({"source": parent.key, "target": child.key, "kind": "explicit_delegation",
                          "requires_source": True, "state": "established",
                          "evidence": [reference(parent), reference(child)], "basis": agent.link_basis})
    return {
        "version": "1.0", "nodes": nodes, "edges": edges, "unresolved": unresolved,
        "dependency_coverage": "partial",
        "limits": [
            "Aucune dependance deduite de la proximite temporelle ou d'une empreinte egale.",
            "Une reference a un job sans producteur explicitement lie reste non resolue.",
            "Une empreinte ou revision observee apres une lecture ne garantit pas sa stabilite avant cette lecture.",
            "Absence d'arete ou d'ecriture journalisee ne prouve pas l'independance ni l'absence de changement.",
        ],
    }


def check_removal(graph: dict[str, Any], removed: list[str]) -> dict[str, Any]:
    """Verifier seulement les dependances explicites ; la couverture reste partielle."""
    removed_set = set(removed)
    broken = [e for e in graph["edges"] if e["requires_source"] and e["source"] in removed_set
              and e["target"] not in removed_set]
    return {"state": "refuted" if broken else "established", "broken_edges": broken,
            "basis": "preservation_of_observed_edges_only", "coverage": "partial"}
