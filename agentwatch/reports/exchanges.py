"""Entre agents : messages rapproches de leur reception, requetes qui n'emettent que des messages, debut de fenetre face
aux messages recus, ressources partagees. Descriptif : des faits et des mesures, aucun verdict, aucun signalement.

# * Pourquoi : les detecteurs raisonnent agent par agent. Ce qui se passe ENTRE les agents (qui ecrit a qui, ce que
#   coute un message, qui relit le travail de qui) n'etait ni mesure ni montre. Constate le 2026-09-21 (session Codex
#   01a0bf95, 4 agents, 1 141 messages) : une requete sur quatre ne fait qu'envoyer un message et relit pour cela tout
#   son contexte (163,9 M tokens d'entree, 25 % de la session) ; la premiere requete d'une fenetre d'un sous-agent
#   grossit avec le cumul des messages qu'il a recus (40 000 -> 76 000 tokens, r = 0,997), pas celle du fil principal.
# * Rapprochement exact : le contenu transmis porte la meme empreinte a l'envoi et a la reception (`payload_fp`,
#   ecrite a l'import). L'ordre seul se trompe (107 fois sur 1 139 dans cette session) : sans empreinte des deux
#   cotes, un message n'est pas rapproche, jamais devine.
# ! Libelles strictement observables. Le texte des messages est chiffre par le fournisseur : il reste semantiquement
#   indetermine et AgentWatch ne cherche pas a le lire. « N'emet qu'un message » ne dit pas « ne travaille pas » (le
#   modele raisonne aussi dans cette requete) ; « message adresse a un autre agent apres une reception » ne dit pas que
#   le meme contenu est relaye ; « premiere reponse apres l'entree d'un message » est un enchainement dans le temps ;
#   une ressource « lue apres l'ecriture d'un autre » ne dit pas si la lecture etait necessaire.
# ! Deux nombres ne s'additionnent jamais : l'entree des requetes d'envoi (RELEVE) et l'estimation du contexte conserve
#   (CALCUL) se recouvrent, puisque les requetes d'envoi d'un agent relisent deja ce contexte. Aucun n'est un gain.
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from statistics import StatisticsError, correlation, linear_regression
from typing import Any

from agentwatch.core import schema as S
from agentwatch.core.correlate import Call, Marker, SessionView
from agentwatch.reports.context import _agent_of, _ref, _resources, _responses, _written, attribute_added

DEFAULTS = {"top_routes": 12, "top_resources": 10, "top_chains": 3, "slow_delivery_seconds": 60}
RETAINED_MIN_CORRELATION = 0.95     # * `report.exchanges.retained_min_correlation` : agents retenus dans l'estimation
# * Une correlation sur 3 points ne prouve rien (au hasard, |r| >= 0,95 une fois sur cinq ; une fois sur 80 avec 5 points).
#   Constate le 2026-09-21 (session 01a0bb58) : un agent retenu sur r = 0,999 avec 3 fenetres. Sans le fait releve a la
#   compaction, l'estimation exige ce nombre de fenetres (`report.exchanges.retained_min_windows`).
RETAINED_MIN_WINDOWS = 5
_ROOT = "/root"


def _settings(cfg: dict[str, Any] | None) -> dict[str, int]:
    user = ((cfg or {}).get("report") or {}).get("exchanges") or {}
    return {k: int(user.get(k, v)) for k, v in DEFAULTS.items()}


def _paths(view: SessionView) -> dict[str, str]:
    """Chemin d'agent (`/root/appearance`) -> cle d'agent. Un chemin inconnu reste un chemin : rien n'est suppose."""
    out = {_ROOT: "main"}
    for a in view.agent_infos:
        if a.agent_path:
            out[a.agent_path] = a.agent_id
    return out


def _src(m: Marker) -> dict[str, Any] | None:
    s = m.meta.get("source")
    return {"file": s.get("file"), "line": s.get("line")} if isinstance(s, dict) else None


