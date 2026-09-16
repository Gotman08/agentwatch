"""Statistiques descriptives et matrice de couverture d'une session."""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX
from agentwatch.core import schema as S
from agentwatch.core.correlate import STATUS_OPEN, SessionView

# * Capacites documentees par client (source : docs officielles, voir docs/compatibility.md).
#   Valeurs : supported | partial | absent. "observed" / "not_tested" viennent des donnees.
CAPABILITIES: dict[str, dict[str, tuple[str, str]]] = {
    CLIENT_CLAUDE_CODE: {
        "tool_start": ("supported", "PreToolUse"),
        "tool_end": ("supported", "PostToolUse (succes uniquement)"),
        "tool_failure": ("supported", "PostToolUseFailure (erreur, refus, interruption)"),
        "exit_code": ("absent", "tool_response de Bash expose stdout/stderr/interrupted, pas de code de sortie"),
        "client_duration": ("partial", "PostToolUse.duration_ms (observe 2.1.270 ; documente comme `duration` a partir de 2.1.267) ; durationMs dans certaines reponses"),
        "output_size": ("supported", "taille du tool_response serialise tel que recu par le hook"),
        "result_fingerprint": ("supported", "empreinte HMAC du tool_response"),
        "mcp_calls": ("supported", "outils nommes mcp__<serveur>__<outil>"),
        "subagents": ("supported", "agent_id / agent_type dans l'entree des hooks"),
        "compaction": ("supported", "PreCompact / PostCompact"),
        "interrupts": ("supported", "PostToolUseFailure.is_interrupt"),
        "turn_boundaries": ("supported", "UserPromptSubmit / Stop (prompt_id a partir de v2.1.196)"),
        "background_commands": ("partial", "run_in_background : la fin reelle passe par BashOutput, non correlee"),
        "hosted_tools": ("partial", "WebFetch/WebSearch sont des outils locaux vus par les hooks ; pas de detail reseau"),
        "long_commands_polling": ("partial", "un appel long = un PreToolUse puis un PostToolUse ; le polling interne n'est pas visible"),
        "token_usage": ("partial", "sous-agents : usage rapporte dans la reponse de l'outil Agent (observe 2.1.270) ; fil principal : absent, import seulement"),
    },
    CLIENT_CODEX: {
        "tool_start": ("supported", "PreToolUse"),
        "tool_end": ("supported", "PostToolUse (y compris apres un code de sortie non nul pour Bash)"),
        "tool_failure": ("partial", "pas de PostToolUseFailure : l'echec doit etre lu dans tool_response"),
        "exit_code": ("partial", "depend de la forme de tool_response (non entierement documentee)"),
        "client_duration": ("absent", "aucune duree documentee dans l'entree des hooks"),
        "output_size": ("supported", "taille du tool_response serialise tel que recu par le hook"),
        "result_fingerprint": ("supported", "empreinte HMAC du tool_response"),
        "mcp_calls": ("supported", "outils nommes mcp__<serveur>__<outil>"),
        "subagents": ("supported", "SubagentStart/SubagentStop ; agent_id sur les evenements"),
        "compaction": ("supported", "PreCompact / PostCompact"),
        "interrupts": ("partial", "evenement Interrupt de session ; pas de statut par appel"),
        "turn_boundaries": ("supported", "turn_id sur les evenements de tour ; UserPromptSubmit / Stop"),
        "background_commands": ("partial", "sessions exec/write_stdin non correlees a un appel unique"),
        "hosted_tools": ("absent", "les outils herberges (WebSearch) ne passent pas par les hooks (documente)"),
        "long_commands_polling": ("partial", "un appel long = un PreToolUse puis un PostToolUse"),
        "token_usage": ("absent", "non fourni aux hooks ; [otel] exporte vers OTLP seulement"),
    },
}


