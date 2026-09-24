"""Lecture facultative des transcripts de Claude Code : usage en tokens par requete et par appel.

# * Le hook ne recoit aucun compte de tokens ; le transcript JSONL du client en contient un
#   par requete API (`message.usage`). Lecture a l'analyse seulement, jamais dans le hook, et
#   seulement sur demande (`agentwatch import-transcripts`) ou si `transcripts.auto_import`
#   est vrai. Le fichier n'est jamais modifie.
# ! Seuls des nombres et des identifiants sont extraits (usage, requestId, tool_use_id,
#   horodatage, modele) : jamais le texte des prompts, des reponses ni des resultats d'outils.
# * Format observe en direct (Claude Code 2.1.275, voir docs/events.md) : une reponse API est
#   ecrite en plusieurs lignes `assistant` (un bloc de contenu par ligne) portant le meme
#   `requestId` et le meme `usage` -> dedoublonnage par requestId. Les resultats d'outils
#   sont des lignes `user` avec des blocs `tool_result` (un par ligne quand les appels sont
#   paralleles) ; ils sont consommes par la requete `assistant` suivante.
# * Attribution par appel : part de l'entree NON mise en cache (input + cache_creation) de la
#   requete qui a consomme le resultat, partagee entre les resultats consommes ensemble, plus
#   la part de la sortie de la requete qui a emis l'appel. Cette entree non mise en cache
#   contient aussi ce qui s'est ajoute au contexte au meme moment (rappels systeme, sortie
#   precedente) : c'est l'entree non mise en cache relevee pour cette requete, pas le poids exact
#   du seul resultat. Aucune conversion depuis des octets, et aucune donnee de facturation : rien
#   ici ne dit ce qui a ete « paye ».
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE
from agentwatch.collector.privacy import sha256_hex
from agentwatch.collector.store import EventStore
from agentwatch.core import normalize as N
from agentwatch.core import schema as S
from agentwatch.core.correlate import SessionView

SOURCE = "claude-code:transcript"
METHOD = ("part de l'entree non mise en cache de la requete qui a consomme le resultat (partagee entre les resultats "
          "consommes ensemble) + part de la sortie de la requete qui a emis l'appel ; inclut ce qui s'est ajoute au "
          "contexte au meme moment")
_SLUG_RE = re.compile(r"[^A-Za-z0-9]")
# (cle AgentWatch, cle du transcript)
_USAGE_KEYS: tuple[tuple[str, str], ...] = (("input_tokens", "input_tokens"), ("cache_creation_tokens", "cache_creation_input_tokens"),
                                            ("cache_read_tokens", "cache_read_input_tokens"), ("output_tokens", "output_tokens"))
_MAX_WARNINGS = 5


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    v = cfg.get(name)
    return v if isinstance(v, dict) else {}


def claude_projects_dir(cfg: dict[str, Any]) -> Path:
    custom = _section(cfg, "transcripts").get("claude_projects_dir")
    return Path(os.path.expanduser(str(custom))) if custom else Path.home() / ".claude" / "projects"


def project_slug(project_dir: str) -> str:
    """Nom du dossier de transcripts d'un projet : tout caractere non alphanumerique devient '-'
    (observe : G:\\UnrealEngine\\Unearthed -> G--UnrealEngine-Unearthed)."""
    return _SLUG_RE.sub("-", N._expand_tilde(project_dir))


def find_transcripts(project_dir: str | None, session_id: str | None, cfg: dict[str, Any],
                     stored_path: str | None = None) -> tuple[Path | None, list[Path]]:
    """(transcript principal, transcripts des sous-agents). Le chemin transmis par le hook
    (`session_meta.transcript_path`, masque par ~) prime ; sinon derivation projet + session."""
    main: Path | None = None
    if stored_path:
        p = Path(N._expand_tilde(N.to_posix(stored_path)))
        if p.is_file():
            main = p
    if main is None and project_dir and session_id:
        cand = claude_projects_dir(cfg) / project_slug(project_dir) / f"{session_id}.jsonl"
        if cand.is_file():
            main = cand
    subs: list[Path] = []
    if main is not None:
        sdir = main.with_suffix("")
        if sdir.is_dir():
            subs = sorted(p for p in sdir.rglob("agent-*.jsonl") if p.is_file())
    return main, subs


def _int(v: Any) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return v if v >= 0 else None
    if isinstance(v, float) and math.isfinite(v) and v >= 0 and v.is_integer():
        return int(v)
    return None


def parse_transcript(path: Path, max_bytes: int = 0) -> dict[str, Any]:
    """Requetes API du transcript : usage, appels emis, resultats consommes. Aucun texte conserve."""
    coverage: dict[str, Any] = {"line_types": {}, "ignored_types": {}, "invalid_json_lines": 0,
                                "non_object_lines": 0, "incomplete_lines": 0, "compaction_lines": []}
    out: dict[str, Any] = {"path": str(path), "requests": [], "agent_id": None, "warnings": [], "lines": 0, "bytes": 0,
                           "coverage": coverage}
    try:
        out["bytes"] = path.stat().st_size
    except OSError as exc:
        out["warnings"].append(f"{path.name} : {exc}")
        return out
    if max_bytes and out["bytes"] > max_bytes:
        out["warnings"].append(f"{path.name} : {out['bytes']} octets > transcripts.max_bytes ({max_bytes}), ignore")
        return out
    requests: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    pending: list[str] = []
    lineno = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw in enumerate(fh, 1):
                if not raw.endswith("\n"):
                    coverage["incomplete_lines"] += 1
                    if len(out["warnings"]) < _MAX_WARNINGS:
                        out["warnings"].append(f"{path.name}:{lineno} : derniere ligne incomplete ignoree (ecriture en cours possible)")
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    o = json.loads(raw)
                except ValueError:
                    coverage["invalid_json_lines"] += 1
                    if len(out["warnings"]) < _MAX_WARNINGS:
                        out["warnings"].append(f"{path.name}:{lineno} : ligne JSON invalide ignoree")
                    continue
                if not isinstance(o, dict):
                    coverage["non_object_lines"] += 1
                    continue
                if out["agent_id"] is None and isinstance(o.get("agentId"), str):
                    out["agent_id"] = o["agentId"]
                msg = o.get("message") if isinstance(o.get("message"), dict) else {}
                content = msg.get("content")
                kind = o.get("type")
                label = kind if isinstance(kind, str) else "(absent)"
                coverage["line_types"][label] = coverage["line_types"].get(label, 0) + 1
                if kind not in ("user", "assistant"):
                    coverage["ignored_types"][label] = coverage["ignored_types"].get(label, 0) + 1
                if kind == "system" and o.get("subtype") == "compact_boundary":
                    coverage["compaction_lines"].append(lineno)
                if kind == "user":
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                                pending.append(b["tool_use_id"])
                elif kind == "assistant":
                    rid = o.get("requestId") or msg.get("id") or o.get("uuid")
                    if not isinstance(rid, str):
                        continue
                    req = requests.get(rid)
                    if req is None:
                        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                        req = {"request_id": rid, "timestamp": o.get("timestamp") if isinstance(o.get("timestamp"), str) else None,
                               "model": msg.get("model") if isinstance(msg.get("model"), str) else None,
                               "usage": {k: _int(usage.get(src)) for k, src in _USAGE_KEYS},
                               "tool_uses": [], "consumed": pending,
                               "source_lines": [], "first_line": lineno, "last_line": lineno}
                        pending = []
                        requests[rid] = req
                        order.append(rid)
                    req["source_lines"].append(lineno)
                    req["last_line"] = lineno
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str) and b["id"] not in req["tool_uses"]:
                                req["tool_uses"].append(b["id"])
    except OSError as exc:
        out["warnings"].append(f"{path.name} : lecture interrompue ({exc})")
    out["requests"] = [requests[r] for r in order]
    out["lines"] = lineno
    return out


def attribute_calls(parsed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Usage par tool_use_id, selon la methode decrite en tete de module."""
    per: dict[str, dict[str, Any]] = {}
    for req in parsed["requests"]:
        u = req["usage"]
        inp, cc, outp = u.get("input_tokens"), u.get("cache_creation_tokens"), u.get("output_tokens")
        uncached = inp + cc if isinstance(inp, int) and isinstance(cc, int) else None
        n = len(req["consumed"])
        for tid in req["consumed"]:
            per.setdefault(tid, {}).update({
                "uncached_input_tokens": uncached // n if uncached is not None else None,
                "input_tokens": inp // n if isinstance(inp, int) else None,
                "cache_creation_tokens": cc // n if isinstance(cc, int) else None,
                "consumers": n, "consumer_request_id": req["request_id"], "consumer_timestamp": req["timestamp"],
                "consumer_source_lines": req.get("source_lines", [])})
        m = len(req["tool_uses"])
        for tid in req["tool_uses"]:
            per.setdefault(tid, {}).update({"output_tokens": outp // m if isinstance(outp, int) else None,
                                            "emitters": m, "emitter_request_id": req["request_id"],
                                            "emitter_source_lines": req.get("source_lines", []), "model": req["model"]})
    return per


