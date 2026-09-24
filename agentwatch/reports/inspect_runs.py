"""Run views over recorded Claude inspection events, never over live run files.

The adapters recognise explicit run_game_scenario/watch_run/t5_view targets and
task receipts. A clock neighbour is never a join key. This is an evidence reader,
not a replacement runner, watcher or proof of the model's input payload.
"""
from __future__ import annotations

import io
import json
import posixpath
import re
from collections import Counter
from pathlib import Path
from typing import Any, IO

from agentwatch.collector import privacy as P
from agentwatch.reports import inspect_claude as I
from agentwatch.reports.claude_context import OccurrenceGraph, _boundary
from agentwatch.reports.run_facts import parse_facts

LIMITS = [
    "Versions des sorties consignees uniquement; aucun artefact actuel ouvert.",
    "Notification enregistree != reception du modele != contenu API demontre.",
    "Compaction: presence ulterieure indeterminee; les archives restent consultables.",
    "Fin de processus, fin du scenario, tests reussis, mesures completes et acceptation metier sont distinctes.",
    "Faits extraits sans attribution de tokens; aucune economie de session deduite.",
]
def _path(value: str, cwd: str = "") -> str:
    value = value.replace("\\", "/")
    # Git Bash drive paths and Windows paths name the same artifact here.
    value = re.sub(r"^/([a-zA-Z])/", lambda m: m[1] + ":/", value)
    if not re.match(r"^(?:[a-zA-Z]:/|/)", value):
        value = cwd.rstrip("/") + "/" + value
    return posixpath.normpath(value).casefold()


def _target(command: str, cwd: str) -> dict:
    from agentwatch.reports.run_commands import target
    return target(command, cwd, _path)


def _tag(text: str, tag: str) -> str | None:
    m = re.search(r"<" + re.escape(tag) + r">(.*?)</" + re.escape(tag) + r">", text, re.S)
    return m[1].strip() if m else None


class _Capture(I.ClaudeExporter):
    """Use inspect's canonical dispatch, filters and recursive privacy policy.

    Text is retained in memory before rendering limits, just as inspect filters
    search before truncation. Discovery never emits these full payloads.
    """
    def __init__(self, key: bytes):
        super().__init__(io.StringIO(), key, fmt="jsonl")
        self.events: list[dict] = []

    def emit(self, src, kind, title, data, flags=None, text_fields=(), selectable=True, event_role=None):
        if kind not in {"appel", "resultat", "notification", "compaction", "message"}:
            return
        self.events.append({"source": self._mask_tree(src), "kind": kind,
                            "data": self._mask_tree(self._omit_reasoning(data)), "flags": flags or [],
                            "text_fields": text_fields, "role": event_role or data.get("role")})


