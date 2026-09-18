"""Correlation des evenements en appels d'outil (cote analyse).

# * Un debut, une fin et une mesure complementaire portant le meme identifiant
#   d'appel decrivent UN appel. Deux appels au contenu identique mais aux
#   identifiants differents restent DEUX appels.
# * L'absence d'evenement final laisse le statut a "open" : ce n'est pas un echec.
# * Les evenements hors ordre sont tries par horodatage de reception ; les
#   doublons d'evenement (import repete) sont elimines par event_id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentwatch.core import normalize as N
from agentwatch.core import schema as S

STATUS_OPEN = "open"
DURATION_RECONSTRUCTED = "reconstructed_between_hooks"

# * Parametres exclus de la cle de comparaison (volatils ou deja portes par la cible).
_VOLATILE_PARAMS = {"command", "shell_heads", "shell_kind", "shell_paths", "timeout", "timeout_ms",
                    "run_in_background", "description", "yield_time_ms", "max_output_tokens",
                    "old_chars", "new_chars", "content_chars", "patch_chars", "_input_type", "_dropped"}
_PHASE_ORDER = {S.PHASE_START: 0, S.PHASE_END: 1, S.PHASE_FAILURE: 1, S.PHASE_OBSERVATION: 2}


@dataclass
class Call:
    key: str
    client: str
    session_id: str | None
    seq: int = 0
    turn_id: str | None = None
    turn_index: int | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    context_epoch: int = 0
    call_id: str | None = None
    tool_name: str | None = None
    category: str = S.CAT_UNKNOWN
    mcp_server: str | None = None
    mcp_tool: str | None = None
    target: str | None = None
    target_kind: str | None = None
    target_key: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    params_key: str = "{}"
    shell_kind: str | None = None
    input_fingerprint: str | None = None
    result_fingerprint: str | None = None
    resource_revision: str | None = None
    result_paths: list[str] | None = None
    status: str = STATUS_OPEN
    exit_code: int | None = None
    error_signature: str | None = None
    error_summary: str | None = None
    output_size_bytes: int | None = None
    output_size_source: str | None = None
    output_truncated: bool | None = None
    duration_ms: int | None = None
    duration_source: str | None = None
    start_ns: int | None = None
    end_ns: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    cwd: str | None = None
    project_dir: str | None = None
    model: str | None = None
    event_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] | None = None
    content_fingerprint: str | None = None
    # * Unite de travail (core/intent.py) : operation normalisee independante de l'outil.
    op: str = "unknown"
    op_target: str | None = None
    op_params: dict[str, Any] = field(default_factory=dict)
    op_key: str = ""
    op_source: str = "tool"
    ambiguous: bool = False
    has_start: bool = False
    has_end: bool = False

    @property
    def order_ns(self) -> int:
        return self.start_ns if self.start_ns is not None else (self.end_ns or 0)

    @property
    def agent_key(self) -> str:
        return self.agent_id or "main"

    @property
    def is_write_like(self) -> bool:
        if self.category in (S.CAT_EDIT, S.CAT_WRITE):
            return True
        return self.category == S.CAT_SHELL and self.shell_kind == "write"

    @property
    def is_read_like(self) -> bool:
        if self.category in (S.CAT_READ, S.CAT_SEARCH, S.CAT_LIST):
            return True
        return self.category == S.CAT_SHELL and self.shell_kind == "read"

    @property
    def unknown_effect(self) -> bool:
        """Appel dont l'effet sur l'etat n'est pas connu (shell run/unknown, MCP, agent, autre)."""
        if self.category == S.CAT_SHELL:
            return self.shell_kind not in ("read", "write")
        return self.category in (S.CAT_MCP, S.CAT_AGENT, S.CAT_UNKNOWN, S.CAT_OTHER)

    def summary(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "call_id": self.call_id, "tool": self.tool_name, "category": self.category,
            "op": self.op, "op_target": self.op_target,
            "target": self.target, "status": self.status, "start_time": self.start_time,
            "end_time": self.end_time, "agent": self.agent_key, "turn_index": self.turn_index,
            "duration_ms": self.duration_ms, "duration_source": self.duration_source,
            "output_size_bytes": self.output_size_bytes, "error_signature": self.error_signature,
        }