def totals(parsed: dict[str, Any]) -> dict[str, Any]:
    """Totaux complets, ou None ; les sommes partielles restent identifiees comme telles.

    Un zero ecrit par le client est connu. Un champ absent (ou invalide) n'est jamais un zero.
    `missing_requests` compte les requetes auxquelles manque au moins un champ d'usage.
    """
    requests = parsed["requests"]
    n = len(requests)
    values = {k: [r["usage"][k] for r in requests if isinstance(r["usage"].get(k), int)] for k, _ in _USAGE_KEYS}
    observed = {k: sum(v) if v else None for k, v in values.items()}
    complete = sum(all(isinstance(r["usage"].get(k), int) for k, _ in _USAGE_KEYS) for r in requests)
    has_values = any(values.values())
    t: dict[str, Any] = {k: sum(v) if n and len(v) == n else None for k, v in values.items()}
    t["requests"] = n
    t["total_tokens"] = sum(t[k] for k, _ in _USAGE_KEYS) if n and complete == n else None
    t["observed_totals"] = observed
    t["observed_tokens"] = sum(v for v in observed.values() if v is not None) if has_values else None
    t["usage_coverage"] = {"requests": n, "complete_requests": complete, "missing_requests": n - complete,
                           "field_requests": {k: len(v) for k, v in values.items()},
                           "status": "complete" if n and complete == n else "partial" if has_values else "missing"}
    return t


