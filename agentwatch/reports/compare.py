"""Avant / apres : une correction (consigne, outil, reglage) a-t-elle reellement diminue les pertes ?

# * La promesse d'AgentWatch : reperer des pertes evitables avec des preuves, puis verifier que les corrections
#   ameliorent reellement le travail. Ici la verification : les memes mesures sur deux periodes, rapportees a
#   l'activite (pour 1 000 reponses du modele ou 1 000 appels), avec un intervalle de confiance a 95 % sur le
#   rapport des taux (loi de Poisson, methode du logarithme). Une difference n'est dite demontree que si
#   l'intervalle exclut 1 ; avec trop peu d'evenements, rien n'est conclu.
# ! Les pertes arrivent par grappes (une longue attente produit plusieurs reprises, un signalement plusieurs appels) :
#   les compter comme independantes donnerait un intervalle trop etroit. L'incertitude est donc calculee sur le
#   nombre de GRAPPES (groupes d'appels repetes, signalements) : correction prudente, qui suppose les evenements
#   d'une grappe entierement lies.
# ! Deux periodes different aussi par le travail fait : une baisse peut venir d'une tache differente. La
#   comparaison le rappelle toujours ; elle ne vaut que pour des periodes de travail comparable.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from agentwatch.core import schema as S
from agentwatch.core.correlate import SessionView

COMPARE_VERSION = "1.0"
_FAILED = (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED)
MIN_EVENTS = 10          # * sous ce nombre d'evenements (avant + apres), aucune conclusion
MIN_ACTIVITY = 200       # * sous ce nombre de reponses ou d'appels dans une periode, aucune conclusion


def measure(view: SessionView, cfg: dict[str, Any]) -> dict[str, Any]:
    """Mesures d'une vue (session ou tranche) : activite et pertes, en comptes (numerateurs)."""
    from agentwatch.detectors import repeated_calls as G
    from agentwatch.detectors import run_detectors
    counts: Counter[str] = Counter()
    clusters: Counter[str] = Counter()
    ctx: Counter[str] = Counter()
    res = G.analyse(view, cfg)
    by_key = {c.key: c for c in view.calls}
    # * Reprise sans apport : le modele est repasse (aller-retour) pour un resultat identique au precedent. Le cible de
    #   l'ajout a AGENTS.md : "pas de reprise du modele uniquement pour constater un etat inchange".
    seen_emit: dict[str, set[str]] = {"G.sans_apport": set(), "G.sans_apport_attente": set()}
    resp_by_id = {m.meta["usage"].get("response_id"): m.meta["usage"] for m in view.markers
                  if m.phase == S.PHASE_USAGE and isinstance(m.meta.get("usage"), dict) and m.meta["usage"].get("scope") == "response"}
    nogain_cost: dict[str, Counter[str]] = {"G.sans_apport": Counter(), "G.sans_apport_attente": Counter()}
    for g in res["groups"]:
        hit: set[str] = set()
        for i, r in enumerate(g["repeats"]):
            if not r.get("round_trip") or r.get("value") not in ("no_gain", "explained_no_gain"):
                continue
            keys = ["G.sans_apport"] + (["G.sans_apport_attente"] if r.get("reason") in ("waiting", "unavailable") else [])
            hit.update(keys)
            call = by_key.get(g["call_keys"][i + 1])
            u = (call.usage or {}) if call else {}
            for k in keys:
                counts[k] += 1
                rid, tok = u.get("emitter_request_id"), u.get("emitter_input_tokens")
                if isinstance(rid, str) and rid not in seen_emit[k]:
                    seen_emit[k].add(rid)
                    if isinstance(tok, int):
                        ctx[k] += tok
                    ru = resp_by_id.get(rid)
                    if ru:
                        nogain_cost[k]["responses"] += 1
                        for f in ("input_tokens", "cached_input_tokens", "output_tokens"):
                            nogain_cost[k][f] += int(ru.get(f) or 0)
        clusters.update(hit)
    for g in res["groups"]:
        if g["verdict"] in ("agent", "outil") and g["round_trips"]:
            counts["G.ameliorable"] += g["round_trips"]
            clusters["G.ameliorable"] += 1
            ctx["G.ameliorable"] += g["context_reread_tokens"] or 0
        if g["kind"] and g["round_trips"]:
            counts[f"G.{g['kind']}|{g['tool']}"] += g["round_trips"]
            clusters[f"G.{g['kind']}|{g['tool']}"] += 1
            ctx[f"G.{g['kind']}|{g['tool']}"] += g["context_reread_tokens"] or 0
    for f in run_detectors(view, cfg, only=["redundant_reads", "error_loops", "tool_gap"]):
        letter = f.rule_id.split(".", 1)[0]
        key = {"A": "A.relectures", "B": "B.echecs_en_boucle", "E": "E.service_a_la_main"}.get(letter)
        if letter == "A":
            counts[key] += int(f.evidence.get("items") or 0) if f.kind == "repeated_read_batch" else len(f.calls) - 1
        elif key:
            counts[key] += len(f.calls)
        if key:
            clusters[key] += 1
    from agentwatch.reports.stats import compaction_request_ids
    comp_ids = compaction_request_ids(view)
    requests = [m.meta["usage"] for m in view.markers if m.phase == S.PHASE_USAGE and isinstance(m.meta.get("usage"), dict)
                and m.meta["usage"].get("scope") == "response"]
    # * Une demande de compaction consomme des tokens reels mais ne repond pas a la conversation : comptee a part.
    responses = [u for u in requests if u.get("response_id") not in comp_ids]
    compactions = [u for u in requests if u.get("response_id") in comp_ids]
    counts["erreurs"] = sum(1 for c in view.calls if c.status in _FAILED)
    # * Occasions d'attendre : sans attente, une habitude d'attente disparait faute d'occasion, pas grace a une correction.
    counts["attentes"] = sum(1 for c in view.calls if c.agent_key not in view.timing_unreliable_agents
                             and G._WAIT_NAME.search(c.mcp_tool or c.tool_name or ""))
    clusters["erreurs"] = counts["erreurs"]          # * un appel en erreur n'est pas groupe : grappe = appel
    # * Taches : un tour du fil principal (demande de l'utilisateur) ou d'un sous-agent (tache confiee) ; termine
    #   (task_complete, avec sa duree), interrompu (turn_aborted) ou coupe par le client (task_complete portant une
    #   erreur : quota epuise, serveur sature). Un tour coupe est une INTERRUPTION, par le client et non par
    #   l'utilisateur : compte a part et nomme, il reste dans le denominateur du taux de taches terminees. Ni tache
    #   terminee, ni ligne qui disparait du bilan.
    #   Constate le 2026-09-21 : les 4 fils de la session 01a0bf95 finissent sur `usage_limit_exceeded` ; le seul
    #   « tour termine » du fil principal etait cette coupure.
    tasks: dict[str, Any] = {"main_done": 0, "main_aborted": 0, "main_cut": 0, "sub_done": 0, "sub_aborted": 0, "sub_cut": 0,
                             "main_durations_ms": [], "sub_durations_ms": []}
    for m in view.markers:
        who = "sub" if m.agent_id else "main"
        if m.phase == S.PHASE_TURN_END and m.meta.get("error_kind"):
            tasks[f"{who}_cut"] += 1
        elif m.phase == S.PHASE_TURN_END:
            tasks[f"{who}_done"] += 1
            d = m.meta.get("duration_ms")
            if isinstance(d, (int, float)) and not isinstance(d, bool):
                tasks[f"{who}_durations_ms"].append(int(d))
        elif m.phase == S.PHASE_INTERRUPT:
            tasks[f"{who}_aborted"] += 1
    context = _context_measure(view, cfg)
    if context and context["exact"]["truncation_known_sessions"]:
        counts["C.execs_coupes"] = clusters["C.execs_coupes"] = context["exact"]["execs_cut_in_the_middle"]
    if context:
        counts["C.relectures_apres_compaction"] = context["attributed"]["rereads_after_compaction"]
        clusters["C.relectures_apres_compaction"] = context["attributed"]["windows_with_rereads"]
    return {"context": context, "calls": len(view.calls), "responses": len(responses),
            "input_tokens": sum(int(u.get("input_tokens") or 0) for u in responses),
            "cached_input_tokens": sum(int(u.get("cached_input_tokens") or 0) for u in responses),
            "output_tokens": sum(int(u.get("output_tokens") or 0) for u in responses),
            "reasoning_output_tokens": sum(int(u.get("reasoning_output_tokens") or 0) for u in responses),
            "compaction": {"requests": len(compactions), **{k: sum(int(u.get(k) or 0) for u in compactions)
                                                            for k in ("input_tokens", "cached_input_tokens", "output_tokens")}},
            "counts": dict(counts), "clusters": dict(clusters), "ctx": dict(ctx), "sessions": 1 if view.calls else 0, "tasks": tasks,
            "nogain_cost": {k: dict(v) for k, v in nogain_cost.items()},
            "settings": settings_of(view),
            "session_ids": [view.session_id] if view.calls else [],
            "first_time": view.first_time, "last_time": view.last_time}


