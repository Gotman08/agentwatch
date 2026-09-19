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
    responses = [m.meta["usage"] for m in view.markers if m.phase == S.PHASE_USAGE and isinstance(m.meta.get("usage"), dict)
                 and m.meta["usage"].get("scope") == "response"]
    counts["erreurs"] = sum(1 for c in view.calls if c.status in _FAILED)
    # * Occasions d'attendre : sans attente, une habitude d'attente disparait faute d'occasion, pas grace a une correction.
    counts["attentes"] = sum(1 for c in view.calls if c.agent_key not in view.timing_unreliable_agents
                             and G._WAIT_NAME.search(c.mcp_tool or c.tool_name or ""))
    clusters["erreurs"] = counts["erreurs"]          # * un appel en erreur n'est pas groupe : grappe = appel
    # * Taches : un tour du fil principal (demande de l'utilisateur) ou d'un sous-agent (tache confiee) ; termine
    #   (task_complete, avec sa duree) ou interrompu (turn_aborted).
    tasks: dict[str, Any] = {"main_done": 0, "main_aborted": 0, "sub_done": 0, "sub_aborted": 0,
                             "main_durations_ms": [], "sub_durations_ms": []}
    for m in view.markers:
        who = "sub" if m.agent_id else "main"
        if m.phase == S.PHASE_TURN_END:
            tasks[f"{who}_done"] += 1
            d = m.meta.get("duration_ms")
            if isinstance(d, (int, float)) and not isinstance(d, bool):
                tasks[f"{who}_durations_ms"].append(int(d))
        elif m.phase == S.PHASE_INTERRUPT:
            tasks[f"{who}_aborted"] += 1
    return {"calls": len(view.calls), "responses": len(responses),
            "input_tokens": sum(int(u.get("input_tokens") or 0) for u in responses),
            "cached_input_tokens": sum(int(u.get("cached_input_tokens") or 0) for u in responses),
            "output_tokens": sum(int(u.get("output_tokens") or 0) for u in responses),
            "reasoning_output_tokens": sum(int(u.get("reasoning_output_tokens") or 0) for u in responses),
            "counts": dict(counts), "clusters": dict(clusters), "ctx": dict(ctx), "sessions": 1 if view.calls else 0, "tasks": tasks,
            "nogain_cost": {k: dict(v) for k, v in nogain_cost.items()},
            "session_ids": [view.session_id] if view.calls else [],
            "first_time": view.first_time, "last_time": view.last_time}


_SUMS = ("calls", "responses", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "sessions")


def merge(measures: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {**{k: 0 for k in _SUMS}, "counts": Counter(), "clusters": Counter(), "ctx": Counter(),
                           "first_time": None, "last_time": None,
                           "nogain_cost": {"G.sans_apport": Counter(), "G.sans_apport_attente": Counter()},
                           "session_ids": [], "tasks": {"main_done": 0, "main_aborted": 0, "sub_done": 0, "sub_aborted": 0,
                                                        "main_durations_ms": [], "sub_durations_ms": []}}
    for m in measures:
        for k in _SUMS:
            out[k] += m.get(k, 0)
        out["counts"].update(m["counts"])
        out["clusters"].update(m.get("clusters") or m["counts"])
        out["ctx"].update(m["ctx"])
        out["session_ids"] += m.get("session_ids", [])
        for k, v in (m.get("nogain_cost") or {}).items():
            out["nogain_cost"].setdefault(k, Counter()).update(v)
        for k, v in (m.get("tasks") or {}).items():
            out["tasks"][k] = out["tasks"][k] + v
        if m["first_time"] and (out["first_time"] is None or m["first_time"] < out["first_time"]):
            out["first_time"] = m["first_time"]
        if m["last_time"] and (out["last_time"] is None or m["last_time"] > out["last_time"]):
            out["last_time"] = m["last_time"]
    out["counts"], out["clusters"], out["ctx"] = dict(out["counts"]), dict(out["clusters"]), dict(out["ctx"])
    out["nogain_cost"] = {k: dict(v) for k, v in out["nogain_cost"].items()}
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
}