def _quantile(values: list[float], q: float) -> float | None:
    return round(values[min(len(values) - 1, int(len(values) * q))], 1) if values else None


def pair_messages(view: SessionView) -> dict[str, Any]:
    """Envois (consignes et messages donnes a un autre agent) et receptions (messages entres dans un fil), rapproches
    par l'empreinte du contenu transmis. `pairs` : une entree par reception rapprochee ; `undelivered` : envois sans
    reception ; `unpaired_received` : receptions sans envoi (reponse finale d'un tour, import sans empreinte)."""
    paths = _paths(view)
    by_key = {v: k for k, v in paths.items()}
    sends: dict[str, list[Marker]] = defaultdict(list)
    sent: list[Marker] = []
    received: list[Marker] = []
    for m in sorted(view.markers, key=lambda x: x.ns):
        if m.phase != S.PHASE_MESSAGE:
            continue
        if m.meta.get("role") == "agent_instruction":
            sent.append(m)
            if m.meta.get("payload_fp"):
                sends[str(m.meta["payload_fp"])].append(m)
        elif m.meta.get("role") == "agent":
            received.append(m)
    pairs: list[dict[str, Any]] = []
    unpaired: list[Marker] = []
    used: set[int] = set()
    for r in received:
        queue = sends.get(str(r.meta.get("payload_fp") or ""), [])
        s = next((x for x in queue if id(x) not in used and x.ns <= r.ns + 5_000_000_000), None)
        if s is None:
            unpaired.append(r)
            continue
        used.add(id(s))
        pairs.append({"from": _agent_of(s.agent_id), "to": _agent_of(r.agent_id), "kind": r.meta.get("message_kind"),
                      "tool": s.meta.get("tool"), "sent_ns": s.ns, "received_ns": r.ns, "sent_time": s.time,
                      "received_time": r.time, "delay_s": round((r.ns - s.ns) / 1e9, 2),
                      "payload_chars": r.meta.get("payload_chars"), "encrypted": bool(r.meta.get("encrypted")),
                      "trigger_turn": r.meta.get("trigger_turn"), "call_id": s.meta.get("message_id"),
                      "sent_source": _src(s), "received_source": _src(r)})
    undelivered = [s for s in sent if id(s) not in used]

    def target(m: Marker) -> str:
        t = str(m.meta.get("target") or "?")
        full = t if t.startswith("/") else f"{by_key.get(_agent_of(m.agent_id), _ROOT)}/{t}"
        return paths.get(full, full)

    return {"sent": sent, "received": received, "pairs": pairs, "unpaired_received": unpaired,
            "undelivered": undelivered, "target_of": target,
            "with_fingerprint": sum(1 for r in received if r.meta.get("payload_fp")),
            "author_of": lambda r: paths.get(str(r.meta.get("author") or ""), str(r.meta.get("author") or "?"))}


