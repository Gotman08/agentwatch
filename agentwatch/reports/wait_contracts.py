"""Contrats locaux d'allongement d'une attente deja utilisee.

Le catalogue contient des conclusions d'audit explicites, pas des garanties
apprises sur la trace. Aucune lecture de source, decouverte ou execution ici.
Une conclusion sur le code n'est applicable aux appels que si leur empreinte
d'implementation observee correspond. Une preference reste une contrainte.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from agentwatch.core.correlate import Call, SessionView

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_KINDS = {"local_source_review", "local_configuration_review"}
CLAIMS = {
    "terminal_return": "Retour apres observation d'un etat terminal, sans attendre le budget maximal.",
    "results_errors_identity": "Conformite au contrat declare sur les etats, erreurs, expiration, annulation et identifiant ; ce fait ne compare pas deux politiques.",
    "effects_preserved": "Les effets requis, y compris les mises a jour locales, sont conserves.",
    "timeout_layers_compatible": "Client, serveur, transport et reprises acceptent la duree proposee.",
    "intermediate_work_preserved": "Les retours intermediaires retires ne portent aucune decision ou interruption necessaire.",
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def job_reference(c: Call) -> dict[str, Any] | None:
    hidden = c.params.get("_fp")
    if type(c.params.get("job_id")) in (str, int) and str(c.params["job_id"]):
        return {"representation": "value", "value": c.params["job_id"]}
    if isinstance(hidden, dict) and isinstance(hidden.get("job_id"), str) and hidden["job_id"]:
        return {"representation": "fingerprint", "value": hidden["job_id"]}
    return None


def scope_for(calls: list[Call]) -> dict[str, Any]:
    """Portee exacte, bornee aux evenements figes : jamais un contrat global de serveur."""
    first = calls[0]
    # Inclure les preuves de depart : une autre trace portant les memes IDs ne reutilise pas l'audit.
    identity = [{"key": c.key, "event_ids": c.event_ids, "params": c.params,
                 "start_ns": c.start_ns, "end_ns": c.end_ns} for c in calls]
    return {"client": first.client, "session_id": first.session_id, "actor": first.agent_key,
            "context_epoch": first.context_epoch, "tool_name": first.tool_name,
            "job": job_reference(first),
            "calls_sha256": hashlib.sha256(_json(identity).encode()).hexdigest()}


def wait_groups(calls: list[Call]) -> list[list[Call]]:
    from agentwatch.detectors.repeated_calls import group_key
    groups: dict[str, list[Call]] = {}
    for c in calls:
        # G peut reunir des epoques ; un contrat de remplacement ne les traverse pas.
        key = _json([c.client, c.session_id, c.project_dir, c.agent_key, c.context_epoch,
                     group_key(c), job_reference(c)])
        groups.setdefault(key, []).append(c)
    return list(groups.values())


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _proof(raw: Any, kinds: set[str]) -> dict[str, Any] | None:
    if (not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or raw["kind"] not in kinds
            or not isinstance(raw.get("basis"), str) or not raw["basis"].strip()):
        return None
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        return None
    for p in sources:
        if not isinstance(p, dict) or not isinstance(p.get("sha256"), str) or not _HEX.fullmatch(p["sha256"]):
            return None
        if not isinstance(p.get("locator"), str) or not p["locator"].strip():
            return None
        if type(p.get("line_start")) is not int or type(p.get("line_end")) is not int or not 1 <= p["line_start"] <= p["line_end"]:
            return None
    return raw


def contract_facts(view: SessionView, members: list[Call], contract: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Proprietes applicables, plus conclusions d'audit conservees separement.

    L'audit est un apport de confiance explicite de l'utilisateur/auditeur ; les
    empreintes documentent ses sources, elles n'en verifient pas la semantique.
    Une contradiction dans la trace prime toujours sur le catalogue.
    """
    from agentwatch.reports.replacements import ESTABLISHED, REFUTED, UNKNOWN, fact

    implementation = contract.get("implementation_sha256")
    observed = [c.evidence.get("tool_implementation_sha256") for c in members]
    valid_hash = isinstance(implementation, str) and bool(_HEX.fullmatch(implementation))
    deployment = _proof(contract.get("deployment_binding"), {"deployment_record_review"})
    deployment_matches = bool(deployment and deployment.get("implementation_sha256") == implementation
                              and deployment.get("calls_sha256") == scope_for(members)["calls_sha256"])
    binding = (REFUTED if valid_hash and any(h and h != implementation for h in observed) else
               ESTABLISHED if valid_hash and all(h == implementation for h in observed) else
               deployment["state"] if valid_hash and deployment_matches and deployment.get("state") in (ESTABLISHED, REFUTED) else UNKNOWN)
    fs = [fact("wait_implementation_bound", binding,
               "Empreinte de l'implementation dans chaque appel, ou audit de deploiement couvrant exactement ces appels ; la version installee aujourd'hui ne suffit pas.", members)]
    if deployment_matches:
        fs[0]["provenance"] = deployment
    records = contract.get("facts", {})
    records = records if isinstance(records, dict) else {}
    audited = {}
    for name, basis in CLAIMS.items():
        kinds = {"task_requirement_review"} if name == "intermediate_work_preserved" else _SOURCE_KINDS
        proof = _proof(records.get(name), kinds)
        state = proof.get("state", UNKNOWN) if proof else UNKNOWN
        if state not in (ESTABLISHED, REFUTED):
            state = UNKNOWN
        # Une conclusion sur le code actuel ne tranche pas le comportement du code historique.
        applied = state if name == "intermediate_work_preserved" or binding == ESTABLISHED else UNKNOWN
        f = fact("wait_" + name, applied, basis)
        if proof:
            f["provenance"] = proof
        if name != "intermediate_work_preserved" and binding != ESTABLISHED and state != UNKNOWN:
            f["blocked_by"] = ["wait_implementation_bound"]
            f["conditional_state"] = state
            f["basis"] += " Conclusion d'audit connue pour le code cite ; son application attend la preuve de version."
        fs.append(f)
        audited[name] = {"audit_state": state, "applied_state": applied, "proof": proof}

    proposed = _number(contract.get("timeout_seconds"))
    original = [_number(c.params.get("timeout_seconds")) for c in members]
    poll = [_number(c.params.get("poll_seconds")) for c in members]
    reviewed = contract.get("reviewed_policy")
    expected_policy = {"timeout_seconds": proposed, "poll_seconds": poll[0] if poll else None}
    fs.append(fact("wait_policy_bound", UNKNOWN if not isinstance(reviewed, dict) else
                   ESTABLISHED if reviewed == expected_policy else REFUTED,
                   "Les conclusions d'audit portent exactement sur la duree et le sondage proposes."))
    fs.append(fact("wait_only_duration_changes", ESTABLISHED if proposed and all(x is not None and proposed > x for x in original)
                   and all(x is not None and x > 0 and x == poll[0] for x in poll) else UNKNOWN,
                   "Seul timeout_seconds augmente ; meme outil, job et parametres, poll_seconds est conserve.", members))
    limit_proof = _proof(records.get("timeout_parameter_limit"), _SOURCE_KINDS)
    limit = _number(limit_proof.get("value_seconds")) if limit_proof and limit_proof.get("state") == ESTABLISHED else None
    limit_state = UNKNOWN if binding != ESTABLISHED or proposed is None or limit is None else ESTABLISHED if proposed <= limit else REFUTED
    fs.append(fact("wait_timeout_parameter_limit", limit_state,
                   "Plafond du parametre accepte par le code ; ce n'est pas une borne de duree murale."))

    requirement = contract.get("control_return_requirement", {})
    requirement = requirement if isinstance(requirement, dict) else {}
    budget = _number(requirement.get("max_seconds")) if requirement.get("kind") == "declared_preference" and requirement.get("basis") else None
    bound_proof = _proof(records.get("end_to_end_return_bound"), _SOURCE_KINDS)
    bound = _number(bound_proof.get("value_seconds")) if bound_proof and bound_proof.get("state") == ESTABLISHED and bound_proof.get("includes_retries_and_locking") is True else None
    # Une duree observee peut refuter une borne, jamais la prouver.
    reliable = all(c.agent_key not in view.timing_unreliable_agents and c.start_ns is not None and c.end_ns is not None and c.end_ns >= c.start_ns for c in members)
    contradiction = reliable and bound is not None and any((c.end_ns - c.start_ns) / 1e9 > bound for c in members)
    state = (REFUTED if binding == ESTABLISHED and (contradiction or (bound is not None and budget is not None and bound > budget)) else
             ESTABLISHED if binding == ESTABLISHED and bound is not None and budget is not None else UNKNOWN)
    fs.append(fact("wait_control_return_budget", state,
                   "Contrainte choisie de reprise du controle comparee a une borne de bout en bout, verrou et reprises compris."))
    identity = contract.get("identifier_requirement")
    identity = identity if isinstance(identity, dict) else {}
    mode = identity.get("mode")
    if mode not in ("self_contained_output", "explicit_call_association"):
        mode = "unspecified"
    # Declarer un autre lieu d'identification ne remplace jamais une preuve et ne
    # change pas le verdict d'audit existant. La suffisance pour le consommateur
    # doit etre justifiee dans le contrat examine, pas deduite d'un id dans la trace.
    identification = {"mode": mode, "status": "declared" if mode != "unspecified" else "unspecified",
                      **{k: identity[k][:512] for k in ("consumer", "basis") if isinstance(identity.get(k), str)},
                      "consumer_sufficiency": "not_automatically_established"}
    comparison = {"decision_basis": "stated_contract_compliance",
                  "results_errors_identity": {k: audited["results_errors_identity"][k] for k in ("audit_state", "applied_state")},
                  "baseline_service_equivalence": UNKNOWN, "replacement_introduces_defect": UNKNOWN,
                  "basis": "No paired comparison of the baseline and replacement is supplied by these contract facts. A contract violation is not evidence that the replacement introduces it."}
    details = {"schema_version": "1.0", "scope": scope_for(members),
               "implementation_sha256": implementation, "implementation_binding": binding,
               "identifier_requirement": identification, "assessment": comparison,
               "audited_facts": audited, "timeout_parameter_limit": limit_proof,
               "end_to_end_return_bound": bound_proof, "control_return_requirement": requirement,
               "timeout_seconds": proposed, "poll_seconds_unchanged": poll[0] if poll else None,
               "observed_bound_contradiction": contradiction,
               "trust": "explicit_curated_audit_not_automatic_source_verification",
               "simulation": None,
               "note": "Allonger un appel n'est pas espacer les sondages internes. Sans etats internes et contrat temporel suffisant, aucun nombre d'appels de remplacement n'est estime."}
    return fs, details