@dataclass
class Marker:
    phase: str
    ns: int
    time: str | None
    agent_id: str | None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentInfo:
    """Un agent (sous-agent ou agent interne) observe dans la session."""
    agent_id: str
    agent_type: str | None
    classification: str            # subagent | stop_only | calls_only
    classification_note: str
    start_time: str | None
    stop_time: str | None
    calls: int
    resumes: int
    parent_call_key: str | None
    parent_call_seq: int | None
    link_basis: str | None         # exact:tool_response.agentId | temporal_containment | ambiguous | None
    model: str | None
    client_duration_ms: int | None
    usage: dict[str, Any] | None


@dataclass
class SessionView:
    client: str
    session_id: str | None
    calls: list[Call] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    agent_infos: list[AgentInfo] = field(default_factory=list)
    model: str | None = None
    project_dir: str | None = None
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    first_ns: int | None = None
    last_ns: int | None = None
    first_time: str | None = None
    last_time: str | None = None
    turns: int = 0
    epochs: int = 1
    agents: list[str] = field(default_factory=list)
    hook_ms_samples: list[float] = field(default_factory=list)
    schema_versions: list[str] = field(default_factory=list)


def _sort_key(ev: dict[str, Any]) -> tuple[int, int]:
    ns = ev.get("received_time_ns")
    if not isinstance(ns, int):
        ns = 0
    return ns, _PHASE_ORDER.get(ev.get("phase") or "", 1)


def _params_key(params: dict[str, Any] | None) -> str:
    if not params:
        return "{}"
    kept = {k: v for k, v in params.items() if k not in _VOLATILE_PARAMS}
    return N.canonical_json(kept)


