"""Detecteur A : travail de lecture probablement redondant, et executions relancees sans changement.

Compare des UNITES DE TRAVAIL (core/intent.py), pas des appels bruts : lire un fichier
via l'outil Read, `cat`, `Get-Content` ou `sed -n` est la meme operation ; chercher via
l'outil Grep ou `rg` aussi. La preuve d'identite du resultat est l'empreinte du CONTENU
obtenu (texte normalise), qui ne depend pas de l'outil.

Deux familles de signalement :
- `repeated_read` : meme lecture/recherche/listage, meme cible, memes parametres, meme
  agent, meme epoque de contexte, sans modification observee de la cible entre les deux ;
- `repeated_run` : meme execution (tests, build, script) relancee sans qu'aucune
  ecriture ni edition n'ait ete observee entre deux lancements, avec le meme statut.

Exclusions : ecriture observee sur la cible, autre agent, compaction/reprise, parametres
differents (plage, filtre, pagination), premier appel en echec, contenu obtenu different.
Degradations : appel a effet inconnu intercale, statut ou empreinte inconnus.
"""

from __future__ import annotations

from typing import Any

from agentwatch.core import intent as I
from agentwatch.core import normalize as N
from agentwatch.core import schema as S
from agentwatch.core.correlate import STATUS_OPEN, Call, SessionView
from agentwatch.detectors import base as B

RULE_ID = "A.redundant_reads"
RULE_VERSION = "2.0"
_FAILED = {S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED}


def _eligible_read(c: Call) -> bool:
    return c.op in I.READ_LIKE_OPS or (c.op == I.OP_UNKNOWN and c.is_read_like)


def _cluster_key(c: Call) -> str:
    return f"{c.agent_key}|{c.context_epoch}|{c.op_key}"


def _writer_targets(w: Call, case_insensitive: bool) -> set[str]:
    """Chemins qu'un appel d'ecriture touche (cible, patch, chemins de commande)."""
    out: set[str] = set()
    if w.op in (I.OP_EDIT, I.OP_WRITE) and w.op_target:
        out.add(str(w.op_target))
    for key in ("patch_paths", "shell_paths"):
        for p in w.params.get(key) or []:
            if isinstance(p, str):
                k = N.path_key(N.normalize_path(p, w.project_dir, w.cwd), case_insensitive)
                if k:
                    out.add(k)
    return out


def _touches(w: Call, target: str | None, case_insensitive: bool) -> bool:
    if target is None:
        return False
    for t in _writer_targets(w, case_insensitive):
        if t == target or t.endswith("/" + target) or target.endswith("/" + t):
            return True
    return False


def _same_result(a: Call, b: Call) -> tuple[bool | None, str]:
    """(identique?, base). None si non comparable."""
    if a.content_fingerprint and b.content_fingerprint:
        return a.content_fingerprint == b.content_fingerprint, "empreinte du contenu obtenu (texte normalise)"
    if a.result_fingerprint and b.result_fingerprint and a.tool_name == b.tool_name:
        return a.result_fingerprint == b.result_fingerprint, "empreinte de la reponse complete (meme outil)"
    return None, "non comparable (empreinte manquante ou outils differents sans contenu)"


def _assess_pair(prev: Call, cur: Call, between: list[Call], case_insensitive: bool) -> dict[str, Any]:
    reasons: list[str] = []
    degrade: list[str] = []
    if prev.status in _FAILED:
        reasons.append("premier appel en echec : relecture legitime (voir detecteur B)")
    if cur.status in _FAILED:
        reasons.append("second appel en echec : pas une repetition reussie")
    same, basis = _same_result(prev, cur)
    if same is False:
        reasons.append(f"resultats differents ({basis}) : la cible a change entre les deux appels")
    path_based = prev.op in (I.OP_READ, I.OP_SEARCH, I.OP_LIST)
    for w in between:
        if prev.op == I.OP_MCP_READ:
            # * Ressource distante : une ecriture locale ne la modifie pas ; seul un appel MCP
            #   non-lecture vers le meme serveur peut l'avoir changee (degrade, n'exclut pas).
            if w.category == S.CAT_MCP and w.op != I.OP_MCP_READ and w.mcp_server == prev.mcp_server:
                degrade.append(f"appel MCP a effet possible sur le meme serveur (#{w.seq} {w.tool_name})")
            continue
        if w.is_write_like and ((not path_based) or _touches(w, prev.op_target, case_insensitive)):
            reasons.append(f"modification intermediaire observee (appel #{w.seq} {w.tool_name})")
            break
        if w.unknown_effect or w.op in I.RUN_LIKE_OPS:
            degrade.append(f"appel a effet inconnu entre les deux (#{w.seq} {w.tool_name})")
    if reasons:
        return {"verdict": "excluded", "reasons": reasons}
    if prev.status in (S.STATUS_UNKNOWN, STATUS_OPEN):
        degrade.append("statut du premier appel inconnu")
    if same is None:
        degrade.append("resultat non comparable : " + basis)
    return {"verdict": "degraded" if degrade else "clean", "reasons": degrade, "same_result_basis": basis}


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    d = cfg.get("detectors", {}).get("redundant_reads", {})
    window_calls = int(d.get("window_calls", 60))
    window_seconds = int(d.get("window_seconds", 900))
    case_insensitive = bool(cfg.get("case_insensitive_paths", False))
    calls = view.calls
    findings: list[B.Finding] = []
    findings += _detect_reads(calls, window_calls, window_seconds, case_insensitive)
    findings += _detect_runs(calls, window_calls, window_seconds)
    return findings


