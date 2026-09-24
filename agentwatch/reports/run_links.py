"""Bounded evidence graph for run inspection; never executes recorded commands."""
from __future__ import annotations

import json
import re

from agentwatch.collector import privacy as P


def source_key(event):
    s = event["source"]
    return s["path"], s["line"], s.get("block")


def _identity(event):
    cid = event["data"].get("call_id")
    return cid if isinstance(cid, str) and cid else None


def _point(source):
    return source["line"], source.get("block") or 0


def _receipts(event, text, call):
    ids = [event.get("background_task_id")]
    if call["target"]["mode"] in {"launch", "watch"} or "launch" in call["target"].get("candidate_modes", []) or call.get("shell"):
        for match in re.finditer(r"(?m)^\s*(?:Monitor started \(task ([\w-]+)|Command running in background with ID:\s*([\w-]+))", text):
            ids.extend(match.groups())
    return list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))


def build(events, key, *, target_for_command, observation, text_of, tag, shell_calls, artifact_resolver=None):
    """Index only supplied events. Late joins retain their establishing frontier.

    Calls, results and task receipts are collected independently of run discovery.
    A task is not a single-valued map: duplicate/contradictory owners remain visible.
    """
    calls, groups, tasks, runs, edges, unresolved = [], {}, {}, [], [], []

    def endpoint(kind, ident):
        return {"kind": kind, "id": ident}

    def link(rule, left, right, sources, thread, limits=()):
        sources = list({(s["path"], s["line"], s.get("block")): s for s in sources}.values())
        scope = {"source_file": sources[0]["path"], "thread": thread}
        body = {"from": left, "to": right, "scope": scope, "rule": rule, "sources": sources,
                "known_from": max(sources, key=_point), "limits": list(limits), "status": "established"}
        body["id"] = P.fingerprint(key, json.dumps(body, sort_keys=True))
        if not any(e["id"] == body["id"] for e in edges):
            edges.append(body)
        return body["id"]

    def issue(event, reason, candidates=(), status="unattached", **extra):
        row = {"source": event["source"], "call_id": _identity(event), "reason": reason,
               "status": status, "candidates": list(candidates), **extra}
        if row not in unresolved:
            unresolved.append(row)

    # A copied identical call is one identity; contradictory copies are not.
    for event in events:
        if event["kind"] != "appel":
            continue
        inp = event["data"].get("input")
        inp = inp if isinstance(inp, dict) else {}
        command = inp.get("command", "")
        command = command if isinstance(command, str) else ""
        target = target_for_command(command, event["cwd"])
        if event["data"].get("name") == "Read" and isinstance(inp.get("file_path"), str):
            target["task_refs"] = re.findall(r"[/\\]tasks[/\\]([\w-]+)\.output\b", inp["file_path"])
            target["partial"] = bool(inp.get("offset") or inp.get("limit"))
        shell = shell_calls.get(source_key(event))
        if shell and shell["status"] == "established":
            target = shell["target"]
        artifact_proof = None
        if target["mode"] == "launch" and artifact_resolver and target.get("runner_script"):
            artifact_proof = artifact_resolver.resolve(target["runner_script"], target["label"], event["source"])
            if artifact_proof:
                target["artifact"] = artifact_proof["artifact"]
        event.update(target=target, shell=shell, artifact_proof=artifact_proof, run=None, link_ids=[], related_runs=[])
        calls.append(event)
        cid = _identity(event)
        if cid:
            groups.setdefault((event["source"]["path"], event["thread"], cid), []).append(event)
        else:
            issue(event, "identifiant_appel_absent_ou_invalide")
    conflicts = {k for k, seq in groups.items() if len({json.dumps([e["data"].get("name"), e["data"].get("input")], sort_keys=True) for e in seq}) > 1}
    owners = {k: seq[0] for k, seq in groups.items() if k not in conflicts}

    def owner(event, cid=None):
        cid = _identity(event) if cid is None else cid
        return owners.get((event["source"]["path"], event["thread"], cid)) if isinstance(cid, str) and cid else None

    def register_task(event, call, tid, rule):
        slot = tasks.setdefault(((event["source"]["path"], event["thread"]), tid), [])
        edge = link(rule, endpoint("call", _identity(call)), endpoint("task", tid),
                    [call["source"], event["source"]], call["thread"])
        if not any(x["call"] is call and x["source"] == event["source"] for x in slot):
            slot.append({"call": call, "source": event["source"], "link_id": edge})
        return edge

    # Receipts remain usable even when the launcher is not yet a recognised run.
    for event in events:
        if event["kind"] == "resultat":
            call = owner(event)
            if call:
                event["call"] = call
                content = text_of(event["data"].get("content"))
                event["receipt_ids"] = _receipts(event, content, call)
                for tid in event["receipt_ids"]:
                    register_task(event, call, tid, "result_call_id_and_task_receipt")
        elif event["kind"] == "notification":
            att = event["data"].get("attachment") or {}
            text = text_of(att.get("prompt"))
            tid, cid = tag(text, "task-id"), tag(text, "tool-use-id")
            event.update(notification_task=tid, notification_call=cid)
    # A notification naming both IDs is another explicit correspondence, but it
    # cannot overwrite a conflicting receipt. Preserve that contradiction below.
    for event in events:
        if event["kind"] == "notification":
            tid, cid = event["notification_task"], event["notification_call"]
            call = owner(event, cid)
            if tid and call and not tasks.get(((event["source"]["path"], event["thread"]), tid)):
                register_task(event, call, tid, "notification_task_id_and_call_id")

    def new_run(call):
        src, target = call["source"], call["target"]
        cid = _identity(call)
        identity = cid or f"source:{src['line']}:{src.get('block', 0)}"
        if (src["path"], call["thread"], cid) in conflicts:
            identity += f"@{src['line']}.{src.get('block', 0)}"
        run = {"id": f"{call['thread']}:{identity}", "scope": src["path"], "thread": call["thread"],
               "label": target["label"], "workspace": target["workspace"], "artifact": target["artifact"],
               "anchor": src, "calls": [], "observations": [], "task_ids": [], "warnings": [], "links": []}
        runs.append(run)
        return run

    def label_candidates(call):
        target, src = call["target"], call["source"]
        return [r for r in runs if r["scope"] == src["path"] and r["thread"] == call["thread"] and _point(r["anchor"]) <= _point(src)
                and r["label"].casefold() == (target["label"] or "").casefold()
                and r["workspace"] == target["workspace"]
                and (not target["artifact"] or r["artifact"] == target["artifact"])]

    def bind(call, run, rule, proof_sources=(), limits=()):
        call["run"] = run
        call["association"] = rule
        if all(source_key(c) != source_key(call) for c in run["calls"]):
            run["calls"].append(call)
        if call["target"]["artifact"]:
            run["artifact"] = call["target"]["artifact"]
        refs = [call["source"], run["anchor"], *proof_sources]
        edge = link(rule, endpoint("call", _identity(call) or f"source:{call['source']['line']}"),
                    endpoint("run", run["id"]), refs, call["thread"], limits)
        call["link_ids"].append(edge)

    # Chronological anchoring keeps successive attempts separate. Later task
    # evidence may attach an earlier read, but never merges same-label attempts.
    for call in calls:
        target, shell = call["target"], call["shell"]
        if not target["mode"]:
            continue
        cid = _identity(call)
        previous = owner(call)
        if previous is not call and previous and previous["run"]:
            bind(call, previous["run"], "identical_call_copy", [previous["source"]])
            continue
        candidates = label_candidates(call)
        if target["mode"] == "launch":
            candidates = [new_run(call)]
        elif target["mode"] == "watch" and not candidates:
            candidates = [new_run(call)]
        if (call["source"]["path"], call["thread"], cid) in conflicts:
            issue(call, "call_id_contradictoire", [r["id"] for r in candidates], "ambiguous")
            continue
        if len(candidates) == 1:
            sources, limits = [], []
            if shell:
                for evidence in shell.get("evidence", []):
                    if evidence.get("source"):
                        sources.append(evidence["source"])
                    sources.extend(evidence.get("sources", []))
                limits.append("Mapping de la version de script consignée; modifications hors journal indéterminées.")
            if call.get("artifact_proof"):
                sources.extend(call["artifact_proof"]["sources"])
                limits.extend(call["artifact_proof"].get("limits", []))
            if target["mode"] == "launch" and not target["artifact"]:
                limits.append("Dossier de sortie non demontre; aucun rattachement par basename seul.")
            if target["mode"] == "launch":
                limits.append("Invocation consignée; ce lien seul ne prouve pas le demarrage du processus enfant.")
            bind(call, candidates[0], "recorded_shell_argument_mapping" if shell else "explicit_run_target_in_workspace", sources, limits)
        else:
            call["related_runs"] = [r["id"] for r in candidates]
            issue(call, "run_absent_ou_ambigu", call["related_runs"], "ambiguous" if candidates else "unattached")

    # Task reads may precede receipt/call records in physical order. All links
    # carry known_from, and the caller supplies only the requested prefix.
    for call in calls:
        refs = call["target"]["task_refs"]
        if not refs:
            continue
        task_owners = [x for tid in refs for x in tasks.get(((call["source"]["path"], call["thread"]), tid), [])]
        candidates = {x["call"]["run"]["id"]: x["call"]["run"] for x in task_owners if x["call"]["run"] is not None}
        unique_owners = {tid: {id(x["call"]) for x in tasks.get(((call["source"]["path"], call["thread"]), tid), [])} for tid in refs}
        for tid in refs:
            proof = [x["source"] for x in tasks.get(((call["source"]["path"], call["thread"]), tid), [])]
            call["link_ids"].append(link("literal_task_output_reference", endpoint("call", _identity(call)), endpoint("task", tid),
                                          [call["source"], *proof], call["thread"]))
        contradictory = any(len(v) > 1 for v in unique_owners.values())
        missing_owner = not all(unique_owners.values())
        if call["run"] is not None and (contradictory or missing_owner or (candidates and call["run"]["id"] not in candidates)):
            # A target label and a task ID disagree: detach rather than choose.
            old = call["run"]
            old["calls"] = [c for c in old["calls"] if c is not call]
            for edge in edges:
                if edge["id"] in call["link_ids"] and edge["to"]["kind"] == "run":
                    edge["status"] = "contradicted_or_incomplete"
                    edge["limits"].append("Les autres references de tache de cet appel ne permettent pas ce rattachement unique.")
            call["run"] = None
            contradictory = contradictory or bool(candidates and old["id"] not in candidates)
        if not contradictory and len(candidates) == 1 and all(unique_owners.values()):
            if call["run"] is None:
                bind(call, next(iter(candidates.values())), "explicit_task_artifact", [x["source"] for x in task_owners])
        elif call["run"] is None or contradictory:
            call["related_runs"] = list(candidates)
            issue(call, "task_reference_conflicting_or_unresolved", candidates, "ambiguous" if contradictory or len(candidates) > 1 else "unattached")

    for event in events:
        if event["kind"] == "resultat":
            call = owner(event)
            if not call:
                if (event["source"]["path"], event["thread"], _identity(event)) in conflicts:
                    issue(event, "resultat_call_id_contradictoire", status="ambiguous")
                continue
            run = call["run"]
            if run is None:
                continue
            text = text_of(event["data"].get("content"))
            event.update(call=call, association=call["association"], transport_receipt=bool(event.get("receipt_ids")))
            edge = link("tool_result_call_id", endpoint("result", f"{_identity(event)}@{event['source']['line']}.{event['source'].get('block', 0)}"),
                        endpoint("call", _identity(call)), [event["source"], call["source"]], event["thread"])
        elif event["kind"] == "notification":
            tid, cid = event["notification_task"], event["notification_call"]
            rows = tasks.get(((event["source"]["path"], event["thread"]), tid), []) if tid else []
            task_calls = {id(x["call"]): x["call"] for x in rows}
            named = owner(event, cid)
            if len(task_calls) > 1 or (cid and task_calls and cid not in {_identity(c) for c in task_calls.values()}):
                issue(event, "task_id_et_tool_use_id_contradictoires", [c["run"]["id"] for c in task_calls.values() if c["run"]],
                      "ambiguous", task_id=tid)
                continue
            call = named or next(iter(task_calls.values()), None)
            if call is None or call["run"] is None:
                issue(event, "notification_sans_lien_unique", task_id=tid)
                continue
            run = call["run"]
            text = text_of((event["data"].get("attachment") or {}).get("prompt"))
            event.update(call=call, task_id=tid, association="explicit_task_id" if rows else "explicit_tool_use_id",
                         process_scope="watcher" if call["target"]["mode"] == "watch" else "command")
            edge = link("notification_identifiers", endpoint("notification", f"{event['source']['line']}.{event['source'].get('block', 0)}"),
                        endpoint("task" if tid else "call", tid or cid), [event["source"], call["source"], *[r["source"] for r in rows]], event["thread"])
        else:
            continue
        obs = observation(event, text, key)
        obs["link_ids"] = [*call["link_ids"], edge]
        relevant = [e for e in edges if e["id"] in obs["link_ids"]]
        obs["association_known_from"] = max((e["known_from"] for e in relevant), key=_point)
        run["observations"].append(obs)

    for (scope, tid), rows in tasks.items():
        linked = {x["call"]["run"]["id"]: x["call"]["run"] for x in rows if x["call"]["run"]}
        for run in linked.values():
            if tid not in run["task_ids"]:
                run["task_ids"].append(tid)
    for call in calls:
        if call["target"].get("unresolved_target"):
            candidates = [r["id"] for r in runs if r["scope"] == call["source"]["path"] and r["thread"] == call["thread"]
                          and any(t["label"] == r["label"] and t["workspace"] == r["workspace"] for t in call["target"].get("candidate_targets", []))]
            call["related_runs"] = candidates
            issue(call, call["target"]["unresolved_target"], candidates, status="ambiguous" if len(candidates) > 1 else "unattached",
                  candidate_basis="literal_target_only_not_a_link")
        if call["shell"] and call["shell"]["status"] != "established":
            args = call["shell"].get("invocation", {}).get("arguments", [])
            candidates = [r["id"] for r in runs if r["scope"] == call["source"]["path"] and r["thread"] == call["thread"] and r["label"] in args]
            call["related_runs"] = candidates
            issue(call, call["shell"].get("reason", "historical_script_unresolved"), candidates,
                  "ambiguous" if len(candidates) > 1 else "unattached", candidate_basis="literal_argument_only_not_a_link")
    for run in runs:
        cids = {_identity(c) for c in run["calls"]}
        tids = set(run["task_ids"])
        run["links"] = [e for e in edges if e["scope"]["source_file"] == run["scope"] and e["scope"]["thread"] == run["thread"] and any(
            (e[k]["kind"] == "run" and e[k]["id"] == run["id"]) or
            (e[k]["kind"] == "call" and e[k]["id"] in cids) or
            (e[k]["kind"] == "task" and e[k]["id"] in tids) for k in ("from", "to"))]
    return {"runs": runs, "unresolved": unresolved, "calls": calls, "links": edges, "tasks": tasks}
