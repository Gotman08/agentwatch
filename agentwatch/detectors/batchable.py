"""Detecteur C : operations regroupables.

Regle : une suite d'au moins `min_group` appels consecutifs du meme outil de lecture
(read/list/search, ou MCP cible par chemin) sur des cibles differentes, executes l'un
apres l'autre (non chevauchants), sans erreur.

L'independance est etablie a partir des preuves disponibles :
- forte : les cibles proviennent toutes d'un listage/recherche precedent (result_paths) ;
- moyenne : cibles dans le meme dossier ou de meme extension ;
- faible : sinon (les cibles peuvent dependre des contenus lus, non observables).
Un outil groupe n'est annonce disponible que s'il est observe : appels paralleles deja vus
dans la session pour ce client, ou outil MCP candidat vu sur le meme serveur.
"""

from __future__ import annotations

import os
from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE
from agentwatch.core import schema as S
from agentwatch.core.correlate import Call, SessionView
from agentwatch.detectors import base as B

RULE_ID = "C.batchable"
RULE_VERSION = "1.0"
_GROUPABLE = {S.CAT_READ, S.CAT_LIST, S.CAT_SEARCH, S.CAT_MCP}
_BATCH_HINTS = ("batch", "many", "multi", "all", "bulk", "list")


def _eligible(c: Call) -> bool:
    if c.category not in _GROUPABLE or c.target_key is None:
        return False
    if c.category == S.CAT_MCP and c.target_kind != "path":
        return False
    return c.status not in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED)


def _overlaps(prev: Call, cur: Call) -> bool | None:
    """True si cur a commence avant la fin de prev (deja parallele) ; None si inconnu."""
    if prev.end_ns is None or cur.start_ns is None:
        return None
    return cur.start_ns < prev.end_ns


def _path_of(c: Call) -> str:
    return (c.target_key or "")[len("path:"):].split("::", 1)[-1]


def _independence(run: list[Call], calls: list[Call], case_insensitive: bool) -> tuple[str, str]:
    targets = {_path_of(c) for c in run}
    first = run[0]
    for prev in reversed(calls[max(0, first.seq - 3): first.seq]):
        if prev.agent_key != first.agent_key or not prev.result_paths:
            continue
        listed = {(p.lower() if case_insensitive else p) for p in prev.result_paths}
        if targets <= listed:
            return "high", f"toutes les cibles figurent dans le resultat de l'appel #{prev.seq} ({prev.tool_name})"
    dirs = {os.path.dirname(t) for t in targets}
    exts = {os.path.splitext(t)[1] for t in targets}
    if len(dirs) == 1:
        return "medium", "cibles dans le meme dossier (aucune dependance entre lectures observee)"
    if len(exts) == 1 and exts != {""}:
        return "medium", "cibles de meme extension (aucune dependance entre lectures observee)"
    return "low", "independance non etablie : une cible peut provenir du contenu lu precedemment"