def compare(before: dict[str, Any], after: dict[str, Any], top_habits: int = 8) -> dict[str, Any]:
    """Lignes de comparaison : mesures globales puis habitudes G les plus frequentes."""
    rows: list[dict[str, Any]] = []
    keys = ["G.sans_apport", "G.sans_apport_attente", "G.ameliorable", "A.relectures", "B.echecs_en_boucle",
            "E.service_a_la_main", "erreurs"]
    habits = Counter({k: before["counts"].get(k, 0) + after["counts"].get(k, 0) for k in
                      set(before["counts"]) | set(after["counts"]) if k.startswith("G.") and "|" in k})
    keys += [k for k, n in habits.most_common(top_habits) if n >= MIN_EVENTS // 2]
    for k in keys:
        per_response = k.startswith("G.") and before["responses"] and after["responses"]
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
    for who, label in (("main", "taches du fil principal terminees (sur terminees + interrompues)"),
                       ("sub", "taches des sous-agents terminees (sur terminees + interrompues)")):
        tb, ta = before["tasks"], after["tasks"]
        k1, n1 = tb[f"{who}_done"], tb[f"{who}_done"] + tb[f"{who}_aborted"]
        k2, n2 = ta[f"{who}_done"], ta[f"{who}_done"] + ta[f"{who}_aborted"]
        ci, concl = _proportions(k1, n1, k2, n2)
        if concl.startswith("hausse"):
            concl += " : amelioration"
        elif concl.startswith("baisse"):
            concl += " : degradation"
        rows.append({"key": f"taches.{who}", "label": label, "unit": "part des taches",
                     "before": {"count": k1, "activity": n1, "rate": 100 * k1 / n1 if n1 else None},
                     "after": {"count": k2, "activity": n2, "rate": 100 * k2 / n2 if n2 else None},
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
             f"- Enregistre le {ref['created_at']} par AgentWatch {ref['agentwatch_version']} (comparaison v{ref['compare_version']})", "",
             "## 1. Mesures (constatees sur la periode)", "",
             "| Mesure | Valeur |", "|---|---|",
             f"| Reponses du modele | {_n(m['responses'])} |",
             f"| Appels d'outils | {_n(m['calls'])} |",
             f"| Tokens d'entree | {_n(m['input_tokens'])}, dont en cache {_n(m['cached_input_tokens'])} "
             f"({_fmt(100 * m['cached_input_tokens'] / m['input_tokens'] if m['input_tokens'] else None)} %), hors cache "
             f"{_n(m['input_tokens'] - m['cached_input_tokens'])} |",
             f"| Tokens de sortie | {_n(m['output_tokens'])}, dont raisonnement {_n(m['reasoning_output_tokens'])} |",
             f"| Reprises du modele sans apport (resultat inchange) | {_n(m['counts'].get('G.sans_apport', 0))}, soit "
             f"{_fmt(per_r('G.sans_apport'))} pour 1 000 reponses |",
             f"| dont pendant une attente | {_n(m['counts'].get('G.sans_apport_attente', 0))}, soit "
             f"{_fmt(per_r('G.sans_apport_attente'))} pour 1 000 reponses |",
             f"| Cout mesure de ces reponses d'attente sans apport | {_n(nga.get('responses'))} reponses : entree "
             f"{_n(nga.get('input_tokens'))} (dont cache {_n(nga.get('cached_input_tokens'))}), sortie {_n(nga.get('output_tokens'))} |",
             f"| Cout mesure de toutes les reponses sans apport | {_n(ng.get('responses'))} reponses : entree "
             f"{_n(ng.get('input_tokens'))} (dont cache {_n(ng.get('cached_input_tokens'))}), sortie {_n(ng.get('output_tokens'))} |",
             f"| Taches du fil principal | {t['main_done']} terminees, {t['main_aborted']} interrompues ; duree mediane "
             f"{_fmt((_median(t['main_durations_ms']) or 0) / 1000 if t['main_durations_ms'] else None)} s |",
             f"| Taches des sous-agents | {t['sub_done']} terminees, {t['sub_aborted']} interrompues ; duree mediane "
             f"{_fmt((_median(t['sub_durations_ms']) or 0) / 1000 if t['sub_durations_ms'] else None)} s |",
             f"| Appels en erreur | {_n(m['counts'].get('erreurs', 0))} |", ""]
    if habits:
        lines += ["Reprises par habitude (G, allers-retours du modele) :", ""]
        lines += [f"- {k[2:].replace('|', ' : ')} : {_n(v)} ({_fmt(1000 * v / m['responses'] if m['responses'] else None)} pour 1 000 reponses)"
                  for k, v in habits]
        lines.append("")
    lines += ["## 2. Economies estimees (hypotheses, pas des gains)", "",
              f"- Si chaque reprise d'attente sans apport etait evitee, la borne haute de l'economie serait de "
              f"{_n(nga.get('responses'))} reponses du modele, soit {_n(nga.get('input_tokens'))} tokens d'entree relus (dont "
              f"{_n(nga.get('cached_input_tokens'))} en cache) et {_n(nga.get('output_tokens'))} de sortie, sur la periode.",
              "- C'est une borne haute : une attente plus longue coute encore une reponse par evenement attendu, et une partie "
              "des reprises peut rester necessaire (verification utile, coordination).",
              "- Hypothese de l'ajout a AGENTS.md : moins de reprises sans apport pendant les attentes, a reussite des taches egale.",
              "", "## 3. Gains constates", "",
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
             f"({b['first_time'] or '?'} a {b['last_time'] or '?'})",
             f"- Apres : {a['sessions']} session(s), {a['calls']} appels, {a['responses']} reponses du modele "
             f"({a['first_time'] or '?'} a {a['last_time'] or '?'})",
             f"- Methode : {result['comparison']['method']}", "",
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
        lines.append(f"| {r['label']} | {unit} | {_fmt(r['before']['rate'])}{' %' if r.get('percent') else ''} ({r['before']['count']}{cb}) | "
                     f"{_fmt(r['after']['rate'])}{' %' if r.get('percent') else ''} ({r['after']['count']}{ca}) | {ratio} | **{r['conclusion']}** |")
    lines += ["", "## Mesures descriptives (sans test : a lire, pas a conclure)", "",
              "| Mesure | Avant | Apres |", "|---|---|---|"]
    for d in result["comparison"]["descriptive"]:
        lines.append(f"| {d['label']} | {_fmt(d['before'])} | {_fmt(d['after'])} |")
    lines += ["", "Contexte relu pour decider les reprises ameliorables (tokens, en grande partie en cache) : avant "
              f"{_fmt((b['ctx'].get('G.ameliorable') or 0) / 1e6, 1)} M, apres {_fmt((a['ctx'].get('G.ameliorable') or 0) / 1e6, 1)} M.",
              "", result["comparison"]["caveat"], ""]
    return "\n".join(lines)
