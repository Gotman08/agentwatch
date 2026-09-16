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
    top = [f for f in ranked if not (f.feedback and f.feedback.get("mark") == "false-positive")][:max_top]
    return {
        "report_version": REPORT_VERSION,
        "agentwatch_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "session": {
            "client": view.client, "session_id": view.session_id, "model": view.model,
            "model_source": "hook input (SessionStart / events)" if view.model else "non observe",
            "project_dir": view.project_dir, "first_time": view.first_time, "last_time": view.last_time,
            "turns": view.turns, "context_epochs": view.epochs, "agents": view.agents,
            "schema_versions_seen": view.schema_versions,
            "client_version_at_configure": (install_meta or {}).get("client_version"),
            "warnings": view.warnings,
        },
        "ranking_criteria": ["confiance (high > medium > low)", "nombre d'appels concernes", "octets de sortie observes"],
        "top_findings": [f.finding_id for f in top],
        "max_listed_per_rule": int(cfg.get("report", {}).get("max_listed_per_rule", 15)),
        "findings": [f.to_dict() for f in ranked],
        "no_issue_statement": None if ranked else "Aucun probleme demontre dans les donnees couvertes.",
        "stats": stats,
        "coverage": coverage,
        "agents": [asdict(a) for a in view.agent_infos],
        "calls": [c.summary() | {"key": c.key, "warnings": c.warnings, "context_epoch": c.context_epoch,
                                  "target_key": c.target_key, "params": c.params} for c in view.calls],
        "markers": [{"phase": m.phase, "time": m.time, "agent_id": m.agent_id, "meta": m.meta} for m in view.markers],
    }
