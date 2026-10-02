"""Export JSON detaille d'une session analysee."""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

from agentwatch import SCHEMA_VERSION, __version__
from agentwatch.config import config_warnings
from agentwatch.core.correlate import SessionView
from agentwatch.detectors.base import Finding, finding_cost_union, rank_findings
from agentwatch.reports.labels import agent_labels, provenance

REPORT_VERSION = "1.1"


def build_report(view: SessionView, stats: dict[str, Any], coverage: list[dict[str, Any]],
                 findings: list[Finding], cfg: dict[str, Any], feedback: dict[str, dict[str, Any]],
                 install_meta: dict[str, Any] | None, *, copy_findings: bool = True) -> dict[str, Any]:
    """Rapport classique. copy_findings=False emprunte les conteneurs seulement
    pour un encodage partage immediat ; le rapport retourne ne doit pas etre mute.
    """
    ranked = rank_findings(findings)
    from agentwatch.reports.replacements import attach_replacements
    graph = attach_replacements(view, ranked, cfg)
    for f in ranked:
        f.feedback = feedback.get(f.finding_id)
    max_top = int(cfg.get("report", {}).get("max_top_findings", 3))
    # * Seuls les signalements de confiance moyenne ou haute peuvent etre des "opportunites" :
    #   on ne remplit pas la tete du rapport avec des candidats a faible confiance.
    top = [f for f in ranked if f.confidence_rank >= 2 and not (f.feedback and f.feedback.get("mark") == "false-positive")][:max_top]
    low_only = bool(ranked) and not top
    collection = view.collection
    agents = [asdict(a) for a in view.agent_infos]
    observed_version = next((m.meta.get("cli_version") for m in view.markers
                             if m.phase in ("session_start", "subagent_start") and m.meta.get("cli_version")), None)
    if view.model and collection == "rollout":
        model_source = "rollout Codex : turn_context / thread_settings du fil principal"
    elif view.model:
        model_source = "hook input (SessionStart / events)"
    elif collection == "rollout":
        model_source = "non observe : aucun turn_context avec modele dans les rollouts lus"
    elif not any(m.phase == "session_start" for m in view.markers):
        model_source = "non observe : aucun SessionStart enregistre, hooks installes apres le debut de la session"
    else:
        model_source = "non observe : le client ne transmet pas le modele du fil principal aux hooks"
    return {
        "report_version": REPORT_VERSION,
        "agentwatch_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "config_warnings": config_warnings(cfg),
        "session": {
            "client": view.client, "session_id": view.session_id, "model": view.model,
            "model_source": model_source,
            "provenance": provenance(collection, view.origins, view.client),
            "project_dir": view.project_dir, "first_time": view.first_time, "last_time": view.last_time,
            "turns": view.turns, "context_epochs": view.epochs, "agents": view.agents,
            "schema_versions_seen": view.schema_versions,
            "client_version_at_configure": (install_meta or {}).get("client_version"),
            "client_version_observed": observed_version,
            "warnings": view.warnings,
        },
        "ranking_criteria": ["confiance (high > medium > low)", "nombre d'appels concernes",
                             "tokens repartis par calcul sur ces appels (a partir des releves par reponse)",
                             "octets de sortie observes"],
        "agent_labels": agent_labels(view.agents, agents),
        "top_findings": [f.finding_id for f in top],
        "max_listed_per_rule": int(cfg.get("report", {}).get("max_listed_per_rule", 15)),
        "repetitions_top": int(cfg.get("detectors", {}).get("repeated_calls", {}).get("report_top", 15)),
        "findings": [f.to_dict() if copy_findings else {field.name: getattr(f, field.name) for field in fields(f)} for f in ranked],
        "finding_cost_union": finding_cost_union(view.calls, [f for f in ranked if not (f.feedback and f.feedback.get("mark") == "false-positive")]),
        "observed_dependency_graph": graph,
        "no_issue_statement": (None if top else
                               (f"Aucune opportunite demontree : {len(ranked)} signalement(s) a faible confiance seulement, listes ci-dessous."
                                if low_only else "Aucun probleme demontre dans les donnees couvertes.")),
        "stats": stats,
        "coverage": coverage,
        "agents": agents,
        "calls": [c.summary() | {"key": c.key, "warnings": c.warnings, "context_epoch": c.context_epoch,
                                  "target_key": c.target_key, "params": c.params} for c in view.calls],
        "markers": [{"phase": m.phase, "time": m.time, "agent_id": m.agent_id, "meta": m.meta} for m in view.markers],
    }