def _messages(view: SessionView, pm: dict[str, Any], st: dict[str, int]) -> dict[str, Any]:
    pairs = pm["pairs"]
    routes: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"sent": 0, "received": 0, "paired": 0, "chars": 0, "delays": []})
    for s in pm["sent"]:
        routes[(_agent_of(s.agent_id), pm["target_of"](s))]["sent"] += 1
    for r in pm["received"]:
        row = routes[(pm["author_of"](r), _agent_of(r.agent_id))]
        row["received"] += 1
        row["chars"] += int(r.meta.get("payload_chars") or 0)
    for p in pairs:
        row = routes[(p["from"], p["to"])]
        row["paired"] += 1
        row["delays"].append(p["delay_s"])
    delays = sorted(p["delay_s"] for p in pairs)
    # * Croisement : A ecrit a B alors qu'un message de B pour A est parti et n'est pas encore entre chez A.
    flights: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for p in pairs:
        flights[(p["from"], p["to"])].append((p["sent_ns"], p["received_ns"]))
    crossed = sum(1 for p in pairs if any(a < p["sent_ns"] < b for a, b in flights.get((p["to"], p["from"]), [])))
    # * Un envoi jamais entre peut etre un appel que le client a REFUSE (« agent thread limit reached ») : le statut de
    #   l'appel d'envoi, deja releve, le dit ; sans lui on lisait un message perdu la ou rien n'avait ete envoye.
    by_call = {str(c.call_id): c for c in view.calls if c.call_id}
    lost = []
    for s in pm["undelivered"]:
        c = by_call.get(str(s.meta.get("message_id")))
        failed = c is not None and c.status in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED)
        lost.append({"from": _agent_of(s.agent_id), "to": pm["target_of"](s), "time": s.time, "tool": s.meta.get("tool"),
                     "source": _src(s), "call_status": c.status if c is not None else None,
                     "call_error": (c.error_summary or c.error_signature) if failed else None})
    rows = [{"from": k[0], "to": k[1], "sent": v["sent"], "received": v["received"], "paired": v["paired"],
             "payload_chars": v["chars"], "delay_median_s": _quantile(sorted(v["delays"]), 0.5),
             "delay_p90_s": _quantile(sorted(v["delays"]), 0.9)}
            for k, v in sorted(routes.items(), key=lambda kv: -(kv[1]["sent"] + kv[1]["received"]))]
    return {"sent": len(pm["sent"]), "received": len(pm["received"]),
            "received_by_kind": dict(Counter(str(r.meta.get("message_kind") or "inconnu") for r in pm["received"])),
            "encrypted_received": sum(1 for r in pm["received"] if r.meta.get("encrypted")),
            "encrypted_sent": sum(1 for s in pm["sent"] if s.meta.get("encrypted")),
            "pairing": {"basis": "empreinte du contenu transmis, identique a l'envoi et a la reception",
                        "available": pm["with_fingerprint"] > 0, "paired": len(pairs),
                        "received_with_fingerprint": pm["with_fingerprint"],
                        "undelivered": lost[:10], "undelivered_total": len(lost),
                        "undelivered_failed_calls": sum(1 for x in lost if x["call_error"]),
                        "received_without_send": len(pm["unpaired_received"])},
            "delivery_delay": {"median_s": _quantile(delays, 0.5), "p90_s": _quantile(delays, 0.9),
                               "max_s": round(delays[-1], 1) if delays else None,
                               "slow": sum(1 for d in delays if d > st["slow_delivery_seconds"]),
                               "slow_threshold_s": st["slow_delivery_seconds"]},
            "crossed": crossed, "routes": rows[: st["top_routes"]], "routes_total": len(rows)}


def _requests(view: SessionView, pm: dict[str, Any], st: dict[str, int]) -> dict[str, Any] | None:
    """Reponses du modele qui n'emettent que des messages : chacune relit tout le contexte de son agent."""
    # * Aucun envoi dans la vue (tranche ou seul un fil recoit) : les comptes sont des ZEROS OBSERVES, pas une absence de
    #   mesure. La section n'existe que s'il y a des echanges entre agents (voir `agent_exchanges`).
    message_calls = {str(m.meta.get("message_id")) for m in pm["sent"] if m.meta.get("message_id")}
    emitted: dict[str, list[Call]] = defaultdict(list)
    for c in view.calls:
        rid = (c.usage or {}).get("emitter_request_id")
        if rid:
            emitted[str(rid)].append(c)
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {"responses": 0, "input_tokens": 0, "message_only": 0,
                                                          "message_only_input_tokens": 0, "message_only_output_tokens": 0})
    order: dict[str, list[tuple[int, str, bool, int]]] = defaultdict(list)   # agent -> (ns, reponse, messages seuls, entree)
    for m in view.markers:
        u = m.meta.get("usage") if m.phase == S.PHASE_USAGE else None
        if not isinstance(u, dict) or u.get("scope") != "response":
            continue
        agent = _agent_of(m.agent_id)
        calls = emitted.get(str(u.get("response_id")), [])
        only = bool(calls) and all(str(c.call_id) in message_calls for c in calls)
        tokens = int(u.get("input_tokens") or 0)
        row = per[agent]
        row["responses"] += 1
        row["input_tokens"] += tokens
        if only:
            row["message_only"] += 1
            row["message_only_input_tokens"] += tokens
            row["message_only_output_tokens"] += int(u.get("output_tokens") or 0)
        order[agent].append((m.ns, str(u.get("response_id")), only, tokens))
    total_in = sum(r["input_tokens"] for r in per.values())
    only_in = sum(r["message_only_input_tokens"] for r in per.values())
    out = {"responses": sum(r["responses"] for r in per.values()), "message_only": sum(r["message_only"] for r in per.values()),
           "message_only_input_tokens": only_in, "share_of_session_input": round(only_in / total_in, 4) if total_in else None,
           "by_agent": {a: {**r, "share_of_agent_input": round(r["message_only_input_tokens"] / r["input_tokens"], 4)
                            if r["input_tokens"] else None} for a, r in per.items()},
           "reactions": _reactions(pm, emitted, order), "chains": _chains(pm, emitted, order, st)}
    return out


