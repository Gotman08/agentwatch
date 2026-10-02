"""Evaluation transversale des remplacements proposes par A-G.

Locale, passive, deterministe. Les faits restent trivalues ; les scenarios ne
modifient pas la trace. Les hypotheses de sensibilite ne deviennent jamais des
preuves. Les couts inconnus restent None, notamment tokens et temps mural.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

from agentwatch.core.correlate import Call, SessionView
from agentwatch.detectors.base import Finding, cost_tokens, emitter, observed_cost
from agentwatch.reports.observed_graph import before, build_observed_graph, check_removal, reference

ESTABLISHED = "established"
REFUTED = "refuted"
UNKNOWN = "unknown"
STATUS_LABELS = {"admissible": "admissible", "rejected": "rejete", "indeterminate": "indetermine"}
STRATEGY_LABELS = {"retain": "Conserver le travail", "reuse": "Reutiliser un resultat",
                   "batch": "Regrouper les acquisitions", "cadence": "Modifier la cadence",
                   "wait": "Attendre un etat terminal", "structured": "Capacite structuree",
                   "guidance": "Skill ou regle de projet"}
_READ_OPS = {"read", "search", "list", "vcs_read"}
_PROTECTED_OPS = {"run_tests", "build", "run_script", "edit", "write", "agent"}
_FACT_LABELS = {
    "complete_call_scope": "perimetre complet",
    "successful_unambiguous_calls": "appels reussis et identifies",
    "read_only_operations": "absence d'effets a conserver",
    "observed_dependencies_preserved": "dependances observees",
    "explicit_references_resolved": "references explicites resolues",
    "same_scope": "memes plages et parametres",
    "same_actor": "meme acteur",
    "same_context_epoch": "contexte apres compaction",
    "acquired_before_reuse": "acquisition anterieure",
    "information_still_available": "information encore disponible",
    "version_stable_before_reuse": "version stable avant reutilisation",
    "freshness_and_checks_preserved": "fraicheur et verifications",
    "observed_content_equal": "egalite de contenu",
    "capability_available_before_work": "capacite disponible avant le travail",
    "equivalent_results_errors_and_effects": "equivalence des resultats, erreurs et effets",
    "permissions_and_limits_compatible": "droits et limites",
    "parameters_known_at_start": "parametres connus au depart",
    "no_intermediate_dependency": "independance des operations",
    "batch_volume": "volume du regroupement",
    "reaction_deadline_and_terminal_state": "delai et etat terminal",
    "reliable_timing": "horodatages fiables",
    "reaction_deadline": "delai de reaction",
    "context_preserved": "contexte et consignes",
    "state_persists_until_next_observation": "persistance de l'etat",
    "required_observations_preserved": "observations necessaires",
    "procedure_preserves_required_work": "travail necessaire preserve",
}

# Questions de verification, jamais des boutons pour declarer une condition vraie.
_VERIFICATIONS = {
    "complete_call_scope": ("Resoudre les identifiants d'appel manquants dans les evenements normalises.", "Comparer un segment complet, sans sous-estimer son cout."),
    "successful_unambiguous_calls": ("Retrouver les fins d'appel et lever les collisions d'identifiants ; conserver erreurs et reprises.", "Determiner quelles operations restent necessaires."),
    "read_only_operations": ("Verifier le contrat de l'outil exact, y compris ses effets de bord.", "Decider si une acquisition peut etre evitee sans supprimer un effet requis."),
    "explicit_references_resolved": ("Retrouver le producteur explicitement reference, son acteur et son contexte.", "Verifier que les consommateurs conserves disposent encore de leurs entrees."),
    "same_scope": ("Comparer plages, filtres et parametres de chaque acquisition.", "Restreindre la reutilisation aux contenus effectivement equivalents."),
    "same_context_epoch": ("Verifier que le resultat exact est conserve dans le contexte de cet acteur apres compaction.", "Envisager une reutilisation apres cette frontiere seulement si la disponibilite est prouvee."),
    "acquired_before_reuse": ("Etablir la fin de l'acquisition source avant la demande suivante.", "Exclure une reutilisation qui supposerait une connaissance du futur."),
    "information_still_available": ("Verifier la presence du resultat exact dans le contexte accessible a cet acteur, au moment du nouvel appel.", "Choisir entre restitution du resultat disponible et nouvelle acquisition."),
    "version_stable_before_reuse": ("Verifier une version immuable ou un mecanisme de validation disponible avant la nouvelle acquisition.", "Determiner si le contenu precedent satisfait encore la version demandee."),
    "freshness_and_checks_preserved": ("Verifier l'exigence de fraicheur et la raison de la verification dans la tache.", "Conserver la nouvelle verification si son objectif exige un etat actualise."),
    "observed_content_equal": ("Retrouver des empreintes comparables du meme perimetre.", "Distinguer repetition de contenu et nouvelle information."),
    "capability_available_before_work": ("Identifier l'outil exact et une preuve locale d'acces anterieure au travail.", "Distinguer un outil utilisable d'une capacite a verifier ou a creer."),
    "equivalent_results_errors_and_effects": ("Verifier le contrat ou un essai separe : memes sorties, identifiants, echecs, annulations et effets.", "Determiner si la capacite peut remplacer l'ensemble des operations requises."),
    "permissions_and_limits_compatible": ("Verifier localement les droits, quotas et limites du remplacement pour ces entrees.", "Decider si la substitution est applicable dans cet environnement."),
    "parameters_known_at_start": ("Retrouver l'origine et la date de disponibilite de chaque parametre.", "Regrouper seulement les operations dont les entrees etaient deja connues."),
    "no_intermediate_dependency": ("Verifier les relations producteur-consommateur entre les operations du lot.", "Exclure du regroupement les etapes qui attendent un resultat intermediaire."),
    "batch_volume": ("Verifier les limites de taille d'entree et de sortie de la capacite groupee.", "Dimensionner un lot qui respecte le contrat et ses limites."),
    "reaction_deadline_and_terminal_state": ("Verifier le contrat d'attente : etats terminaux, echecs, annulation, delai maximal et reprises.", "Choisir une attente qui restitue encore l'etat requis dans le delai."),
    "reliable_timing": ("Retrouver des horodatages propres aux actions, non reecrits lors d'un import.", "Autoriser une simulation temporelle interpretable."),
    "reaction_deadline": ("Preciser lancement ou resultat recu, puis verifier les bornes de reponse, reprises et ordonnancement.", "Determiner si la cadence respecte le delai de reaction demande."),
    "context_preserved": ("Verifier les consignes et frontieres de contexte entre les consultations.", "Conserver toute consultation rendue necessaire par une nouvelle consigne."),
    "state_persists_until_next_observation": ("Verifier dans le contrat du service que les etats requis restent consultables jusqu'a la prochaine interrogation.", "Espacer les consultations seulement si aucun etat requis ne peut disparaitre."),
    "required_observations_preserved": ("Identifier les etats et informations intermediaires necessaires aux decisions, erreurs et verifications.", "Conserver les consultations qui portent une information necessaire."),
    "procedure_preserves_required_work": ("Relire la procedure proposee avec ses entrees, controles qualite, echecs et effets attendus.", "Adopter une regle seulement si elle conserve le travail requis, sans gain d'appels presume."),
}

_WAIT_CHECKS = {
    "implementation_bound": ("version de l'attente", "Relier l'empreinte du code audite a chaque appel observe ; une installation actuelle ne prouve pas la version historique."),
    "terminal_return": ("retour terminal", "Verifier le chemin de retour immediat apres detection d'un etat terminal dans l'implementation applicable."),
    "results_errors_identity": ("conformite du contrat de sortie et d'identification", "Preciser si l'identifiant doit etre dans la sortie ou accessible par association explicite appel-arguments-reponse suffisante pour le consommateur ; verifier ce contrat sur succes, echec, annulation et expiration. Une non-conformite ne prouve pas une degradation introduite par le remplacement."),
    "effects_preserved": ("effets requis", "Verifier les effets locaux et distants requis du remplacement."),
    "timeout_layers_compatible": ("timeouts des differentes couches", "Verifier les limites du client, du serveur, du transport et des reprises pour la duree proposee."),
    "intermediate_work_preserved": ("decisions et interruptions intermediaires", "Etablir les besoins de decision et de reprise du controle pendant l'attente ; l'absence d'appel intermediaire ne suffit pas."),
    "timeout_parameter_limit": ("limite du parametre d'attente", "Verifier le plafond accepte par l'implementation applicable, separement de la duree murale."),
    "control_return_budget": ("budget de reprise du controle", "Declarer le delai acceptable et verifier une borne de bout en bout incluant verrous et reprises."),
    "policy_bound": ("politique couverte par l'audit", "Lier l'audit aux timeout_seconds et poll_seconds exactement proposes."),
    "only_duration_changes": ("parametres preserves", "Verifier que seule la duree maximale augmente et que le sondage interne reste identique."),
    "same_job_actor_context": ("job, acteur et contexte identiques", "Retrouver un identifiant de job non ambigu dans le meme contexte."),
    "available_before_continuation": ("attente deja disponible", "Verifier le premier appel conserve avant la continuation."),
    "sequential_observations": ("attentes successives", "Verifier les horodatages et exclure les attentes qui se chevauchent pour ce job."),
}
for _name, (_label, _check) in _WAIT_CHECKS.items():
    _FACT_LABELS["wait_" + _name] = _label
    _VERIFICATIONS["wait_" + _name] = (_check, "Trancher l'allongement de l'attente pour ce seul perimetre.")


def fact(name: str, state: str, basis: str, calls: Iterable[Call] = ()) -> dict[str, Any]:
    return {"name": name, "state": state, "basis": basis, "evidence": [reference(c) for c in calls]}


def validity(facts: list[dict[str, Any]]) -> str:
    if any(f["state"] == REFUTED for f in facts):
        return "rejected"
    if any(f["state"] != ESTABLISHED for f in facts):
        return "indeterminate"
    return "admissible"


def _all(values: list[str]) -> str:
    return REFUTED if REFUTED in values else (ESTABLISHED if values and all(v == ESTABLISHED for v in values) else UNKNOWN)


def _readonly(c: Call) -> str:
    if c.is_write_like or c.op in _PROTECTED_OPS:
        return REFUTED
    if c.op in _READ_OPS:
        return ESTABLISHED
    # Le nom d'un outil MCP est une heuristique, jamais un contrat d'absence d'effet.
    hint = c.evidence.get("read_only_hint")
    return ESTABLISHED if hint is True else (REFUTED if hint is False else UNKNOWN)


def _guards(view: SessionView, graph: dict[str, Any], members: list[Call],
            removed: list[str], expected_count: int) -> list[dict[str, Any]]:
    keys = {c.key for c in members}
    proof = check_removal(graph, removed)
    incomplete = any(c.status in ("unknown", "open") or c.ambiguous for c in members)
    failed = any(c.status not in ("success", "unknown", "open") or c.evidence.get("error_hint")
                 or c.evidence.get("result_phase") == "failed" for c in members)
    broken_sources = {e["source"] for e in proof["broken_edges"]}
    return [
        fact("complete_call_scope", ESTABLISHED if members and len(keys) == len(members) == expected_count else UNKNOWN,
             "Tous les appels du signalement doivent etre resolus une seule fois.", members),
        fact("successful_unambiguous_calls", REFUTED if failed else (UNKNOWN if incomplete or not members else ESTABLISHED),
             "Conserver erreurs, interruptions, appels ouverts et identites ambigues.", members),
        fact("read_only_operations", _all([_readonly(c) for c in members]),
             "Les tests, ecritures, soumissions et effets inconnus ne sont pas supprimables comme des lectures.", members),
        fact("observed_dependencies_preserved", proof["state"],
             "Aucun producteur retire ne doit laisser un consommateur observe sans sa dependance.",
             [c for c in view.calls if c.key in broken_sources] if broken_sources else []),
        fact("explicit_references_resolved", UNKNOWN if any(r["target"] in keys for r in graph["unresolved"]) else ESTABLISHED,
             "Les references explicites manquantes ou hors portee empechent de valider le remplacement."),
    ]


def _cost(members: list[Call], status: str, estimated_calls: int | None, *, retain: bool = False) -> dict[str, Any]:
    observed = observed_cost(members)
    n = len(members)
    replacement = estimated_calls if status == "admissible" else None
    delta = n - replacement if replacement is not None else None
    return {
        "kind": "scenario_estimate_not_measured_saving", "scope": [c.key for c in members],
        "calls": {"observed": n, "replacement_estimate": replacement, "reduction_estimate": delta,
                  "relative_reduction_percent": round(100 * delta / n, 2) if delta is not None and n else None},
        "output_bytes": {"observed_sum": observed["output_bytes_sum"],
                         "known_for": observed["output_bytes_known_for"],
                         "replacement_estimate": observed["output_bytes_sum"] if retain else None,
                         "reduction_estimate": 0 if retain and observed["output_bytes_sum"] is not None else None},
        "tokens": {"observed_allocation": observed["tokens"],
                   "reduction_estimate": 0 if retain and cost_tokens(observed) is not None else None},
        "wall_time_reduction_ms": None,
        "note": "Pas de conversion octets/tokens, de somme des durees paralleles, ni de gain reel infere d'une suppression.",
    }


def _sensitivity(facts: list[dict[str, Any]], members: list[Call], estimated_calls: int | None) -> dict[str, Any]:
    unknowns = [f["name"] for f in facts if f["state"] == UNKNOWN]
    # Une variation a la fois, puis l'hypothese optimiste explicite ; aucun reclassement du scenario reel.
    variations = []
    def implications(values):
        known = {f["name"]: f["state"] for f in values}
        return [dict(f, state=f["conditional_state"]) if f.get("blocked_by")
                and f.get("conditional_state") in (ESTABLISHED, REFUTED)
                and all(known.get(name) == ESTABLISHED for name in f["blocked_by"]) else f for f in values]
    for name in unknowns:
        for state in (ESTABLISHED, REFUTED):
            variant = [dict(f, state=state) if f["name"] == name else f for f in facts]
            variations.append({"assumption": name, "assumed_state": state, "validity": validity(implications(variant))})
    optimistic = [dict(f, state=ESTABLISHED) if f["state"] == UNKNOWN else f for f in facts]
    hypothetical = validity(implications(optimistic))
    return {"unknown_preconditions": unknowns, "one_at_a_time": variations,
            "interaction_coverage": "not_exhaustive", "joint_assumptions_consistency": "not_verified",
            "all_unknowns_assumed_established": {
                "hypothetical_only": True, "assumptions": unknowns, "validity": hypothetical,
                "calls_reduction_estimate": (len(members) - estimated_calls
                                             if hypothetical == "admissible" and estimated_calls is not None else None)},
            "label": ("refuted_by_observation" if validity(facts) == "rejected" else
                      "depends_on_unverified_preconditions" if unknowns else "admissible_for_stated_contract"),
            "note": "Variations non exhaustives ; coherence conjointe non verifiee, aucune borne de gain demontree. Elles n'expliquent pas ce qu'aurait fait le LLM."}


def _scenario(kind: str, members: list[Call], removed: list[str], facts: list[dict[str, Any]],
              contract: str, estimated_calls: int | None, **extra: Any) -> dict[str, Any]:
    state = validity(facts)
    requests = []
    for f in facts:
        if f["state"] != UNKNOWN or f["name"] not in _VERIFICATIONS:
            continue
        check = (f.get("blocked_by") or [f["name"]])[0]
        requests.append({"precondition": f["name"], "missing_proof": f["basis"], "evidence": f["evidence"],
                         "verification_precondition": check, "verification": _VERIFICATIONS[check][0],
                         "decision_unlocked": _VERIFICATIONS[check][1]})
    return {"strategy": kind, "label": STRATEGY_LABELS[kind], "validity": state, "contract": contract,
            "affected_calls": [c.key for c in members], "removed_calls": removed,
            "preconditions": facts, "cost": _cost(members, state, estimated_calls, retain=kind == "retain"),
            "verification_requests": requests,
            "sensitivity": _sensitivity(facts, members, estimated_calls), **extra}


def _reuse(view: SessionView, graph: dict[str, Any], members: list[Call], expected: int) -> dict[str, Any]:
    first = members[0] if members else None
    removed = [c.key for c in members[1:]]
    fs = _guards(view, graph, members, removed, expected)
    same_scope = len({(c.client, c.session_id, c.project_dir, c.op_key) for c in members}) == 1
    known_scope = bool(members) and all(c.op_key and c.op_target for c in members)
    same_actor = len({(c.client, c.session_id, c.agent_key) for c in members}) == 1
    same_epoch = len({c.context_epoch for c in members}) == 1
    fs += [
        fact("same_scope", ESTABLISHED if known_scope and same_scope else (REFUTED if known_scope else UNKNOWN),
             "Meme operation et memes plages, filtres et parametres.", members),
        fact("same_actor", ESTABLISHED if first and same_actor else (REFUTED if first else UNKNOWN),
             "Aucun transfert de connaissance entre acteurs n'est presume.", members),
        fact("same_context_epoch", ESTABLISHED if first and same_epoch else UNKNOWN,
             "Une compaction exige de retablir la disponibilite de l'information.", members),
        fact("acquired_before_reuse", (ESTABLISHED if first and len(members) > 1 and
                                      all(before(first, c) for c in members[1:]) and
                                      first.agent_key not in view.timing_unreliable_agents else UNKNOWN),
             "Le resultat source doit etre termine avant chaque reutilisation.", members),
        fact("information_still_available", UNKNOWN,
             "Une meme empreinte et une meme epoque ne prouvent pas que l'acteur dispose encore du resultat."),
        fact("version_stable_before_reuse", UNKNOWN,
             "Les versions relevees APRES les acquisitions et l'absence d'ecriture ne prouvent pas la stabilite avant reutilisation."),
        fact("freshness_and_checks_preserved", UNKNOWN,
             "Une verification de fraicheur ou de qualite peut etre necessaire meme si la sortie est identique."),
    ]
    fingerprints = [c.content_fingerprint for c in members]
    if all(fingerprints) and fingerprints:
        fs.append(fact("observed_content_equal", ESTABLISHED if len(set(fingerprints)) == 1 else REFUTED,
                       "Egalite retrospective des contenus normalises seulement.", members))
    else:
        fs.append(fact("observed_content_equal", UNKNOWN, "Empreinte de contenu comparable absente.", members))
    return _scenario("reuse", members, removed, fs,
                     "Restituer le meme perimetre et une version valide au meme acteur, tout en conservant les verifications requises.",
                     1 if members else None)


def _capabilities(view: SessionView, cfg: dict[str, Any], finding: Finding, kind: str,
                  members: list[Call]) -> list[dict[str, Any]]:
    entries = cfg.get("replacements", {}).get("capabilities", [])
    if not isinstance(entries, list):
        entries = []
    result = []
    first = members[0] if members else None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("strategy") != kind:
            continue
        if "wait_contract" in entry:
            continue  # Contrats scopes traites par wait_contracts ; jamais appliques a un autre groupe.
        # Au moins une liaison explicite ; pas de correspondance par mots dans le nom.
        if not entry.get("rule_id") or entry["rule_id"] != finding.rule_id:
            continue
        if entry.get("target") is not None and entry["target"] != finding.evidence.get("target"):
            continue
        if entry.get("client") is not None and entry["client"] != view.client:
            continue
        tool = entry.get("tool_name")
        observed = [c for c in view.calls if first and tool and c.tool_name == tool and c.status == "success"
                    and before(c, first) and c.agent_key == first.agent_key and c.context_epoch == first.context_epoch
                    and c.agent_key not in view.timing_unreliable_agents]
        status = ("to_create" if entry.get("status") == "to_create" else
                  "observed_available" if observed else "declared_unverified")
        result.append({"id": str(entry.get("id") or tool or kind), "tool_name": tool, "status": status,
                       "contract": entry.get("contract"), "max_items": entry.get("max_items"),
                       "replacement_calls": entry.get("replacement_calls"),
                       "evidence": [reference(c) for c in observed[-3:]],
                       "equivalence": UNKNOWN})
    if not result:
        result = [{"id": kind, "tool_name": None, "status": "unknown", "contract": None,
                   "max_items": None, "replacement_calls": None, "evidence": [], "equivalence": UNKNOWN}]
    return result


def _substitution(view: SessionView, cfg: dict[str, Any], graph: dict[str, Any], finding: Finding,
                  members: list[Call], kind: str, expected: int) -> list[dict[str, Any]]:
    results = []
    for capability in _capabilities(view, cfg, finding, kind, members):
        removed = [c.key for c in members]
        fs = _guards(view, graph, members, removed, expected)
        if kind == "structured":
            # Un outil structure peut realiser un test ou une ecriture. Le contrat doit
            # les conserver ; on ne les traite pas comme des lectures supprimables.
            fs = [f for f in fs if f["name"] != "read_only_operations"]
        fs += [
            fact("capability_available_before_work",
                 ESTABLISHED if capability["status"] == "observed_available" else
                 REFUTED if capability["status"] == "to_create" else UNKNOWN,
                 "Presence observee de cet outil exact avant le travail ; cela ne prouve pas son equivalence."),
            fact("equivalent_results_errors_and_effects", UNKNOWN,
                 "Le contrat doit conserver sorties, erreurs, identifiants, permissions et effets de chaque operation."),
            fact("permissions_and_limits_compatible", UNKNOWN,
                 "Les permissions et quotas de cette operation ne se deduisent pas du nom du serveur."),
        ]
        if kind == "batch":
            emitters = [emitter(c) for c in members]
            known = bool(emitters) and all(emitters) and len(set(emitters)) == 1
            fs.append(fact("parameters_known_at_start", ESTABLISHED if known else UNKNOWN,
                           "Tous les parametres doivent etre disponibles avant la premiere acquisition.", members))
            internal = [e for e in graph["edges"] if e["requires_source"] and
                        e["source"] in removed and e["target"] in removed]
            fs.append(fact("no_intermediate_dependency", REFUTED if internal else UNKNOWN,
                           "L'absence d'arete dans un graphe partiel ne prouve pas l'independance."))
            limit = capability["max_items"]
            fs.append(fact("batch_volume", ESTABLISHED if type(limit) is int and limit >= len(members) else
                           REFUTED if type(limit) is int else UNKNOWN,
                           "Limite du catalogue local ; le volume des sorties reste a verifier."))
        if kind == "wait":
            fs.append(fact("reaction_deadline_and_terminal_state", UNKNOWN,
                           "Delai maximal, annulation, echec et etat terminal doivent etre preserves."))
        contract = {
            "batch": "Acquerir chaque perimetre, conserver l'identite et l'erreur de chaque resultat, respecter limites et dependances.",
            "wait": "Attendre le meme job jusqu'a un etat terminal avec delai maximal, retourner son identifiant, son etat et ses erreurs.",
            "structured": "Executer les memes operations avec sorties structurees, memes droits, erreurs et effets de bord.",
        }[kind]
        count = capability["replacement_calls"]
        count = count if type(count) is int and count >= 1 else (1 if kind in ("batch", "wait") and members else None)
        results.append(_scenario(kind, members, removed, fs, contract, count,
                                 capability=capability, declared_contract=capability["contract"]))
    return results


def _reaction_contract(cfg: dict[str, Any], cooldown: float, members: list[Call]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compatibilite avec des bornes DECLAREES, jamais une garantie apprise sur le futur observe.

    La borne de reponse couvre l'obtention d'un resultat utilisable, timeouts et
    reprises compris. Les durees observees peuvent la contredire, pas la prouver.
    """
    r = cfg.get("replacements", {})
    def bound(name: str) -> float | None:
        v = r.get(name)
        return float(v) if type(v) in (int, float) and math.isfinite(v) and v >= 0 else None
    kind = r.get("reaction_deadline_kind", "result_received")
    deadline = bound("reaction_deadline_s")
    latency = bound("response_latency_bound_s")
    jitter = bound("scheduling_jitter_bound_s")
    includes_retries = r.get("response_bound_includes_retries") is True
    durations = [(c.end_ns - c.start_ns) / 1e9 for c in members
                 if c.start_ns is not None and c.end_ns is not None and c.end_ns >= c.start_ns and not c.ambiguous]
    contradiction = kind == "result_received" and latency is not None and any(t > latency for t in durations)
    supported = kind in ("poll_start", "result_received")
    known = supported and jitter is not None and (kind == "poll_start" or (latency is not None and includes_retries))
    lower = cooldown + (jitter or 0) + ((latency or 0) if kind == "result_received" else 0)
    state = (REFUTED if supported and (contradiction or (deadline is not None and lower > deadline)) else
             ESTABLISHED if known and deadline is not None else UNKNOWN)
    contract = {"kind": kind, "deadline_s": deadline, "cooldown_s": cooldown,
                "response_latency_bound_s": latency, "scheduling_jitter_bound_s": jitter,
                "response_bound_includes_retries": includes_retries,
                "declared_detection_bound_s": lower if known else None,
                "observed_response_bound_contradiction": contradiction,
                "source": "explicit_local_configuration", "empirically_validated": False,
                "assumptions": "Etats requis persistants et acces au service dans les bornes declarees ; pas de garantie deduite d'un maximum observe."}
    description = ("Contrat resultat recu : cadence + borne de reponse (timeouts et reprises inclus) + gigue <= delai. "
                   if kind == "result_received" else
                   "Contrat lancement : cadence + gigue <= delai ; aucune garantie sur la reception du resultat. ")
    if contradiction:
        description += "Une duree observee depasse deja la borne declaree. "
    description += "Les bornes absentes restent inconnues ; la compatibilite arithmetique n'etablit pas le contrat du service."
    return fact("reaction_deadline", state, description, members if contradiction else []), contract