# ---------------------------------------------------------------------------- lectures
def _detect_reads(calls: list[Call], window_calls: int, window_seconds: int, case_insensitive: bool) -> list[B.Finding]:
    last_by_key: dict[str, Call] = {}
    clusters: dict[str, dict[str, Any]] = {}
    cluster_of: dict[str, str] = {}
    for cur in calls:
        if not _eligible_read(cur) or not cur.op_key:
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
        clusters[cid]["between"].append([b.seq for b in between if b.unknown_effect or b.is_write_like or b.op in I.RUN_LIKE_OPS])
        cluster_of[cur.key] = cid

    findings: list[B.Finding] = []
    for cl in clusters.values():
        members: list[Call] = cl["calls"]
        verdicts = cl["verdicts"]
        n = len(members)
        first = members[0]
        tools = sorted({m.tool_name or "?" for m in members})
        all_clean = all(v["verdict"] == "clean" for v in verdicts)
        comparable = [v for v in verdicts if v.get("same_result_basis", "").startswith("empreinte")]
        all_same_known = len(comparable) == len(verdicts)
        if all_clean and all_same_known:
            conf, why = B.CONFIDENCE_HIGH, ("contenu obtenu identique a chaque fois, aucune modification ni appel a effet "
                                            "inconnu observe entre les appels, meme agent et meme epoque de contexte")
        elif all_same_known:
            conf, why = B.CONFIDENCE_MEDIUM, ("contenu identique mais un appel a effet inconnu s'est intercale : "
                                              "la relecture peut etre une verification volontaire")
        elif all_clean:
            conf, why = B.CONFIDENCE_MEDIUM, "aucun changement observe mais le contenu n'a pas pu etre compare : repetition a examiner"
        else:
            conf, why = B.CONFIDENCE_LOW, "contenu non comparable et appel a effet inconnu intercale : repetition a examiner"
        if first.op == I.OP_MCP_READ and conf == B.CONFIDENCE_HIGH:
            conf = B.CONFIDENCE_MEDIUM
            why += (" ; outil MCP : l'etat renvoye peut etre volatil et le serveur peut avoir des effets non declares ; "
                    "les ecritures locales intercalees ne sont pas considerees comme modifiant une ressource distante")
        cross_tool = len(tools) > 1
        title = f"{first.op} de {first.op_target!r} repete {n} fois via {', '.join(tools)}"
        if first.op == I.OP_UNKNOWN:
            title = f"Commande de lecture repetee {n} fois : {first.target!r}"
        counter = [
            "Une verification apres une commande a effet inconnu peut etre justifiee." if not all_clean else
            "Aucune verification necessaire n'a ete observee, mais l'intention du modele n'est pas visible.",
            B.LIMIT_CONTEXT_UNKNOWN, B.LIMIT_EXTERNAL_CHANGES,
        ]
        if first.op == I.OP_MCP_READ:
            counter.append("Interroger un etat distant en attendant un changement (polling) est parfois voulu ; "
                           "un contenu identique a chaque fois montre seulement qu'aucune information nouvelle n'a ete obtenue.")
        if cross_tool:
            counter.append("Le meme travail a ete fait par des outils differents : la forme differait, pas la tache.")
        if any(c.evidence.get("result_count") == 0 for c in members):
            counter.append("Au moins une recherche a retourne zero resultat : ce n'est pas en soi une preuve d'inutilite.")
        missing = []
        if not all_same_known:
            missing.append("empreinte de contenu absente pour au moins un appel (pas d'evenement de fin, ou reponse sans texte)")
        if any(c.duration_ms is None for c in members):
            missing.append("duree inconnue pour certains appels")
        findings.append(B.Finding(
            rule_id=RULE_ID, rule_version=RULE_VERSION, kind="repeated_read" if not cross_tool else "repeated_read_cross_tool",
            title=title, confidence=conf, confidence_rationale=why + ". " + B.LIMIT_HEURISTIC,
            calls=[c.key for c in members], call_refs=B.refs(members),
            evidence={
                "operation": first.op, "target": first.op_target, "params": first.op_params, "tools_used": tools,
                "same_result_basis": sorted({v.get("same_result_basis", "") for v in verdicts}),
                "statuses": [c.status for c in members],
                "intervening_notable_calls": [{"count": len(b), "first_seqs": b[:5]} for b in cl["between"]],
                "context_epoch": first.context_epoch, "agent": first.agent_key, "pair_verdicts": verdicts,
            },
            explanation=(f"La meme unite de travail ({first.op} sur {first.op_target!r}, memes parametres) a ete refaite {n} fois "
                         f"dans la fenetre ({window_calls} appels / {window_seconds} s)" + (f", par des outils differents ({', '.join(tools)})." if cross_tool else ".")
                         + (" Le contenu obtenu etait identique a chaque fois." if all_same_known else " Le contenu n'a pas pu etre compare pour tous les appels.")),
            counter_indications=counter, missing_data=missing, observed_cost=B.observed_cost(members[1:]),
            proposal={
                "type": "reuse_previous_result",
                "text": (("Pour un etat distant : attendre un evenement (ex. wait_for_job) ou espacer les interrogations, "
                          "et reutiliser le dernier etat tant qu'aucune action n'a ete soumise au serveur.")
                         if first.op == I.OP_MCP_READ else
                         ("Reutiliser le resultat de la premiere fois tant qu'aucune modification de la cible n'est observee ; "
                          "si une verification est voulue, preferer une lecture ciblee (plage ou filtre) et un seul outil.")),
                "requires_judgment": "Decider si la repetition etait une verification intentionnelle.",
            },
            validation_protocol=[
                "Ouvrir le transcript aux horodatages des appels et verifier que le premier resultat etait encore utilisable.",
                "Comparer les empreintes de contenu (identiques => meme texte obtenu, quel que soit l'outil).",
                "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
            ],
        ))
    return findings