def evaluate_waits(view, cfg, graph, finding, members):
    from agentwatch.reports.replacements import ESTABLISHED, UNKNOWN, _guards, _scenario, _substitution, fact

    entries = cfg.get("replacements", {}).get("capabilities", [])
    entries = entries if isinstance(entries, list) else []
    results = []
    unresolved = len(set(finding.calls) - {c.key for c in members})
    for group in wait_groups(members):
        scope = scope_for(group)
        matches = [e for e in entries if isinstance(e, dict) and e.get("strategy") == "wait"
                   and e.get("rule_id") == finding.rule_id and e.get("tool_name") == group[0].tool_name
                   and isinstance(e.get("wait_contract"), dict) and e["wait_contract"].get("schema_version") == "1.0"
                   and e["wait_contract"].get("scope") == scope]
        if not matches:
            # Compatibilite avec le catalogue initial. Meme sans contrat, ne jamais fusionner les jobs.
            results.extend(_substitution(view, cfg, graph, finding, group, "wait", len(group) + unresolved))
            continue
        for entry in matches:
            # La premiere acquisition reste acquise avant de proposer d'allonger les suivantes.
            removed = [c.key for c in group[1:]]
            fs = _guards(view, graph, group, removed, len(group) + unresolved)
            fs += [fact("wait_same_job_actor_context", ESTABLISHED if job_reference(group[0]) else UNKNOWN,
                        "Meme outil, job, acteur, session et epoque ; aucune fusion entre perimetres.", group),
                   fact("wait_available_before_continuation", ESTABLISHED if group[0].status == "success" else UNKNOWN,
                        "Le premier appel conserve atteste l'acces a cet outil avant la continuation.", group),
                   fact("wait_sequential_observations", ESTABLISHED if len(group) > 1 and all(
                       a.end_ns is not None and b.start_ns is not None and a.end_ns <= b.start_ns
                       and a.agent_key not in view.timing_unreliable_agents for a,b in zip(group, group[1:])) else UNKNOWN,
                        "Le resultat precedent est acquis avant de prolonger la continuation ; aucun chevauchement presume equivalent.", group)]
            extra, details = contract_facts(view, group, entry["wait_contract"])
            fs.extend(extra)
            results.append(_scenario("wait", group, removed, fs,
                "Conserver la premiere attente, puis augmenter sa duree maximale pour le meme job, avec le meme sondage interne ; conserver resultats, erreurs et possibilites de controle requises.",
                None, wait_contract=details,
                capability={"id": str(entry.get("id") or group[0].tool_name), "tool_name": group[0].tool_name,
                            "status": "observed_available", "equivalence": UNKNOWN}))
    return results