def _grouped_tool(run: list[Call], view: SessionView, parallel_seen: bool) -> dict[str, Any]:
    first = run[0]
    if first.category == S.CAT_MCP:
        candidates = sorted({c.tool_name for c in view.calls if c.mcp_server == first.mcp_server and c.tool_name != first.tool_name
                             and any(h in (c.mcp_tool or "").lower() for h in _BATCH_HINTS)})
        if candidates:
            return {"status": "candidate_observed", "tools": candidates,
                    "note": "outil(s) du meme serveur observe(s) dans la session ; compatibilite a verifier"}
        return {"status": "proposal", "note": f"proposer au serveur MCP {first.mcp_server!r} un outil acceptant plusieurs cibles"}
    if first.category == S.CAT_SHELL:
        return {"status": "proposal", "note": "une seule commande avec plusieurs chemins (ex. cat a b c, rg motif a b c) ; non verifiee"}
    if parallel_seen:
        return {"status": "verified_in_session", "note": "des appels chevauchants du meme outil ont ete observes dans cette session : le client sait paralleliser"}
    if view.client == CLIENT_CLAUDE_CODE:
        return {"status": "documented_not_verified", "note": "Claude Code peut emettre plusieurs appels d'outil dans un meme tour ; non observe dans cette session"}
    return {"status": "unknown", "note": "capacite de regroupement non verifiee pour ce client"}


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    d = cfg.get("detectors", {}).get("batchable", {})
    min_group = int(d.get("min_group", 3))
    max_gap = int(d.get("max_gap_calls", 0))
    case_insensitive = bool(cfg.get("case_insensitive_paths", False))
    calls = view.calls
    parallel_seen = any(_overlaps(a, b) for a, b in zip(calls, calls[1:]) if a.tool_name == b.tool_name and a.agent_key == b.agent_key)
    findings: list[B.Finding] = []
    run: list[Call] = []
    gap = 0

    def flush() -> None:
        nonlocal run
        if len(run) >= min_group:
            findings.append(_finding(run, view, calls, case_insensitive, parallel_seen))
        run = []

    for c in calls:
        if run and c.agent_key != run[-1].agent_key:
            flush()
            gap = 0
        if _eligible(c):
            if run and c.tool_name == run[-1].tool_name and c.target_key not in {r.target_key for r in run}:
                if _overlaps(run[-1], c):
                    flush()          # ? deja parallele : ce n'est pas un regroupement a proposer
                    run = [c]
                    continue
                run.append(c)
            else:
                flush()              # * autre outil ou cible deja vue : nouvelle serie
                run = [c]
            gap = 0
        elif run:
            gap += 1
            if gap > max_gap:
                flush()
                gap = 0
    flush()
    return findings


def _finding(run: list[Call], view: SessionView, calls: list[Call], case_insensitive: bool, parallel_seen: bool) -> B.Finding:
    first = run[0]
    indep, indep_why = _independence(run, calls, case_insensitive)
    conf = {"high": B.CONFIDENCE_HIGH, "medium": B.CONFIDENCE_MEDIUM, "low": B.CONFIDENCE_LOW}[indep]
    grouped = _grouped_tool(run, view, parallel_seen)
    return B.Finding(
        rule_id=RULE_ID, rule_version=RULE_VERSION, kind="sequential_similar_calls",
        title=f"{len(run)} appels {first.tool_name} sequentiels sur des cibles differentes",
        confidence=conf, confidence_rationale=f"independance : {indep_why}. " + B.LIMIT_HEURISTIC,
        calls=[c.key for c in run], call_refs=B.refs(run),
        evidence={"tool": first.tool_name, "targets": [c.target for c in run], "independence": indep,
                  "independence_basis": indep_why, "sequential": True, "grouped_tool": grouped,
                  "durations_ms": [c.duration_ms for c in run], "duration_sources": sorted({c.duration_source for c in run if c.duration_source})},
        explanation=(f"{len(run)} appels {first.tool_name} ont ete emis l'un apres l'autre (chacun apres la fin du precedent) "
                     f"sur {len(run)} cibles distinctes, sans appel intermediaire."),
        counter_indications=[
            "Si une cible a ete choisie d'apres le contenu lu juste avant, les appels ne sont pas independants.",
            "Un regroupement concatene les sorties : verifier que la taille totale reste acceptable pour le contexte.",
            "Les contraintes du client (limites de parallelisme, sandbox, quotas MCP) ne sont pas observees.",
        ],
        missing_data=["contenu des resultats non conserve : la dependance entre lectures est inferee, pas observee"]
                     + (["durees inconnues"] if all(c.duration_ms is None for c in run) else []),
        observed_cost=B.observed_cost(run),
        proposal={
            "type": "group_calls",
            "grouped_tool": grouped,
            "text": ("Emettre ces lectures en une seule fois (appels paralleles dans le meme tour, ou un outil acceptant "
                     "plusieurs cibles) lorsque l'independance est etablie."),
            "requires_judgment": "Confirmer que les cibles etaient connues avant la premiere lecture.",
        },
        validation_protocol=[
            "Verifier dans le transcript que la liste des cibles etait connue avant le premier appel.",
            "Comparer la duree totale (source indiquee) avec celle d'un tour groupe apres modification.",
            "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
        ],
    )
