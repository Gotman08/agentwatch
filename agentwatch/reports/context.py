"""Contexte : ce que les sorties d'outils y ajoutent, combien de requetes les relisent, ce qui est relu apres chaque
compaction, et ce que la session a consomme du quota. Descriptif : aucun verdict, aucun signalement.

# * Pourquoi : chaque requete du modele relit tout son contexte. Une sortie de 10 000 tokens arrivee au debut d'une
#   fenetre de 70 requetes est relue 70 fois ; c'est la que part l'entree d'une longue session, pas dans l'appel.
#   Constate le 2026-09-21 (session Codex 01a0bf95, 646 M tokens d'entree) : 47 % de l'entree est la relecture de
#   sorties d'outils encore en contexte, et les 8 premieres reponses apres chacune des 71 compactions font entrer
#   26 % des tokens apportes par les sorties.
# * Mesure, pas estimation : les tokens qu'une sortie ajoute sont lus dans les releves par reponse du client,
#   entree(reponse qui consomme la sortie) - entree(reponse precedente) - sortie(reponse precedente), dans la meme
#   fenetre de contexte. Verifie sur 262 sorties de moins de 120 caracteres : ecart median de 24 tokens. Rien n'est
#   deduit d'octets : la taille brute d'une sortie ne dit pas ce que le modele recoit (sortie coupee par le client,
#   image encodee).
# ! Limites dites dans le resultat : un message entre entre les deux reponses est compte avec la sortie (`mixed`) ;
#   la premiere reponse d'une fenetre n'a pas de reponse precedente comparable (`unmeasured`) ; le partage entre
#   les sorties consommees par une meme reponse est un calcul (prorata deja fait a l'import), pas une mesure.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from agentwatch.core import schema as S
from agentwatch.core.correlate import Call, SessionView

DEFAULTS = {"recovery_responses": 8, "top_outputs": 10, "top_families": 12, "top_resources": 15}
_READ_HEADS = {"get-content", "gc", "cat", "type", "head", "tail", "bat"}
_ASSIGN_RE = re.compile(r"^\$[\w:]+\s*=\s*")
_INCOMING_ROLES = {"user", "agent"}


def _settings(cfg: dict[str, Any] | None) -> dict[str, int]:
    user = ((cfg or {}).get("report") or {}).get("context") or {}
    return {k: int(user.get(k, v)) for k, v in DEFAULTS.items()}


def _agent_of(agent_id: Any) -> str:
    return str(agent_id) if agent_id else "main"


def _responses(view: SessionView) -> dict[str, list[dict[str, Any]]]:
    """Releves par reponse de chaque agent, dans l'ordre des rangs (`index`)."""
    per: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for m in view.markers:
        u = m.meta.get("usage") if m.phase == S.PHASE_USAGE else None
        if not isinstance(u, dict) or u.get("scope") != "response" or not isinstance(u.get("index"), int):
            continue
        per[_agent_of(m.agent_id)][u["index"]] = {"index": u["index"], "window": int(u.get("window") or 0), "ns": m.ns,
                                                  "in": int(u.get("input_tokens") or 0), "out": int(u.get("output_tokens") or 0)}
    return {a: [d[i] for i in sorted(d)] for a, d in per.items()}


def _growth(resps: list[dict[str, Any]], incoming_ns: list[int]) -> dict[int, tuple[int, bool]]:
    """rang de reponse -> (tokens entres dans le contexte depuis la reponse precedente, un message est aussi entre)."""
    import bisect
    out: dict[int, tuple[int, bool]] = {}
    for prev, cur in zip(resps, resps[1:]):
        if cur["index"] != prev["index"] + 1 or cur["window"] != prev["window"]:
            continue
        added = cur["in"] - prev["in"] - prev["out"]
        if added <= 0:
            continue
        lo = bisect.bisect_right(incoming_ns, prev["ns"])
        out[cur["index"]] = (added, lo < len(incoming_ns) and incoming_ns[lo] <= cur["ns"])
    return out


def _heads(c: Call) -> list[Any]:
    heads = c.params.get("shell_heads")
    return heads if isinstance(heads, list) else []


