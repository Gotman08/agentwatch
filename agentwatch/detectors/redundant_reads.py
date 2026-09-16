"""Detecteur A : lectures ou recherches probablement redondantes.

Regle : deux appels de lecture (read/search/list, ou shell classe "lecture") sur la meme
cible avec les memes parametres, dans le meme contexte (session, agent, epoque de
contexte), sans modification observee de la cible entre les deux.

Exclusions : modification intermediaire de la cible, autre agent, compaction/reprise
(epoque differente), parametres differents (plage, filtre, pagination), premier appel
en echec, resultats differents (relecture justifiee par un changement).
Degradations : appel a effet inconnu entre les deux (shell "run"/inconnu, MCP, agent),
statut ou empreinte de resultat inconnus -> "repetition a examiner".
"""

from __future__ import annotations

from typing import Any

from agentwatch.core import normalize as N
from agentwatch.core import schema as S
from agentwatch.core.correlate import STATUS_OPEN, Call, SessionView
from agentwatch.detectors import base as B

RULE_ID = "A.redundant_reads"
RULE_VERSION = "1.0"


def _cluster_key(c: Call) -> str:
    return f"{c.agent_key}|{c.context_epoch}|{c.tool_name}|{c.target_key}|{c.params_key}"


def _touches_target(w: Call, target_key: str | None, case_insensitive: bool) -> bool:
    """Un appel d'ecriture touche-t-il la cible (chemin) ?"""
    if target_key is None:
        return False
    if w.target_key == target_key:
        return True
    if not target_key.startswith("path:"):
        return False
    wanted = target_key[len("path:"):].split("::", 1)[-1]
    for key in ("patch_paths", "shell_paths"):
        for p in w.params.get(key) or []:
            if not isinstance(p, str):
                continue
            norm = N.path_key(N.normalize_path(p, w.project_dir, w.cwd), case_insensitive) or ""
            if norm == wanted or norm.endswith("/" + wanted) or wanted.endswith("/" + norm):
                return True
    return False