def build_observations(client: str, session_id: str | None, view: SessionView, parsed_main: dict[str, Any],
                       parsed_subs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evenements d'observation (un par appel connu des hooks) + un evenement d'usage de session.

    # * Un tool_use_id absent des hooks (appel anterieur a l'installation, agent sans hooks)
    #   n'engendre aucun appel fictif : il est seulement compte dans l'evenement de session.
    """
    by_call_id = {c.call_id: c for c in view.calls if c.call_id}
    events: list[dict[str, Any]] = []
    matched = unmatched = 0
    agents: dict[str, Any] = {}
    for parsed in [parsed_main, *parsed_subs]:
        per = attribute_calls(parsed)
        for tid, row in per.items():
            call = by_call_id.get(tid)
            if call is None:
                unmatched += 1
                continue
            matched += 1
            ev = S.empty_event()
            ev.update({"source": S.SOURCE_IMPORT, "client": client, "session_id": session_id, "call_id": tid,
                       "agent_id": call.agent_id, "agent_type": call.agent_type, "phase": S.PHASE_OBSERVATION,
                       "hook_event_name": "transcript_usage", "model": row.get("model"), "event_time": row.get("consumer_timestamp"),
                       "usage": {"scope": "call", "source": SOURCE, "method": METHOD, "cache_read_tokens": None,
                                 **{k: row.get(k) for k in ("input_tokens", "output_tokens", "cache_creation_tokens", "uncached_input_tokens",
                                                            "consumers", "emitters", "consumer_request_id", "emitter_request_id")}}})
            ev["evidence"]["import_file"] = os.path.basename(parsed["path"])
            ev["evidence"]["usage_sources"] = {part: {"file": os.path.basename(parsed["path"]), "lines": row.get(f"{part}_source_lines", [])}
                                               for part in ("emitter", "consumer")}
            # Versionner l'observation : une relecture corrige aussi les anciennes parts qui confondaient None et 0.
            ev["event_id"] = sha256_hex(f"transcript-v2|{session_id}|{tid}|{json.dumps(ev['usage'], sort_keys=True)}|"
                                        f"{json.dumps(ev['evidence']['usage_sources'], sort_keys=True)}".encode("utf-8"))[:32]
            events.append(ev)
        if parsed is not parsed_main:
            agents[parsed["agent_id"] or os.path.basename(parsed["path"])[len("agent-"):-len(".jsonl")]] = totals(parsed)
    tot = totals(parsed_main)
    meta = {"transcript": os.path.basename(parsed_main["path"]), "transcript_bytes": parsed_main["bytes"],
            "calls_matched": matched, "calls_without_hook_events": unmatched, "subagent_transcripts": len(parsed_subs), "agents": agents,
            "parser_coverage": parsed_main.get("coverage", {})}
    sev = S.empty_event()
    sev.update({"source": S.SOURCE_IMPORT, "client": client, "session_id": session_id, "phase": S.PHASE_USAGE,
                "hook_event_name": "transcript_usage", "model": next((r["model"] for r in parsed_main["requests"] if r["model"]), None),
                "usage": {"scope": "session", "source": SOURCE, **tot}, "session_meta": meta})
    sev["event_id"] = sha256_hex(f"transcript-session|{session_id}|{json.dumps(tot, sort_keys=True)}|"
                                 f"{json.dumps(meta, sort_keys=True)}".encode("utf-8"))[:32]
    events.append(sev)
    return events, {"requests": tot["requests"], "total_tokens": tot["total_tokens"], "observed_tokens": tot["observed_tokens"],
                    "usage_coverage": tot["usage_coverage"], "calls_matched": matched,
                    "calls_without_hook_events": unmatched, "subagent_transcripts": len(parsed_subs),
                    "warnings": list(parsed_main["warnings"]) + [w for p in parsed_subs for w in p["warnings"]]}


def import_session(store: EventStore, cfg: dict[str, Any], client: str, skey: str, view: SessionView,
                   existing_ids: set[str]) -> dict[str, Any]:
    """Importe l'usage du transcript d'une session dans le spool (idempotent : identifiants derives du contenu)."""
    summary: dict[str, Any] = {"client": client, "session": skey, "transcript": None, "subagent_transcripts": 0, "requests": 0,
                               "total_tokens": None, "observed_tokens": None, "usage_coverage": None,
                               "calls_matched": 0, "calls_without_hook_events": 0, "written": 0, "skipped": 0,
                               "warnings": []}
    if client != CLIENT_CLAUDE_CODE:
        summary["warnings"].append("transcripts lus pour claude-code seulement (rollouts Codex non lus)")
        return summary
    stored = next((m.meta.get("transcript_path") for m in view.markers if m.phase == S.PHASE_SESSION_START and m.meta.get("transcript_path")), None)
    main, subs = find_transcripts(view.project_dir, view.session_id, cfg, stored)
    if main is None:
        summary["warnings"].append(f"transcript introuvable (projet {view.project_dir!r}, session {view.session_id!r}, "
                                   f"dossier {claude_projects_dir(cfg)})")
        return summary
    max_bytes = int(_section(cfg, "transcripts").get("max_bytes", 0) or 0)
    parsed_main = parse_transcript(main, max_bytes)
    parsed_subs = [parse_transcript(p, max_bytes) for p in subs]
    events, info = build_observations(client, view.session_id, view, parsed_main, parsed_subs)
    for ev in events:
        if ev["event_id"] in existing_ids:
            summary["skipped"] += 1
            continue
        store.write_event(ev)
        existing_ids.add(ev["event_id"])
        summary["written"] += 1
    summary.update({"transcript": str(main), **{k: info[k] for k in ("requests", "total_tokens", "observed_tokens", "usage_coverage", "calls_matched",
                                                                   "calls_without_hook_events", "subagent_transcripts")}})
    summary["warnings"].extend(info["warnings"])
    return summary