def settings_of(view: SessionView) -> dict[str, Any]:
    """Reglages de fil ecrits par le client (`thread_settings_applied`, marqueurs `settings`) : modele, effort, niveau de
    service, avec le nombre de releves et leurs instants, et le nombre de CHANGEMENTS de niveau de service (par fil, dans
    l'ordre). Constate le 2026-09-22 : `service_tier` passe de `default` a `priority` a 20:29Z, et les points de quota par
    requete ont triple ; sans ce releve, une comparaison de quota ne saurait pas qu'elle compare deux niveaux."""
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    per_agent: dict[str, list[Any]] = {}
    for m in sorted((m for m in view.markers if m.phase == S.PHASE_ACTIVITY and m.meta.get("kind") == "settings"), key=lambda m: m.ns):
        key = (m.meta.get("model"), m.meta.get("reasoning_effort"), m.meta.get("service_tier"))
        r = rows.setdefault(key, {"model": key[0], "effort": key[1], "service_tier": key[2], "count": 0, "first_time": m.time, "last_time": m.time})
        r["count"] += 1
        r["last_time"] = m.time
        per_agent.setdefault(m.agent_id or "main", []).append(m.meta.get("service_tier"))
    changes = sum(sum(1 for a, b in zip(L, L[1:]) if a != b) for L in per_agent.values())
    return {"known": bool(rows), "configs": list(rows.values()), "tier_changes": changes}


def _merge_settings(parts: list[dict[str, Any] | None]) -> dict[str, Any]:
    out: dict[str, Any] = {"known": False, "configs": [], "tier_changes": 0}
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for p in parts:
        if not p:
            continue
        out["known"] = out["known"] or bool(p.get("known"))
        out["tier_changes"] += int(p.get("tier_changes") or 0)
        for c in p.get("configs") or []:
            key = (c.get("model"), c.get("effort"), c.get("service_tier"))
            r = by_key.setdefault(key, {**c, "count": 0})
            r["count"] += int(c.get("count") or 0)
            r["first_time"] = min(x for x in (r.get("first_time"), c.get("first_time")) if x) if (r.get("first_time") or c.get("first_time")) else None
            r["last_time"] = max(x for x in (r.get("last_time"), c.get("last_time")) if x) if (r.get("last_time") or c.get("last_time")) else None
    out["configs"] = sorted(by_key.values(), key=lambda c: c.get("first_time") or "")
    return out


def settings_text(s: dict[str, Any] | None) -> str:
    """« modele / effort / niveau (n releves, du .. au ..) ; ... » ; non releve pour une mesure anterieure a ce releve."""
    if not s:
        return f"{MISSING} (mesure anterieure a ce releve)"
    if not s.get("known"):
        return f"{MISSING} (aucun reglage de fil ecrit par le client)"
    return " ; ".join(f"{c.get('model') or '?'} / {c.get('effort') or '?'} / {c.get('service_tier') or '?'} ({c.get('count')} releve(s), "
                      f"{(c.get('first_time') or '?')[:16]} -> {(c.get('last_time') or '?')[:16]})" for c in s.get("configs") or [])


def settings_compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Les deux periodes ont-elles le meme niveau de service ? Sinon les ratios de quota ne se comparent pas."""
    def tiers(m: dict[str, Any]) -> set[Any] | None:
        s = m.get("settings")
        if not s or not s.get("known"):
            return None
        return {c.get("service_tier") for c in s.get("configs") or []}
    tb, ta = tiers(before), tiers(after)
    if tb is None or ta is None:
        status = "non releve"
    elif tb == ta and len(tb) == 1:
        status = "identique"
    else:
        status = "differents"
    return {"before": before.get("settings"), "after": after.get("settings"), "tiers_before": sorted(x or "?" for x in (tb or [])),
            "tiers_after": sorted(x or "?" for x in (ta or [])), "service_tier_status": status,
            "quota_limit": "les points de quota par requete ne se comparent qu'a niveau de service identique. La documentation du "
                           "fournisseur indique un surcout du mode prioritaire ; le ratio observe par requete n'en est pas le multiplicateur."}


def settings_lines(sc: dict[str, Any] | None) -> list[str]:
    if not sc:
        return []
    mixed = " ; niveaux MELANGES dans une periode" if (len(sc["tiers_before"]) > 1 or len(sc["tiers_after"]) > 1) else ""
    changes = (int(((sc.get("before") or {}).get("tier_changes") or 0)), int(((sc.get("after") or {}).get("tier_changes") or 0)))
    return [f"- Reglages de fil (modele / effort / niveau de service), avant : {settings_text(sc.get('before'))} ; apres : "
            f"{settings_text(sc.get('after'))} ; changements de niveau de service : {changes[0]} avant, {changes[1]} apres.",
            f"- Niveau de service : {sc['service_tier_status']}"
            + (f" ({', '.join(sc['tiers_before']) or '?'} -> {', '.join(sc['tiers_after']) or '?'})" if sc["service_tier_status"] == "differents" else "")
            + mixed + f". Limite : {sc['quota_limit']}"]


def _context_measure(view: SessionView, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Indicateurs de contexte d'une vue, repris de `reports.context` (aucun calcul refait ici), en quantites qui
    s'additionnent d'une session a l'autre et rangees par NATURE, jamais melangees :

    - `exact` : releves du client et comptes de faits (entrees par reponse, differences d'entree entre deux reponses,
      lignes `compacted`, avertissements de coupe, quota, fins de tour en erreur). Le total des tokens ajoutes par les
      sorties et celui des tokens relus sont exacts : ils ne dependent pas du partage entre appels ;
    - `attributed` : attributions reconstruites par AgentWatch (partage d'une reponse entre ses appels, ressource
      reconnue d'une fenetre a l'autre, etat d'une relecture) ;
    - les scenarios d'economie ne sont pas mesures : ils sont derives a l'affichage, comme hypotheses.
    """
    from agentwatch.reports.context import context_costs
    c = context_costs(view, cfg)
    if not c:
        return None
    ac, tr, floor = c["after_compaction"], c["truncation"], c["floor"]
    lim = c["limits"]
    q = lim.get("quota") or {}
    points = (q["last_percent"] - q["first_percent"]) if q else None
    by_status = ac.get("by_status") or {}
    avoidable = [by_status.get(k) or {} for k in ("identical", "no_change_observed")]
    ex_exact, ex_attr = _exchange_measure(view, cfg)
    return {
        "sessions": 1,
        "exact": {"session_input_tokens": c["session_input_tokens"], "requests": c["responses"],
                  "threads": len(floor["thread_first_input"]), "thread_first_input_sum": sum(floor["thread_first_input"].values()),
                  "threads_before_view": floor["threads_started_before_view"],
                  "thread_first_reread_tokens": floor["thread_first_reread_tokens"],
                  "thread_first_basis_input_tokens": floor["thread_first_basis_input_tokens"],
                  "windows_after_compaction": c["windows_after_compaction"], "window_first_inputs": list(floor["window_first_inputs"]),
                  "added_tokens": c["added_tokens"], "reread_tokens": c["reread_tokens"],
                  "mixed_added_tokens": c["mixed"]["added_tokens"],
                  "window_start_added_tokens": list(ac["window_start"].get("added_tokens_by_window") or []),
                  "truncation_known_sessions": 1 if tr["known"] else 0, "truncated_calls": tr["truncated_calls"],
                  "execs_cut_in_the_middle": tr["execs_cut_in_the_middle"], "tokens_cut_from_execs": tr["tokens_cut_from_execs"],
                  "quota_known_sessions": 1 if q else 0, "quota_points": points if points is not None and points >= 0 else 0,
                  "turns_cut": lim["turns_cut"]["total"], "turns_cut_by_kind": dict(lim["turns_cut"]["by_kind"]), **ex_exact},
        "attributed": {"rereads_after_compaction": ac["rereads"], "windows_with_rereads": ac.get("windows_with_rereads", 0),
                       "rereads_added_tokens": ac["added_tokens"], "rereads_reread_tokens": ac["reread_tokens"],
                       "rereads_unchanged": sum(int(v.get("rereads") or 0) for v in avoidable),
                       "rereads_unchanged_added_tokens": sum(int(v.get("added_tokens") or 0) for v in avoidable),
                       "rereads_unchanged_reread_tokens": sum(int(v.get("reread_tokens") or 0) for v in avoidable),
                       "truncated_calls_added_tokens": tr["added_tokens_of_truncated_calls"], **ex_attr,
                       "families": {f["family"]: {"calls": f["calls"], "added_tokens": f["added_tokens"],
                                                  "reread_tokens": f["reread_tokens"]} for f in c["families"]}}}