def family(c: Call) -> str:
    """Famille d'outil lisible : `linear/get_issue`, `get-content`, `rg`, `script en ligne`..."""
    if c.mcp_server:
        return f"{c.mcp_server}/{c.mcp_tool}"
    if c.category != S.CAT_SHELL:
        return c.tool_name or "?"
    heads = _heads(c)
    head = _ASSIGN_RE.sub("", str(heads[0])) if heads else ""
    if not head or head.startswith(("@", "$", "&", "(", "[", "if", "foreach", "try")):
        return "shell : script compose"
    return f"shell : {head[:30]}"


def _norm_path(p: Any, project: str | None) -> str | None:
    if not isinstance(p, str) or not p.strip():
        return None
    s = p.strip().strip("'\"").replace("\\", "/").lower()
    root = (project or "").replace("\\", "/").lower().rstrip("/")
    if root and s.startswith(root + "/"):
        s = s[len(root) + 1:]
    return s[2:] if s.startswith("./") else s


def _resources(c: Call, project: str | None) -> list[tuple[str, str]]:
    """Ce que l'appel a lu, en ressources comparables d'une fenetre a l'autre : (cle, etiquette)."""
    if c.category == S.CAT_SHELL:
        if any(_ASSIGN_RE.sub("", str(h)) in _READ_HEADS for h in _heads(c)):
            paths = [_norm_path(p, project) for p in c.params.get("shell_paths") or []]
            found = sorted({p for p in paths if p})
            if found:
                return [(f"file:{p}", p) for p in found]
    if c.op == "read" and c.op_target:
        p = _norm_path(c.op_target, project)
        return [(f"file:{p}", p)] if p else []
    if c.op == "mcp_read" and c.op_key:
        return [(f"mcp:{c.op_key}", c.label or c.op_key)]
    return []


def _written(c: Call, project: str | None) -> list[str]:
    """Ressources (cles `file:`) que l'appel a modifiees, d'apres ses chemins observes."""
    if not c.is_write_like:
        return []
    paths = list(c.params.get("patch_paths") or []) if isinstance(c.params.get("patch_paths"), list) else []
    if not paths and c.target_kind == "path":
        paths = [c.target]
    found = (_norm_path(p, c.project_dir or project) for p in paths)
    return sorted({f"file:{n}" for n in found if n})


def _writes(view: SessionView) -> dict[str, list[int]]:
    """chemin -> instants des modifications observees, tous agents confondus."""
    out: dict[str, list[int]] = defaultdict(list)
    for c in view.calls:
        for key in _written(c, view.project_dir):
            out[key].append(c.order_ns)
    return out


def _ref(c: Call) -> dict[str, Any]:
    src = c.evidence.get("source_end") or c.evidence.get("source_start") or {}
    return {"seq": c.seq, "tool": c.tool_name, "label": c.label, "agent": c.agent_key, "window": c.context_epoch,
            "start_time": c.start_time, "source": {k: src.get(k) for k in ("file", "line")} if src else None}


def limits(view: SessionView) -> dict[str, Any]:
    """Quota du compte au fil de la session et tours coupes par le client (jamais des travaux termines)."""
    quota = sorted((m for m in view.markers if m.phase == S.PHASE_ACTIVITY and m.meta.get("kind") == "quota"
                    and isinstance(m.meta.get("used_percent"), (int, float))), key=lambda m: m.ns)
    cut = [m for m in view.markers if m.phase == S.PHASE_TURN_END and m.meta.get("error_kind")]
    out: dict[str, Any] = {"quota": None,
                           "turns_cut": {"total": len(cut), "by_kind": dict(Counter(str(m.meta["error_kind"]) for m in cut)),
                                         "agents": sorted({_agent_of(m.agent_id) for m in cut}),
                                         "last_time": max((m.time for m in cut if m.time), default=None)}}
    if quota:
        first, last = quota[0], quota[-1]
        out["quota"] = {"first_percent": first.meta["used_percent"], "first_time": first.time,
                        "last_percent": last.meta["used_percent"], "last_time": last.time,
                        "max_percent": max(m.meta["used_percent"] for m in quota),
                        "window_minutes": last.meta.get("window_minutes"), "resets_at": last.meta.get("resets_at"),
                        "readings": len(quota)}
    return out