def load_events(cfg: dict, home: str, session: str, *, thread: str | None = None,
                until: int | None = None, through_line: int | None = None) -> tuple[list[dict], dict, bytes]:
    files = I.session_files(cfg, session)
    infos = [I._scan(p, until=until, through_line=through_line) for p in files]
    for n, info in enumerate(infos):
        info["thread_id"] = files[0].stem if n == 0 else next(iter(info["agents"])) if len(info["agents"]) == 1 else info["path"].stem
    if thread:
        infos = [i for i in infos if i["thread_id"].startswith(thread) or i["path"].stem.startswith(thread)]
        if len(infos) != 1:
            raise ValueError("fil Claude Code introuvable ou prefixe ambigu")
    if through_line is not None and (not thread or through_line < 1):
        raise ValueError("--through-line positif exige --thread pour une frontiere physique non ambigue")
    key = P.ensure_key(home)
    capture = _Capture(key)
    counts: Counter = Counter()
    all_events = []
    for info in infos:
        graph = OccurrenceGraph()
        scan = {"graph": graph, "compactions": []}
        for src, obj, issue in I._rows(info["path"]):
            if through_line is not None and src["line"] > through_line:
                break
            if issue or not isinstance(obj, dict):
                counts[issue or "non_object"] += 1
                continue
            # Unknown dates remain a coverage gap, not historical knowledge.
            if src["ns"] is None:
                counts["unknown_timestamp"] += 1
                if until is not None:
                    continue
            if until is not None and src["ns"] is not None and src["ns"] >= until:
                counts["after_cutoff"] += 1
                continue
            uid = obj.get("uuid")
            compact = obj.get("type") == "system" and obj.get("subtype") == "compact_boundary"
            if isinstance(uid, str):
                graph.add(uid, {"source": src, "parent": obj.get("parentUuid"), "compaction": compact})
            if compact:
                scan["compactions"].append({"id": uid or str(src["line"]), "source": src})
            boundary, limits, _, basis = _boundary(scan, uid, src["line"], src["ns"])
            capture.events = []
            I._line(capture, obj, src, info, {"calls": {}})
            single_result = sum(e["kind"] == "resultat" for e in capture.events) == 1
            for event in capture.events:
                event.update(thread=info["thread_id"], cwd=obj.get("cwd", ""),
                             boundary=boundary, boundary_limits=limits, boundary_basis=basis)
                # Explicit transport receipt, not another copy of stdout.
                tr = obj.get("toolUseResult")
                event["background_task_id"] = tr.get("backgroundTaskId") if isinstance(tr, dict) and single_result and event["kind"] == "resultat" else None
                all_events.append(event)
        counts["files"] += 1
    return all_events, dict(counts), key


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(b.get("text", "") for b in value if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def build_index(events: list[dict], key: bytes) -> dict:
    from agentwatch.reports.run_shell import resolve_shell_calls
    from agentwatch.reports.run_links import build
    from agentwatch.reports.run_artifacts import resolve_run_artifacts
    shells = resolve_shell_calls(events, key, target_for_command=_target, normalize_path=_path)
    artifacts = resolve_run_artifacts(events, key, normalize_path=_path, text_of=_text)
    return build(events, key, target_for_command=_target, observation=_observation,
                 text_of=_text, tag=_tag, shell_calls=shells, artifact_resolver=artifacts)


def _observation(event: dict, text: str, key: bytes) -> dict:
    parsed = parse_facts(text)
    call = event.get("call")
    source = event["source"]
    is_notification = event["kind"] == "notification"
    partial = parsed["truncated"] or bool(call and call["target"]["partial"])
    return {"kind": event["kind"], "source": source, "call_source": call["source"] if call else None,
            "uuid": event["data"].get("uuid"), "request_id": call["data"].get("requestId") if call else None,
            "call_id": event["data"].get("call_id") or (call["data"].get("call_id") if call else None), "task_id": event.get("task_id"),
            "association": event["association"], "boundary": event["boundary"],
            "boundary_limits": event["boundary_limits"], "text": text,
            "version": P.fingerprint(key, text), "version_basis": "masked_recorded_output_not_artifact_hash",
            "facts": [] if event["data"].get("is_error") else parsed["facts"],
            "unparsed_lines": parsed["unparsed_lines"], "partial": partial,
            "is_error": event["data"].get("is_error"), "transport_receipt": event.get("transport_receipt", False),
            "process_scope": event.get("process_scope") or ("watcher" if call and call["target"]["mode"] == "watch" else "command"),
            "notification_recorded": is_notification,
            "reception_observed": None, "api_consumption_proven": None,
            "full_result": {"thread": event["thread"], "source_line": source["line"],
                            "kind": event["kind"], "max_chars_required": len(text) + 2048}}


def _fact_key(fact: dict) -> str:
    return json.dumps([fact["field"], fact.get("role"), fact.get("at"), fact.get("phase")], ensure_ascii=False)


def _value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def read_run(run: dict, *, fields: list[str] | None = None, since: int | None = None,
             max_chars: int = 240, selected: set[tuple] | None = None) -> dict:
    """Project recorded complements. Numeric series summarize visible samples only.

    Classifications describe facts, not the necessity of an entire model request.
    Independent verification and business necessity require analyst judgement.
    """
    observations = sorted(run["observations"], key=lambda o: (o["source"]["line"], o["source"].get("block", -1)))
    seen: dict[str, list[tuple[dict, dict]]] = {}
    timeline, all_facts = [], []
    for obs in observations:
        categories: Counter = Counter()
        facts = []
        for fact in obs["facts"]:
            if fields and fact["field"] not in fields:
                continue
            # A watcher completion cannot answer a command completion question.
            f = dict(fact)
            if f["field"] in {"process_status", "process_exit"}:
                f["role"] = f.get("role") or obs["process_scope"]
            key = _fact_key(f)
            previous = seen.get(key, [])
            same = [(p, o) for p, o in previous if _value(p["value"]) == _value(f["value"])]
            if same:
                category = "deja_fourni" if any(o["boundary"] == obs["boundary"] and obs["boundary"] is not None for _, o in same) else "relecture_apres_compaction_ou_contexte_indetermine"
            elif previous:
                category = "valeur_modifiee_ou_contradictoire"
            elif f.get("at") is not None and any(p["field"] == f["field"] and p.get("role") == f.get("role")
                                                  and p.get("at") is not None and p["at"] < f["at"] for p, _ in all_facts):
                category = "etat_plus_recent"
            else:
                category = "information_nouvelle"
            categories[category] += 1
            facts.append({**f, "category": category})
            seen.setdefault(key, []).append((f, obs))
            all_facts.append((f, obs))
        src = obs["source"]
        if (since is None or (src["ns"] is not None and src["ns"] >= since)) and (selected is None or (src["path"], src["line"], src.get("block")) in selected):
            timeline.append({k: obs[k] for k in ("kind", "source", "uuid", "request_id", "call_source", "call_id", "task_id", "association", "boundary",
                                                "version", "version_basis", "partial", "is_error", "unparsed_lines",
                                                "notification_recorded", "reception_observed", "api_consumption_proven", "full_result")}
                            | {"preview": obs["text"][:min(max_chars, 240)], "preview_truncated": len(obs["text"]) > min(max_chars, 240),
                               "link_ids": obs.get("link_ids", []), "association_known_from": obs.get("association_known_from"),
                               "transport_receipt": obs.get("transport_receipt", False), "categories": dict(categories),
                               "matched_facts": len(facts), "facts": facts})
    available_fields = sorted({f["field"] for f, _ in all_facts})
    wanted = list(dict.fromkeys(fields or available_fields))
    results = {}
    for field in wanted:
        samples = [(f, o) for f, o in all_facts if f["field"] == field
                   and (since is None or (o["source"]["ns"] is not None and o["source"]["ns"] >= since))
                   and (selected is None or (o["source"]["path"], o["source"]["line"], o["source"].get("block")) in selected)]
        if not samples:
            results[field] = {"status": "missing", "value": None}
            continue
        roles = {}
        for role in dict.fromkeys(f.get("role") for f, _ in samples):
            seq = [(f, o) for f, o in samples if f.get("role") == role]
            numeric = all(isinstance(f["value"], (int, float)) and not isinstance(f["value"], bool) for f, _ in seq)
            # Latest observed version is not necessarily the greatest scenario time.
            def evidence(pair):
                f, o = pair
                return {"value": f["value"], "at": f.get("at"), "phase": f.get("phase"), "source": o["source"], "version": o["version"],
                        "observed_at": o["source"]["ts"], "partial": o["partial"], "kind": o["kind"]}
            unique = {(_fact_key(f), _value(f["value"])) for f, _ in seq}
            entry = {"latest_recorded": evidence(seq[-1]), "distinct_visible_samples": len(unique),
                     "conflicting_keys": sum(len({_value(f["value"]) for f, _ in seq if _fact_key(f) == k}) > 1
                                             for k in {_fact_key(f) for f, _ in seq})}
            if numeric:
                entry.update(min_visible=evidence(min(seq, key=lambda p: p[0]["value"])),
                             max_visible=evidence(max(seq, key=lambda p: p[0]["value"])))
            roles[str(role) if role is not None else "unspecified"] = entry
        results[field] = {"status": "observed", "roles": roles, "coverage": "visible_samples_only_not_complete_run"}
    return {"id": run["id"], "label": run["label"], "artifact": run["artifact"], "anchor": run["anchor"],
            "task_ids": run["task_ids"], "fields": results, "timeline": timeline,
            "missing": [f for f, v in results.items() if v["status"] == "missing"], "limits": LIMITS,
            "links": run.get("links", []), "chain": chain_summary(run),
            "launch_evidence": [{"call_id": c["data"].get("call_id"), "shell": c.get("shell"),
                                 "artifact_mapping": c.get("artifact_proof")} for c in run.get("calls", [])
                                if c["target"]["mode"] == "launch"],
            "readings": reading_summary(timeline)}


def reading_summary(timeline: list[dict]) -> list[dict]:
    """Classify visible extraction, never equate no parsed delta with waste."""
    out = []
    for row in timeline:
        if row["kind"] != "resultat":
            continue
        categories = row["categories"]
        if row.get("transport_receipt"):
            state = "recu_de_tache"
        elif row["is_error"]:
            state = "erreur_a_conserver"
        elif row["unparsed_lines"] and not row["facts"]:
            state = "preuve_non_interpretee_a_examiner"
        elif categories.get("information_nouvelle") or categories.get("valeur_modifiee_ou_contradictoire"):
            state = "faits_nouveaux_ou_modifies"
        elif categories.get("etat_plus_recent"):
            state = "etat_plus_recent"
        elif row["facts"]:
            state = "aucun_nouveau_fait_extrait"
        else:
            state = "aucun_apport_parse_identifie"
        out.append({"source": row["source"], "call_id": row["call_id"], "classification": state,
                    "categories": categories, "unparsed_lines": row["unparsed_lines"], "partial": row["partial"],
                    "independent_verification": None, "usefulness_judgement": None, "full_result": row["full_result"]})
    return out


def chain_summary(run: dict) -> dict:
    calls = run.get("calls", [])
    launchers = []
    watchers = []
    for c in calls:
        item = {"call_id": c["data"].get("call_id"), "source": c["source"], "link_ids": c.get("link_ids", [])}
        shell = c.get("shell")
        if shell:
            item["script"] = {k: shell[k] for k in ("status", "invocation", "reason") if k in shell}
            item["script"]["version"] = next((e for e in shell.get("evidence", []) if e.get("kind") == "script_version"), None)
        if c.get("artifact_proof"):
            item["artifact_mapping"] = c["artifact_proof"]
        if c["target"]["mode"] == "launch":
            launchers.append(item)
        elif c["target"]["mode"] == "watch":
            watchers.append(item)
    return {"launchers": launchers, "watchers": watchers, "task_ids": run["task_ids"],
            "link_count": len(run.get("links", [])), "basis": "recorded_identifiers_and_targets; no_manifest_link"}


def launcher_states(index: dict) -> list[dict]:
    """Explicit population, including unresolved shell invocations, not just wins."""
    states = []
    seen = set()
    for call in index["calls"]:
        if call["target"]["mode"] != "launch" and "launch" not in call["target"].get("candidate_modes", []) and not call.get("shell"):
            continue
        cid, scope = call["data"].get("call_id"), call["source"]["path"]
        ident = (scope, cid, json.dumps(call["data"].get("input"), sort_keys=True))
        if ident in seen:
            continue
        seen.add(ident)
        tids = [tid for (s, tid), rows in index["tasks"].items() if s == (scope, call["thread"]) and any(x["call"] is call for x in rows)]
        run = call.get("run")
        related = [u for u in index["unresolved"] if u["source"] == call["source"]]
        if any(u["status"] == "ambiguous" for u in related):
            status = "ambiguous"
        elif run and tids and any(o["kind"] == "notification" for o in run["observations"]) and any(
                o["kind"] == "resultat" and not o["transport_receipt"] for o in run["observations"]):
            status = "complete"
        elif run or tids:
            status = "partial"
        else:
            status = "not_demonstrable"
        states.append({"call_id": cid, "source": call["source"], "status": status,
                       "run": run["id"] if run else None, "task_ids": tids,
                       "candidates": call.get("related_runs", []), "issues": related,
                       "shell": call.get("shell"), "link_ids": call.get("link_ids", [])})
    return states


def evidence_ref(event: dict, key: bytes, max_chars: int = 240) -> dict:
    data = event["data"]
    content = _text(data.get("content")) if event["kind"] == "resultat" else _text((data.get("attachment") or {}).get("prompt"))
    if event["kind"] == "appel":
        content = json.dumps(data.get("input"), ensure_ascii=False)
    return {"source": event["source"], "kind": event["kind"], "call_id": data.get("call_id"),
            "preview": content[:max_chars], "preview_truncated": len(content) > max_chars,
            "version": P.fingerprint(key, content), "version_basis": "masked_recorded_output_not_artifact_hash",
            "is_error": data.get("is_error"), "partial": parse_facts(content)["truncated"],
            "full_result": {"thread": event["thread"], "kind": event["kind"], "source_line": event["source"]["line"],
                            "max_chars_required": len(content) + 2048}}


def call_diagnostic(index, calls, events, key, fields, selected, max_chars, since=None):
    """A selected call stays inspectable even if no run association is proven."""
    call = calls[0]
    cid, scope = call["data"].get("call_id"), call["source"]["path"]
    tids = {tid for (s, tid), rows in index["tasks"].items() if s == (scope, call["thread"]) and any(any(x["call"] is c for c in calls) for x in rows)}
    related_calls = {c["data"].get("call_id") for c in index["calls"] if c["source"]["path"] == scope
                     and (c["data"].get("call_id") == cid or tids.intersection(c["target"]["task_refs"]))}
    evidence = [evidence_ref(e, key, max_chars) for e in events if e["source"]["path"] == scope
                and (since is None or (e["source"]["ns"] is not None and e["source"]["ns"] >= since))
                and (e["data"].get("call_id") in related_calls or e.get("notification_task") in tids or e.get("notification_call") == cid)
                and (selected is None or (e["source"]["path"], e["source"]["line"], e["source"].get("block")) in selected)]
    links = [e for e in index["links"] if e["scope"]["source_file"] == scope and any(
        (e[k]["kind"] == "call" and e[k]["id"] in related_calls) or (e[k]["kind"] == "task" and e[k]["id"] in tids) for k in ("from", "to"))]
    issues = [u for u in index["unresolved"] if u["source"]["path"] == scope and (u.get("call_id") in related_calls or u.get("task_id") in tids)]
    return {"call_id": cid, "scope": {"source_file": scope, "thread": call["thread"]}, "source": call["source"],
            "status": "ambiguous" if any(u["status"] == "ambiguous" for u in issues) else "unattached",
            "candidate_runs": sorted({r for c in calls for r in c.get("related_runs", [])}),
            "shell": call.get("shell"), "links": links, "issues": issues, "evidence": evidence,
            "fields": {f: {"status": "not_attributed_to_a_run", "value": None} for f in fields or []},
            "limits": ["Preuves presentes mais rattachement au run non etabli; candidats jamais traites comme liens."]}


def analyse(cfg: dict, home: str, session: str, *, run: str | None = None, fields: list[str] | None = None,
            thread: str | None = None, since: int | None = None, until: int | None = None,
            max_chars: int = 240, limit: int = 20, offset: int = 0, through_line: int | None = None, **filters) -> dict:
    if max_chars < 1 or limit < 1 or limit > 100 or offset < 0:
        raise ValueError("max_chars/limit positifs, limit <= 100 et offset >= 0 requis")
    if since is not None and until is not None and since >= until:
        raise ValueError("periode vide ou inversee")
    events, coverage, key = load_events(cfg, home, session, thread=thread, until=until, through_line=through_line)
    index = build_index(events, key)
    matcher = I.ClaudeExporter(io.StringIO(), key, **filters)
    selected = None
    if matcher.filter_active:
        selected = {(e["source"]["path"], e["source"]["line"], e["source"].get("block")) for e in events
                    if matcher._matches(e["source"], e["kind"], e["data"], e["text_fields"], e["role"])}
    base = {"schema": "agentwatch.run-view.v1", "session": session, "client": "claude-code",
            "period": {"since": since, "until_exclusive": until, "through_line_inclusive": through_line}, "coverage": coverage,
            "unresolved_links": index["unresolved"][:limit], "unresolved_link_count": len(index["unresolved"]), "limits": LIMITS}
    if run is not None:
        candidates = [r for r in index["runs"] if run in [r["id"], r["label"], r["artifact"], *r["task_ids"]]
                      or (("/" in run or "\\" in run) and _path(run) == r["artifact"])]
        by_call = [c for c in index["calls"] if run == c["data"].get("call_id")]
        if by_call:
            if len({(c["source"]["path"], c["thread"]) for c in by_call}) > 1:
                raise ValueError("appel ambigu entre fils; preciser --thread")
            call_runs = list({c["run"]["id"]: c["run"] for c in by_call if c.get("run")}.values())
            if candidates and (len(call_runs) != 1 or any(r is not call_runs[0] for r in candidates)):
                raise ValueError("selecteur ambigu entre appel et run; utiliser l'identifiant complet du run")
            candidates = call_runs
            if len(call_runs) != 1:
                return matcher._mask_tree({**base, "schema": "agentwatch.run-call.v1", "call": call_diagnostic(
                    index, by_call, events, key, fields, selected, min(max_chars, 240), since)})
        if len(candidates) != 1:
            raise ValueError("run introuvable ou ambigu; utiliser --list-runs puis l'identifiant complet")
        return matcher._mask_tree({**base, "view": read_run(candidates[0], fields=fields, since=since, max_chars=max_chars, selected=selected)})
    if fields:
        raise ValueError("--field necessite --run")
    visible = {r["id"]: [o for o in r["observations"] if (since is None or (o["source"]["ns"] is not None and o["source"]["ns"] >= since))
               and (selected is None or (o["source"]["path"], o["source"]["line"], o["source"].get("block")) in selected)]
               for r in index["runs"]}
    runs = [r for r in index["runs"] if visible[r["id"]]]
    refs = [{k: r[k] for k in ("id", "thread", "label", "artifact", "anchor", "task_ids")}
            | {"observations": len(visible[r["id"]]), "preview": visible[r["id"]][0]["text"][:min(max_chars, 240)],
               "launcher_call_ids": [c["data"].get("call_id") for c in r["calls"] if c["target"]["mode"] == "launch"],
               "link_count": len(r.get("links", []))}
            for r in runs[offset:offset + limit]]
    launchers = launcher_states(index)
    if since is not None or selected is not None:
        visible_sources = {(e["source"]["path"], e["source"]["line"], e["source"].get("block")) for e in events
                           if (since is None or (e["source"]["ns"] is not None and e["source"]["ns"] >= since))
                           and (selected is None or (e["source"]["path"], e["source"]["line"], e["source"].get("block")) in selected)}
        visible_calls = {(e["source"]["path"], e["data"].get("call_id")) for e in events
                         if (e["source"]["path"], e["source"]["line"], e["source"].get("block")) in visible_sources}
        launchers = [l for l in launchers if l["run"] in {r["id"] for r in runs}
                     or (l["source"]["path"], l["call_id"]) in visible_calls]
    return matcher._mask_tree({**base, "runs": refs, "total_runs": len(runs), "offset": offset,
                              "next_offset": offset + limit if offset + limit < len(runs) else None,
                              "launchers": launchers[offset:offset + limit], "launcher_count": len(launchers),
                              "launcher_next_offset": offset + limit if offset + limit < len(launchers) else None,
                              "launcher_status_counts": dict(Counter(l["status"] for l in launchers))})


def render(result: dict, out: IO[str], fmt: str) -> None:
    if fmt == "jsonl":
        out.write(json.dumps(result, ensure_ascii=False) + "\n")
    else:
        from agentwatch.reports.inspect import _fence
        body = json.dumps(result, ensure_ascii=False, indent=2)
        fence = _fence(body)
        out.write("# AgentWatch — vue par run\n\n" + fence + "json\n" + body + "\n" + fence + "\n")


def compact_answer(result: dict) -> dict:
    """Requested fields with a shared evidence table; full inspection stays available."""
    if "call" in result:
        return result
    if "view" not in result:
        raise ValueError("la reponse par champs necessite --run")
    view = result["view"]
    sources = {}
    fields = {}
    for field, data in view["fields"].items():
        fields[field] = {k: v for k, v in data.items() if k != "roles"}
        if "roles" not in data:
            continue
        roles = {}
        for role, values in data["roles"].items():
            reduced = {}
            for name, value in values.items():
                if not isinstance(value, dict) or "source" not in value:
                    reduced[name] = value
                    continue
                source = value["source"]
                ref = f"{source['line']}.{source.get('block', 0)}:{value['version'][:12]}"
                sources[ref] = {"line": source["line"], "block": source.get("block"), "observed_at": value["observed_at"],
                                "version": value["version"], "partial": value["partial"], "kind": value["kind"]}
                reduced[name] = {"value": value["value"], "at": value["at"], "phase": value.get("phase"), "evidence": ref}
            roles[role] = reduced
        fields[field]["roles"] = roles
    return {"schema": "agentwatch.run-fields.v1", "run": view["id"], "label": view["label"],
            "period": result["period"], "source_file": view["anchor"]["path"], "fields": fields,
            "missing": view["missing"], "evidence": sources,
            "full_result": "inspect --client claude-code --session <session> --thread <thread> --kind <kind> --source-line <line> --max-chars <bound>",
            "limits": LIMITS, "coverage": result["coverage"], "unresolved_link_count": result["unresolved_link_count"],
            "chain": view.get("chain"),
            "complements": [{k: o[k] for k in ("source", "kind", "call_id", "partial", "preview", "preview_truncated", "unparsed_lines", "full_result")}
                            for o in view["timeline"][-10:]],
            "earlier_complement_count": max(0, len(view["timeline"]) - 10)}