def _exchange_measure(view: SessionView, cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Indicateurs entre agents, repris de `reports.exchanges` (aucun calcul refait ici), par nature.

    - releves : messages envoyes, entres, rapproches ; requetes qui n'emettent que des messages et leur entree ; ce
      qu'emet la premiere reponse apres un message recu ; messages enchaines sans appel d'outil hors messagerie ;
    - calculs : estimation du contexte conserve, tokens des lectures apres l'ecriture d'un autre agent.
    # ! L'entree des requetes d'envoi et l'estimation du contexte conserve se recouvrent : deux lignes, jamais une
    #   somme, jamais un scenario d'economie.
    """
    from agentwatch.reports.exchanges import agent_exchanges
    ex = agent_exchanges(view, cfg)
    if not ex or not ex.get("messages"):
        # * 0 explicite : « aucun echange entre agents dans cette vue » (indicateurs SANS OBJET), a distinguer d'une
        #   mesure enregistree avant ces indicateurs, ou la cle est absente (donnee MANQUANTE).
        return {"exchange_sessions": 0}, {}
    m, rq = ex["messages"], ex.get("message_requests") or {}
    ch = rq.get("chains") or {}
    first: dict[str, int] = {}
    for row in (rq.get("reactions") or {}).values():
        for k, n in row.items():
            first[k] = first.get(k, 0) + n
    known = 1 if m["pairing"]["available"] else 0
    res = ex.get("resources") or {}
    est = ex.get("retained_context_estimate") or {}
    fact = est.get("kept_messages_fact") or {}
    exact = {"exchange_sessions": 1, "pairing_known_sessions": known, "messages_sent": m["sent"], "messages_received": m["received"],
             "messages_paired": m["pairing"]["paired"], "messages_undelivered": m["pairing"]["undelivered_total"] if known else 0,
             "messages_slow": m["delivery_delay"]["slow"], "message_only_requests": rq.get("message_only", 0),
             "message_only_input_tokens": rq.get("message_only_input_tokens", 0),
             "chain_messages": ch.get("messages", 0), "chains": ch.get("chains", 0),
             "chain_intermediate_input_tokens": ch.get("intermediate_input_tokens", 0),
             "first_response_after_message": first, "files_written_by_several_agents": res.get("shared_write_total", 0),
             # ! RELEVE des messages gardes par la compaction : des comptes, tenus a part de l'estimation en tokens. Une
             #   session importee sans ce fait ne compte pas dans `kept_fact_known_sessions` : absent n'est pas zero.
             "kept_fact_known_sessions": 1 if fact.get("agents_with_fact") else 0,
             "kept_fact_agents": fact.get("agents_with_fact", 0), "kept_fact_agents_missing": fact.get("agents_without_fact", 0),
             # * sommes de sessions : 0 ici ne se lit qu'avec `kept_fact_known_sessions` (la ligne affiche « - » sinon)
             "kept_agent_messages_at_last_compaction": fact.get("kept_at_last_compaction") or 0,
             "received_before_last_compaction": fact.get("received_before_last_compaction") or 0}
    attributed = {"retained_context_estimate_tokens": est.get("tokens", 0), "retained_context_estimate_requests": est.get("requests", 0),
                  "retained_context_agents": len(est.get("agents") or []),
                  "retained_context_agents_by_basis": dict(est.get("agents_by_basis") or {}),
                  "handoff_added_tokens": (res.get("handoff") or {}).get("added_tokens", 0),
                  "shared_reference_added_tokens_by_others": (res.get("shared_reference") or {}).get("added_tokens_by_others", 0)}
    return exact, attributed


def _merge_context(parts: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    parts = [p for p in parts if p]
    if not parts:
        return None
    out: dict[str, Any] = {"sessions": 0, "exact": {}, "attributed": {"families": {}}}
    for p in parts:
        out["sessions"] += int(p.get("sessions") or 0)
        for nature in ("exact", "attributed"):
            for k, v in (p.get(nature) or {}).items():
                cur = out[nature].get(k)
                if k == "families":
                    for fam, row in v.items():
                        tgt = out["attributed"]["families"].setdefault(fam, {"calls": 0, "added_tokens": 0, "reread_tokens": 0})
                        for f in tgt:
                            tgt[f] += int(row.get(f) or 0)
                elif isinstance(v, list):
                    out[nature][k] = (cur or []) + v
                elif isinstance(v, dict):
                    merged = dict(cur or {})
                    for kk, n in v.items():
                        merged[kk] = merged.get(kk, 0) + n
                    out[nature][k] = merged
                else:
                    out[nature][k] = (cur or 0) + (v or 0)
    return out


_SUMS = ("calls", "responses", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "sessions")


def merge(measures: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {**{k: 0 for k in _SUMS}, "counts": Counter(), "clusters": Counter(), "ctx": Counter(),
                           "first_time": None, "last_time": None,
                           "nogain_cost": {"G.sans_apport": Counter(), "G.sans_apport_attente": Counter()},
                           "compaction": Counter(),
                           "session_ids": [], "tasks": {"main_done": 0, "main_aborted": 0, "main_cut": 0, "sub_done": 0,
                                                        "sub_aborted": 0, "sub_cut": 0,
                                                        "main_durations_ms": [], "sub_durations_ms": []}}
    out["context"] = _merge_context([m.get("context") for m in measures])
    out["settings"] = _merge_settings([m.get("settings") for m in measures])
    for m in measures:
        for k in _SUMS:
            out[k] += m.get(k, 0)
        out["counts"].update(m["counts"])
        out["clusters"].update(m.get("clusters") or m["counts"])
        out["ctx"].update(m["ctx"])
        out["session_ids"] += m.get("session_ids", [])
        for k, v in (m.get("nogain_cost") or {}).items():
            out["nogain_cost"].setdefault(k, Counter()).update(v)
        out["compaction"].update(m.get("compaction") or {})
        for k, v in (m.get("tasks") or {}).items():
            out["tasks"][k] = out["tasks"][k] + v
        if m["first_time"] and (out["first_time"] is None or m["first_time"] < out["first_time"]):
            out["first_time"] = m["first_time"]
        if m["last_time"] and (out["last_time"] is None or m["last_time"] > out["last_time"]):
            out["last_time"] = m["last_time"]
    out["counts"], out["clusters"], out["ctx"] = dict(out["counts"]), dict(out["clusters"]), dict(out["ctx"])
    out["nogain_cost"] = {k: dict(v) for k, v in out["nogain_cost"].items()}
    out["compaction"] = dict(out["compaction"])
    return out


def rate_ratio(k1: int, n1: int, k2: int, n2: int, c1: int | None = None, c2: int | None = None) -> dict[str, Any] | None:
    """Rapport des taux (apres / avant) et IC 95 % (Poisson, methode du logarithme ; 0,5 ajoute si un compte est nul).

    `c1`, `c2` : nombre de grappes ; l'ecart-type est alors calcule sur les grappes (evenements d'une grappe lies)."""
    if n1 <= 0 or n2 <= 0:
        return None
    a, b = (k1 + 0.5, k2 + 0.5) if 0 in (k1, k2) else (float(k1), float(k2))
    rr = (b / n2) / (a / n1)
    ca = float(c1) if c1 is not None else a
    cb = float(c2) if c2 is not None else b
    ca, cb = (ca + 0.5, cb + 0.5) if 0 in (ca, cb) else (ca, cb)
    se = math.sqrt(1 / ca + 1 / cb)
    return {"ratio": rr, "low": rr * math.exp(-1.96 * se), "high": rr * math.exp(1.96 * se),
            "clusters": [c1, c2] if c1 is not None else None}


MIN_CLUSTERS = 5         # * sous ce nombre de grappes (avant + apres), aucune conclusion


def _conclusion(k1: int, n1: int, k2: int, n2: int, rr: dict[str, Any] | None, loss: bool, clusters: int | None = None) -> str:
    if rr is None or k1 + k2 < MIN_EVENTS or min(n1, n2) < MIN_ACTIVITY or (clusters is not None and clusters < MIN_CLUSTERS):
        return "trop peu de donnees pour conclure"
    if rr["high"] < 1:
        return "baisse demontree" + (" : amelioration" if loss else "")
    if rr["low"] > 1:
        return "hausse demontree" + (" : degradation" if loss else "")
    return "pas de difference demontree"


_LABELS = {
    "G.sans_apport": "reprises du modele sans apport (etat ou resultat inchange)",
    "G.sans_apport_attente": "dont pendant une attente (en cours, indisponible)",
    "G.ameliorable": "reprises ameliorables (G : agent ou outil)",
    "A.relectures": "relectures identiques (A)",
    "B.echecs_en_boucle": "echecs en boucle (B)",
    "E.service_a_la_main": "commandes a la main vers un service (E)",
    "erreurs": "appels en erreur",
    "C.relectures_apres_compaction": "relectures d'une ressource deja lue, apres une compaction (grappes : fenetres concernees)",
    "C.execs_coupes": "execs dont la sortie a ete coupee au milieu par le client",
}


def compare(before: dict[str, Any], after: dict[str, Any], top_habits: int = 8) -> dict[str, Any]:
    """Lignes de comparaison : mesures globales puis habitudes G les plus frequentes."""
    rows: list[dict[str, Any]] = []
    keys = ["G.sans_apport", "G.sans_apport_attente", "G.ameliorable", "A.relectures", "B.echecs_en_boucle",
            "E.service_a_la_main", "erreurs"]
    cb, ca = before.get("context") or {}, after.get("context") or {}
    if cb and ca:
        keys.append("C.relectures_apres_compaction")
        full = all((c["exact"].get("truncation_known_sessions") or 0) >= (c.get("sessions") or 0) for c in (cb, ca))
        if full:     # * une periode importee avant le releve des coupes donnerait un faux zero
            keys.append("C.execs_coupes")
    habits = Counter({k: before["counts"].get(k, 0) + after["counts"].get(k, 0) for k in
                      set(before["counts"]) | set(after["counts"]) if k.startswith("G.") and "|" in k})
    keys += [k for k, n in habits.most_common(top_habits) if n >= MIN_EVENTS // 2]
    for k in keys:
        # * Unite de la reference (avant) : une periode vide ou sans releve par reponse n'en change pas.
        per_response = (k.startswith("G.") or k == "C.relectures_apres_compaction") and bool(before["responses"])
        n1, n2 = (before["responses"], after["responses"]) if per_response else (before["calls"], after["calls"])
        k1, k2 = int(before["counts"].get(k, 0)), int(after["counts"].get(k, 0))
        c1 = int((before.get("clusters") or before["counts"]).get(k, 0))
        c2 = int((after.get("clusters") or after["counts"]).get(k, 0))
        rr = rate_ratio(k1, n1, k2, n2, c1, c2)
        label = _LABELS.get(k) or ("habitude G " + k[2:].replace("|", " : "))
        rows.append({"key": k, "label": label, "unit": "pour 1 000 reponses" if per_response else "pour 1 000 appels",
                     "before": {"count": k1, "activity": n1, "rate": 1000 * k1 / n1 if n1 else None,
                                "context_tokens": before["ctx"].get(k)},
                     "after": {"count": k2, "activity": n2, "rate": 1000 * k2 / n2 if n2 else None,
                               "context_tokens": after["ctx"].get(k)},
                     "clusters": [c1, c2],
                     "ratio": rr, "conclusion": _conclusion(k1, n1, k2, n2, rr, loss=True, clusters=c1 + c2)})
    rows += _task_rows(before, after)
    return {"compare_version": COMPARE_VERSION, "rows": rows, "descriptive": _descriptive(before, after),
            "settings": settings_compare(before, after),
            "context": {"exact": _context_rows(before, after, "exact"), "attributed": _context_rows(before, after, "attributed"),
                        "scenarios": [_scenarios(before), _scenarios(after)]},
            "method": ("taux rapportes a l'activite (G : pour 1 000 reponses du modele ; autres : pour 1 000 appels) ; "
                       "rapport apres/avant avec intervalle de confiance a 95 % (loi de Poisson, methode du logarithme), "
                       "incertitude calculee sur les grappes (groupes d'appels repetes, signalements) et non sur les evenements, "
                       f"qui arrivent groupes ; difference demontree seulement si l'intervalle exclut 1, avec au moins {MIN_EVENTS} "
                       f"evenements, {MIN_CLUSTERS} grappes et {MIN_ACTIVITY} reponses ou appels par periode"),
            "caveat": ("Deux periodes different aussi par le travail fait : une baisse peut venir d'une tache differente. "
                       "Comparer des periodes de travail comparable (meme projet, meme type de tache).")}


def _proportions(k1: int, n1: int, k2: int, n2: int) -> tuple[dict[str, Any] | None, str]:
    """Difference de proportions (apres - avant) et IC 95 % (approximation normale) ; conclusion prudente."""
    if min(n1, n2) < 20:
        return None, "trop peu de donnees pour conclure"
    p1, p2 = k1 / n1, k2 / n2
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2) or 1e-9
    d = p2 - p1
    ci = {"diff": d, "low": d - 1.96 * se, "high": d + 1.96 * se}
    if ci["low"] > 0:
        return ci, "hausse demontree"
    if ci["high"] < 0:
        return ci, "baisse demontree"
    return ci, "pas de difference demontree"


def _task_rows(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for who, label in (("main", "taches du fil principal terminees (sur terminees + interrompues + coupees par le client)"),
                       ("sub", "taches des sous-agents terminees (sur terminees + interrompues + coupees par le client)")):
        tb, ta = before["tasks"], after["tasks"]
        k1, n1 = tb[f"{who}_done"], tb[f"{who}_done"] + tb[f"{who}_aborted"] + tb.get(f"{who}_cut", 0)
        k2, n2 = ta[f"{who}_done"], ta[f"{who}_done"] + ta[f"{who}_aborted"] + ta.get(f"{who}_cut", 0)
        ci, concl = _proportions(k1, n1, k2, n2)
        if concl.startswith("hausse"):
            concl += " : amelioration"
        elif concl.startswith("baisse"):
            concl += " : degradation"
        rows.append({"key": f"taches.{who}", "label": label, "unit": "part des taches",
                     "before": {"count": k1, "activity": n1, "rate": 100 * k1 / n1 if n1 else None,
                                "aborted": tb[f"{who}_aborted"], "cut": tb.get(f"{who}_cut", 0)},
                     "after": {"count": k2, "activity": n2, "rate": 100 * k2 / n2 if n2 else None,
                               "aborted": ta[f"{who}_aborted"], "cut": ta.get(f"{who}_cut", 0)},
                     "ratio": None, "difference": ci, "conclusion": concl, "percent": True})
    return rows


def _median(v: list[int]) -> float | None:
    if not v:
        return None
    s = sorted(v)
    return float(s[len(s) // 2]) if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def _descriptive(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Mesures sans test (moyennes et medianes) : a lire, pas a conclure."""
    out = []
    for key, label, fn in (
        ("uncached_per_response", "tokens d'entree hors cache par reponse",
         lambda m: (m["input_tokens"] - m["cached_input_tokens"]) / m["responses"] if m["responses"] else None),
        ("cached_per_response", "tokens d'entree en cache par reponse",
         lambda m: m["cached_input_tokens"] / m["responses"] if m["responses"] else None),
        ("output_per_response", "tokens de sortie par reponse", lambda m: m["output_tokens"] / m["responses"] if m["responses"] else None),
        ("cache_share", "part de l'entree en cache (%)",
         lambda m: 100 * m["cached_input_tokens"] / m["input_tokens"] if m["input_tokens"] else None),
        ("waits", "appels d'attente (occasions d'attendre)", lambda m: m["counts"].get("attentes", 0)),
        ("no_gain_per_100_waits", "reprises d'attente sans apport pour 100 appels d'attente",
         lambda m: 100 * m["counts"].get("G.sans_apport_attente", 0) / m["counts"]["attentes"] if m["counts"].get("attentes") else None),
        ("ctx_no_gain_per_1000", "contexte relu pour les reprises sans apport, tokens pour 1 000 reponses",
         lambda m: 1000 * m["ctx"].get("G.sans_apport", 0) / m["responses"] if m["responses"] else None),
        ("main_task_median_s", "duree mediane d'une tache du fil principal (s)",
         lambda m: (_median(m["tasks"]["main_durations_ms"]) or 0) / 1000 if m["tasks"]["main_durations_ms"] else None),
        ("sub_task_median_s", "duree mediane d'une tache de sous-agent (s)",
         lambda m: (_median(m["tasks"]["sub_durations_ms"]) or 0) / 1000 if m["tasks"]["sub_durations_ms"] else None),
    ):
        out.append({"key": key, "label": label, "before": fn(before), "after": fn(after)})
    return out


