"""Six detecteurs deterministes, independants et configurables.

Chaque module expose `detect(view, cfg) -> list[Finding]` et les constantes RULE_ID / RULE_VERSION.
"""

from __future__ import annotations

from typing import Any

from agentwatch.core.correlate import SessionView
from agentwatch.detectors.base import Finding

_REGISTRY = ("redundant_reads", "error_loops", "batchable", "automation_candidates", "tool_gap", "repeated_guidance")


def run_detectors(view: SessionView, cfg: dict[str, Any], only: list[str] | None = None) -> list[Finding]:
    import importlib

    findings: list[Finding] = []
    dcfg = cfg.get("detectors", {})
    for name in _REGISTRY:
        if only and name not in only:
            continue
        if not dcfg.get(name, {}).get("enabled", True):
            continue
        module = importlib.import_module(f"agentwatch.detectors.{name}")
        findings.extend(module.detect(view, cfg))
    return findings