def _cadences(view: SessionView, cfg: dict[str, Any], graph: dict[str, Any], members: list[Call]) -> list[dict[str, Any]]:
    from agentwatch.detectors.repeated_calls import _episodes, group_key, phase_of, simulate_cooldown

    grouped: dict[str, list[Call]] = {}
    for c in members:
        grouped.setdefault(group_key(c), []).append(c)
    d = cfg.get("detectors", {}).get("repeated_calls", {})
    candidates = d.get("cooldowns_s", [10, 30, 60, 120, 300, 600])
    candidates = candidates if isinstance(candidates, list) else []
    candidates = sorted({float(x) for x in candidates if type(x) in (int, float) and math.isfinite(x) and x > 0})
    results = []
    for group in grouped.values():
        if len(group) < 2:
            continue
        reliable = all(c.start_ns is not None and c.end_ns is not None and not c.ambiguous
                       and c.agent_key not in view.timing_unreliable_agents for c in group)
        phases = [phase_of(c) for c in group]
        for cooldown in candidates:
            sim = simulate_cooldown(group, phases, _episodes(group, float(d.get("episode_gap_s", 1200))), cooldown) if reliable else None
            removed = [group[i].key for i in sim["removed_indices"]] if sim else []
            fs = _guards(view, graph, group, removed, len(group))
            reaction_fact, reaction_contract = _reaction_contract(cfg, cooldown, group if reliable else [])
            same_context = len({(c.agent_key, c.context_epoch) for c in group}) == 1
            fs += [
                fact("reliable_timing", ESTABLISHED if reliable else UNKNOWN, "Horodatages fiables requis.", group),
                fact("policy_independent_of_outcome", ESTABLISHED,
                     "Cadences du catalogue de configuration, jamais ajustees sur la fin du job."),
                reaction_fact,
                fact("context_preserved", ESTABLISHED if same_context else UNKNOWN,
                     "Une nouvelle consigne ou frontiere de contexte peut modifier le travail necessaire.", group),
                fact("state_persists_until_next_observation", UNKNOWN,
                     "La simulation suppose que les etats intermediaires ne disparaissent pas entre deux consultations."),
                fact("required_observations_preserved", UNKNOWN,
                     "Les observations necessaires, erreurs, decisions et verifications doivent survivre a la cadence."),
            ]
            results.append(_scenario("cadence", group, removed, fs,
                                     "Espacer les consultations sans perdre etats requis ni erreurs ; servir et compter la derniere demande differee.",
                                     sim["calls"] if sim else None, cooldown_s=cooldown, replay=sim,
                                     reaction_contract=reaction_contract))
    return results