def compute_stats(view: SessionView) -> dict[str, Any]:
    calls = view.calls
    by_tool: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "errors": 0, "unknown_status": 0, "open": 0, "output_bytes": 0, "output_known": 0,
        "client_durations": [], "reconstructed_durations": []})
    err_sig: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    for c in calls:
        row = by_tool[c.tool_name or "?"]
        row["calls"] += 1
        status_counter[c.status] += 1
        if c.status in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED):
            row["errors"] += 1
            if c.error_signature:
                err_sig[c.error_signature] += 1
        elif c.status == S.STATUS_UNKNOWN:
            row["unknown_status"] += 1
        elif c.status == STATUS_OPEN:
            row["open"] += 1
        # * Edit/Write enregistres avant le schema 1.1 : taille = fichier recopie, non mesuree.
        echo_only = c.category in (S.CAT_EDIT, S.CAT_WRITE) and (c.output_size_source or "") == "serialized_tool_response"
        if isinstance(c.output_size_bytes, int) and not echo_only:
            row["output_bytes"] += c.output_size_bytes
            row["output_known"] += 1
        if isinstance(c.duration_ms, int):
            if c.duration_source and c.duration_source.startswith("client"):
                row["client_durations"].append(c.duration_ms)
            elif c.duration_source == "reconstructed_between_hooks":
                row["reconstructed_durations"].append(c.duration_ms)
    tools = []
    for name, row in sorted(by_tool.items(), key=lambda kv: kv[1]["calls"], reverse=True):
        tools.append({
            "tool": name, "calls": row["calls"], "errors": row["errors"], "unknown_status": row["unknown_status"],
            "open": row["open"], "output_bytes": row["output_bytes"] if row["output_known"] else None,
            "output_known_for": row["output_known"],
            "client_duration_median_ms": int(median(row["client_durations"])) if row["client_durations"] else None,
            "client_duration_n": len(row["client_durations"]),
            "reconstructed_duration_median_ms": int(median(row["reconstructed_durations"])) if row["reconstructed_durations"] else None,
            "reconstructed_duration_n": len(row["reconstructed_durations"]),
        })
    # * Les Edit/Write enregistres avant la version 1.1 incluent le fichier recopie dans leur
    #   taille : on les ecarte du classement plutot que d'afficher un faux volume.
    biggest = sorted((c for c in calls if isinstance(c.output_size_bytes, int)
                      and not (c.category in (S.CAT_EDIT, S.CAT_WRITE) and (c.output_size_source or "") == "serialized_tool_response")),
                     key=lambda c: c.output_size_bytes or 0, reverse=True)[:5]
    longest = sorted((c for c in calls if isinstance(c.duration_ms, int)), key=lambda c: c.duration_ms or 0, reverse=True)[:5]
    return {
        "calls": len(calls),
        "events": view.counts.get("events", 0),
        "status": dict(status_counter),
        "tools": tools,
        "error_signatures": [{"signature": s, "count": n} for s, n in err_sig.most_common(10)],
        "largest_outputs": [c.summary() for c in biggest],
        "longest_calls": [c.summary() for c in longest],
        "hook_overhead_ms": _describe(view.hook_ms_samples),
        "correlation": dict(view.counts),
        "turns": view.turns, "context_epochs": view.epochs, "agents": view.agents,
        "usage": _usage_summary(view),
        "work_units": work_units(view),
    }


def work_units(view: SessionView, limit: int = 25) -> list[dict[str, Any]]:
    """Unites de travail : meme operation normalisee sur la meme cible, tous outils confondus.

    # * Repond a « a-t-on refait le meme travail ? » plutot qu'a « a-t-on refait le meme appel ? ».
    """
    groups: dict[str, dict[str, Any]] = {}
    for c in view.calls:
        if c.op in ("other", "agent") or not c.op_key:
            continue
        g = groups.setdefault(c.op_key, {"op": c.op, "target": c.op_target if c.op != "unknown" else c.target, "calls": 0,
                                         "tools": set(), "agents": set(), "statuses": {}, "content_fps": set(), "seqs": []})
        g["calls"] += 1
        g["tools"].add(c.tool_name or "?")
        g["agents"].add(c.agent_key)
        g["statuses"][c.status] = g["statuses"].get(c.status, 0) + 1
        if c.content_fingerprint:
            g["content_fps"].add(c.content_fingerprint)
        g["seqs"].append(c.seq)
    rows = []
    for g in groups.values():
        rows.append({"op": g["op"], "target": g["target"], "calls": g["calls"], "tools": sorted(g["tools"]),
                     "agents": len(g["agents"]), "statuses": g["statuses"],
                     "distinct_contents": len(g["content_fps"]) if g["content_fps"] else None,
                     "seqs": g["seqs"][:20], "repeated": g["calls"] >= 2})
    rows.sort(key=lambda r: (r["calls"], len(r["tools"])), reverse=True)
    return rows[:limit]


