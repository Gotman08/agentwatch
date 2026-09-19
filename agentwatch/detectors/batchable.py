"""Detecteur C : operations regroupables.

Regle : une suite d'au moins `min_group` appels consecutifs du meme outil de lecture
(read/list/search, ou MCP cible par chemin) sur des cibles differentes, emis dans des
reponses successives du modele (un aller-retour par appel), sans erreur.

# ! Des appels emis ensemble dans une meme reponse peuvent etre executes l'un apres l'autre
#   par le client : ce n'est pas au modele qu'il faut le reprocher. Faux positif constate le
#   2026-09-19 (3 Read emis dans une reponse, executes en serie par Claude Code). Preuve de
#   separation : identifiant de la requete emettrice lu dans le transcript (exact), sinon ecart
#   entre la fin d'un appel et le debut du suivant (heuristique) : mesure sur 249 paires reelles,
#   deux lectures d'une meme reponse sont separees de -119 a 1 967 ms (surcout des hooks), deux
#   reponses distinctes d'au moins 2 559 ms (temps de reponse du modele). Seuil par defaut 2 000 ms.

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
RULE_VERSION = "1.2"
_GROUPABLE = {S.CAT_READ, S.CAT_LIST, S.CAT_SEARCH, S.CAT_MCP}
_SHELL_OPS = ("read", "search", "list")
_BATCH_HINTS = ("batch", "many", "multi", "all", "bulk", "list")


def _eligible(c: Call) -> bool:
    # * Commande shell traduite en lecture, recherche ou listage (core/intent.py) : Codex lit TOUT par le shell
    #   (Get-Content, rg, Get-ChildItem) ; sans ce cas, la regle ne voyait jamais rien sur Codex.
    if c.category == S.CAT_SHELL:
        if c.op not in _SHELL_OPS or not c.op_target:
            return False
    elif c.category not in _GROUPABLE or c.target_key is None:
        return False
    if c.category == S.CAT_MCP and c.target_kind != "path":
        return False
    return c.status not in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED)


def _kind(c: Call) -> str:
    """Outil compare : pour le shell, l'operation (une lecture et une recherche ne forment pas une serie)."""
    return f"{c.tool_name}:{c.op}" if c.category == S.CAT_SHELL else str(c.tool_name)


def _target(c: Call) -> str | None:
    """Cible comparee : le fichier lu pour le shell (deux commandes differentes sur un meme fichier = meme cible)."""
    return f"op:{c.op_target}" if c.category == S.CAT_SHELL else c.target_key


def _overlaps(prev: Call, cur: Call) -> bool | None:
    """True si cur a commence avant la fin de prev (deja parallele) ; None si inconnu."""
    if prev.end_ns is None or cur.start_ns is None:
        return None
    return cur.start_ns < prev.end_ns


def _emitter(c: Call) -> str | None:
    """Requete API qui a emis l'appel (transcript Claude Code ou rollout Codex importe), sinon None."""
    rid = (c.usage or {}).get("emitter_request_id")
    return rid if isinstance(rid, str) and rid else None


def _gap_ms(prev: Call, cur: Call) -> int | None:
    if prev.end_ns is None or cur.start_ns is None:
        return None
    return (cur.start_ns - prev.end_ns) // 1_000_000


def _same_response(prev: Call, cur: Call, gap_threshold_ms: int) -> tuple[bool, str]:
    """(emis dans la meme reponse du modele ?, base de la decision)."""
    ea, eb = _emitter(prev), _emitter(cur)
    if ea and eb:
        return ea == eb, "transcript"
    gap = _gap_ms(prev, cur)
    if gap is None:
        return False, "inconnu"
    return gap < gap_threshold_ms, "ecart"