def _window_index(resps: dict[str, list[dict[str, Any]]]) -> tuple[dict[tuple[str, int], int], dict[tuple[str, int], int],
                                                                   dict[tuple[str, int], int]]:
    """(premier rang de chaque fenetre, dernier rang, fenetre de chaque rang), par agent."""
    last_of: dict[tuple[str, int], int] = {}
    first_of: dict[tuple[str, int], int] = {}
    window_of: dict[tuple[str, int], int] = {}
    for a, L in resps.items():
        for r in L:
            last_of[(a, r["window"])] = r["index"]
            first_of.setdefault((a, r["window"]), r["index"])
            window_of[(a, r["index"])] = r["window"]
    return first_of, last_of, window_of


def attribute_added(view: SessionView, resps: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Tokens que chaque sortie ajoute au contexte : le gain mesure de la reponse qui la consomme, partage au prorata
    des parts deja calculees a l'import (taille des sorties), a parts egales si elles sont nulles. Cle d'appel ->
    tokens (`added`), requetes suivantes de la fenetre (`resid`), rang dans la fenetre (`rank`)."""
    resps = _responses(view) if resps is None else resps
    incoming: dict[str, list[int]] = defaultdict(list)
    for m in view.markers:
        if m.phase == S.PHASE_MESSAGE and m.meta.get("role") in _INCOMING_ROLES:
            incoming[_agent_of(m.agent_id)].append(m.ns)
    growth = {a: _growth(L, sorted(incoming.get(a, []))) for a, L in resps.items()}
    first_of, last_of, window_of = _window_index(resps)
    by_consumer: dict[tuple[str, int], list[Call]] = defaultdict(list)
    no_usage = 0
    for c in view.calls:
        idx = (c.usage or {}).get("consumer_index")
        if isinstance(idx, int):
            by_consumer[(c.agent_key, idx)].append(c)
        else:
            no_usage += 1
    added: dict[str, float] = {}
    resid: dict[str, int] = {}
    rank: dict[str, int] = {}
    unmeasured = mixed_calls = 0
    mixed_tokens = 0.0
    for (a, idx), group in by_consumer.items():
        g = growth.get(a, {}).get(idx)
        if g is None:
            unmeasured += len(group)
            continue
        weights = [max(0, int((c.usage or {}).get("uncached_input_tokens") or 0)) for c in group]
        total_w = sum(weights)
        window = window_of.get((a, idx), 0)
        for c, w in zip(group, weights):
            added[c.key] = g[0] * (w / total_w if total_w else 1 / len(group))
            resid[c.key] = max(0, last_of.get((a, window), idx) - idx)
            rank[c.key] = idx - first_of.get((a, window), idx)
            if g[1]:
                mixed_calls += 1
                mixed_tokens += added[c.key]
    return {"added": added, "resid": resid, "rank": rank, "unmeasured": unmeasured, "no_usage": no_usage,
            "mixed_calls": mixed_calls, "mixed_tokens": mixed_tokens}


def context_costs(view: SessionView, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """None si la session n'a aucun releve par reponse (hooks seuls, transcript non importe)."""
    resps = _responses(view)
    if not resps:
        return None
    st = _settings(cfg)
    first_of, _last_of, _window_of = _window_index(resps)
    session_input = sum(r["in"] for L in resps.values() for r in L)
    # * Socle : ce que relit la premiere requete d'un fil (instructions, outils, skills, consigne) puis la premiere de
    #   chaque fenetre (socle + resume de compaction). Relu par TOUTES les requetes : le reduire se mesure ici.
    by_index = {a: {r["index"]: r for r in L} for a, L in resps.items()}
    # ! Tranche de temps (`report --day`, `compare`) : un fil ou une fenetre commences AVANT la tranche n'y montrent
    #   pas leur premiere requete. Le socle n'est releve que pour un fil dont la requete de rang 0 est dans la vue, et
    #   la premiere fenetre visible d'un fil entame (`partial`) est ecartee des mesures de debut de fenetre : sinon une
    #   requete de milieu de fenetre (130 000 tokens) passait pour un socle.
    thread_first = {a: L[0]["in"] for a, L in resps.items() if L and L[0]["index"] == 0}
    partial = {(a, L[0]["window"]) for a, L in resps.items() if L and L[0]["index"] != 0}
    window_first = sorted(by_index[a][i]["in"] for (a, w), i in first_of.items() if w > 0 and (a, w) not in partial)
    floor_reread = sum(thread_first[a] * len(resps[a]) for a in thread_first)
    floor_basis = sum(r["in"] for a in thread_first for r in resps[a])
    floor = {"thread_first_input": thread_first, "window_first_inputs": window_first,
             "thread_first_reread_tokens": floor_reread, "thread_first_basis_input_tokens": floor_basis,
             "threads_started_before_view": len(resps) - len(thread_first),
             "window_first_input_median": round(median(window_first)) if window_first else None,
             "thread_first_share_of_session_input": round(floor_reread / floor_basis, 4) if floor_basis else None}

    # * Appels groupes par reponse consommatrice : voir `attribute_added` (partage aussi utilise entre agents).
    attr = attribute_added(view, resps)
    added, resid, rank = attr["added"], attr["resid"], attr["rank"]
    unmeasured, no_usage = attr["unmeasured"], attr["no_usage"]
    mixed_calls, mixed_tokens = attr["mixed_calls"], attr["mixed_tokens"]
    measured = [c for c in view.calls if c.key in added]
    total_added = sum(added.values())
    total_reads = sum(added[c.key] * resid[c.key] for c in measured)

    fam: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "added": 0.0, "reads": 0.0, "truncated": 0})
    for c in measured:
        row = fam[family(c)]
        row["calls"] += 1
        row["added"] += added[c.key]
        row["reads"] += added[c.key] * resid[c.key]
        row["truncated"] += 1 if c.output_truncated else 0
    families = [{"family": k, "calls": v["calls"], "added_tokens": round(v["added"]), "reread_tokens": round(v["reads"]),
                 "share_of_session_input": round(v["reads"] / session_input, 4) if session_input else None,
                 "truncated_calls": v["truncated"]}
                for k, v in sorted(fam.items(), key=lambda kv: -kv[1]["reads"])[: st["top_families"]]]
    top = sorted(measured, key=lambda c: -(added[c.key] * resid[c.key]))[: st["top_outputs"]]
    outputs = [{**_ref(c), "added_tokens": round(added[c.key]), "later_requests_in_window": resid[c.key],
                "reread_tokens": round(added[c.key] * resid[c.key]), "output_size_bytes": c.output_size_bytes,
                "delivered_chars": c.evidence.get("delivered_chars"), "truncated": c.output_truncated,
                "original_token_count": c.evidence.get("original_token_count")} for c in top]

    # * Ce que le client a coupe avant de le donner au modele (releve depuis l'import qui lit `formatted_output`).
    delivery_known = [c for c in view.calls if "delivered_chars" in c.evidence or "exec_delivered_chars" in c.evidence]
    cut_calls = [c for c in view.calls if c.output_truncated]
    cut_execs: dict[str, int] = {}
    for c in view.calls:
        n = c.evidence.get("exec_truncated_tokens")
        if isinstance(n, int) and c.evidence.get("exec_call_id"):
            cut_execs[f"{c.agent_key}|{c.evidence['exec_call_id']}"] = n
    truncation = {"known": bool(delivery_known), "calls_with_delivery_facts": len(delivery_known),
                  "truncated_calls": len(cut_calls),
                  "original_tokens_of_truncated_calls": sum(int(c.evidence.get("original_token_count") or 0) for c in cut_calls),
                  "added_tokens_of_truncated_calls": round(sum(added.get(c.key, 0.0) for c in cut_calls)),
                  "execs_cut_in_the_middle": len(cut_execs), "tokens_cut_from_execs": sum(cut_execs.values())}

    return {"basis": "entree(reponse consommatrice) - entree(reponse precedente) - sortie(reponse precedente), meme fenetre ; "
                     "releves par reponse ecrits par le client",
            "session_input_tokens": session_input, "responses": sum(len(L) for L in resps.values()), "floor": floor,
            "windows_after_compaction": sum(1 for key in first_of if key[1] > 0 and key not in partial),
            "calls_measured": len(measured), "calls_unmeasured": unmeasured, "calls_without_usage": no_usage,
            "added_tokens": round(total_added), "reread_tokens": round(total_reads),
            "reread_share_of_session_input": round(total_reads / session_input, 4) if session_input else None,
            "mixed": {"calls": mixed_calls, "added_tokens": round(mixed_tokens)},
            "families": families, "top_outputs": outputs, "truncation": truncation,
            "after_compaction": _after_compaction(view, measured, added, resid, rank, st, total_added, partial),
            "limits": limits(view)}