# * Trois lectures qu'un tiret confondait (constate le 2026-09-21 sur la reprise de 01a0bf95) : un ZERO OBSERVE reste
#   un nombre ; MISSING = la donnee n'a pas ete relevee (import anterieur, mesure enregistree avant l'indicateur) ;
#   NOT_APPLICABLE = l'indicateur n'a pas de sens pour cette periode (aucun echange entre agents, aucun message
#   rapproche a enchainer, aucune compaction).
MISSING = "non releve"
NOT_APPLICABLE = "sans objet"


def _exch(e: dict[str, Any], value: Any) -> Any:
    """Indicateur d'echange : valeur si la periode a des echanges, sans objet si elle n'en a pas, manquant sinon."""
    if "exchange_sessions" not in e:
        return MISSING
    return value() if e.get("exchange_sessions") else NOT_APPLICABLE


def _paired(e: dict[str, Any], value: Any) -> Any:
    """Indicateur qui exige le rapprochement envoi -> reception et au moins un message rapproche."""
    if "exchange_sessions" not in e:
        return MISSING
    if not e.get("exchange_sessions"):
        return NOT_APPLICABLE
    if not e.get("pairing_known_sessions"):
        return MISSING
    return value() if e.get("messages_paired") else NOT_APPLICABLE


def _kept(e: dict[str, Any]) -> Any:
    if "kept_fact_known_sessions" not in e:
        return MISSING
    if e.get("kept_fact_known_sessions"):
        return _ratio(e.get("kept_agent_messages_at_last_compaction"), e.get("received_before_last_compaction"), 100)
    return MISSING if e.get("kept_fact_agents_missing") else NOT_APPLICABLE