def _target_key(ev: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    target = ev.get("target")
    if not isinstance(target, str):
        return None
    kind = ev.get("target_kind")
    if kind == "path":
        key = N.path_key(target, bool(cfg.get("case_insensitive_paths", False))) or target
        if not N._is_abs(target) and not target.startswith("~"):
            key = f"{(ev.get('project_dir') or '')}::{key}"
        return f"path:{key}"
    if kind == "command":
        return f"cmd:{ev.get('cwd') or ''}::{target}"
    return f"{kind or 'x'}:{target}"


def build_session(events: list[dict[str, Any]], cfg: dict[str, Any]) -> SessionView:
    """Construit la vue de session (appels correles + marqueurs) a partir d'evenements bruts."""
    client = next((e.get("client") for e in events if isinstance(e, dict) and e.get("client")), None) or "unknown"
    view = SessionView(client=str(client), session_id=None)
    counts = {"events": 0, "duplicate_events": 0, "invalid_events": 0, "orphan_ends": 0,
              "duplicate_phases": 0, "open_calls": 0, "heuristic_pairs": 0, "unmapped_events": 0}
    seen_ids: set[str] = set()
    valid: list[dict[str, Any]] = []
    for ev in events:
        problems = S.validate_event(ev)
        if problems:
            counts["invalid_events"] += 1
            view.warnings.append("evenement invalide ignore : " + "; ".join(problems[:2]))
            continue
        eid = ev["event_id"]
        if eid in seen_ids:
            counts["duplicate_events"] += 1
            continue
        seen_ids.add(eid)
        valid.append(ev)
        sv = ev.get("schema_version")
        if sv and sv not in view.schema_versions:
            view.schema_versions.append(sv)
    valid.sort(key=_sort_key)
    counts["events"] = len(valid)

    calls_by_key: dict[str, Call] = {}
    open_by_agent: dict[str, list[Call]] = {}
    turn_index = 0
    turn_ids: dict[str, int] = {}
    epoch = 0
    seen_session_start = False
    agents: dict[str, None] = {}

    for ev in valid:
        phase: str = ev.get("phase") or S.PHASE_UNKNOWN
        raw_ns = ev.get("received_time_ns")
        ns: int = raw_ns if isinstance(raw_ns, int) else 0
        if view.session_id is None and ev.get("session_id"):
            view.session_id = ev["session_id"]
        if ev.get("source") != S.SOURCE_IMPORT:
            # * Un import (usage, transcript) date du moment de l'import : il ne deplace ni le debut ni la fin de la session.
            first_ns, last_ns = view.first_ns, view.last_ns
            if first_ns is None or ns < first_ns:
                view.first_ns, view.first_time = ns, ev.get("received_time")
            if last_ns is None or ns > last_ns:
                view.last_ns, view.last_time = ns, ev.get("received_time")
        if ev.get("model") and not view.model:
            view.model = ev["model"]
        if ev.get("project_dir") and not view.project_dir:
            view.project_dir = ev["project_dir"]
        hm = (ev.get("evidence") or {}).get("hook_ms")
        if isinstance(hm, (int, float)):
            view.hook_ms_samples.append(float(hm))
        agent = ev.get("agent_id") or "main"
        agents.setdefault(agent, None)
        tid = ev.get("turn_id")
        if isinstance(tid, str) and tid not in turn_ids:
            turn_index += 1
            turn_ids[tid] = turn_index

        if phase in (S.PHASE_START, S.PHASE_END, S.PHASE_FAILURE, S.PHASE_OBSERVATION):
            _apply_tool_event(ev, phase, ns, agent, epoch, turn_ids.get(tid) if isinstance(tid, str) else turn_index or None,
                              calls_by_key, open_by_agent, counts, cfg)
            continue

        meta = dict(ev.get("session_meta") or {})
        meta.update({k: v for k, v in (ev.get("evidence") or {}).items() if k in ("prompt_chars", "stop_hook_active")})
        if phase == S.PHASE_USAGE and isinstance(ev.get("usage"), dict):
            meta["usage"] = ev["usage"]   # * usage de session (import) : un marqueur, jamais un appel
        view.markers.append(Marker(phase=phase, ns=ns, time=ev.get("received_time"), agent_id=ev.get("agent_id"), meta=meta))
        if phase == S.PHASE_SESSION_START:
            if seen_session_start:
                epoch += 1   # ? reprise / clear / compact : le contexte du modele a change
            seen_session_start = True
            model = (ev.get("session_meta") or {}).get("model")
            if model:
                view.model = model
        elif phase == S.PHASE_COMPACT_END:
            epoch += 1
        elif phase == S.PHASE_TURN_START and not isinstance(tid, str):
            turn_index += 1
        elif phase == S.PHASE_INTERRUPT:
            for c in open_by_agent.get(agent, []):
                c.warnings.append("interrupt observed while call open (status left open)")
        elif phase == S.PHASE_UNKNOWN:
            counts["unmapped_events"] += 1

    calls = sorted(calls_by_key.values(), key=lambda c: (c.order_ns, c.seq))
    for i, c in enumerate(calls):
        c.seq = i
        if c.status == STATUS_OPEN:
            counts["open_calls"] += 1
        if c.duration_ms is None and c.start_ns is not None and c.end_ns is not None:
            c.duration_ms = max(0, (c.end_ns - c.start_ns) // 1_000_000)
            c.duration_source = DURATION_RECONSTRUCTED
        if c.model is None:
            c.model = view.model
    view.calls = calls
    view.counts = counts
    view.turns = turn_index
    view.epochs = epoch + 1
    view.agents = list(agents)
    view.agent_infos = _build_agents(view)
    from agentwatch.core.intent import attach_intents  # import tardif : intent depend de Call
    attach_intents(calls)
    return view


def _build_agents(view: SessionView) -> list[AgentInfo]:
    """Resume par agent et lien avec l'appel Agent du fil principal qui l'a lance.

    # * Lien exact quand la reponse de l'outil Agent porte l'identifiant du sous-agent
    #   (observe sur Claude Code 2.1.270). Sinon, inclusion temporelle dans un unique appel
    #   Agent du fil principal : heuristique, signalee comme telle. Plusieurs candidats :
    #   ambigu, aucun lien invente.
    """
    ids: dict[str, dict[str, Any]] = {}

    def slot(aid: str) -> dict[str, Any]:
        return ids.setdefault(aid, {"type": None, "start": None, "stop": None, "start_ns": None, "stop_ns": None,
                                    "calls": [], "first_ns": None, "last_ns": None})

    for m in view.markers:
        if m.phase in (S.PHASE_SUBAGENT_START, S.PHASE_SUBAGENT_STOP) and m.agent_id:
            s = slot(m.agent_id)
            s["type"] = s["type"] or m.meta.get("agent_type")
            if m.phase == S.PHASE_SUBAGENT_START:
                # * Un agent repris re-emet SubagentStart : on garde le premier debut et on compte.
                s["starts"] = s.get("starts", 0) + 1
                s["last_start_ns"] = max(s.get("last_start_ns") or 0, m.ns)
                if s["start_ns"] is None or m.ns < s["start_ns"]:
                    s["start"], s["start_ns"] = m.time, m.ns
            else:
                s["stops"] = s.get("stops", 0) + 1
                if s["stop_ns"] is None or m.ns > s["stop_ns"]:
                    s["stop"], s["stop_ns"] = m.time, m.ns
    for c in view.calls:
        if c.agent_id:
            s = slot(c.agent_id)
            s["type"] = s["type"] or c.agent_type
            s["calls"].append(c)
    spawners = [c for c in view.calls if c.agent_id is None and c.category == S.CAT_AGENT]
    exact = {c.evidence.get("spawned_agent_id"): c for c in spawners if c.evidence.get("spawned_agent_id")}
    infos: list[AgentInfo] = []
    for aid, s in ids.items():
        call_ns = [c.order_ns for c in s["calls"] if c.order_ns]
        first_ns = min([n for n in (s["start_ns"], *call_ns) if n] or [0]) or None
        last_ns = max([n for n in (s["stop_ns"], *call_ns) if n] or [0]) or None
        parent: Call | None = exact.get(aid)
        basis: str | None = "exact:tool_response.agentId" if parent else None
        if parent is None and first_ns:
            cands = [c for c in spawners if c.start_ns and c.start_ns <= first_ns
                     and (c.end_ns is None or (last_ns or first_ns) <= c.end_ns)]
            if len(cands) == 1:
                parent, basis = cands[0], "temporal_containment (heuristique)"
                if s["type"] and cands[0].params.get("subagent_type") not in (None, s["type"]):
                    basis += " ; type different de subagent_type"
            elif len(cands) > 1:
                basis = f"ambiguous ({len(cands)} appels Agent candidats, aucun lien retenu)"
        if s["calls"] or s["start"]:
            classification, note = "subagent", "sous-agent observe (demarrage et/ou appels d'outils)"
        else:
            classification, note = "stop_only", ("seulement un SubagentStop : aucun appel observe ; agent interne du client "
                                                  "ou demarre avant l'installation des hooks")
        starts, stops = s.get("starts", 0), s.get("stops", 0)
        # ? Un agent repris apres interruption : plus de debuts que d'arrets acheves. Un arret
        #   suivi d'un nouveau debut signale la reprise ; le dernier arret fait foi pour la fin.
        resumes = max(0, starts - 1)
        last_start_ns = s.get("last_start_ns")
        # * L'arret ne compte que s'il suit le DERNIER debut ; sinon l'agent a ete repris et tourne encore.
        stop_time = s["stop"] if s["stop_ns"] and (not last_start_ns or s["stop_ns"] >= last_start_ns) else None
        infos.append(AgentInfo(
            agent_id=aid, agent_type=s["type"], classification=classification, classification_note=note,
            start_time=s["start"], stop_time=stop_time, calls=len(s["calls"]), resumes=resumes,
            parent_call_key=parent.key if parent else None, parent_call_seq=parent.seq if parent else None,
            link_basis=basis, model=(parent.evidence.get("spawned_agent_model") if parent else None),
            client_duration_ms=(parent.evidence.get("spawned_agent_duration_ms") if parent else None),
            usage=(parent.usage if parent else None),
        ))
    infos.sort(key=lambda a: (a.start_time or a.stop_time or ""))
    return infos


def _apply_tool_event(ev: dict[str, Any], phase: str, ns: int, agent: str, epoch: int, turn_index: int | None,
                      calls_by_key: dict[str, Call], open_by_agent: dict[str, list[Call]],
                      counts: dict[str, int], cfg: dict[str, Any]) -> None:
    call_id = ev.get("call_id")
    fp = (ev.get("input_fingerprint") or {}).get("value")
    key: str | None = f"{agent}|{call_id}" if call_id else None
    call: Call | None = calls_by_key.get(key) if key else None
    ambiguous = False
    if call is None and key is None:
        # ? Pas d'identifiant : appariement heuristique avec un debut ouvert de meme outil et
        #   meme empreinte d'entree, sinon nouvel appel marque ambigu.
        if phase in (S.PHASE_END, S.PHASE_FAILURE):
            for cand in reversed(open_by_agent.get(agent, [])):
                if cand.tool_name == ev.get("tool_name") and cand.input_fingerprint == fp and not cand.has_end:
                    call, key = cand, cand.key
                    counts["heuristic_pairs"] += 1
                    break
        if call is None:
            key = f"{agent}|anon-{ev['event_id']}"
        ambiguous = True
    if call is None:
        assert key is not None
        call = Call(key=key, client=ev.get("client") or "unknown", session_id=ev.get("session_id"),
                    seq=len(calls_by_key), agent_id=ev.get("agent_id"), agent_type=ev.get("agent_type"),
                    call_id=call_id, context_epoch=epoch, turn_id=ev.get("turn_id"), turn_index=turn_index,
                    cwd=ev.get("cwd"), project_dir=ev.get("project_dir"), model=ev.get("model"))
        calls_by_key[key] = call
        if phase != S.PHASE_START:
            counts["orphan_ends"] += 1
            call.warnings.append("start not observed for this call")
    call.ambiguous = call.ambiguous or ambiguous
    call.event_ids.append(ev["event_id"])
    _merge_static(call, ev, cfg)

    if phase == S.PHASE_START:
        if call.has_start:
            counts["duplicate_phases"] += 1
            call.warnings.append("duplicate start event")
            return
        call.has_start = True
        call.start_ns, call.start_time = ns, ev.get("received_time")
        open_by_agent.setdefault(agent, []).append(call)
    elif phase in (S.PHASE_END, S.PHASE_FAILURE):
        if call.has_end:
            counts["duplicate_phases"] += 1
            call.warnings.append("duplicate end event")
            return
        call.has_end = True
        call.end_ns, call.end_time = ns, ev.get("received_time")
        call.status = ev.get("status") or S.STATUS_UNKNOWN
        call.exit_code = ev.get("exit_code")
        call.error_summary = ev.get("error_summary")
        # * Signature recalculee a l'analyse depuis le resume masque : les evenements anciens
        #   beneficient des ameliorations de normalisation sans etre re-ingeres.
        call.error_signature = (N.make_error_signature(call.error_summary) if call.error_summary else None) or ev.get("error_signature")
        call.output_size_bytes = ev.get("output_size_bytes")
        call.output_size_source = ev.get("output_size_source")
        call.output_truncated = ev.get("output_truncated")
        rf = (ev.get("result_fingerprint") or {}).get("value")
        call.result_fingerprint = rf
        call.content_fingerprint = (ev.get("content_fingerprint") or {}).get("value") if isinstance(ev.get("content_fingerprint"), dict) else None
        rs = ev.get("resource_state") or {}
        call.resource_revision = rs.get("revision")
        if isinstance(ev.get("result_paths"), list):
            call.result_paths = ev["result_paths"]
        if isinstance(ev.get("duration_ms"), int):
            call.duration_ms, call.duration_source = ev["duration_ms"], ev.get("duration_source") or "client"
        for k in ("result_count", "numLines", "totalLines", "stdout_chars", "stderr_chars", "status_basis", "exit_code_source", "error_hint",
                  "spawned_agent_id", "spawned_agent_type", "spawned_agent_model", "spawned_agent_status",
                  "spawned_agent_duration_ms", "spawned_agent_tool_calls", "spawned_agent_tool_stats"):
            if k in (ev.get("evidence") or {}):
                call.evidence[k] = ev["evidence"][k]
        if isinstance(ev.get("usage"), dict):
            call.usage = ev["usage"]
        lst = open_by_agent.get(agent, [])
        if call in lst:
            lst.remove(call)
    else:  # observation complementaire : n'ecrase pas les valeurs connues
        if isinstance(ev.get("duration_ms"), int) and call.duration_ms is None:
            call.duration_ms, call.duration_source = ev["duration_ms"], ev.get("duration_source") or "observation"
        if isinstance(ev.get("usage"), dict) and call.usage is None:
            call.usage = ev["usage"]
    for w in ev.get("warnings") or []:
        if w not in call.warnings:
            call.warnings.append(w)


def _merge_static(call: Call, ev: dict[str, Any], cfg: dict[str, Any]) -> None:
    """Champs identiques entre debut et fin : on garde la premiere valeur non nulle."""
    if call.tool_name is None and ev.get("tool_name"):
        call.tool_name = ev["tool_name"]
        call.category = ev.get("tool_category") or S.CAT_UNKNOWN
        call.mcp_server, call.mcp_tool = ev.get("mcp_server"), ev.get("mcp_tool")
    if call.target is None and ev.get("target") is not None:
        call.target, call.target_kind = ev.get("target"), ev.get("target_kind")
        call.target_key = _target_key(ev, cfg)
    if not call.params and ev.get("params"):
        call.params = dict(ev["params"])
        call.params_key = _params_key(call.params)
        call.shell_kind = call.params.get("shell_kind")
    if call.input_fingerprint is None:
        call.input_fingerprint = (ev.get("input_fingerprint") or {}).get("value")
    if call.turn_id is None and ev.get("turn_id"):
        call.turn_id = ev["turn_id"]