def _after_compaction(view: SessionView, measured: list[Call], added: dict[str, float], resid: dict[str, int],
                      rank: dict[str, int], st: dict[str, int], total_added: float,
                      partial: set[tuple[str, int]] | None = None) -> dict[str, Any]:
    """Ce qu'un agent relit dans une fenetre ulterieure alors qu'il l'avait deja lu : attendu apres une compaction, mais
    chiffrable, et ce qui revient a chaque fenetre se rend durable ou plus court."""
    writes = _writes(view)
    project = view.project_dir
    last_read: dict[tuple[str, str], Call] = {}
    single: dict[str, bool] = {}
    rows: dict[str, dict[str, Any]] = {}
    windows_hit: set[tuple[str, int]] = set()
    status_tot: dict[str, dict[str, float]] = defaultdict(lambda: {"rereads": 0, "added": 0.0, "reads": 0.0})
    early = {"rereads": 0, "added": 0.0}
    for c in sorted(measured, key=lambda c: c.order_ns):
        res = _resources(c, c.project_dir or project)
        single[c.key] = len(res) == 1
        for key, label in res:
            prev = last_read.get((c.agent_key, key))
            last_read[(c.agent_key, key)] = c
            if prev is None or c.context_epoch <= prev.context_epoch:
                continue
            if any(prev.order_ns < t < c.order_ns for t in writes.get(key, [])):
                status = "modified_between"
            elif single[c.key] and single.get(prev.key) and c.content_fingerprint and prev.content_fingerprint:
                status = "identical" if c.content_fingerprint == prev.content_fingerprint else "different_or_other_excerpt"
            else:
                status = "no_change_observed"
            tok = added[c.key] / len(res)
            row = rows.setdefault(key, {"resource": label, "rereads": 0, "agents": set(), "windows": set(), "added": 0.0,
                                        "reads": 0.0, "status": Counter(), "refs": []})
            row["rereads"] += 1
            windows_hit.add((c.agent_key, c.context_epoch))
            row["agents"].add(c.agent_key)
            row["windows"].add((c.agent_key, c.context_epoch))
            row["added"] += tok
            row["reads"] += tok * resid[c.key]
            row["status"][status] += 1
            if len(row["refs"]) < 5:
                row["refs"].append(_ref(c))
            tot = status_tot[status]
            tot["rereads"] += 1
            tot["added"] += tok
            tot["reads"] += tok * resid[c.key]
            if rank[c.key] <= st["recovery_responses"] and (c.agent_key, c.context_epoch) not in (partial or ()):
                early["rereads"] += 1
                early["added"] += tok
    # * Debut de fenetre : tout ce que les sorties font entrer dans les premieres reponses apres une compaction.
    per_window: dict[tuple[str, int], float] = defaultdict(float)
    for c in measured:
        if c.context_epoch > 0 and rank[c.key] <= st["recovery_responses"] and (c.agent_key, c.context_epoch) not in (partial or ()):
            per_window[(c.agent_key, c.context_epoch)] += added[c.key]
    vals = sorted(per_window.values())
    resources = [{"resource": r["resource"], "rereads": r["rereads"], "agents": sorted(r["agents"]), "windows": len(r["windows"]),
                  "added_tokens": round(r["added"]), "reread_tokens": round(r["reads"]), "status": dict(r["status"]),
                  "refs": r["refs"]}
                 for r in sorted(rows.values(), key=lambda r: -r["added"])[: st["top_resources"]]]
    return {"recovery_responses": st["recovery_responses"],
            "rereads": sum(int(v["rereads"]) for v in status_tot.values()),
            "windows_with_rereads": len(windows_hit),
            "added_tokens": round(sum(v["added"] for v in status_tot.values())),
            "reread_tokens": round(sum(v["reads"] for v in status_tot.values())),
            "by_status": {k: {"rereads": int(v["rereads"]), "added_tokens": round(v["added"]), "reread_tokens": round(v["reads"])}
                          for k, v in status_tot.items()},
            "early": {"rereads": early["rereads"], "added_tokens": round(early["added"])},
            "window_start": {"windows": len(vals), "added_tokens_total": round(sum(vals)), "added_tokens_by_window": [round(v) for v in vals],
                             "added_tokens_median": round(median(vals)) if vals else None,
                             "added_tokens_p90": round(vals[(9 * len(vals)) // 10]) if vals else None,
                             "share_of_added_tokens": round(sum(vals) / total_added, 4) if total_added else None},
            "resources": resources}