def evaluate_finding(view: SessionView, finding: Finding, cfg: dict[str, Any],
                     graph: dict[str, Any] | None = None) -> dict[str, Any]:
    graph = graph if graph is not None else build_observed_graph(view)
    selected = set(finding.calls)
    members = sorted((c for c in view.calls if c.key in selected), key=lambda c: c.seq)
    expected = len(finding.calls)
    baseline = _scenario("retain", members, [], [fact("unchanged_work", ESTABLISHED, "Reference : trace conservee.")],
                         "Conserver toutes les operations observees.", len(members) if members else None)
    scenarios = [baseline]
    letter = finding.rule_id.split(".", 1)[0]
    if letter in ("A", "G"):
        scenarios.append(_reuse(view, graph, members, expected))
    if letter == "C":
        scenarios.extend(_substitution(view, cfg, graph, finding, members, "batch", expected))
    if letter in ("D", "E"):
        scenarios.extend(_substitution(view, cfg, graph, finding, members, "structured", expected))
    if letter == "G":
        scenarios.extend(_cadences(view, cfg, graph, members))
        from agentwatch.reports.wait_contracts import evaluate_waits
        scenarios.extend(evaluate_waits(view, cfg, graph, finding, members))
    if letter in ("B", "D", "E", "F"):
        scenarios.append(_scenario("guidance", members, [],
                                  [fact("procedure_preserves_required_work", UNKNOWN,
                                        "Une skill ou regle doit conserver les etapes necessaires et etre validee.")],
                                  "Formaliser une procedure ou consigne ; aucun appel en moins attribue automatiquement.", None))
    for i, s in enumerate(scenarios):
        s["scenario_id"] = f"{finding.finding_id}:{s['strategy']}:{i}"
    admissible = [s for s in scenarios[1:] if s["validity"] == "admissible"]
    deltas = [s["cost"]["calls"]["reduction_estimate"] for s in admissible
              if s["affected_calls"] == baseline["affected_calls"] and s["cost"]["calls"]["reduction_estimate"] is not None]
    conditional = [s["sensitivity"]["all_unknowns_assumed_established"]["calls_reduction_estimate"]
                   for s in scenarios[1:] if s["affected_calls"] == baseline["affected_calls"] and
                   s["sensitivity"]["all_unknowns_assumed_established"]["calls_reduction_estimate"] is not None]
    return {
        "version": "1.0", "finding_id": finding.finding_id, "scenarios": scenarios,
        "recommendation": "admissible_alternative" if admissible else "retain_until_preconditions_verified",
        "admissible_alternative_ids": [s["scenario_id"] for s in admissible],
        "calls_reduction_range": [min(deltas), max(deltas)] if deltas else None,
        "sensitivity_summary": {
            "hypothetical_only": True, "scope": baseline["affected_calls"],
            "calls_reduction_range_if_all_unknowns_established": [min(conditional), max(conditional)] if conditional else None,
            "note": "Seulement les alternatives de meme perimetre ; ce n'est ni un gain valide ni une recommandation."},
        "status_counts": dict(Counter(s["validity"] for s in scenarios[1:])),
        "scope": {"client": view.client, "session_id": view.session_id, "calls": [c.key for c in members],
                  "unresolved_finding_refs": sorted(selected - {c.key for c in members})},
        "limits": [
            "Les conclusions portent sur AgentWatch et ses preuves, pas sur le raisonnement interne du modele.",
            "Une transformation de trace n'etablit pas ce que le LLM aurait fait dans une autre session.",
            "Scenarios alternatifs et signalements chevauchants : reductions non additionnables.",
            "Les gains restent estimes sous contrat ; aucune economie reelle de tokens n'est demontree.",
        ],
    }