def _reactions(pm: dict[str, Any], emitted: dict[str, list[Call]],
               order: dict[str, list[tuple[int, str, bool, int]]]) -> dict[str, dict[str, int]] | None:
    """Premiere reponse du destinataire apres l'entree d'un message rapproche, par ce qu'elle EMET : `tool_call` (au
    moins un appel d'outil hors messagerie), `message_to_sender` (n'emet que des messages, dont un a l'emetteur),
    `message_to_other_agent` (n'emet que des messages, aucun a l'emetteur), `text_only`, `no_later_response`.
    Enchainement dans le temps seulement : ni « repond », ni « relaie le meme contenu » ne sont etablis."""
    if not pm["pairs"]:
        return None
    target_of_call = {str(m.meta.get("message_id")): pm["target_of"](m) for m in pm["sent"] if m.meta.get("message_id")}
    for L in order.values():
        L.sort()
    keys = {a: [x[0] for x in L] for a, L in order.items()}
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in pm["pairs"]:
        L = order.get(p["to"], [])
        i = bisect.bisect_right(keys.get(p["to"], []), p["received_ns"])
        if i >= len(L):
            kind = "no_later_response"
        else:
            calls = emitted.get(L[i][1], [])
            targets = {target_of_call[str(c.call_id)] for c in calls if str(c.call_id) in target_of_call}
            if not calls:
                kind = "text_only"
            elif not L[i][2]:
                kind = "tool_call"
            else:
                kind = "message_to_sender" if p["from"] in targets else "message_to_other_agent"
        out[p["to"]][kind] += 1
    return {a: dict(v) for a, v in out.items()}


