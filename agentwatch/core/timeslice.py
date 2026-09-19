"""Tranche de temps d'une session : ce qui s'est passe entre deux instants, pour un rapport ou une comparaison.

# * Un fil Codex vit plusieurs jours (le 15, repris le 19) : un rapport de session melange les jours. Une tranche
#   garde les appels et marqueurs dont l'instant tombe dans [debut, fin[, et recalcule les totaux de tokens a partir
#   des releves PAR REPONSE de la tranche (les releves de fil couvrent tout le fil, ils seraient faux ici).
# * Les heures sans fuseau sont des heures locales ; une date seule designe le debut du jour local.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from agentwatch.core import schema as S
from agentwatch.core.correlate import Marker, SessionView

_ROLLOUT = "codex:rollout"


def parse_when(text: str) -> int:
    """'2026-09-19', '2026-09-19T11:45', '2026-09-19 11:45:10', '...Z' ou '...+02:00' -> nanosecondes UTC."""
    s = text.strip().replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"instant illisible : {text!r} (attendu : AAAA-MM-JJ ou AAAA-MM-JJTHH:MM, heure locale par defaut)") from exc
    if dt.tzinfo is None:
        return int(time.mktime(dt.timetuple()) * 1_000_000_000) + dt.microsecond * 1000
    return int(dt.timestamp() * 1_000_000_000)


def day_bounds(day: str) -> tuple[int, int]:
    """[debut, fin[ d'un jour local."""
    d = datetime.fromisoformat(day.strip()[:10])
    return parse_when(d.strftime("%Y-%m-%d")), parse_when((d + timedelta(days=1)).strftime("%Y-%m-%d"))


def _inside(ns: int | None, since: int | None, until: int | None) -> bool:
    if not ns:
        return False
    return (since is None or ns >= since) and (until is None or ns < until)


def slice_view(view: SessionView, since: int | None, until: int | None) -> SessionView:
    """Vue restreinte a [since, until[ : appels (par leur debut, a defaut leur fin), marqueurs, tokens de la tranche."""
    from agentwatch.core.correlate import _build_agents
    calls = [c for c in view.calls if _inside(c.order_ns, since, until)]
    markers: list[Marker] = []
    per_thread: dict[str, dict[str, Any]] = {}
    for m in view.markers:
        u = m.meta.get("usage") if m.phase == S.PHASE_USAGE else None
        if isinstance(u, dict) and u.get("source") == _ROLLOUT and u.get("scope") == "thread":
            continue      # * total de tout le fil : remplace plus bas par la somme des reponses de la tranche
        if not _inside(m.ns, since, until):
            continue
        markers.append(m)
        if isinstance(u, dict) and u.get("scope") == "response":
            t = per_thread.setdefault(str(u.get("thread_id")), {
                "scope": "thread", "source": _ROLLOUT, "thread_id": u.get("thread_id"), "agent_id": u.get("agent_id"),
                "requests": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0,
                "sliced": True, "last_ns": m.ns})
            t["requests"] += 1
            for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
                t[k] += int(u.get(k) or 0)
            t["last_ns"] = max(t["last_ns"], m.ns)
    for t in per_thread.values():
        t["total_tokens"] = t["input_tokens"] + t["output_tokens"]
        markers.append(Marker(phase=S.PHASE_USAGE, ns=t.pop("last_ns"), time=None, agent_id=t.get("agent_id"), meta={"usage": t}))
    markers.sort(key=lambda m: m.ns)
    stamps = [c.order_ns for c in calls if c.order_ns] + [m.ns for m in markers if m.ns]
    first, last = (min(stamps), max(stamps)) if stamps else (None, None)
    by_ns = {c.order_ns: (c.start_time or c.end_time) for c in calls}
    by_ns.update({m.ns: m.time for m in markers if m.time})
    note = ("tranche " + (S.now_iso(since / 1e9) if since else "debut") + " a " + (S.now_iso(until / 1e9) if until else "fin")
            + f" : {len(calls)} appel(s) sur {len(view.calls)}")
    out = replace(view, calls=calls, markers=markers, cache={}, warnings=[*view.warnings, note],
                  first_ns=first, last_ns=last, first_time=by_ns.get(first) if first else None,
                  last_time=by_ns.get(last) if last else None,
                  turns=sum(1 for m in markers if m.phase == S.PHASE_TURN_START) or len({c.turn_id for c in calls if c.turn_id}),
                  epochs=1 + sum(1 for m in markers if m.phase == S.PHASE_COMPACT_END and not m.agent_id),
                  agents=sorted({c.agent_key for c in calls}))
    out.agent_infos = _build_agents(out)
    return out