def attach_replacements(view: SessionView, findings: list[Finding], cfg: dict[str, Any]) -> dict[str, Any] | None:
    if not cfg.get("replacements", {}).get("enabled", True):
        for f in findings:
            f.replacement_analysis = None
        return None
    graph = build_observed_graph(view)
    for f in findings:
        f.replacement_analysis = evaluate_finding(view, f, cfg, graph)
    return graph


def summary_lines(analysis: dict[str, Any] | None) -> list[str]:
    """Resume commun Markdown, terminal enrichi, HTML/SVG."""
    if not analysis:
        return []
    out = ["Remplacements sous conditions (estimations, aucune economie mesuree) :",
           f"Perimetre : {len(analysis['scope']['calls'])} appels resolus, acquisition initiale comprise si presente.", ""]
    checks: dict[str, dict[str, Any]] = {}
    for s in analysis["scenarios"]:
        if s["strategy"] == "retain":
            continue
        suffix = f" ({s['cooldown_s']:g} s)" if "cooldown_s" in s else ""
        blocked = []
        for state, label in ((REFUTED, "refutees"), (UNKNOWN, "inconnues")):
            names = [_FACT_LABELS.get(f["name"], f["name"]) for f in s["preconditions"] if f["state"] == state]
            if names:
                blocked.append(label + " : " + ", ".join(names))
        out.append(f"- {s['label']}{suffix} : {STATUS_LABELS[s['validity']]}"
                   + (f" ; {' ; '.join(blocked)}" if blocked else ""))
        if s.get("capability"):
            cap = s["capability"]
            availability = {"observed_available": "disponibilite observee", "declared_unverified": "declaree, non verifiee",
                            "to_create": "a creer", "unknown": "inconnue"}[cap["status"]]
            out.append(f"  Capacite : {availability} ; equivalence inconnue.")
        if s.get("wait_contract"):
            contract = s["wait_contract"]
            out.append(f"  Attente proposee : {contract['timeout_seconds']} s ; sondage interne conserve : {contract['poll_seconds_unchanged']} s ; implementation liee aux appels : {contract['implementation_binding']}.")
            assessment = contract.get("assessment", {})
            compliance = assessment.get("results_errors_identity", {})
            out.append(f"  Conformite du contrat de sortie et d'identification : audit {compliance.get('audit_state', UNKNOWN)}, application aux appels {compliance.get('applied_state', UNKNOWN)}.")
            identity = contract.get("identifier_requirement", {})
            mode = {"self_contained_output": "identifiant dans chaque sortie autonome",
                    "explicit_call_association": "association explicite appel-arguments-reponse", "unspecified": "besoin non precise"}.get(identity.get("mode"), "besoin non precise")
            out.append(f"  Identification : {mode} ; suffisance pour le consommateur a justifier dans l'audit.")
            out.append("  Equivalence avec l'attente actuelle : non etablie ; degradation introduite par l'allongement : non etablie. Un contrat non satisfait ne demontre pas cette causalite.")
        out.append(f"  Contrat : {s['contract']}")
        for request in s.get("verification_requests", []):
            checks.setdefault(request.get("verification_precondition", request["precondition"]), request)
        hypothetical = s["sensitivity"]["all_unknowns_assumed_established"]
        reduction = hypothetical["calls_reduction_estimate"]
        if hypothetical["assumptions"] and reduction is not None and hypothetical["validity"] == "admissible":
            n = s["cost"]["calls"]["observed"]
            out.append(f"  Sensibilite : {reduction}/{n} appels en moins seulement si TOUTES les conditions inconnues sont "
                       "supposees etablies ; hypothese non validee.")
    if checks:
        out.append("Verifications pour trancher (communes aux scenarios concernes) :")
        for name, request in checks.items():
            out.append(f"- {_FACT_LABELS.get(name, name)} : {request['verification']} Decision : {request['decision_unlocked']}")
        out.append("Ces verifications ne valident pas les autres conditions ; aucune hypothese ne devient une preuve.")
    out.append("")
    out.append("Sensibilite : les hypotheses detaillees et leurs variations figurent dans le JSON ; "
               "interactions non exhaustives, aucune borne de gain demontree ; les scenarios et signalements ne s'additionnent pas.")
    return out
