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
_TOKEN = r'''(?:"[^"\n]+"|'[^'\n]+'|[^\s;&|<>]+)'''


def _arg(pattern: str, text: str) -> str | None:
    match = re.search(pattern + r"\s+(" + _TOKEN + ")", text, re.I)
    return match.group(1).strip("\"'") if match else None


def _path(value: str, cwd: str = "") -> str:
    value = value.replace("\\", "/")
    # Git Bash drive paths and Windows paths name the same artifact here.
    value = re.sub(r"^/([a-zA-Z])/", lambda m: m[1] + ":/", value)
    if not re.match(r"^(?:[a-zA-Z]:/|/)", value):
        value = cwd.rstrip("/") + "/" + value
    return posixpath.normpath(value).casefold()


def _target(command: str, cwd: str) -> dict:
    cd = _arg(r"(?:^|[;&]\s*)cd(?:\s+/d)?", command)
    workspace = _path(cd or cwd)
    python = r"(?:^|&&|\|\||[;\n])\s*python(?:3(?:\.\d+)?)?(?:\.exe)?\s+(?:-[uB]\s+)*[\"']?[^\s;&|\"']*"
    watch = _arg(python + r"watch_run\.py[\"']?", command)
    view = _arg(python + r"t5_view\.py[\"']?", command)
    invocation = re.search(python + r"run_game_scenario\.py\b([^;\n]*)", command)
    launch = _arg(r"--label", invocation[1]) if invocation else None
    target = watch or view or launch
    label = re.split(r"[/\\]", target.rstrip("/\\"))[-1] if target else None
    artifact = _path(target, workspace) if target and ("/" in target or "\\" in target) else None
    return {"workspace": workspace, "label": label, "artifact": artifact,
            "mode": "launch" if launch else "watch" if watch else "view" if view else None,
            "partial": bool(re.search(r"\|\s*(?:head|tail|cut|grep)\b", command)),
            "task_refs": sorted(set(re.findall(r"[/\\]tasks[/\\]([\w-]+)\.output\b", command)))}


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
    infos = [I._scan(p) for p in files]
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
            for event in capture.events:
                event.update(thread=info["thread_id"], cwd=obj.get("cwd", ""),
                             boundary=boundary, boundary_limits=limits, boundary_basis=basis)
                # Explicit transport receipt, not another copy of stdout.
                tr = obj.get("toolUseResult")
                event["background_task_id"] = tr.get("backgroundTaskId") if isinstance(tr, dict) else None
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
    runs, calls, tasks, task_calls, unresolved = [], {}, {}, {}, []
    for ev in events:
        data, src = ev["data"], ev["source"]
        scope = src["path"]
        if ev["kind"] == "appel":
            cid = data.get("call_id")
            valid_id = isinstance(cid, str) and bool(cid)
            inp = data.get("input") if isinstance(data.get("input"), dict) else {}
            command = inp.get("command", "")
            command = command if isinstance(command, str) else ""
            target = _target(command, ev["cwd"])
            ev["target"] = target
            candidates = []
            if target["label"]:
                candidates = [r for r in runs if r["scope"] == scope and r["label"].casefold() == target["label"].casefold()
                              and r["workspace"] == target["workspace"]
                              and (not target["artifact"] or not r["artifact"] or r["artifact"] == target["artifact"])]
            previous_call = calls.get((scope, cid)) if valid_id else None
            if previous_call and previous_call.get("run") is not None:
                candidates = [previous_call["run"]]
            elif target["mode"] == "launch" or (target["mode"] == "watch" and not candidates):
                identity = cid if valid_id else f"source:{src['line']}:{src.get('block', 0)}"
                run = {"id": f"{ev['thread']}:{identity}", "scope": scope, "thread": ev["thread"],
                       "label": target["label"], "workspace": target["workspace"], "artifact": target["artifact"],
                       "anchor": src, "calls": [], "observations": [], "task_ids": [], "warnings": []}
                runs.append(run)
                candidates = [run]
            if not candidates and target["task_refs"]:
                candidates = list({id(r): r for tid in target["task_refs"] for r in tasks.get((scope, tid), [])}.values())
            ev["run"] = candidates[0] if len(candidates) == 1 else None
            if ev["run"] is not None:
                run = ev["run"]
                run["calls"].append(ev)
                if target["artifact"]:
                    run["artifact"] = target["artifact"]
                ev["association"] = "explicit_task_artifact" if target["task_refs"] and not target["label"] else "explicit_run_target_in_workspace"
            elif target["label"] or target["task_refs"]:
                unresolved.append({"source": src, "reason": "run_absent_ou_ambigu", "candidates": [r["id"] for r in candidates]})
            if valid_id:
                calls[(scope, cid)] = ev
            else:
                unresolved.append({"source": src, "reason": "identifiant_appel_absent_ou_invalide"})
        elif ev["kind"] == "resultat":
            cid = data.get("call_id")
            call = calls.get((scope, cid)) if isinstance(cid, str) and cid else None
            run = call.get("run") if call else None
            if run is None:
                continue
            text = _text(data.get("content"))
            receipt = re.search(r"Monitor started \(task ([\w-]+)|(?:background with ID:|backgroundTaskId[\"': ]+)\s*([\w-]+)", text)
            tid = ev["background_task_id"] or (next((v for v in receipt.groups() if v), None) if receipt else None)
            if isinstance(tid, str):
                tasks.setdefault((scope, tid), []).append(run)
                task_calls[(scope, tid)] = call
                if tid not in run["task_ids"]:
                    run["task_ids"].append(tid)
            ev.update(call=call, association=call["association"], transport_receipt=bool(tid))
            run["observations"].append(_observation(ev, text, key))
        elif ev["kind"] == "notification":
            att = data.get("attachment") if isinstance(data.get("attachment"), dict) else {}
            text = _text(att.get("prompt"))
            tid, cid = _tag(text, "task-id"), _tag(text, "tool-use-id")
            candidates = tasks.get((scope, tid), [])
            call = calls.get((scope, cid)) if cid else task_calls.get((scope, tid))
            if candidates and call and call.get("run") is not None and all(call["run"] is not r for r in candidates):
                unresolved.append({"source": src, "reason": "task_id_et_tool_use_id_contradictoires", "task_id": tid, "call_id": cid})
                continue
            if not candidates and cid:
                candidates = [call["run"]] if call and call.get("run") is not None else []
            candidates = list({id(r): r for r in candidates}.values())
            if len(candidates) != 1:
                unresolved.append({"source": src, "reason": "notification_sans_lien_unique", "task_id": tid})
                continue
            ev.update(association="explicit_task_id" if tid else "explicit_tool_use_id", task_id=tid,
                      call=call,
                      process_scope="watcher" if call and call["target"]["mode"] == "watch" else "command" if call else "unknown")
            candidates[0]["observations"].append(_observation(ev, text, key))
    return {"runs": runs, "unresolved": unresolved}


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
                            | {"preview": obs["text"][:min(max_chars, 240)], "categories": dict(categories),
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
            "missing": [f for f, v in results.items() if v["status"] == "missing"], "limits": LIMITS}


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
            | {"observations": len(visible[r["id"]]), "preview": visible[r["id"]][0]["text"][:min(max_chars, 240)]}
            for r in runs[offset:offset + limit]]
    return matcher._mask_tree({**base, "runs": refs, "total_runs": len(runs), "offset": offset, "next_offset": offset + limit if offset + limit < len(runs) else None})


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
            "limits": LIMITS, "coverage": result["coverage"], "unresolved_link_count": result["unresolved_link_count"]}