def _assess_pair(prev: Call, cur: Call, between: list[Call], case_insensitive: bool) -> dict[str, Any]:
    """Verdict pour (prev -> cur) : excluded / degraded / clean, avec raisons."""
    reasons: list[str] = []
    degrade: list[str] = []
    if prev.status in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED):
        reasons.append("premier appel en echec : relecture legitime (voir detecteur B)")
    if cur.status in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED):
        reasons.append("second appel en echec : pas une repetition reussie")
    if prev.result_fingerprint and cur.result_fingerprint and prev.result_fingerprint != cur.result_fingerprint:
        reasons.append("resultats differents : la cible a change entre les deux appels")
    shell_read = prev.category == S.CAT_SHELL
    for w in between:
        if w.is_write_like and (shell_read or _touches_target(w, prev.target_key, case_insensitive)):
            reasons.append(f"modification intermediaire observee (appel #{w.seq} {w.tool_name})")
            break
        if w.unknown_effect:
            degrade.append(f"appel a effet inconnu entre les deux (#{w.seq} {w.tool_name})")
    if reasons:
        return {"verdict": "excluded", "reasons": reasons}
    if prev.status in (S.STATUS_UNKNOWN, STATUS_OPEN):
        degrade.append("statut du premier appel inconnu")
    if not (prev.result_fingerprint and cur.result_fingerprint):
        degrade.append("empreinte de resultat indisponible pour au moins un des deux appels")
    return {"verdict": "degraded" if degrade else "clean", "reasons": degrade}


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    d = cfg.get("detectors", {}).get("redundant_reads", {})
    window_calls = int(d.get("window_calls", 60))
    window_seconds = int(d.get("window_seconds", 900))
    case_insensitive = bool(cfg.get("case_insensitive_paths", False))
    calls = view.calls
    last_by_key: dict[str, Call] = {}
    clusters: dict[str, dict[str, Any]] = {}
    cluster_of: dict[str, str] = {}
    findings: list[B.Finding] = []

    for cur in calls:
        if not cur.is_read_like or cur.target_key is None:
            continue
        key = _cluster_key(cur)
        prev = last_by_key.get(key)
        last_by_key[key] = cur
        if prev is None or not B.within_window(prev, cur, window_calls, window_seconds):
            continue
        between = [c for c in calls[prev.seq + 1: cur.seq] if c.agent_key == cur.agent_key]
        verdict = _assess_pair(prev, cur, between, case_insensitive)
        if verdict["verdict"] == "excluded":
            continue
        cid = cluster_of.get(prev.key)
        if cid is None:
            cid = prev.key
            clusters[cid] = {"calls": [prev], "verdicts": [], "between": []}
            cluster_of[prev.key] = cid
        clusters[cid]["calls"].append(cur)
        clusters[cid]["verdicts"].append(verdict)
        clusters[cid]["between"].append([b.seq for b in between if b.unknown_effect or b.is_write_like])
        cluster_of[cur.key] = cid

    for cid, cl in clusters.items():
        members: list[Call] = cl["calls"]
        verdicts = cl["verdicts"]
        n = len(members)
        fps = {c.result_fingerprint for c in members if c.result_fingerprint}
        all_clean = all(v["verdict"] == "clean" for v in verdicts)
        fps_known = sum(1 for c in members if c.result_fingerprint) == n
        if all_clean and fps_known and len(fps) == 1:
            conf, why = B.CONFIDENCE_HIGH, ("empreintes de resultat identiques, aucune modification ni appel a effet "
                                            "inconnu observe entre les appels, meme agent et meme epoque de contexte")
        elif fps_known and len(fps) == 1:
            conf, why = B.CONFIDENCE_MEDIUM, ("empreintes identiques mais un appel a effet inconnu s'est intercale : "
                                              "la relecture peut etre une verification volontaire")
        elif all_clean:
            conf, why = B.CONFIDENCE_MEDIUM, ("aucun changement observe mais l'empreinte de resultat manque pour au "
                                              "moins un appel : repetition a examiner")
        else:
            conf, why = B.CONFIDENCE_LOW, "empreinte manquante et appel a effet inconnu intercale : repetition a examiner"
        first = members[0]
        zero_results = any(c.evidence.get("result_count") == 0 for c in members)
        counter = [
            "Une verification apres une commande a effet inconnu peut etre justifiee." if not all_clean else
            "Aucune verification necessaire n'a ete observee, mais l'intention du modele n'est pas visible.",
            B.LIMIT_CONTEXT_UNKNOWN,
            B.LIMIT_EXTERNAL_CHANGES,
        ]
        if zero_results:
            counter.append("Au moins une recherche a retourne zero resultat : ce n'est pas en soi une preuve d'inutilite.")
        missing = []
        if not fps_known:
            missing.append("empreinte de resultat absente (appel sans evenement de fin ou statut inconnu)")
        if any(c.duration_ms is None for c in members):
            missing.append("duree inconnue pour certains appels")
        title = f"{first.tool_name} repete {n} fois sur {first.target!r}"
        if first.category == S.CAT_SHELL:
            title = f"Commande de lecture repetee {n} fois : {first.target!r}"
        findings.append(B.Finding(
            rule_id=RULE_ID, rule_version=RULE_VERSION, kind="repeated_read" if first.category != S.CAT_SHELL else "repeated_shell_read",
            title=title, confidence=conf, confidence_rationale=why + ". " + B.LIMIT_HEURISTIC,
            calls=[c.key for c in members], call_refs=B.refs(members),
            evidence={
                "target": first.target, "target_kind": first.target_kind, "params": {k: v for k, v in first.params.items() if k not in ("shell_heads",)},
                "result_fingerprints_distinct": len(fps), "result_fingerprints_known": fps_known,
                "statuses": [c.status for c in members], "intervening_notable_calls": cl["between"],
                "context_epoch": first.context_epoch, "agent": first.agent_key,
                "pair_verdicts": verdicts,
            },
            explanation=(f"L'appel {first.tool_name} sur la meme cible avec les memes parametres a ete emis {n} fois "
                         f"dans la fenetre ({window_calls} appels / {window_seconds} s). "
                         + ("Le resultat observe etait identique a chaque fois." if len(fps) == 1 and fps_known else
                            "Le resultat n'a pas pu etre compare pour tous les appels.")),
            counter_indications=counter, missing_data=missing, observed_cost=B.observed_cost(members[1:]),
            proposal={
                "type": "reuse_previous_result",
                "text": ("Reutiliser le resultat de la premiere lecture tant qu'aucune modification de la cible "
                         "n'est observee ; si une verification est voulue apres une commande a effet inconnu, "
                         "preferer une lecture ciblee (plage de lignes ou filtre) plutot qu'une relecture complete."),
                "requires_judgment": "Decider si la relecture etait une verification intentionnelle.",
            },
            validation_protocol=[
                "Ouvrir le transcript du client a l'horodatage des appels et verifier que le premier resultat etait encore visible.",
                "Comparer les empreintes de resultat (identiques => contenu identique au moment des appels).",
                "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
            ],
        ))
    return findings