def _floor(e: dict[str, Any], value: Any) -> Any:
    """Socle : releve seulement pour les fils dont la premiere requete est dans la vue. Sans un tel fil, la donnee est
    MANQUANTE (fils commences avant la tranche, ou mesure anterieure a ce champ), pas « sans objet » : le socle existe."""
    if e.get("threads"):
        return value()
    return NOT_APPLICABLE if e.get("threads_before_view") == 0 and "threads_before_view" in e else MISSING


def _ratio(a: Any, b: Any, scale: float = 1.0) -> float | None:
    return scale * a / b if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b else None


_CONTEXT_ROWS: dict[str, list[tuple[str, str, Any]]] = {
    # * Releves du client et comptes de faits : rien n'est partage ni reconnu par AgentWatch.
    "exact": [
        ("input_per_request", "entree par requete du modele, demandes de compaction comprises (tokens)",
         lambda e, a: _ratio(e.get("session_input_tokens"), e.get("requests"))),
        ("thread_first_input", "socle : entree de la premiere requete d'un fil (tokens, moyenne)",
         lambda e, a: _floor(e, lambda: _ratio(e.get("thread_first_input_sum"), e.get("threads")))),
        ("floor_share", "part de l'entree due au socle relu par chaque requete (%), fils commences dans la periode",
         lambda e, a: _floor(e, lambda: _ratio(e.get("thread_first_reread_tokens"), e.get("thread_first_basis_input_tokens"), 100))),
        ("window_first_input", "premiere requete apres une compaction (tokens, mediane)",
         lambda e, a: _median(e.get("window_first_inputs") or [])),
        ("requests_per_window", "requetes par fenetre de contexte",
         lambda e, a: _ratio(e.get("requests"), (e.get("windows_after_compaction") or 0) + (e.get("threads") or 0))),
        ("added_per_request", "tokens ajoutes au contexte par les sorties d'outils, par requete",
         lambda e, a: _ratio(e.get("added_tokens"), e.get("requests"))),
        ("reread_share", "part de l'entree qui est la relecture de sorties d'outils (%)",
         lambda e, a: _ratio(e.get("reread_tokens"), e.get("session_input_tokens"), 100)),
        ("window_start", "tokens ajoutes par les sorties en debut de fenetre apres compaction (mediane)",
         lambda e, a: _median(e.get("window_start_added_tokens") or [])),
        ("execs_cut", "execs dont la sortie a ete coupee au milieu par le client",
         lambda e, a: e.get("execs_cut_in_the_middle") if e.get("truncation_known_sessions") else MISSING),
        ("quota_points", "points de quota consommes pendant les sessions",
         lambda e, a: e.get("quota_points") if e.get("quota_known_sessions") else MISSING),
        ("requests_per_quota_point", "requetes du modele par point de quota",
         lambda e, a: (_ratio(e.get("requests"), e.get("quota_points")) if e.get("quota_points") else NOT_APPLICABLE)
         if e.get("quota_known_sessions") else MISSING),
        ("turns_cut", "tours coupes par le client (quota epuise, serveur)", lambda e, a: e.get("turns_cut")),
        # * Entre agents (sessions a plusieurs agents seulement ; - sinon).
        ("messages_sent", "messages envoyes a un autre agent", lambda e, a: _exch(e, lambda: e.get("messages_sent", 0))),
        ("messages_received", "messages d'autres agents entres dans un fil", lambda e, a: _exch(e, lambda: e.get("messages_received", 0))),
        ("messages_per_100_requests", "messages envoyes a un autre agent, pour 100 requetes du modele",
         lambda e, a: _exch(e, lambda: _ratio(e.get("messages_sent", 0), e.get("requests"), 100))),
        ("message_only_requests", "requetes qui n'emettent que des messages, pour 100 requetes",
         lambda e, a: _exch(e, lambda: _ratio(e.get("message_only_requests", 0), e.get("requests"), 100))),
        ("message_only_input_tokens", "entree des requetes qui n'emettent que des messages (tokens)",
         lambda e, a: _exch(e, lambda: e.get("message_only_input_tokens", 0))),
        ("message_only_input_share", "part de l'entree relue par ces requetes (%)",
         lambda e, a: _exch(e, lambda: _ratio(e.get("message_only_input_tokens", 0), e.get("session_input_tokens"), 100))),
        ("chain_messages_share", "messages rapproches enchaines sans appel d'outil hors messagerie entre deux (%)",
         lambda e, a: _paired(e, lambda: _ratio(e.get("chain_messages", 0), e.get("messages_paired"), 100))),
        ("first_response_messages_only", "premiere reponse apres un message recu qui n'emet que des messages (%)",
         lambda e, a: _paired(e, lambda: _ratio(sum(n for k, n in (e.get("first_response_after_message") or {}).items()
                                                    if k.startswith("message_to_")),
                                                sum((e.get("first_response_after_message") or {}).values()), 100))),
        ("messages_slow_share", "messages entres chez le destinataire apres le delai de livraison lente (%)",
         lambda e, a: _paired(e, lambda: _ratio(e.get("messages_slow", 0), e.get("messages_paired"), 100))),
        ("kept_agent_messages_share", "messages d'agents gardes a la derniere compaction, sur 100 recus jusque-la",
         lambda e, a: _kept(e)),
    ],
    # * Attributions reconstruites : partage d'une reponse entre ses appels, ressource reconnue d'une fenetre a l'autre.
    "attributed": [
        ("rereads_per_window", "relectures apres compaction, par fenetre",
         lambda e, a: _ratio(a.get("rereads_after_compaction"), e.get("windows_after_compaction"))),
        ("rereads_added_share", "part des tokens ajoutes qui vient de relectures apres compaction (%)",
         lambda e, a: _ratio(a.get("rereads_added_tokens"), e.get("added_tokens"), 100)),
        ("rereads_unchanged", "dont relectures d'une ressource identique ou sans modification observee",
         lambda e, a: a.get("rereads_unchanged")),
        ("truncated_added", "tokens entres par des sorties de commande que le client a coupees",
         lambda e, a: a.get("truncated_calls_added_tokens") if e.get("truncation_known_sessions") else MISSING),
        # * ESTIMATION, a ne jamais additionner a l'entree des requetes d'envoi (elles relisent deja ce contexte).
        ("retained_context_estimate", "estimation du contexte conserve : surplus de debut de fenetre x requetes (tokens)",
         lambda e, a: _exch(e, lambda: a.get("retained_context_estimate_tokens", 0))),
        ("retained_basis_fact_agents", "agents de cette estimation retenus sur le fait releve a la compaction",
         lambda e, a: _exch(e, lambda: (a.get("retained_context_agents_by_basis") or {}).get("fait releve a la compaction", 0)
                            if "retained_context_agents_by_basis" in a else MISSING)),
        ("retained_basis_correlation_agents", "agents de cette estimation retenus sur la correlation, faute du fait",
         lambda e, a: _exch(e, lambda: (a.get("retained_context_agents_by_basis") or {}).get("correlation", 0)
                            if "retained_context_agents_by_basis" in a else MISSING)),
        ("retained_context_per_request", "cette estimation, par requete des fenetres concernees (tokens)",
         lambda e, a: _exch(e, lambda: _ratio(a.get("retained_context_estimate_tokens", 0), a.get("retained_context_estimate_requests"))
                            if a.get("retained_context_estimate_requests") else NOT_APPLICABLE)),
        ("handoff_added", "tokens ajoutes par les lectures apres l'ecriture d'un autre agent",
         lambda e, a: _exch(e, lambda: a.get("handoff_added_tokens", 0))),
        ("shared_reference_added", "tokens ajoutes par les lectures d'une ressource deja lue par un autre agent, jamais modifiee",
         lambda e, a: _exch(e, lambda: a.get("shared_reference_added_tokens_by_others", 0))),
    ],
}