# ---------------------------------------------------------------------------- executions
def _detect_runs(calls: list[Call], window_calls: int, window_seconds: int) -> list[B.Finding]:
    chains: dict[str, list[Call]] = {}
    closed: list[list[Call]] = []
    for c in calls:
        # * Les relances d'une execution en ECHEC relevent du detecteur B (boucles d'erreurs).
        if c.op not in I.RUN_LIKE_OPS or c.status in _FAILED:
            continue
        key = _cluster_key(c)
        chain = chains.get(key)
        if chain:
            prev = chain[-1]
            between = [b for b in calls[prev.seq + 1: c.seq] if b.agent_key == c.agent_key]
            changed = any(b.is_write_like for b in between)
            if changed or prev.status != c.status or not B.within_window(prev, c, window_calls, window_seconds):
                closed.append(chain)
                chains[key] = [c]
                continue
            chain.append(c)
        else:
            chains[key] = [c]
    closed.extend(chains.values())
    findings: list[B.Finding] = []
    for chain in closed:
        if len(chain) < 2:
            continue
        first = chain[0]
        fps = [c.content_fingerprint for c in chain]
        same_output = all(fps) and len(set(fps)) == 1
        unknown_between = any(b.unknown_effect for a, c in zip(chain, chain[1:]) for b in calls[a.seq + 1: c.seq] if b.agent_key == c.agent_key)
        if same_output and not unknown_between:
            conf, why = B.CONFIDENCE_MEDIUM, f"{len(chain)} lancements avec la meme sortie et le meme statut, sans ecriture ni edition observee entre eux"
        else:
            conf, why = B.CONFIDENCE_LOW, ("relances sans ecriture observee, mais sortie differente ou non comparable, ou appel a effet inconnu intercale : "
                                           "peut etre un test de non-determinisme")
        findings.append(B.Finding(
            rule_id=RULE_ID, rule_version=RULE_VERSION, kind="repeated_run",
            title=f"{first.op} relance {len(chain)} fois sans changement observe : {first.target!r}",
            confidence=conf, confidence_rationale=why + ". " + B.LIMIT_HEURISTIC,
            calls=[c.key for c in chain], call_refs=B.refs(chain),
            evidence={"operation": first.op, "target": first.op_target, "params": first.op_params,
                      "statuses": [c.status for c in chain], "same_output": same_output,
                      "content_fingerprints_known": all(fps), "unknown_effect_between": unknown_between},
            explanation=(f"La meme execution ({first.op} : {first.target!r}) a ete relancee {len(chain)} fois avec le meme statut, "
                         "sans qu'aucune ecriture, edition ou patch n'ait ete observe entre les lancements."),
            counter_indications=[
                "Relancer pour verifier un test instable (flaky) ou un effet non deterministe est parfois voulu.",
                "Une commande peut avoir des effets de bord ou dependre d'un etat externe non observe.",
                B.LIMIT_EXTERNAL_CHANGES,
            ],
            missing_data=[] if all(fps) else ["sortie non comparable pour certains lancements"],
            observed_cost=B.observed_cost(chain[1:]),
            proposal={"type": "avoid_rerun_without_change",
                      "text": "Ne relancer qu'apres une modification, ou reutiliser la sortie precedente ; si l'objectif est de tester la stabilite, le dire explicitement.",
                      "requires_judgment": "Determiner si la relance visait a verifier un comportement non deterministe."},
            validation_protocol=[
                "Verifier dans le transcript ce qui a motive chaque relance.",
                "Comparer les sorties (empreintes de contenu) : identiques => aucune information nouvelle obtenue.",
                "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
            ],
        ))
    return findings