def _chains(pm: dict[str, Any], emitted: dict[str, list[Call]], order: dict[str, list[tuple[int, str, bool, int]]],
            st: dict[str, int]) -> dict[str, Any] | None:
    """Messages enchaines sans appel d'outil hors messagerie entre deux : un message entre chez B, et la premiere
    reponse de B ensuite n'emet que des messages, dont l'un entre chez C... Enchainement dans le temps : le texte
    (chiffre) n'est pas compare, et « sans appel d'outil » ne veut pas dire « sans travail »."""
    pairs = pm["pairs"]
    if not pairs:
        return None
    by_call = {p["call_id"]: p for p in pairs if p.get("call_id")}
    for L in order.values():
        L.sort()
    keys = {a: [x[0] for x in L] for a, L in order.items()}

    def following(p: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
        L = order.get(p["to"], [])
        i = bisect.bisect_right(keys.get(p["to"], []), p["received_ns"])
        if i >= len(L) or not L[i][2]:
            return None, 0
        nxt = next((by_call[c.call_id] for c in emitted.get(L[i][1], []) if c.call_id in by_call), None)
        return nxt, L[i][3]

    seen: set[int] = set()
    chains: list[tuple[list[dict[str, Any]], int]] = []
    for p in sorted(pairs, key=lambda x: x["sent_ns"]):
        if id(p) in seen:
            continue
        chain, tokens, cur = [p], 0, p
        while True:
            nxt, cost = following(cur)
            if nxt is None or id(nxt) in seen or nxt is cur:
                break
            seen.add(id(nxt))
            chain.append(nxt)
            tokens += cost
            cur = nxt
        if len(chain) >= 2:
            chains.append((chain, tokens))
    shapes = Counter(" > ".join([c[0]["from"]] + [x["to"] for x in c]) for c, _t in chains)
    longest = sorted(chains, key=lambda ct: -len(ct[0]))[: st["top_chains"]]
    return {"chains": len(chains), "messages": sum(len(c) for c, _t in chains),
            "intermediate_input_tokens": sum(t for _c, t in chains),
            "by_length": dict(sorted(Counter(len(c) for c, _t in chains).items())),
            "shapes": [{"agents": k.split(" > "), "count": n} for k, n in shapes.most_common(6)],
            "longest": [{"messages": len(c), "intermediate_input_tokens": t, "start_time": c[0]["sent_time"],
                         "end_time": c[-1]["received_time"],
                         "steps": [{"from": x["from"], "to": x["to"], "sent_time": x["sent_time"],
                                    "payload_chars": x["payload_chars"], "sent_source": x["sent_source"],
                                    "received_source": x["received_source"]} for x in c]} for c, t in longest]}


def _resident(view: SessionView, pm: dict[str, Any]) -> list[dict[str, Any]]:
    """Premiere requete de chaque fenetre de contexte, face au cumul des messages recus jusque-la : pente et
    correlation (a partir de 3 fenetres). `window_start_surplus_tokens` : surplus de la premiere requete de chaque
    fenetre par rapport a la premiere fenetre apres compaction, multiplie par les requetes de la fenetre. Un CALCUL
    sur des releves, qui ne dit pas la cause du surplus. `compactions` : le FAIT releve dans chaque ligne de compaction
    (messages d'agents gardes, face aux messages recus jusque-la), quand l'import l'a ecrit ; il prime sur la correlation."""
    got: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in pm["received"]:
        got[_agent_of(r.agent_id)].append((r.ns, int(r.meta.get("payload_chars") or 0)))
    kept_facts: dict[str, list[tuple[int, int]]] = defaultdict(list)       # agent -> (instant, messages d'agents gardes)
    compactions_seen: Counter[str] = Counter()                             # ! toutes, y compris celles importees sans ce fait
    for m in view.markers:
        if m.phase != S.PHASE_COMPACT_END:
            continue
        compactions_seen[_agent_of(m.agent_id)] += 1
        if isinstance(m.meta.get("replacement_agent_messages"), int):
            kept_facts[_agent_of(m.agent_id)].append((m.ns, m.meta["replacement_agent_messages"]))
    out = []
    for agent, L in _responses(view).items():
        recs = sorted(got.get(agent, []))
        if not recs:
            continue
        times = [x[0] for x in recs]
        firsts: dict[int, dict[str, Any]] = {}
        # ! Tranche de temps : la premiere fenetre visible d'un fil entame ne montre pas sa vraie premiere requete.
        partial = L[0]["window"] if L and L[0]["index"] != 0 else None
        per_window = Counter(r["window"] for r in L)
        for r in L:
            if r["window"] > 0 and r["window"] != partial and r["window"] not in firsts:
                n = bisect.bisect_right(times, r["ns"])
                firsts[r["window"]] = {"window": r["window"], "input_tokens": r["in"], "messages": n,
                                       "payload_chars": sum(x[1] for x in recs[:n])}
        pts = [firsts[w] for w in sorted(firsts)]
        base = pts[0]["input_tokens"] if pts else 0
        row: dict[str, Any] = {"agent": agent, "windows_after_compaction": len(pts), "received": len(recs),
                               "received_payload_chars": sum(x[1] for x in recs),
                               "first": pts[0] if pts else None, "last": pts[-1] if pts else None,
                               "tokens_per_payload_char": None, "correlation": None,
                               # ! None = fait NON IMPORTE (import anterieur au releve), jamais « zero message garde »
                               "compactions": [{"kept_agent_messages": k, "received_before": bisect.bisect_right(times, ns)}
                                               for ns, k in sorted(kept_facts.get(agent, []))] or None,
                               "compactions_observed": compactions_seen.get(agent, 0),
                               "compactions_with_fact": len(kept_facts.get(agent, [])),
                               "requests_in_these_windows": sum(per_window[p["window"]] for p in pts),
                               "window_start_surplus_tokens": sum(max(0, p["input_tokens"] - base) * per_window[p["window"]]
                                                                  for p in pts)}
        xs, ys = [p["payload_chars"] for p in pts], [p["input_tokens"] for p in pts]
        if len(pts) >= 3:
            try:
                row["tokens_per_payload_char"] = round(linear_regression(xs, ys).slope, 4)
                row["correlation"] = round(correlation(xs, ys), 3)
            except StatisticsError:
                pass     # * aucun message recu entre les fenetres, ou entree constante : rien a mesurer
        out.append(row)
    return sorted(out, key=lambda r: r["agent"] != "main")


def _retained_estimate(rows: list[dict[str, Any]], cfg: dict[str, Any] | None) -> dict[str, Any]:
    """ESTIMATION du contexte conserve : somme des surplus de debut de fenetre des agents dont la compaction GARDE les
    messages recus. Base, par agent : le fait releve dans ses lignes de compaction (au moins un message d'agent garde)
    quand l'import l'a ecrit ; sinon, la correlation entre debut de fenetre et messages recus, avec assez de fenetres
    pour qu'elle dise quelque chose. Jamais additionnee a l'entree des requetes d'envoi, jamais convertie en gain."""
    user = ((cfg or {}).get("report") or {}).get("exchanges") or {}
    threshold = float(user.get("retained_min_correlation", RETAINED_MIN_CORRELATION))
    min_windows = int(user.get("retained_min_windows", RETAINED_MIN_WINDOWS))
    kept, basis_of, too_few = [], {}, []
    for r in rows:
        facts = r.get("compactions")
        if facts is not None:
            if any(f["kept_agent_messages"] > 0 for f in facts):
                kept.append(r)
                basis_of[r["agent"]] = "fait releve a la compaction"
        elif isinstance(r.get("correlation"), (int, float)) and r["correlation"] >= threshold:
            if r["windows_after_compaction"] >= min_windows:
                kept.append(r)
                basis_of[r["agent"]] = "correlation"
            else:
                too_few.append(r["agent"])
    # * Ce qui est RELEVE (messages gardes, comptes) reste a part de ce qui est ESTIME (tokens associes a leur conservation).
    with_fact = [r for r in rows if r.get("compactions")]
    return {"nature": "estimation", "min_correlation": threshold, "min_windows": min_windows, "agents": [r["agent"] for r in kept],
            "agent_basis": basis_of, "agents_with_too_few_windows": too_few,
            "agents_by_basis": dict(Counter(basis_of.values())),
            "kept_messages_fact": {"agents_with_fact": len(with_fact),
                                   "agents_without_fact": sum(1 for r in rows if r.get("compactions_observed") and not r.get("compactions")),
                                   # ! None quand aucun agent n'a ce fait : non importe, pas « zero message garde »
                                   "kept_at_last_compaction": (sum(r["compactions"][-1]["kept_agent_messages"] for r in with_fact)
                                                               if with_fact else None),
                                   "received_before_last_compaction": (sum(r["compactions"][-1]["received_before"] for r in with_fact)
                                                                       if with_fact else None)},
            "tokens": sum(int(r["window_start_surplus_tokens"]) for r in kept),
            "requests": sum(int(r["requests_in_these_windows"]) for r in kept),
            "basis": "surplus de la premiere requete de chaque fenetre par rapport a la premiere fenetre apres compaction, "
                     "multiplie par les requetes de la fenetre ; agents dont la compaction garde les messages recus (fait releve) "
                     "ou, a defaut, dont ce debut de fenetre suit le cumul des messages recus",
            "not_additive_with": "message_requests.message_only_input_tokens"}


def _shared_resources(view: SessionView, pm: dict[str, Any], st: dict[str, int]) -> dict[str, Any]:
    """Ressources touchees par plusieurs agents. `shared_reference` : lue par plusieurs, jamais modifiee dans la
    session ; `handoff` : lue par un agent apres qu'un AUTRE l'a modifiee en dernier ; `shared_write` : modifiee par
    plusieurs. Les tokens d'un appel qui lit plusieurs ressources sont partages a parts egales entre elles."""
    added = attribute_added(view)["added"]
    reads: dict[str, list[tuple[Call, float]]] = defaultdict(list)
    writes: dict[str, list[Call]] = defaultdict(list)
    labels: dict[str, str] = {}
    for c in view.calls:
        found = _resources(c, c.project_dir or view.project_dir)
        for key, label in found:
            reads[key].append((c, added.get(c.key, 0.0) / len(found)))
            labels[key] = label
        for key in _written(c, view.project_dir):
            writes[key].append(c)
            labels.setdefault(key, key[5:])
    arrivals: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in pm["received"]:
        arrivals[(pm["author_of"](r), _agent_of(r.agent_id))].append(r.ns)
    for L in arrivals.values():
        L.sort()

    reference: list[dict[str, Any]] = []
    routes: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"resources": set(), "reads": 0, "added_tokens": 0.0,
                                                                         "after_message": 0})
    handoffs: list[dict[str, Any]] = []
    for key, L in reads.items():
        L.sort(key=lambda ct: ct[0].order_ns)
        W = sorted(writes.get(key, []), key=lambda c: c.order_ns)
        agents = Counter(c.agent_key for c, _t in L)
        if not W and len(agents) >= 2:
            first = L[0][0].agent_key
            reference.append({"label": labels[key], "reads": dict(agents), "first_reader": first,
                              "reads_by_others": sum(1 for c, _t in L if c.agent_key != first),
                              "added_tokens": round(sum(t for _c, t in L)),
                              "added_tokens_by_others": round(sum(t for c, t in L if c.agent_key != first)),
                              "distinct_contents": len({c.content_fingerprint for c, _t in L if c.content_fingerprint}) or None,
                              "refs": [_ref(c) for c, _t in L[:6]]})
        if not W:
            continue
        w_times = [c.order_ns for c in W]
        for c, tokens in L:
            i = bisect.bisect_left(w_times, c.order_ns)
            if i == 0 or W[i - 1].agent_key == c.agent_key:
                continue
            author = W[i - 1]
            arr = arrivals.get((author.agent_key, c.agent_key), [])
            j = bisect.bisect_right(arr, author.order_ns)
            told = j < len(arr) and arr[j] <= c.order_ns
            row = routes[(author.agent_key, c.agent_key)]
            row["resources"].add(key)
            row["reads"] += 1
            row["added_tokens"] += tokens
            row["after_message"] += 1 if told else 0
            handoffs.append({"label": labels[key], "author": author.agent_key, "reader": c.agent_key,
                             "added_tokens": round(tokens), "message_between": told, "write": _ref(author), "read": _ref(c)})
    shared_write = []
    for key, W in writes.items():
        agents = Counter(c.agent_key for c in W)
        if len(agents) >= 2:
            W.sort(key=lambda c: c.order_ns)
            # * Suites d'ecritures consecutives d'un meme agent, sur TOUTES les ecritures : couper la liste des ecritures
            #   montrait un seul agent pour un fichier modifie par plusieurs (12 ecritures du premier, puis l'autre).
            runs: list[dict[str, Any]] = []
            for c in W:
                if runs and runs[-1]["agent"] == c.agent_key:
                    runs[-1].update({"last_seq": c.seq, "last_time": c.start_time, "writes": runs[-1]["writes"] + 1})
                else:
                    runs.append({"agent": c.agent_key, "first_seq": c.seq, "last_seq": c.seq, "first_time": c.start_time,
                                 "last_time": c.start_time, "writes": 1})
            shared_write.append({"label": labels[key], "writes": dict(agents),
                                 "reads": dict(Counter(c.agent_key for c, _t in reads.get(key, []))),
                                 "runs": runs[:12], "runs_total": len(runs)})
    reference.sort(key=lambda r: -r["added_tokens_by_others"])
    handoffs.sort(key=lambda r: -r["added_tokens"])
    total_added = sum(added.values())
    ref_tokens = sum(r["added_tokens_by_others"] for r in reference)
    hand_tokens = sum(r["added_tokens"] for r in routes.values())
    return {"added_tokens_measured": round(total_added),
            "shared_reference": {"resources": len(reference), "reads_by_others": sum(r["reads_by_others"] for r in reference),
                                 "added_tokens_by_others": ref_tokens,
                                 "share_of_added": round(ref_tokens / total_added, 4) if total_added else None,
                                 "top": reference[: st["top_resources"]]},
            "handoff": {"reads": sum(r["reads"] for r in routes.values()), "added_tokens": round(hand_tokens),
                        "share_of_added": round(hand_tokens / total_added, 4) if total_added else None,
                        "routes": [{"author": k[0], "reader": k[1], "resources": len(v["resources"]), "reads": v["reads"],
                                    "added_tokens": round(v["added_tokens"]), "reads_after_author_message": v["after_message"]}
                                   for k, v in sorted(routes.items(), key=lambda kv: -kv[1]["added_tokens"])],
                        "top": handoffs[: st["top_resources"]]},
            "shared_write": sorted(shared_write, key=lambda r: -sum(r["writes"].values()))[: st["top_resources"]],
            "shared_write_total": len(shared_write)}