def _usage_summary(view: SessionView) -> dict[str, Any]:
    """Usage en tokens : uniquement ce que le client rapporte, avec source et perimetre."""
    rows = []
    for a in view.agent_infos:
        if a.usage:
            rows.append({"agent_id": a.agent_id, "agent_type": a.agent_type, "model": a.model, **a.usage})
    imported = [c.usage for c in view.calls if c.usage and c.usage.get("source") not in (None, "claude-code:Agent.tool_response")]
    if not rows and not imported:
        return {"status": "non mesure", "note": "aucune donnee d'usage fournie par les hooks pour le fil principal ; voir agentwatch import-usage",
                "agents": [], "imported": []}
    return {
        "status": f"rapporte par le client pour {len(rows)} sous-agent(s) (portee : agent) ; non mesure pour le fil principal",
        "note": "valeurs telles que fournies dans la reponse de l'outil Agent ; aucune conversion ni estimation de cout",
        "agents": rows, "imported": imported,
    }


def _describe(samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {"n": 0}
    s = sorted(samples)
    return {"n": len(s), "median": round(s[len(s) // 2], 1), "p90": round(s[min(len(s) - 1, int(len(s) * 0.9))], 1),
            "max": round(s[-1], 1), "note": "temps dans le processus du hook (hors demarrage de l'interpreteur)"}


def coverage_matrix(view: SessionView) -> list[dict[str, Any]]:
    """Ligne par capacite : statut documente + observation dans cette session."""
    caps = CAPABILITIES.get(view.client, {})
    calls = view.calls
    marker_phases = {m.phase for m in view.markers}
    observed: dict[str, bool] = {
        "tool_start": any(c.has_start for c in calls),
        "tool_end": any(c.has_end and c.status not in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED) for c in calls),
        "tool_failure": any(c.status in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED) for c in calls),
        "exit_code": any(c.exit_code is not None for c in calls),
        "client_duration": any(c.duration_source and c.duration_source.startswith("client") for c in calls),
        "output_size": any(c.output_size_bytes is not None for c in calls),
        "result_fingerprint": any(c.result_fingerprint for c in calls),
        "mcp_calls": any(c.category == S.CAT_MCP for c in calls),
        "subagents": any(c.agent_id for c in calls) or bool({S.PHASE_SUBAGENT_START, S.PHASE_SUBAGENT_STOP} & marker_phases),
        "compaction": bool({S.PHASE_COMPACT_START, S.PHASE_COMPACT_END} & marker_phases),
        "interrupts": any(c.status == S.STATUS_INTERRUPTED for c in calls) or S.PHASE_INTERRUPT in marker_phases,
        "turn_boundaries": view.turns > 0,
        "background_commands": any(c.params.get("run_in_background") for c in calls),
        "hosted_tools": any(c.category == S.CAT_WEB for c in calls),
        "long_commands_polling": any(isinstance(c.duration_ms, int) and c.duration_ms > 30000 for c in calls),
        "token_usage": any(c.usage for c in calls),
    }
    rows = []
    for cap, (status, basis) in caps.items():
        seen = observed.get(cap, False)
        rows.append({"capability": cap, "documented": status, "basis": basis,
                     "observed_in_session": "observed" if seen else ("absent" if status == "absent" else "not_observed")})
    return rows
