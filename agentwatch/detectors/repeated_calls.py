"""Detecteur G : appels repetes, pourquoi ils sont refaits, a quel rythme, et ce qu'on peut y gagner.

Un groupe = un meme appel (meme agent, meme outil, meme cible, memes parametres ; delais d'attente et tailles de
sortie exclus) fait au moins `min_calls` fois. Tous les outils : shell, MCP, fonctions (attente d'un sous-agent),
lectures. Chaque reprise (appel precedent, appel suivant) recoit :

- une RAISON OBSERVEE, la premiere qui s'applique :
  `same_response` (dans la meme reponse du modele : boucle de script ou appels paralleles, aucun aller-retour),
  `context_loss` (compaction ou reprise de CET agent entre les deux), `new_input` (message de l'utilisateur ou
  d'un autre agent, nouveau tour), `after_change` (ecriture sur la cible, action sur le meme serveur MCP),
  `unavailable` (le resultat precedent disait le service indisponible), `after_failure` (le precedent a echoue),
  `waiting` (le precedent disait "en cours", ou l'attente est arrivee a echeance), `possible_change` (action a
  effet inconnu du meme agent entre les deux), `none` (rien d'observe) ;
- la RAISON DECLAREE par l'agent dans ses commentaires entre les deux appels (categories fixes : retry, wait,
  unavailable, in_progress, verify, after_change, fix, explore ; import des rollouts Codex) ;
- son APPORT : etat different (information nouvelle), phase differente (decision possible), ou identique ;
- son INTERVALLE depuis l'appel precedent (debut a debut) et le temps mort (fin a debut).

Verdict par groupe, sur les reprises qui ont coute un aller-retour du modele :
- `agent` : reprises sans raison ni apport (l'agent redemande ce qu'il sait deja), ou sondage avec un outil
  d'etat alors qu'un outil d'attente du meme serveur sert ailleurs dans la session ;
- `outil` : sondage d'un traitement en cours, ou attente qui arrive a echeance : une attente bloquante, un delai
  plus long ou une cadence minimale imposee par l'outil feraient le meme travail en moins d'appels ;
- `environnement` : le service etait indisponible ; reessayer etait legitime, seule la cadence se discute ;
- `echec` : reprises apres echec (boucles d'erreur : detecteur B) ;
- `justifie` : reprises expliquees (modification, nouvelle consigne, perte de contexte) ou qui ont appris
  quelque chose ; `gratuit` : repetitions dans une meme reponse (aucun aller-retour) ;
- `indetermine` : empreintes d'etat manquantes.
La cadence est simulee : pour chaque delai minimal candidat (10 s a 10 min), appels evites et retard ajoute a la
detection de chaque changement de phase observe (en cours -> termine, indisponible -> disponible).

# ! Rien n'est invente : une raison non observee reste `none`, un apport sans empreinte reste inconnu, et la
#   raison declaree n'est qu'une categorie reconnue par motifs dans un texte jamais conserve.
# * Partage des roles : les lectures locales redondantes restent au detecteur A, les boucles d'echec au
#   detecteur B. G les montre dans son tableau (pourquoi, a quel rythme) sans les signaler une seconde fois.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from agentwatch.core import intent as I
from agentwatch.core import schema as S
from agentwatch.core.correlate import Call, SessionView
from agentwatch.detectors import base as B
from agentwatch.detectors.redundant_reads import _touches

RULE_ID = "G.repeated_calls"
RULE_VERSION = "1.0"
CACHE_KEY = "repeated_calls"

REASONS = ("same_response", "context_loss", "new_input", "after_change", "unavailable", "after_failure", "waiting",
           "possible_change", "none")
REASON_LABELS = {
    "same_response": "meme reponse", "context_loss": "contexte perdu", "new_input": "nouvelle consigne",
    "after_change": "apres modification", "unavailable": "service indisponible", "after_failure": "apres echec",
    "waiting": "attente en cours", "possible_change": "action inconnue entre deux", "none": "sans raison observee",
}
DECLARED_LABELS = {"retry": "reessai", "wait": "attente", "unavailable": "indisponible", "in_progress": "en cours",
                   "verify": "verification", "after_change": "apres modification", "fix": "correction", "explore": "exploration"}
VERDICT_LABELS = {
    "agent": "ameliorable : agent", "outil": "ameliorable : outil", "environnement": "environnement (pas l'agent)",
    "echec": "apres echec (voir B)", "justifie": "justifie", "gratuit": "sans aller-retour", "indetermine": "indetermine",
}
IMPROVABLE = ("agent", "outil")
_FAILED = {S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED}
_WAIT_NAME = re.compile(r"(?i)(?:^|[_.])(?:wait|await|poll|watch|sleep)")
_READ_NAME = re.compile(r"(?i)(?:^|[_.])(?:list|get|read|status|show|info)")
_TIMEOUT_PARAMS = ("timeout", "timeout_ms", "timeout_s", "timeout_sec", "timeout_seconds", "max_wait", "max_wait_s",
                   "max_wait_seconds", "wait_s", "wait_seconds", "yield_time_ms", "poll_interval", "interval")
_DEFAULT_COOLDOWNS = (10, 30, 60, 120, 300, 600)


# ---------------------------------------------------------------------------- petits outils
def group_key(c: Call) -> str:
    return "\x1f".join((c.agent_key, str(c.tool_name), str(c.target_key), c.params_key))


def phase_of(c: Call) -> str:
    """Phase du resultat : unavailable | in_progress | failed | done | unknown (faits d'ingestion, sinon statut)."""
    ph = c.evidence.get("result_phase")
    if ph == "unavailable":
        return ph
    if c.evidence.get("error_hint") and c.error_signature:
        return "failed"
    if ph in ("in_progress", "failed", "done"):
        return str(ph)
    if c.evidence.get("timed_out") is True:
        return "in_progress"
    if c.status in _FAILED:
        return "failed"
    if c.status == S.STATUS_SUCCESS:
        return "done"
    return "unknown"


def _state(c: Call) -> tuple[str, str] | None:
    """Empreinte comparable du resultat : etat (horodatages neutralises), sinon contenu obtenu. Jamais la reponse
    complete : elle porte la duree de l'appel et differerait sans que rien n'ait change."""
    sf = c.evidence.get("state_fp")
    if isinstance(sf, str) and sf:
        return "state", sf
    if c.content_fingerprint:
        return "content", c.content_fingerprint
    return None


def _namespace(tool: str | None) -> str | None:
    if not tool or tool.startswith("mcp__"):
        return None
    return tool.split(".", 1)[0] if "." in tool else None


def _q(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def _max_in_window(ns: list[int], width_ns: int) -> int:
    best, j = 0, 0
    for i in range(len(ns)):
        while ns[i] - ns[j] > width_ns:
            j += 1
        best = max(best, i - j + 1)
    return best


def fmt_duration(s: float | None) -> str:
    """Duree lisible : 850 ms, 12 s, 3 min 20 s, 1 h 05."""
    if s is None:
        return "?"
    if s < 1:
        return f"{int(round(s * 1000))} ms"
    if s < 60:
        return f"{s:.0f} s" if s >= 10 else f"{s:.1f} s"
    if s < 3600:
        m, r = divmod(int(round(s)), 60)
        return f"{m} min {r:02d} s" if r else f"{m} min"
    h, r = divmod(int(round(s)), 3600)
    return f"{h} h {r // 60:02d}"


# ---------------------------------------------------------------------------- contexte entre deux appels
class _Context:
    """Index de ce qui s'est passe entre deux appels : consignes, commentaires de l'agent, autres appels."""

    def __init__(self, view: SessionView, cfg: dict[str, Any]) -> None:
        self.calls = view.calls
        self.starts = [c.order_ns for c in view.calls]
        self.case_insensitive = bool(cfg.get("case_insensitive_paths", False))
        self.inputs: dict[str, list[int]] = defaultdict(list)
        self.declared: dict[str, list[tuple[int, list[str]]]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        for m in view.markers:
            agent = m.agent_id or "main"
            if m.phase == S.PHASE_MESSAGE:
                mid = m.meta.get("message_id")
                if isinstance(mid, str):
                    if (agent, mid) in seen:
                        continue     # * copie de l'historique (sous-agent) ou reimport : un seul message
                    seen.add((agent, mid))
                role = m.meta.get("role")
                if role in ("user", "agent") and (m.meta.get("chars") or 0) > 0:
                    self.inputs[agent].append(m.ns)
                elif role == "assistant" and m.meta.get("declared"):
                    self.declared[agent].append((m.ns, [str(x) for x in m.meta["declared"]]))
            elif m.phase == S.PHASE_TURN_START:
                self.inputs[agent].append(m.ns)
        for v in self.inputs.values():
            v.sort()
        for d in self.declared.values():
            d.sort(key=lambda t: t[0])
        self.declared_ns = {a: [t[0] for t in d] for a, d in self.declared.items()}

    def new_input(self, agent: str, lo: int, hi: int) -> bool:
        arr = self.inputs.get(agent) or []
        i = bisect.bisect_right(arr, lo)
        return i < len(arr) and arr[i] <= hi

    def declared_between(self, agent: str, lo: int, hi: int) -> list[str]:
        arr = self.declared_ns.get(agent) or []
        i, j = bisect.bisect_right(arr, lo), bisect.bisect_right(arr, hi)
        out: set[str] = set()
        for _, labels in self.declared[agent][i:j]:
            out.update(labels)
        return sorted(out)

    def actions_between(self, prev: Call, cur: Call, gk: str, limit: int = 400) -> tuple[bool, bool]:
        """(modification pertinente observee, action a effet inconnu du meme agent) entre les deux appels."""
        i = bisect.bisect_right(self.starts, prev.order_ns)
        j = bisect.bisect_left(self.starts, cur.order_ns)
        strong = weak = False
        for w in self.calls[i:min(j, i + limit)]:
            if w.key in (prev.key, cur.key) or group_key(w) == gk:
                continue
            if self._strong(w, cur):
                return True, weak
            if w.agent_key == cur.agent_key and (w.unknown_effect or w.is_write_like or w.op in I.RUN_LIKE_OPS):
                weak = True
        if j - i > limit:
            weak = True      # ? trop d'appels intercales pour tout examiner : un effet reste possible
        return strong, weak

    def _strong(self, w: Call, c: Call) -> bool:
        if c.category == S.CAT_MCP:
            # * Ressource distante : seule une action du meme serveur (soumission, annulation...) la change.
            return (w.category == S.CAT_MCP and w.mcp_server == c.mcp_server and w.tool_name != c.tool_name
                    and w.op != I.OP_MCP_READ and not _WAIT_NAME.search(w.mcp_tool or ""))
        if c.op == I.OP_READ and c.op_target:
            return w.is_write_like and _touches(w, str(c.op_target), self.case_insensitive)
        if c.op in I.READ_LIKE_OPS or c.category == S.CAT_SHELL or c.op in I.RUN_LIKE_OPS:
            return w.is_write_like
        ns = _namespace(c.tool_name)
        if ns:
            # * Fonctions d'un meme espace (collaboration.*) : un message ou une tache donnee a un sous-agent
            #   relance legitimement l'attente.
            return (w.agent_key == c.agent_key and _namespace(w.tool_name) == ns and w.tool_name != c.tool_name
                    and not _WAIT_NAME.search(w.tool_name or "") and not _READ_NAME.search(w.tool_name or ""))
        return False


# ---------------------------------------------------------------------------- reprises
def _repeat(prev: Call, cur: Call, ctx: _Context, gk: str, gap_ms: int) -> dict[str, Any]:
    same_resp, _basis = B.same_response(prev, cur, gap_ms)
    p_phase, c_phase = phase_of(prev), phase_of(cur)
    sp, sc = _state(prev), _state(cur)
    outcome = ("same" if sp[1] == sc[1] else "changed") if sp and sc and sp[0] == sc[0] else "unknown"
    flags: list[str] = []
    if same_resp:
        flags.append("same_response")
    if cur.context_epoch != prev.context_epoch:
        flags.append("context_loss")
    if ctx.new_input(cur.agent_key, prev.order_ns, cur.order_ns):
        flags.append("new_input")
    strong, weak = ctx.actions_between(prev, cur, gk)
    if strong:
        flags.append("after_change")
    if p_phase == "unavailable":
        flags.append("unavailable")
    elif p_phase == "failed":
        flags.append("after_failure")
    elif p_phase == "in_progress":
        flags.append("waiting")
    if weak and not strong:
        flags.append("possible_change")
    interval = (cur.order_ns - prev.order_ns) / 1e9 if cur.order_ns and prev.order_ns else None
    idle = (cur.start_ns - prev.end_ns) / 1e9 if cur.start_ns is not None and prev.end_ns is not None else None
    reason = flags[0] if flags else "none"
    declared = ctx.declared_between(cur.agent_key, prev.order_ns, cur.order_ns)
    basis = "observee"
    if reason in ("none", "possible_change"):
        # * Rien d'observe entre les deux : un outil dont la fonction est d'attendre (wait, sleep, wait_agent) attend
        #   encore ; sinon, la raison annoncee par l'agent (commentaire) est retenue, marquee comme telle.
        if _WAIT_NAME.search(cur.mcp_tool or cur.tool_name or ""):
            reason, basis = "waiting", "nature de l'outil (attente)"
        elif "unavailable" in declared:
            reason, basis = "unavailable", "annoncee par l'agent"
        elif "in_progress" in declared or "wait" in declared:
            reason, basis = "waiting", "annoncee par l'agent"
    phase_changed = p_phase != c_phase and "unknown" not in (p_phase, c_phase)
    if reason in ("same_response",):
        value = "free"
    elif reason in ("context_loss", "new_input", "after_change") or outcome == "changed" or phase_changed:
        value = "useful"
    elif outcome == "same":
        value = "explained_no_gain" if reason != "none" else "no_gain"
    else:
        value = "unknown"
    return {"seq": cur.seq, "key": cur.key, "reason": reason, "reason_basis": basis, "flags": flags, "outcome": outcome,
            "phase": c_phase, "prev_phase": p_phase, "phase_changed": phase_changed, "round_trip": not same_resp,
            "interval_s": interval, "idle_s": idle, "value": value, "declared": declared}


def _episodes(calls: list[Call], gap_s: float) -> list[tuple[int, int]]:
    """Plages [debut, fin] d'appels separes de moins de `gap_s` : une attente, une serie de tentatives."""
    out: list[tuple[int, int]] = []
    start = 0
    for k in range(1, len(calls)):
        if calls[k].order_ns - calls[k - 1].order_ns > gap_s * 1e9:
            out.append((start, k - 1))
            start = k
    if calls:
        out.append((start, len(calls) - 1))
    return out


def simulate_cooldown(calls: list[Call], phases: list[str], episodes: list[tuple[int, int]], cooldown_s: float,
                      round_trip: list[bool] | None = None) -> dict[str, Any]:
    """Ce qu'aurait donne un delai minimal entre deux appels : appels gardes, evites (dont allers-retours du modele),
    et retard ajoute a la detection de chaque changement de phase.

    # * Modele : un appel arrive avant la fin du delai est retenu jusqu'a cette fin, puis servi (attente cote outil,
    #   ou consigne suivie par l'agent) ; les appels intermediaires disparaissent. Un changement vu par un appel
    #   evite l'est donc a la fin du delai : retard = dernier appel servi + delai - instant du changement (<= delai).
    """
    cd = int(cooldown_s * 1e9)
    kept = 0
    avoided_rt = 0
    delays: list[float] = []
    for a, b in episodes:
        last: int | None = None
        pending: int | None = None
        for k in range(a, b + 1):
            t = calls[k].order_ns
            if k > a and phases[k] != phases[k - 1] and "unknown" not in (phases[k], phases[k - 1]) and pending is None:
                pending = t
            if last is None or t - last >= cd:
                kept += 1
                last = t
                if pending is not None:
                    delays.append(max(0.0, (t - pending) / 1e9))
                    pending = None
            else:
                if round_trip is not None and round_trip[k]:
                    avoided_rt += 1
                if pending is not None:
                    delays.append(max(0.0, (last + cd - pending) / 1e9))   # * servi a la fin du delai
                    pending = None
    return {"cooldown_s": cooldown_s, "calls": kept, "avoided": len(calls) - kept, "avoided_round_trips": avoided_rt,
            "changes": len(delays),
            "max_delay_s": round(max(delays), 1) if delays else 0.0,
            "mean_delay_s": round(sum(delays) / len(delays), 1) if delays else 0.0}


def _cadence(calls: list[Call], reps: list[dict[str, Any]], episodes: list[tuple[int, int]], d: dict[str, Any]) -> dict[str, Any]:
    phases = [phase_of(calls[0])] + [r["phase"] for r in reps]
    rts = [False] + [bool(r["round_trip"]) for r in reps]
    sims = [simulate_cooldown(calls, phases, episodes, float(c), rts) for c in d.get("cooldowns_s", _DEFAULT_COOLDOWNS)]
    spans = [(calls[b].order_ns - calls[a].order_ns) / 1e9 for a, b in episodes if b > a]
    typical = median(spans) if spans else 0.0
    tolerance = max(float(d.get("min_tolerated_delay_s", 30)), float(d.get("tolerated_delay_ratio", 0.1)) * typical)
    ok = [s for s in sims if s["cooldown_s"] <= tolerance and s["avoided"] > 0]
    best = max(ok, key=lambda s: s["cooldown_s"]) if ok else None
    changes = max((s["changes"] for s in sims), default=0)
    return {"typical_wait_s": round(typical, 1), "tolerated_delay_s": round(tolerance, 1), "simulation": sims,
            "recommended": best, "phase_changes_seen": changes,
            "basis": (f"retard tolere = max({d.get('min_tolerated_delay_s', 30)} s, "
                      f"{int(float(d.get('tolerated_delay_ratio', 0.1)) * 100)} % de l'attente typique)"
                      + ("" if changes else " ; aucun changement de phase observe : retard de detection non estimable"))}


def _interval_stats(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    med = median(values)
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    cv = (var ** 0.5) / mean if mean > 0 else None
    pattern = "irregulier"
    if len(values) >= 4:
        half = len(values) // 2
        first, second = median(values[:half]), median(values[half:])
        if first > 0 and second >= 1.5 * first:
            pattern = "espacement croissant (backoff)"
        elif cv is not None and cv < 0.3:
            pattern = "cadence fixe"
    elif cv is not None and cv < 0.3 and len(values) >= 2:
        pattern = "cadence fixe"
    if sum(1 for v in values if v < 5) >= max(2, len(values) // 2):
        pattern = "rafales (< 5 s)" if pattern == "irregulier" else pattern + ", rafales"
    return {"median": round(med, 1), "min": round(min(values), 1), "p90": round(_q(values, 0.9), 1),
            "max": round(max(values), 1), "cv": round(cv, 2) if cv is not None else None, "pattern": pattern, "n": len(values)}


def _context_reread(calls: list[Call]) -> int | None:
    """Contexte relu pour decider ces appels : entree totale (dont cache) des reponses emettrices, une fois chacune."""
    seen: dict[str, int] = {}
    for c in calls:
        u = c.usage or {}
        rid, tok = u.get("emitter_request_id"), u.get("emitter_input_tokens")
        if isinstance(rid, str) and isinstance(tok, int):
            seen[rid] = tok
    return sum(seen.values()) if seen else None


def _timeouts_used(calls: list[Call]) -> dict[str, list[Any]]:
    """Delais demandes par l'agent (parametres numeriques de delai, gardes en clair), tries, 6 au plus par parametre."""
    from agentwatch.core.normalize import is_timing_param
    out: dict[str, set[float]] = defaultdict(set)
    for c in calls:
        for k, v in c.params.items():
            if k in _TIMEOUT_PARAMS or is_timing_param(k, v):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k].add(float(v))
    return {k: [str(int(x)) if x == int(x) else str(x) for x in sorted(v)][:6] for k, v in out.items()}


# ---------------------------------------------------------------------------- analyse
def analyse(view: SessionView, cfg: dict[str, Any]) -> dict[str, Any]:
    """Groupes d'appels repetes (raisons, rythme, verdict) et rythme de chaque outil. Calcule une fois par vue."""
    cached = view.cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    d = cfg.get("detectors", {}).get("repeated_calls", {})
    min_calls = max(2, int(d.get("min_calls", 3)))
    gap_ms = int(cfg.get("detectors", {}).get("batchable", {}).get("same_response_gap_ms", 2000))
    episode_gap = float(d.get("episode_gap_s", 1200))
    ctx = _Context(view, cfg)
    by_group: dict[str, list[Call]] = defaultdict(list)
    for c in view.calls:
        if c.tool_name:
            by_group[group_key(c)].append(c)
    # * Outils d'attente vus par serveur MCP : une alternative au sondage deja a portee de l'agent.
    wait_tools: dict[str, Counter[str]] = defaultdict(Counter)
    for c in view.calls:
        if c.category == S.CAT_MCP and c.mcp_server and _WAIT_NAME.search(c.mcp_tool or ""):
            wait_tools[c.mcp_server][c.tool_name or "?"] += 1
    same_request_agents: dict[str, set[str]] = defaultdict(set)
    for c in view.calls:
        same_request_agents["\x1f".join((str(c.tool_name), str(c.target_key), c.params_key))].add(c.agent_key)

    repeat_of: dict[str, dict[str, Any]] = {}
    groups: list[dict[str, Any]] = []
    for gk, calls in by_group.items():
        if len(calls) < 2:
            continue
        reps = [_repeat(p, c, ctx, gk, gap_ms) for p, c in zip(calls, calls[1:])]
        for r in reps:
            repeat_of[r["key"]] = r
        if len(calls) < min_calls:
            continue
        groups.append(_summarise(gk, calls, reps, d, episode_gap, wait_tools, same_request_agents))
    groups.sort(key=lambda g: (g["verdict"] in IMPROVABLE, g["round_trips"], g["calls"]), reverse=True)
    result = {"groups": groups, "rhythm": _rhythm(view, repeat_of, gap_ms, int(d.get("rhythm_top", 15))),
              "totals": _totals(groups), "min_calls": min_calls}
    view.cache[CACHE_KEY] = result
    return result


def _summarise(gk: str, calls: list[Call], reps: list[dict[str, Any]], d: dict[str, Any], episode_gap: float,
               wait_tools: dict[str, Counter[str]], same_request_agents: dict[str, set[str]]) -> dict[str, Any]:
    first = calls[0]
    rt = [r for r in reps if r["round_trip"]]
    n_rt = len(rt)
    reasons = Counter(r["reason"] for r in rt)
    values = Counter(r["value"] for r in rt)
    outcomes = Counter(r["outcome"] for r in rt)
    declared = Counter(label for r in rt for label in r["declared"])
    phases = Counter(phase_of(c) for c in calls)
    episodes = _episodes(calls, episode_gap)
    intervals = [float(r["interval_s"]) for r in rt if isinstance(r["interval_s"], (int, float))]
    idles = [float(r["idle_s"]) for r in rt if isinstance(r["idle_s"], (int, float)) and r["idle_s"] >= 0]
    ns = sorted(c.order_ns for c in calls if c.order_ns)
    rt_calls = [c for c, r in zip(calls[1:], reps) if r["round_trip"]]
    polling = reasons["waiting"] + reasons["unavailable"]
    iv_stats = _interval_stats(intervals)
    # * Suivi periodique : reprises a intervalle regulier, rien d'observe entre deux, phase inchangee. L'etat peut
    #   changer (compteurs de progression) sans qu'aucune decision n'en decoule : c'est un sondage.
    periodic = bool(n_rt >= 5 and iv_stats and iv_stats["cv"] is not None and iv_stats["cv"] < 0.5
                    and reasons["none"] + reasons["possible_change"] >= 0.6 * n_rt
                    and sum(1 for r in rt if r["phase_changed"]) <= max(1, n_rt // 10))
    is_wait_tool = bool(_WAIT_NAME.search(first.mcp_tool or first.tool_name or ""))
    # * Qui peut changer la cadence ? Un serveur MCP se modifie (attente bloquante, delai minimal, plafond) ; un outil
    #   du client (shell, wait, wait_agent) ne se modifie pas : c'est l'agent qui choisit sa boucle et ses delais.
    by_tool_owner = "outil" if first.category == S.CAT_MCP else "agent"
    timed_out = sum(1 for c in calls if c.evidence.get("timed_out") is True)
    wait_alt = None
    if first.category == S.CAT_MCP and first.mcp_server and not is_wait_tool:
        alts = wait_tools.get(first.mcp_server) or Counter()
        if alts:
            tool, n = alts.most_common(1)[0]
            wait_alt = {"tool": tool, "calls": n}
    domain_a = first.op in I.READ_LIKE_OPS - {I.OP_MCP_READ} or first.op in I.RUN_LIKE_OPS
    known = n_rt - outcomes["unknown"]
    cadence = None
    verdict, kind, why, suggestion = "indetermine", None, "", ""
    if n_rt == 0:
        verdict, why = "gratuit", "toutes les repetitions sont dans une meme reponse du modele (script, appels paralleles)"
    elif polling * 2 >= n_rt or periodic:
        cadence = _cadence(calls, reps, episodes, d)
        rec = cadence["recommended"]
        cad_txt = (f"un appel toutes les {fmt_duration(rec['cooldown_s'])} au plus aurait evite {rec['avoided']} appel(s) sur "
                   f"{len(calls)} (dont {rec['avoided_round_trips']} aller(s)-retour(s) du modele)"
                   + (f", pour un retard de detection de {fmt_duration(rec['max_delay_s'])} au plus" if cadence["phase_changes_seen"]
                      else ", retard de detection non estimable (aucun changement de phase observe)") if rec else "")
        if reasons["unavailable"] > reasons["waiting"]:
            verdict, kind = "environnement", "retry_cadence"
            why = (f"{reasons['unavailable']} reprise(s) sur {n_rt} suivent un resultat disant le service indisponible : "
                   "reessayer etait legitime")
            suggestion = ("cote outil : renvoyer un delai de reprise (retry_after) ou attendre la disponibilite avant de repondre"
                          + (f" ; {cad_txt}" if cad_txt else ""))
        elif wait_alt:
            verdict, kind = "agent", "polling_instead_of_wait"
            why = (f"{reasons['waiting']} reprise(s) sur {n_rt} sondent un traitement en cours alors que "
                   f"`{wait_alt['tool']}` (attente du meme serveur) sert {wait_alt['calls']} fois dans la session")
            suggestion = f"consigne : attendre avec `{wait_alt['tool']}` plutot que sonder" + (f" ; sinon {cad_txt}" if cad_txt else "")
        elif is_wait_tool or timed_out:
            verdict, kind = by_tool_owner, "wait_timeout"
            used = _timeouts_used(calls)
            why = (f"l'attente est relancee {max(timed_out, reasons['waiting'])} fois avant que ce qu'elle attend n'arrive"
                   + (f" (delai demande : {', '.join(f'{k}={v}' for k, v in used.items())})" if used else ""))
            suggestion = ("consigne : demander un delai d'attente plus long (parametre de l'outil) ; une attente par evenement "
                          "attendu" if by_tool_owner == "agent" else
                          "cote serveur : relever le delai maximal d'attente (ou attendre jusqu'au changement d'etat) ; une "
                          "attente par evenement attendu")
        else:
            verdict, kind = by_tool_owner, "polling_cadence"
            if polling * 2 >= n_rt:
                why = f"{reasons['waiting']} reprise(s) sur {n_rt} sondent un traitement en cours"
            else:
                why = (f"suivi periodique : {n_rt} reprises a intervalle regulier (median {fmt_duration(iv_stats['median'])}), "
                       "rien d'observe entre deux et phase inchangee")
            suggestion = (("cote outil : une attente bloquante (jusqu'au changement d'etat, avec delai max) ou une cadence minimale"
                           if by_tool_owner == "outil" else
                           "consigne : attendre dans un seul appel (boucle dans le script jusqu'a la fin, avec delai max) plutot "
                           "qu'un appel par aller-retour du modele")
                          + (f" ; {cad_txt}" if cad_txt else ""))
    elif values["no_gain"] * 2 >= n_rt and values["no_gain"] >= 2:
        verdict, kind = "agent", "unexplained_repeats"
        why = (f"{values['no_gain']} reprise(s) sur {n_rt} sans raison observee et au meme resultat : "
               "l'agent redemande ce qu'il a deja")
        suggestion = ("consigne (AGENTS.md, skill) : reutiliser le resultat obtenu ; ou le garder court (resume) pour qu'il reste "
                      "en contexte")
        if domain_a:
            kind = None      # * lecture ou execution locale : signalee par le detecteur A
    elif reasons["after_failure"] * 2 >= n_rt:
        verdict, why = "echec", f"{reasons['after_failure']} reprise(s) sur {n_rt} suivent un echec"
        suggestion = "voir le detecteur B (boucles d'erreur) pour les echecs identiques"
    elif values["useful"] * 2 >= n_rt:
        verdict = "justifie"
        why = ("reprises expliquees : " + ", ".join(f"{REASON_LABELS[k]} {v}" for k, v in reasons.most_common()
                                                    if k in ("context_loss", "new_input", "after_change"))
               + (f" ; etat change {outcomes['changed']} fois" if outcomes["changed"] else ""))
        if reasons["context_loss"] * 2 >= n_rt:
            suggestion = (f"{reasons['context_loss']} reprise(s) suivent une compaction : garder cette information hors du "
                          "contexte (fichier d'etat, notes) eviterait de la redemander apres chaque compaction")
    elif known * 2 < n_rt:
        verdict, why = "indetermine", f"apport inconnu pour {outcomes['unknown']} reprise(s) sur {n_rt} (empreinte d'etat absente)"
    else:
        verdict = "justifie" if values["useful"] >= values["no_gain"] else "agent"
        why = (f"reprises mixtes : utiles {values['useful']}, expliquees sans apport {values['explained_no_gain']}, "
               f"sans raison ni apport {values['no_gain']}")
        if verdict == "agent" and values["no_gain"] >= 2 and not domain_a:
            kind = "unexplained_repeats"
            suggestion = "consigne : reutiliser le resultat obtenu"
    phase_known = sum(1 for c in calls if c.evidence.get("result_phase") not in (None, "unknown"))
    if n_rt >= 5 and known >= 0.8 * n_rt and (not polling or phase_known >= 0.8 * len(calls)):
        confidence = B.CONFIDENCE_HIGH
    elif n_rt >= 2 and known >= 0.5 * n_rt:
        confidence = B.CONFIDENCE_MEDIUM
    else:
        confidence = B.CONFIDENCE_LOW
    target = first.target if first.target is not None else first.mcp_tool
    return {
        "group": gk, "agent": first.agent_key, "tool": first.tool_name, "category": first.category, "op": first.op,
        "target": (str(target)[:160] if target is not None else None), "params_key": first.params_key[:200],
        "calls": len(calls), "round_trips": n_rt, "same_response": len(reps) - n_rt, "episodes": len(episodes),
        "first_time": first.start_time or first.end_time, "last_time": calls[-1].start_time or calls[-1].end_time,
        "span_s": round((ns[-1] - ns[0]) / 1e9, 1) if len(ns) > 1 else 0.0,
        "reasons": dict(reasons), "values": dict(values), "outcomes": dict(outcomes), "declared": dict(declared),
        "phases": dict(phases), "phase_changes": sum(1 for r in rt if r["phase_changed"]),
        "interval_s": iv_stats, "idle_median_s": round(median(idles), 1) if idles else None, "periodic": periodic,
        "max_per_min": _max_in_window(ns, 60 * 10**9), "max_per_10min": _max_in_window(ns, 600 * 10**9),
        "context_reread_tokens": _context_reread(rt_calls), "observed_cost": B.observed_cost(rt_calls),
        "verdict": verdict, "verdict_label": VERDICT_LABELS[verdict], "kind": kind, "why": why, "suggestion": suggestion,
        "cadence": cadence, "wait_alternative": wait_alt, "timed_out": timed_out,
        "other_agents": max(0, len(same_request_agents.get("\x1f".join((str(first.tool_name), str(first.target_key),
                                                                        first.params_key)), set())) - 1),
        "confidence": confidence, "call_keys": [c.key for c in calls], "seqs": [c.seq for c in calls],
        "repeats": [{k: r[k] for k in ("seq", "reason", "reason_basis", "flags", "outcome", "phase", "value", "interval_s",
                                       "idle_s", "declared", "round_trip")} for r in reps],
    }


def _rhythm(view: SessionView, repeat_of: dict[str, dict[str, Any]], gap_ms: int, top: int) -> list[dict[str, Any]]:
    """Rythme de chaque outil : volume, intervalles entre appels successifs d'un meme agent, pics par minute et
    par 10 minutes (tous agents : la charge que voit l'outil), part des appels identiques a un precedent."""
    by_tool: dict[str, list[Call]] = defaultdict(list)
    for c in view.calls:
        by_tool[c.tool_name or "?"].append(c)
    rows: list[dict[str, Any]] = []
    for tool, cs in by_tool.items():
        ns = sorted(c.order_ns for c in cs if c.order_ns)
        by_agent: dict[str, list[Call]] = defaultdict(list)
        for c in cs:
            by_agent[c.agent_key].append(c)
        intervals: list[float] = []
        for seq in by_agent.values():
            for a, b in zip(seq, seq[1:]):
                if a.order_ns and b.order_ns and not B.same_response(a, b, gap_ms)[0]:
                    intervals.append((b.order_ns - a.order_ns) / 1e9)
        reps = [repeat_of[c.key] for c in cs if c.key in repeat_of]
        dur = [c.duration_ms for c in cs if isinstance(c.duration_ms, int)]
        rows.append({
            "tool": tool, "calls": len(cs), "agents": len(by_agent),
            "span_s": round((ns[-1] - ns[0]) / 1e9, 1) if len(ns) > 1 else 0.0,
            "interval_median_s": round(median(intervals), 1) if intervals else None,
            "interval_p10_s": round(_q(intervals, 0.1), 1) if intervals else None,
            "max_per_min": _max_in_window(ns, 60 * 10**9), "max_per_10min": _max_in_window(ns, 600 * 10**9),
            "identical_repeats": len(reps), "repeats_round_trip": sum(1 for r in reps if r["round_trip"]),
            "repeats_no_gain": sum(1 for r in reps if r["value"] in ("no_gain", "explained_no_gain")),
            "duration_ms_sum": sum(dur) if dur else None,
        })
    rows.sort(key=lambda r: (r["calls"], r["identical_repeats"]), reverse=True)
    return rows[:top]


def _totals(groups: list[dict[str, Any]]) -> dict[str, Any]:
    by_verdict: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    calls = rts = 0
    for g in groups:
        by_verdict[g["verdict"]] += g["round_trips"]
        by_reason.update(g["reasons"])
        calls += g["calls"]
        rts += g["round_trips"]
    return {"groups": len(groups), "calls": calls, "round_trips": rts, "round_trips_by_verdict": dict(by_verdict),
            "round_trips_by_reason": dict(by_reason)}


# ---------------------------------------------------------------------------- signalements
_TITLES = {
    "polling_cadence": "Sondage a cadence serree",
    "polling_instead_of_wait": "Sondage alors qu'un outil d'attente existe",
    "wait_timeout": "Attente relancee a chaque echeance",
    "retry_cadence": "Reessais contre un service indisponible",
    "unexplained_repeats": "Appel refait sans raison ni apport",
}


def _eligible(g: dict[str, Any], min_avoidable: int) -> bool:
    kind = g["kind"]
    if not kind:
        return False
    if kind in ("polling_cadence", "retry_cadence"):
        rec = (g.get("cadence") or {}).get("recommended")
        return bool(rec) and rec["avoided"] >= min_avoidable      # * rien de chiffrable : le tableau garde le groupe
    if kind == "unexplained_repeats":
        return g["values"].get("no_gain", 0) >= 2
    return g["round_trips"] >= 2


def _sum(groups: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: Counter[str] = Counter()
    for g in groups:
        out.update(g.get(key) or {})
    return dict(out)


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    """Un signalement par habitude (nature de la repetition x outil), qui rassemble ses groupes : 30 cellules
    attendues de la meme facon sont une seule habitude a corriger, pas 30 signalements."""
    d = cfg.get("detectors", {}).get("repeated_calls", {})
    min_avoidable = int(d.get("min_avoidable_calls", 3))
    res = analyse(view, cfg)
    by_key = {c.key: c for c in view.calls}
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for g in res["groups"]:
        if _eligible(g, min_avoidable):
            buckets[(str(g["kind"]), str(g["tool"]))].append(g)
    findings: list[B.Finding] = []
    for (kind, tool), gs in buckets.items():
        gs.sort(key=lambda g: (g["round_trips"], g["calls"]), reverse=True)
        lead = gs[0]
        members = [by_key[k] for g in gs for k in g["call_keys"] if k in by_key]
        rt_members = [by_key[g["call_keys"][i + 1]] for g in gs for i, r in enumerate(g["repeats"])
                      if r["round_trip"] and g["call_keys"][i + 1] in by_key]
        calls = sum(g["calls"] for g in gs)
        rts = sum(g["round_trips"] for g in gs)
        free = sum(g["same_response"] for g in gs)
        reasons, declared, outcomes = _sum(gs, "reasons"), _sum(gs, "declared"), _sum(gs, "outcomes")
        ctx_tokens = [g["context_reread_tokens"] for g in gs if isinstance(g.get("context_reread_tokens"), int)]
        used = _timeouts_used(members)
        medians = [g["interval_s"]["median"] for g in gs if g.get("interval_s")]
        highs = [g for g in gs if g["confidence"] == B.CONFIDENCE_HIGH]
        confidence = (B.CONFIDENCE_HIGH if highs and rts >= 5 else
                      B.CONFIDENCE_MEDIUM if any(g["confidence"] != B.CONFIDENCE_LOW for g in gs) else B.CONFIDENCE_LOW)
        reasons_txt = ", ".join(f"{REASON_LABELS.get(k, k)} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
        declared_txt = ", ".join(f"{DECLARED_LABELS.get(k, k)} {v}" for k, v in sorted(declared.items(), key=lambda kv: -kv[1]))
        where = f"{len(gs)} cible(s) ou parametre(s) distincts" if len(gs) > 1 else f"cible {lead.get('target') or '-'!s:.80}"
        explanation = (f"{tool} : {calls} appels refaits a l'identique ({where}) ; {rts} reprise(s) ont coute un aller-retour "
                       f"du modele" + (f", {free} autre(s) etaient dans une meme reponse ou une boucle de script (aucun "
                                       f"aller-retour)" if free else "") + ". "
                       f"Raisons observees : {reasons_txt or 'aucune'}. "
                       + (f"Raisons annoncees par l'agent : {declared_txt}. " if declared_txt else "")
                       + (f"Intervalle median par groupe : {', '.join(fmt_duration(m) for m in medians[:5])}. " if medians else "")
                       + (f"Delais demandes : {', '.join(f'{k}={v}' for k, v in used.items())}. " if used else "")
                       + f"Verdict : {lead['verdict_label']} : {lead['why']}.")
        if ctx_tokens:
            explanation += (f" Contexte relu pour decider ces reprises : {sum(ctx_tokens)} tokens (entree des reponses "
                            f"emettrices, en grande partie en cache).")
        findings.append(B.Finding(
            rule_id=RULE_ID, rule_version=RULE_VERSION, kind=kind,
            title=(f"{_TITLES[kind]} : {tool} ({calls} appels, {rts} aller(s)-retour(s)"
                   + (f", {len(gs)} groupes)" if len(gs) > 1 else ")")),
            confidence=confidence,
            confidence_rationale=(f"{len(gs)} groupe(s), {rts} reprise(s) avec aller-retour ; apport connu pour "
                                  f"{rts - outcomes.get('unknown', 0)} ; phase lue dans le resultat pour "
                                  f"{sum(1 for c in members if c.evidence.get('result_phase') not in (None, 'unknown'))}/"
                                  f"{len(members)} appels. " + B.LIMIT_HEURISTIC),
            calls=[c.key for c in members], call_refs=B.refs(members[:30]),
            evidence={"tool": tool, "kind": kind, "verdict": lead["verdict"], "groups": len(gs), "calls": calls,
                      "round_trips": rts, "same_response": free, "reasons": reasons, "declared": declared, "outcomes": outcomes,
                      "phases": _sum(gs, "phases"), "context_reread_tokens": sum(ctx_tokens) if ctx_tokens else None,
                      "timeouts_used": used, "wait_alternative": lead.get("wait_alternative"),
                      "top_groups": [{"agent": g["agent"], "target": g.get("target"), "calls": g["calls"],
                                      "round_trips": g["round_trips"], "interval_s": g.get("interval_s"),
                                      "max_per_min": g["max_per_min"], "why": g["why"]} for g in gs[:8]],
                      "cadence": lead["cadence"]},
            explanation=explanation,
            counter_indications=[
                "Le raisonnement du modele entre deux appels n'est pas observe : seuls les faits (resultats, actions, "
                "messages) et les categories annoncees dans ses commentaires le sont.",
                "Un etat identique a l'empreinte (horodatages neutralises) peut cacher un detail qui comptait pour l'agent.",
                "La simulation de cadence suppose qu'un etat observe persiste jusqu'a l'appel suivant.",
            ],
            missing_data=[m for m in (
                None if not outcomes.get("unknown") else f"apport inconnu pour {outcomes['unknown']} reprise(s)",
                None if declared else "aucune raison annoncee par l'agent (commentaires non importes ou absents)",
            ) if m],
            observed_cost=B.observed_cost(rt_members),
            proposal={"type": "repeated_calls", "verdict": lead["verdict"], "text": lead["suggestion"], "cadence": lead["cadence"]},
            validation_protocol=[
                "Relire 2 reprises du groupe principal : la raison observee et la raison annoncee concordent-elles ?",
                "Appliquer la suggestion (consigne, delai, attente bloquante), puis comparer le nombre d'appels et le retard "
                "de detection sur une session comparable (agentwatch trends).",
                "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
            ],
        ))
    return findings