def agent_exchanges(view: SessionView, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """None quand il n'y a rien entre agents a decrire : un seul agent ET aucun message d'agent envoye ni recu.

    # ! Une tranche de temps ou un seul fil travaille peut quand meme RECEVOIR des messages d'autres agents (constate le
    #   2026-09-21 : 4 messages entres, 0 envoye). Ces receptions sont gardees et « 0 envoye » est un fait.
    """
    st = _settings(cfg)
    pm = pair_messages(view)
    if len(view.agents) < 2 and not view.agent_infos and not pm["sent"] and not pm["received"]:
        return None
    limits = ["le texte des messages entre agents n'est pas lu (chiffre par le fournisseur ou garde en empreinte) : il reste "
              "semantiquement indetermine ; seuls l'emetteur, le destinataire, l'instant, la taille et le rapprochement sont connus",
              "« n'emet que des messages » ne dit pas « ne travaille pas » : le modele raisonne aussi dans ces requetes",
              "« message adresse a un autre agent apres une reception » ne dit pas que le meme contenu est relaye ; « premiere "
              "reponse apres l'entree d'un message » est un enchainement dans le temps, pas un lien de sens",
              "l'entree des requetes d'envoi (releve) et l'estimation du contexte conserve (calcul) se recouvrent : elles ne "
              "s'additionnent pas, et aucune n'est un gain",
              "une lecture apres l'ecriture d'un autre agent ne dit pas si elle etait necessaire"]
    if pm["received"] and not pm["with_fingerprint"]:
        limits.insert(0, "messages importes avant l'ecriture de l'empreinte du contenu transmis : aucun rapprochement "
                         "envoi/reception (reimporter la session pour l'obtenir)")
    resident = _resident(view, pm)
    return {"agents": len(view.agents), "messages": _messages(view, pm, st) if (pm["sent"] or pm["received"]) else None,
            "message_requests": _requests(view, pm, st), "resident_messages": resident,
            "retained_context_estimate": _retained_estimate(resident, cfg),
            "resources": _shared_resources(view, pm, st), "limits": limits}