def _context_rows(before: dict[str, Any], after: dict[str, Any], nature: str) -> list[dict[str, Any]]:
    """Indicateurs de contexte d'une nature (`exact` ou `attributed`), sans test : a lire, pas a conclure."""
    def val(m: dict[str, Any], fn: Any) -> float | None:
        c = m.get("context") or {}
        return fn(c.get("exact") or {}, c.get("attributed") or {}) if c else None
    return [{"key": k, "label": label, "before": val(before, fn), "after": val(after, fn)} for k, label, fn in _CONTEXT_ROWS[nature]]


def _scenarios(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Scenarios d'economie d'une periode : des HYPOTHESES derivees des attributions, en bornes hautes. Jamais un gain :
    un gain ne se constate que sur les releves exacts de deux periodes de travail comparable."""
    c = m.get("context") or {}
    e, a = c.get("exact") or {}, c.get("attributed") or {}
    if not c:
        return []
    total = e.get("session_input_tokens") or 0
    out = [{"key": "unchanged_rereads", "label": "si les relectures apres compaction d'une ressource identique ou sans modification "
            "observee etaient evitees (etat durable et court)", "added_tokens": a.get("rereads_unchanged_added_tokens"),
            "reread_tokens": a.get("rereads_unchanged_reread_tokens"),
            "share_of_input": _ratio(a.get("rereads_unchanged_reread_tokens"), total, 100),
            "caveat": "borne haute : une partie de ces relectures est necessaire ; « sans modification observee » ne prouve pas un contenu identique"}]
    if e.get("truncation_known_sessions"):
        out.append({"key": "truncated_outputs", "label": "si les sorties de commande coupees par le client avaient ete lues par extrait",
                    "added_tokens": a.get("truncated_calls_added_tokens"), "reread_tokens": None, "share_of_input": None,
                    "caveat": "borne haute : un extrait coute aussi des tokens ; la relecture evitee n'est pas chiffree"})
    return out


def agents_md_versions(views: list[SessionView]) -> list[dict[str, Any]]:
    """Versions d'AGENTS.md vues par Codex (empreinte du texte injecte), dans l'ordre d'apparition."""
    seen: dict[str, dict[str, Any]] = {}
    for v in views:
        for m in v.markers:
            fp = m.meta.get("agents_md_fp") if m.phase == S.PHASE_MESSAGE and m.meta.get("role") == "context" else None
            if not fp:
                continue
            row = seen.setdefault(fp, {"fingerprint": fp, "chars": m.meta.get("agents_md_chars"), "first_ns": m.ns,
                                       "first_time": m.time, "last_time": m.time, "sessions": set()})
            if m.ns and m.ns < row["first_ns"]:
                row["first_ns"], row["first_time"] = m.ns, m.time
            if m.time and (row["last_time"] is None or m.time > row["last_time"]):
                row["last_time"] = m.time
            row["sessions"].add(v.session_id)
    out = sorted(seen.values(), key=lambda r: r["first_ns"] or 0)
    for r in out:
        r["sessions"] = len(r["sessions"])
    return out


def _n(v: Any) -> str:
    return f"{int(v):,}".replace(",", " ") if isinstance(v, (int, float)) else "-"


def _num(v: Any) -> str:
    if isinstance(v, str):
        return v               # * « non releve » ou « sans objet » : jamais un tiret, jamais un zero
    if v is None:
        return NOT_APPLICABLE  # * division sans denominateur (aucune requete, aucune fenetre) : l'indicateur n'a pas d'objet
    return _n(v) if isinstance(v, int) or (isinstance(v, float) and abs(v) >= 1000) else _fmt(float(v))


def _context_reference_lines(m: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(lignes de mesures, lignes de scenarios) des indicateurs de contexte d'une reference, natures separees."""
    if not m.get("context"):
        return [], []
    out = ["## 1 bis. Contexte : releves exacts", "",
           "Releves du client et comptes de faits : rien n'y est partage ni reconnu par AgentWatch.", "",
           "| Releve | Valeur |", "|---|---|"]
    out += [f"| {r['label']} | {_num(r['before'])} |" for r in _context_rows(m, m, "exact")]
    out += ["", "## 1 ter. Contexte : attributions reconstruites", "",
            "Calculs d'AgentWatch sur ces releves : partage d'une reponse entre ses appels (prorata des tailles livrees), ressource "
            "reconnue d'une fenetre a l'autre, etat d'une relecture, estimation du contexte conserve. A lire, pas a additionner aux "
            "releves : l'estimation du contexte conserve recouvre en partie l'entree des requetes qui n'emettent que des messages "
            "(elles relisent deja ce contexte) ; ni l'une ni l'autre n'est un gain, et aucun scenario n'en est derive.", "",
            "| Attribution | Valeur |", "|---|---|"]
    out += [f"| {r['label']} | {_num(r['before'])} |" for r in _context_rows(m, m, "attributed")]
    fams = sorted(((m["context"].get("attributed") or {}).get("families") or {}).items(), key=lambda kv: -kv[1]["reread_tokens"])[:8]
    if fams:
        total = (m["context"].get("exact") or {}).get("session_input_tokens") or 0
        out += ["", "| Famille d'outil (attribution) | Appels | Tokens ajoutes | Relus ensuite | Part de l'entree |", "|---|---|---|---|---|"]
        out += [f"| {k} | {_n(v['calls'])} | {_n(v['added_tokens'])} | {_n(v['reread_tokens'])} | "
                f"{_fmt(100 * v['reread_tokens'] / total if total else None)} % |" for k, v in fams]
    out.append("")
    sc = [f"- {x['label']} : {_n(x.get('added_tokens'))} tokens ajoutes"
          + (f", {_n(x['reread_tokens'])} relus ensuite ({_fmt(x.get('share_of_input'))} % de l'entree)" if x.get("reread_tokens") else "")
          + f". {x['caveat'][0].upper()}{x['caveat'][1:]}." for x in _scenarios(m)]
    return out, sc


def render_reference(ref: dict[str, Any]) -> str:
    m = ref["measure"]
    t = m["tasks"]
    per_r = (lambda k: 1000 * m["counts"].get(k, 0) / m["responses"]) if m["responses"] else (lambda k: 0.0)
    nga = m.get("nogain_cost", {}).get("G.sans_apport_attente", {})
    ng = m.get("nogain_cost", {}).get("G.sans_apport", {})
    habits = sorted(((k, v) for k, v in m["counts"].items() if k.startswith("G.") and "|" in k), key=lambda kv: -kv[1])[:8]
    lines = ["# AgentWatch - rapport de reference", "",
             f"- Periode : du {ref['period']['since']} au {ref['period']['until']} (UTC ; debut inclus, fin exclue)",
             f"- Filtres : client `{ref['filters']['client']}`, projet contenant `{ref['filters']['project'] or '(tous)'}`",
             f"- Sessions : {m['sessions']} ({', '.join(str(s)[:13] for s in m['session_ids'][:12])}"
             + (" ..." if len(m['session_ids']) > 12 else "") + ")"
             + (f" ; {ref['excluded_unreliable_sessions']} ecartee(s), horodatages non fiables" if ref.get("excluded_unreliable_sessions") else ""),
             f"- Versions d'AGENTS.md vues dans la periode : " + (", ".join(f"`{v['fingerprint'][:8]}` ({v['chars']} car.)"
                                                                  for v in ref.get("agents_md_versions") or []) or "aucune"),
             f"- Reglages de fil (modele / effort / niveau de service) : {settings_text(m.get('settings'))}",
             f"- Enregistre le {ref['created_at']} par AgentWatch {ref['agentwatch_version']} (comparaison v{ref['compare_version']})", "",
             "## 1. Mesures (constatees sur la periode)", "",
             "| Mesure | Valeur |", "|---|---|",
             f"| Reponses du modele (hors demandes de compaction) | {_n(m['responses'])} |",
             f"| Demandes de compaction (requete au modele pour resumer l'historique) | {_n((m.get('compaction') or {}).get('requests', 0))} : entree "
             f"{_n((m.get('compaction') or {}).get('input_tokens', 0))} (dont cache {_n((m.get('compaction') or {}).get('cached_input_tokens', 0))}), "
             f"sortie {_n((m.get('compaction') or {}).get('output_tokens', 0))} |",
             f"| Appels d'outils | {_n(m['calls'])} |",
             f"| Tokens d'entree des reponses | {_n(m['input_tokens'])}, dont en cache {_n(m['cached_input_tokens'])} "
             f"({_fmt(100 * m['cached_input_tokens'] / m['input_tokens'] if m['input_tokens'] else None)} %), hors cache "
             f"{_n(m['input_tokens'] - m['cached_input_tokens'])} |",
             f"| Tokens de sortie des reponses | {_n(m['output_tokens'])}, dont raisonnement {_n(m['reasoning_output_tokens'])} |",
             f"| Reprises du modele sans apport (resultat inchange) | {_n(m['counts'].get('G.sans_apport', 0))}, soit "
             f"{_fmt(per_r('G.sans_apport'))} pour 1 000 reponses |",
             f"| dont pendant une attente | {_n(m['counts'].get('G.sans_apport_attente', 0))}, soit "
             f"{_fmt(per_r('G.sans_apport_attente'))} pour 1 000 reponses, en "
             f"{_n((m.get('clusters') or {}).get('G.sans_apport_attente', 0))} episode(s) |",
             f"| Appels d'attente (occasions d'attendre) | {_n(m['counts'].get('attentes', 0))}, dont "
             f"{_fmt(100 * m['counts'].get('G.sans_apport_attente', 0) / m['counts']['attentes'] if m['counts'].get('attentes') else None)}"
             f" % relances sans apport |",
             f"| Cout mesure de ces reponses d'attente sans apport | {_n(nga.get('responses'))} reponses : entree "
             f"{_n(nga.get('input_tokens'))} (dont cache {_n(nga.get('cached_input_tokens'))}), sortie {_n(nga.get('output_tokens'))} |",
             f"| Cout mesure de toutes les reponses sans apport | {_n(ng.get('responses'))} reponses : entree "
             f"{_n(ng.get('input_tokens'))} (dont cache {_n(ng.get('cached_input_tokens'))}), sortie {_n(ng.get('output_tokens'))} |",
             f"| Taches du fil principal | {t['main_done']} terminees, {t['main_aborted']} interrompues, "
             f"{t.get('main_cut', 0)} coupees par le client (quota epuise, serveur : des interruptions, jamais des taches "
             f"terminees) ; duree mediane des terminees "
             f"{_fmt((_median(t['main_durations_ms']) or 0) / 1000 if t['main_durations_ms'] else None)} s |",
             f"| Taches des sous-agents | {t['sub_done']} terminees, {t['sub_aborted']} interrompues, "
             f"{t.get('sub_cut', 0)} coupees par le client ; duree mediane des terminees "
             f"{_fmt((_median(t['sub_durations_ms']) or 0) / 1000 if t['sub_durations_ms'] else None)} s |",
             f"| Appels en erreur | {_n(m['counts'].get('erreurs', 0))} |", ""]
    ctx_lines, ctx_scenarios = _context_reference_lines(m)
    if habits:
        lines += ["Reprises par habitude (G, allers-retours du modele) :", ""]
        lines += [f"- {k[2:].replace('|', ' : ')} : {_n(v)} ({_fmt(1000 * v / m['responses'] if m['responses'] else None)} pour 1 000 reponses)"
                  for k, v in habits]
        lines.append("")
    lines += ctx_lines
    lines += ["## 2. Economies estimees (hypotheses, pas des gains)", "",
              f"- Si chaque reprise d'attente sans apport etait evitee, la borne haute de l'economie serait de "
              f"{_n(nga.get('responses'))} reponses du modele, soit {_n(nga.get('input_tokens'))} tokens d'entree relus (dont "
              f"{_n(nga.get('cached_input_tokens'))} en cache) et {_n(nga.get('output_tokens'))} de sortie, sur la periode.",
              "- C'est une borne haute : une attente plus longue coute encore une reponse par evenement attendu, et une partie "
              "des reprises peut rester necessaire (verification utile, coordination).",
              "- Hypothese de l'ajout a AGENTS.md : moins de reprises sans apport pendant les attentes, a reussite des taches egale."]
    lines += ctx_scenarios
    lines += ["", "## 3. Gains constates", "",
              "Aucun a ce jour. Un gain ne sera constate qu'apres comparaison des sessions suivantes avec cette reference "
              "(`agentwatch compare --reference ...`), et seulement si l'intervalle de confiance exclut l'absence d'effet.", ""]
    return "\n".join(lines)


def _fmt(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}".replace(".", ",")


def render_markdown(result: dict[str, Any]) -> str:
    b, a = result["before"], result["after"]
    lines = ["# AgentWatch - avant / apres", "",
             f"- Bascule : {result['at_label']} ; client `{result['client']}`",
             f"- Avant : {b['sessions']} session(s), {b['calls']} appels, {b['responses']} reponses du modele "
             f"(+ {((b.get('compaction') or {}).get('requests', 0))} demande(s) de compaction) "
             f"({b['first_time'] or '?'} a {b['last_time'] or '?'}) ; tokens d'entree {_n(b.get('input_tokens'))} dont en cache "
             f"{_n(b.get('cached_input_tokens'))}, hors cache {_n((b.get('input_tokens') or 0) - (b.get('cached_input_tokens') or 0))} ; "
             f"sortie {_n(b.get('output_tokens'))}",
             f"- Apres : {a['sessions']} session(s), {a['calls']} appels, {a['responses']} reponses du modele "
             f"(+ {((a.get('compaction') or {}).get('requests', 0))} demande(s) de compaction) "
             f"({a['first_time'] or '?'} a {a['last_time'] or '?'}) ; tokens d'entree {_n(a.get('input_tokens'))} dont en cache "
             f"{_n(a.get('cached_input_tokens'))}, hors cache {_n((a.get('input_tokens') or 0) - (a.get('cached_input_tokens') or 0))} ; "
             f"sortie {_n(a.get('output_tokens'))}",
             f"- Methode : {result['comparison']['method']}", *settings_lines(result["comparison"].get("settings")), "",
             "## Ecarts testes (constates seulement si l'intervalle exclut l'absence d'effet)", "",
             "| Mesure | Unite | Avant (nombre) | Apres (nombre) | Apres / avant ou ecart [IC 95 %] | Conclusion |",
             "|---|---|---|---|---|---|"]
    for r in result["comparison"]["rows"]:
        rr, dd = r.get("ratio"), r.get("difference")
        if rr:
            ratio = f"{_fmt(rr['ratio'], 2)} [{_fmt(rr['low'], 2)} ; {_fmt(rr['high'], 2)}]"
        elif dd:
            ratio = f"{_fmt(100 * dd['diff'], 1)} pts [{_fmt(100 * dd['low'], 1)} ; {_fmt(100 * dd['high'], 1)}]"
        else:
            ratio = "-"
        unit = r["unit"]
        cl = r.get("clusters")
        cb = f", {cl[0]} grappe(s)" if cl and not r.get("percent") and r["key"] != "erreurs" else ""
        ca = f", {cl[1]} grappe(s)" if cl and not r.get("percent") and r["key"] != "erreurs" else ""
        if r["key"].startswith("taches."):
            # * Interruptions nommees : par l'utilisateur (turn_aborted) et par le client (quota epuise, serveur).
            cb = f" sur {r['before']['activity']} ; {r['before'].get('aborted', 0)} interrompue(s), {r['before'].get('cut', 0)} coupee(s) par le client"
            ca = f" sur {r['after']['activity']} ; {r['after'].get('aborted', 0)} interrompue(s), {r['after'].get('cut', 0)} coupee(s) par le client"
        lines.append(f"| {r['label']} | {unit} | {_fmt(r['before']['rate'])}{' %' if r.get('percent') else ''} ({r['before']['count']}{cb}) | "
                     f"{_fmt(r['after']['rate'])}{' %' if r.get('percent') else ''} ({r['after']['count']}{ca}) | {ratio} | **{r['conclusion']}** |")
    lines += ["", "## Mesures descriptives (sans test : a lire, pas a conclure)", "",
              "| Mesure | Avant | Apres |", "|---|---|---|"]
    for d in result["comparison"]["descriptive"]:
        lines.append(f"| {d['label']} | {_fmt(d['before'])} | {_fmt(d['after'])} |")
    ctx = result["comparison"].get("context") or {}
    if any(r["before"] is not None or r["after"] is not None for r in ctx.get("exact") or []):
        lines += ["", "## Contexte : releves exacts (sans test : a lire, pas a conclure)", "",
                  "Releves du client et comptes de faits. Un tour coupe par le client est une interruption : il figure ici et dans le "
                  "denominateur des taches, jamais parmi les taches terminees.", "",
                  "| Releve | Avant | Apres |", "|---|---|---|"]
        lines += [f"| {r['label']} | {_num(r['before'])} | {_num(r['after'])} |" for r in ctx["exact"]]
        lines += ["", "## Contexte : attributions reconstruites (calculs d'AgentWatch, sans test)", "",
                  "| Attribution | Avant | Apres |", "|---|---|---|"]
        lines += [f"| {r['label']} | {_num(r['before'])} | {_num(r['after'])} |" for r in ctx["attributed"]]
        lines += ["", "## Scenarios d'economie (hypotheses, bornes hautes : jamais des gains)", ""]
        for name, sc in zip(("Avant", "Apres"), ctx.get("scenarios") or []):
            for x in sc:
                lines.append(f"- {name} : {x['label']} : {_n(x.get('added_tokens'))} tokens ajoutes"
                             + (f", {_n(x['reread_tokens'])} relus ensuite ({_fmt(x.get('share_of_input'))} % de l'entree)"
                                if x.get("reread_tokens") else "") + f" ({x['caveat']})")
        lines.append("")
        lines.append("Un gain ne se lit que dans les releves exacts des deux periodes, a travail comparable ; une attribution ou un "
                     "scenario qui baisse n'en est pas un.")
    lines += ["", "Contexte relu pour decider les reprises ameliorables (tokens, en grande partie en cache) : avant "
              f"{_fmt((b['ctx'].get('G.ameliorable') or 0) / 1e6, 1)} M, apres {_fmt((a['ctx'].get('G.ameliorable') or 0) / 1e6, 1)} M.",
              "", result["comparison"]["caveat"], ""]
    return "\n".join(lines)
