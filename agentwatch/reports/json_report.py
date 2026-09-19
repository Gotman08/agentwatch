"""Export JSON detaille d'une session analysee."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agentwatch import SCHEMA_VERSION, __version__
from agentwatch.core.correlate import SessionView
from agentwatch.detectors.base import Finding, rank_findings

REPORT_VERSION = "1.0"


def build_report(view: SessionView, stats: dict[str, Any], coverage: list[dict[str, Any]],
                 findings: list[Finding], cfg: dict[str, Any], feedback: dict[str, dict[str, Any]],
                 install_meta: dict[str, Any] | None) -> dict[str, Any]:
    ranked = rank_findings(findings)
    for f in ranked:
        f.feedback = feedback.get(f.finding_id)
    max_top = int(cfg.get("report", {}).get("max_top_findings", 3))
    # * Seuls les signalements de confiance moyenne ou haute peuvent etre des "opportunites" :
    #   on ne remplit pas la tete du rapport avec des candidats a faible confiance.
    top = [f for f in ranked if f.confidence_rank >= 2 and not (f.feedback and f.feedback.get("mark") == "false-positive")][:max_top]
    low_only = bool(ranked) and not top
    return {
        "report_version": REPORT_VERSION,
        "agentwatch_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "session": {
            "client": view.client, "session_id": view.session_id, "model": view.model,
            "model_source": ("hook input (SessionStart / events)" if view.model else
                             ("non observe : aucun SessionStart enregistre, hooks installes apres le debut de la session"
                              if not any(m.phase == "session_start" for m in view.markers) else
                              "non observe : le client ne transmet pas le modele du fil principal aux hooks")),
            "project_dir": view.project_dir, "first_time": view.first_time, "last_time": view.last_time,
            "turns": view.turns, "context_epochs": view.epochs, "agents": view.agents,
            "schema_versions_seen": view.schema_versions,
            "client_version_at_configure": (install_meta or {}).get("client_version"),
            "client_version_observed": next((m.meta.get("cli_version") for m in view.markers
                                             if m.phase in ("session_start", "subagent_start") and m.meta.get("cli_version")), None),
            "warnings": view.warnings,
        },
        "ranking_criteria": ["confiance (high > medium > low)", "nombre d'appels concernes",
                             "tokens mesures (transcripts ou rollouts importes)", "octets de sortie observes"],
        "top_findings": [f.finding_id for f in top],
        "max_listed_per_rule": int(cfg.get("report", {}).get("max_listed_per_rule", 15)),
        "findings": [f.to_dict() for f in ranked],
        "no_issue_statement": (None if top else
                               (f"Aucune opportunite demontree : {len(ranked)} signalement(s) a faible confiance seulement, listes ci-dessous."
                                if low_only else "Aucun probleme demontre dans les donnees couvertes.")),
        "stats": stats,
        "coverage": coverage,
        "agents": [asdict(a) for a in view.agent_infos],
        "calls": [c.summary() | {"key": c.key, "warnings": c.warnings, "context_epoch": c.context_epoch,
                                  "target_key": c.target_key, "params": c.params} for c in view.calls],
        "markers": [{"phase": m.phase, "time": m.time, "agent_id": m.agent_id, "meta": m.meta} for m in view.markers],
    }