def _path_of(c: Call) -> str:
    if c.category == S.CAT_SHELL:
        return str(c.op_target or "")
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
    if first.category == S.CAT_SHELL and view.client != "codex":
        return {"status": "proposal", "note": "une seule commande avec plusieurs chemins (ex. cat a b c, rg motif a b c) ; non verifiee"}
    if first.category == S.CAT_SHELL and parallel_seen:
        return {"status": "verified_in_session", "note": ("le modele a deja regroupe plusieurs actions dans un meme exec (ou une meme "
                                                          "commande) dans cette session : lire ces fichiers en une fois est possible")}
    if first.category == S.CAT_SHELL:
        return {"status": "proposal", "note": "un seul exec lisant tous les fichiers (Promise.all ou boucle), ou une commande a plusieurs chemins"}
    if parallel_seen:
        return {"status": "verified_in_session", "note": ("des appels du meme outil emis ensemble (chevauchants, ou dans une meme "
                                                          "reponse du modele) ont ete observes dans cette session")}
    if view.client == CLIENT_CLAUDE_CODE:
        return {"status": "documented_not_verified", "note": "Claude Code peut emettre plusieurs appels d'outil dans un meme tour ; non observe dans cette session"}
    return {"status": "unknown", "note": "capacite de regroupement non verifiee pour ce client"}


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    d = cfg.get("detectors", {}).get("batchable", {})
    min_group = int(d.get("min_group", 3))
    max_gap = int(d.get("max_gap_calls", 0))
    gap_threshold = int(d.get("same_response_gap_ms", 2000))
    case_insensitive = bool(cfg.get("case_insensitive_paths", False))
    calls = view.calls
    parallel_seen = any(_overlaps(a, b) or _same_response(a, b, gap_threshold)[0]
                        for a, b in zip(calls, calls[1:]) if _kind(a) == _kind(b) and a.agent_key == b.agent_key)
    findings: list[B.Finding] = []
    run: list[Call] = []
    gap = 0

    def flush() -> None:
        nonlocal run
        if len(run) >= min_group:
            findings.append(_finding(run, view, calls, case_insensitive, parallel_seen, gap_threshold))
        run = []

    for c in calls:
        if run and c.agent_key != run[-1].agent_key:
            flush()
            gap = 0
        if _eligible(c):
            if run and _kind(c) == _kind(run[-1]) and _target(c) not in {_target(r) for r in run}:
                if _overlaps(run[-1], c) or _same_response(run[-1], c, gap_threshold)[0]:
                    flush()          # ? deja emis ensemble (parallele, ou meme reponse executee en serie) : rien a regrouper
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


def _finding(run: list[Call], view: SessionView, calls: list[Call], case_insensitive: bool, parallel_seen: bool,
             gap_threshold: int) -> B.Finding:
    first = run[0]
    indep, indep_why = _independence(run, calls, case_insensitive)
    conf = {"high": B.CONFIDENCE_HIGH, "medium": B.CONFIDENCE_MEDIUM, "low": B.CONFIDENCE_LOW}[indep]
    grouped = _grouped_tool(run, view, parallel_seen)
    exact = all(_same_response(a, b, gap_threshold)[1] == "transcript" for a, b in zip(run, run[1:]))
    if exact:
        separation = "requetes emettrices distinctes (transcript ou rollout)"
    else:
        separation = f"ecarts d'au moins {gap_threshold} ms entre la fin d'un appel et le debut du suivant (heuristique)"
    return B.Finding(
        rule_id=RULE_ID, rule_version=RULE_VERSION, kind="sequential_similar_calls",
        title=(f"{len(run)} appels {first.tool_name} ({first.op}) sequentiels sur des cibles differentes" if first.category == S.CAT_SHELL
               else f"{len(run)} appels {first.tool_name} sequentiels sur des cibles differentes"),
        confidence=conf, confidence_rationale=f"independance : {indep_why} ; reponses distinctes : {separation}. " + B.LIMIT_HEURISTIC,
        calls=[c.key for c in run], call_refs=B.refs(run),
        evidence={"tool": first.tool_name, "targets": [c.target for c in run], "independence": indep,
                  "independence_basis": indep_why, "sequential": True, "grouped_tool": grouped,
                  "round_trips": len(run), "separation_basis": separation,
                  "gaps_ms": [_gap_ms(a, b) for a, b in zip(run, run[1:])],
                  "durations_ms": [c.duration_ms for c in run], "duration_sources": sorted({c.duration_source for c in run if c.duration_source})},
        explanation=(f"{len(run)} appels {first.tool_name} ont ete emis dans {len(run)} reponses successives du modele "
                     f"(chacun apres le resultat du precedent) sur {len(run)} cibles distinctes, sans appel intermediaire."),
        counter_indications=[
            "Si une cible a ete choisie d'apres le contenu lu juste avant, les appels ne sont pas independants.",
            "Un regroupement concatene les sorties : verifier que la taille totale reste acceptable pour le contexte.",
            "Les contraintes du client (limites de parallelisme, sandbox, quotas MCP) ne sont pas observees.",
        ],
        missing_data=["contenu des resultats non conserve : la dependance entre lectures est inferee, pas observee"]
                     + ([] if exact else
                        ["requete emettrice inconnue (transcript ou rollout non importe) : separation des reponses deduite des ecarts"])
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
