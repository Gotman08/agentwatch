"""Structure commune d'un signalement et utilitaires de fenetre."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from agentwatch.core.correlate import Call

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
_CONF_RANK = {CONFIDENCE_HIGH: 3, CONFIDENCE_MEDIUM: 2, CONFIDENCE_LOW: 1}

# * Limite standard, toujours rappelee : on ne sait pas si l'ancien resultat est
#   encore dans le contexte du modele.
LIMIT_CONTEXT_UNKNOWN = ("La disponibilite du resultat precedent dans le contexte du modele n'est pas "
                         "observable par les hooks : seule une compaction connue est prise en compte.")
LIMIT_EXTERNAL_CHANGES = "Une modification externe (autre processus, editeur, git) n'est pas observable."
LIMIT_HEURISTIC = "Le niveau de confiance est heuristique, pas une probabilite calibree."


@dataclass
class Finding:
    rule_id: str
    rule_version: str
    kind: str
    title: str
    confidence: str
    confidence_rationale: str
    calls: list[str]                     # cles d'appel
    call_refs: list[dict[str, Any]]      # resumes lisibles des appels
    evidence: dict[str, Any]
    explanation: str
    counter_indications: list[str]
    missing_data: list[str]
    observed_cost: dict[str, Any]
    proposal: dict[str, Any]
    validation_protocol: list[str]
    finding_id: str = ""
    feedback: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.finding_id:
            raw = f"{self.rule_id}|{self.rule_version}|{self.kind}|" + "|".join(sorted(self.calls))
            self.finding_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def confidence_rank(self) -> int:
        return _CONF_RANK.get(self.confidence, 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def observed_cost(calls: Iterable[Call]) -> dict[str, Any]:
    """Cout observe des appels concernes : octets de sortie et durees connues, par provenance.

    # ! Les durees paralleles ne sont pas sommees comme du temps mural : on donne la somme
    #   par provenance ET le nombre d'appels sans mesure. Pas d'estimation en tokens.
    """
    calls = list(calls)
    bytes_known = [c.output_size_bytes for c in calls if isinstance(c.output_size_bytes, int)]
    client_dur = [c.duration_ms for c in calls if c.duration_source and c.duration_source.startswith("client") and isinstance(c.duration_ms, int)]
    recon_dur = [c.duration_ms for c in calls if c.duration_source == "reconstructed_between_hooks" and isinstance(c.duration_ms, int)]
    return {
        "calls": len(calls),
        "output_bytes_sum": sum(bytes_known) if bytes_known else None,
        "output_bytes_known_for": len(bytes_known),
        "duration_client_ms_sum": sum(client_dur) if client_dur else None,
        "duration_reconstructed_ms_sum": sum(recon_dur) if recon_dur else None,
        "duration_unknown_for": len(calls) - len(client_dur) - len(recon_dur),
        "tokens": "non mesure",
        "note": "sommes par provenance ; les appels paralleles ne sont pas convertis en temps mural economisable",
    }


def refs(calls: Iterable[Call]) -> list[dict[str, Any]]:
    return [c.summary() for c in calls]


def within_window(a: Call, b: Call, window_calls: int, window_seconds: int) -> bool:
    """Deux appels sont dans la fenetre s'ils sont proches en rang ET en temps (si connu)."""
    if abs(a.seq - b.seq) > window_calls:
        return False
    if a.order_ns and b.order_ns and abs(a.order_ns - b.order_ns) > window_seconds * 1_000_000_000:
        return False
    return True


def same_context(a: Call, b: Call) -> bool:
    return a.agent_key == b.agent_key and a.context_epoch == b.context_epoch and a.session_id == b.session_id


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Classement transparent : confiance, puis nombre d'appels, puis octets observes."""
    return sorted(findings, key=lambda f: (f.confidence_rank, len(f.calls), f.observed_cost.get("output_bytes_sum") or 0), reverse=True)
