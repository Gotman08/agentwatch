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
                         "observable (ni par les hooks, ni dans les rollouts) : seule une compaction connue est prise en compte.")
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
    replacement_analysis: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.finding_id:
            raw = f"{self.rule_id}|{self.rule_version}|{self.kind}|" + "|".join(sorted(self.calls))
            self.finding_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def confidence_rank(self) -> int:
        return _CONF_RANK.get(self.confidence, 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


TRANSCRIPT_SOURCE = "claude-code:transcript"
ROLLOUT_SOURCE = "codex:rollout"
MEASURED_SOURCES = (TRANSCRIPT_SOURCE, ROLLOUT_SOURCE)


def tokens_of(call: Call) -> int | None:
    """Parts calculees d'un appel : entree non mise en cache + sortie, None si une composante manque."""
    u = call.usage
    if not isinstance(u, dict) or u.get("source") not in MEASURED_SOURCES:
        return None
    parts = [u.get("uncached_input_tokens"), u.get("output_tokens")]
    return sum(parts) if all(isinstance(p, int) and not isinstance(p, bool) for p in parts) else None


def observed_cost(calls: Iterable[Call]) -> dict[str, Any]:
    """Cout observe des appels concernes : octets de sortie, durees connues et tokens mesures, par provenance.

    # ! Les durees paralleles ne sont pas sommees comme du temps mural : on donne la somme
    #   par provenance ET le nombre d'appels sans mesure. Les tokens ne sont jamais estimes
    #   depuis des octets : ils viennent d'un import de transcript, avec leur methode.
    """
    calls = list(calls)
    bytes_known = [c.output_size_bytes for c in calls if isinstance(c.output_size_bytes, int)]
    client_dur = [c.duration_ms for c in calls if c.duration_source and c.duration_source.startswith("client") and isinstance(c.duration_ms, int)]
    from agentwatch.core.correlate import DURATION_RECONSTRUCTED_ROLLOUT, RECONSTRUCTED_SOURCES
    recon = [c for c in calls if c.duration_source in RECONSTRUCTED_SOURCES and isinstance(c.duration_ms, int)]
    recon_dur = [c.duration_ms for c in recon]
    # * Base de la reconstruction : entre deux hooks (leur surcout est compris), ou entre deux horodatages du rollout.
    bases = {"rollout" if c.duration_source == DURATION_RECONSTRUCTED_ROLLOUT else "hooks" for c in recon}
    with_tokens = [c for c in calls if isinstance(c.usage, dict) and c.usage.get("source") in MEASURED_SOURCES
                   and any(isinstance(c.usage.get(k), int) and not isinstance(c.usage[k], bool)
                           for k in ("uncached_input_tokens", "output_tokens"))]
    tokens: Any = "non mesure"
    if with_tokens:
        components = {name: [c.usage[key] for c in with_tokens if isinstance(c.usage.get(key), int)
                              and not isinstance(c.usage[key], bool)]
                      for name, key in (("uncached_input", "uncached_input_tokens"), ("output", "output_tokens"))}
        observed = {k: sum(v) if v else None for k, v in components.items()}
        complete = sum(tokens_of(c) is not None for c in with_tokens)
        tokens = {
            "total": sum(tokens_of(c) for c in with_tokens) if complete == len(with_tokens) else None,
            **{k: sum(v) if len(v) == len(with_tokens) else None for k, v in components.items()},
            "observed_total": sum(v for v in observed.values() if v is not None),
            "observed_components": observed,
            "known_for": len(with_tokens),
            "complete_for": complete,
            "component_known_for": {k: len(v) for k, v in components.items()},
            "source": "+".join(sorted({str(c.usage.get("source")) for c in with_tokens})),
            # * Une part par appel est un CALCUL d'AgentWatch a partir des releves par reponse : ni une mesure par appel,
            #   ni une estimation depuis des octets. Aucune donnee de facturation n'entre ici.
            "kind": "allocated",
            "note": ("parts calculees : entree non mise en cache de la requete qui a consomme chaque resultat (prorata) + sortie "
                     "de la requete emettrice (part) ; les releves mesures sont par reponse du modele, pas par appel"),
        }
    return {
        "calls": len(calls),
        "output_bytes_sum": sum(bytes_known) if bytes_known else None,
        "output_bytes_known_for": len(bytes_known),
        "duration_client_ms_sum": sum(client_dur) if client_dur else None,
        "duration_reconstructed_ms_sum": sum(recon_dur) if recon_dur else None,
        "duration_reconstructed_basis": ("+".join(sorted(bases)) if bases else None),
        "duration_unknown_for": len(calls) - len(client_dur) - len(recon_dur),
        "tokens": tokens,
        "note": "sommes par provenance ; les appels paralleles ne sont pas convertis en temps mural economisable",
    }


def cost_tokens(cost: dict[str, Any]) -> int | None:
    """Total de tokens mesures d'un cout observe, None si non mesure."""
    t = cost.get("tokens")
    return int(t["total"]) if isinstance(t, dict) and isinstance(t.get("total"), int) else None


def finding_cost_union(calls: Iterable[Call], findings: Iterable[Finding]) -> dict[str, Any]:
    """Union des appels d'UNE session ; les alternatives et signalements ne sont pas additifs."""
    refs = [key for f in findings for key in f.calls]
    selected = set(refs)
    unique = {c.key: c for c in calls if c.key in selected}
    return {"basis": "unique_call_keys_within_session", "observed_cost": observed_cost(unique.values()),
            "call_references": len(refs), "unique_calls": len(unique),
            "overlapping_references": len(refs) - len(selected),
            "unresolved_call_keys": sorted(selected - unique.keys()),
            "savings_estimate": None,
            "note": "Union des couts observes ; parts de tokens par appel deja allouees, aucune somme de gains de scenarios."}


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


def emitter(c: Call) -> str | None:
    """Requete du modele qui a emis l'appel (transcript Claude Code ou rollout Codex importe), sinon None."""
    rid = (c.usage or {}).get("emitter_request_id")
    return rid if isinstance(rid, str) and rid else None


def gap_ms(prev: Call, cur: Call) -> int | None:
    if prev.end_ns is None or cur.start_ns is None:
        return None
    return (cur.start_ns - prev.end_ns) // 1_000_000


def same_response(prev: Call, cur: Call, gap_threshold_ms: int) -> tuple[bool, str]:
    """(emis dans la meme reponse du modele ?, base) : requete emettrice si connue (exact), sinon ecart entre la fin
    de l'un et le debut de l'autre sous le seuil (heuristique : surcout des hooks, pas un aller-retour du modele)."""
    ea, eb = emitter(prev), emitter(cur)
    if ea and eb:
        return ea == eb, "transcript"
    gap = gap_ms(prev, cur)
    if gap is None:
        return False, "inconnu"
    return gap < gap_threshold_ms, "ecart"


def responses_of(calls: list[Call], gap_threshold_ms: int) -> int:
    """Nombre de reponses du modele (allers-retours) pour une suite d'appels consecutifs d'un meme agent."""
    return 1 + sum(1 for a, b in zip(calls, calls[1:]) if not same_response(a, b, gap_threshold_ms)[0]) if calls else 0


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Classement transparent : confiance, nombre d'appels, tokens mesures (si importes), octets observes."""
    return sorted(findings, key=lambda f: (f.confidence_rank, len(f.calls), cost_tokens(f.observed_cost) or 0,
                                           f.observed_cost.get("output_bytes_sum") or 0), reverse=True)
