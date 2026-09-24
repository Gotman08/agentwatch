"""Fenêtres Claude observées, sans déduire la rétention du seul ordre du transcript.

Le graphe est résolu par occurrence locale: une copie réapparentée après compaction
ne réécrit pas le passé de la requête originale. Les requêtes sont ensuite réunies
par leur identifiant, jamais par leur contenu. Aucun texte brut n'est stocké.
"""
from __future__ import annotations

import bisect
import io
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from agentwatch.collector import privacy as P, transcripts as T
from agentwatch.core.normalize import categorize_tool
from agentwatch.reports import inspect_claude as I

INPUT_KEYS = ("input_tokens", "cache_creation_tokens", "cache_read_tokens")
CONFIG_KEYS = ("permissionMode", "effort", "service_tier")
CONTEXT_ATTACHMENTS = {"thinking_drop", "prompt_snapshot", "deferred_tools_delta", "mcp_instructions_delta",
                       "instructions", "environment", "model", "compact_file_reference"}
RETENTION_LIMIT = ("Contenu observe dans le journal; conservation demontree seulement pour les UUID listes par une "
                   "compaction. Presence dans les requetes suivantes indeterminee. Les deltas sont des calculs "
                   "sur releves, pas des mesures du poids d'un outil ni des economies.")


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(b["text"] for b in value if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _input(usage: dict) -> int | None:
    values = [usage.get(k) for k in INPUT_KEYS]
    return sum(values) if all(isinstance(v, int) for v in values) else None


def _consensus(values: list[Any]) -> tuple[Any, bool]:
    known = {json.dumps(v, sort_keys=True): v for v in values if v is not None}
    return (next(iter(known.values())), False) if len(known) == 1 else (None, len(known) > 1)


def source_files(cfg: dict, sessions: list[str]) -> list[Path]:
    return sorted({p for session in sessions for p in I.session_files(cfg, session)})


class OccurrenceGraph:
    """Parents tels qu'ils existaient à la ligne étudiée, sans fusion des copies."""
    def __init__(self):
        self.nodes: dict[str, list[dict]] = defaultdict(list)
        self.lines: dict[str, list[int]] = defaultdict(list)

    def add(self, uid: str, node: dict) -> None:
        self.nodes[uid].append(node)
        self.lines[uid].append(node["source"]["line"])

    def boundary(self, uid: str | None, cutoff: int) -> tuple[str | None, list[str]]:
        seen = set()
        while uid:
            if uid in seen:
                return None, ["cycle_parentUuid"]
            seen.add(uid)
            rank = bisect.bisect_right(self.lines.get(uid, []), cutoff) - 1
            if rank < 0:
                return None, ["parentUuid_absent_ou_posterieur:" + uid]
            node = self.nodes[uid][rank]
            if node["compaction"]:
                return uid, []
            parent = node["parent"]
            if parent is None:
                return "initial:" + uid, ["debut_du_graphe_observe_pas_preuve_de_demarrage"]
            uid = parent
        return None, ["uuid_absent"]


def _boundary(scan: dict, uid: str | None, line: int, ns: int | None) -> tuple:
    boundary, limits = scan["graph"].boundary(uid, line)
    graph_boundary, basis = boundary, "parentUuid_local_a_cette_occurrence"
    recorded = [c for c in scan["compactions"] if c["source"]["line"] <= line
                and ns is not None and c["source"]["ns"] is not None and c["source"]["ns"] <= ns]
    if recorded:
        boundary = max(recorded, key=lambda c: (c["source"]["ns"], c["source"]["line"]))["id"]
        basis = "compaction_enregistree_precedant_la_requete_dans_ce_fichier_et_dans_le_temps"
    compact = next((c for c in scan["compactions"] if c["id"] == boundary), None)
    if compact and ns is not None and compact["source"]["ns"] is not None and ns < compact["source"]["ns"]:
        boundary, limits = None, ["requete_historique_anterieure_a_la_frontiere_copiee"]
    return boundary, limits, graph_boundary, basis


def _scan(path: Path, sanitizer: I.ClaudeExporter, key: bytes) -> dict:
    parsed = T.parse_transcript(path)
    graph = OccurrenceGraph()
    by_line, contents, calls, compactions, metadata = {}, [], [], [], []
    counts = Counter()
    for src, obj, issue in I._rows(path):
        if issue or not isinstance(obj, dict):
            counts[issue or "non_object"] += 1
            continue
        kind = obj.get("type")
        kind = kind if isinstance(kind, str) else "unknown"
        counts[kind] += 1
        uid, parent = _string(obj.get("uuid")), _string(obj.get("parentUuid"))
        is_compact = kind == "system" and obj.get("subtype") == "compact_boundary"
        node = {"source": src, "parent": parent, "compaction": is_compact,
                "config": {k: obj.get(k) for k in CONFIG_KEYS}}
        by_line[src["line"]] = {**node, "uuid": uid}
        if uid:
            graph.add(uid, node)
        if is_compact:
            raw = obj.get("compactMetadata")
            cm = raw if isinstance(raw, dict) else {}
            preserved = cm.get("preservedMessages")
            preserved = preserved if isinstance(preserved, dict) else {}
            ids = preserved.get("allUuids", preserved.get("uuids"))
            ids = sorted(set(ids)) if isinstance(ids, list) and all(isinstance(v, str) for v in ids) else None
            compactions.append({"id": uid or f"{path}:{src['line']}", "source": src,
                                "preTokens": T._int(cm.get("preTokens")), "postTokens": T._int(cm.get("postTokens")),
                                "trigger": sanitizer.text(cm.get("trigger"))[0], "preserved_ids": ids,
                                "preserved_ids_available": ids is not None})
        if kind not in ("assistant", "user", "attachment", "file-history-snapshot", "queue-operation"):
            # Metadata carries observations; bridge-session/mode are not a context reset.
            if kind in ("bridge-session", "mode", "atis-latch", "system"):
                detail = {k: obj[k] for k in obj if k not in ("content", "compactMetadata", "uuid", "parentUuid")}
                metadata.append({"kind": kind, "source": src, "detail": sanitizer.text(detail)[0],
                                 "is_context_boundary": is_compact})
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        for block_index, block in enumerate(I._blocks(obj)):
            if not isinstance(block, dict):
                continue
            bkind = block.get("type")
            if bkind == "tool_use":
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                target = next((inp[k] for k in ("file_path", "path", "command", "query", "pattern") if isinstance(inp.get(k), str)), None)
                name = _string(block.get("name"))
                calls.append({"call_id": _string(block.get("id")), "name": name,
                              "category": categorize_tool("claude-code", name)[0], "target": sanitizer.text(target)[0],
                              "source": {**src, "block": block_index}, "uuid": uid})
            elif bkind in ("tool_result", "text"):
                text = _text(block.get("content")) if bkind == "tool_result" else _text(block.get("text"))
                if bkind == "text" and kind not in ("user", "assistant"):
                    continue
                raw = block.get("content") if bkind == "tool_result" else block.get("text")
                text_only = isinstance(raw, str) or (isinstance(raw, list) and all(
                    isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) for b in raw))
                encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                contents.append({"kind": "tool_result" if bkind == "tool_result" else kind + "_text", "uuid": uid,
                                 "call_id": _string(block.get("tool_use_id")), "source": {**src, "block": block_index},
                                 "bytes": len(text.encode("utf-8")) if text_only else None,
                                 "text_bytes": len(text.encode("utf-8")), "serialized_json_bytes": len(encoded.encode("utf-8")),
                                 "text_only": text_only, "content_fingerprint": P.fingerprint(key, text if text_only else encoded),
                                 "preview": sanitizer.text(text)[0], "is_error": block.get("is_error"),
                                 "internal_error_hint": "Traceback (most recent call last)" in text or "<tool_use_error>" in text})
        if isinstance(msg.get("content"), str):
            text = msg["content"]
            contents.append({"kind": kind + "_text", "uuid": uid, "source": src, "bytes": len(text.encode("utf-8")),
                             "content_fingerprint": P.fingerprint(key, text), "preview": sanitizer.text(text)[0]})
        attachment = obj.get("attachment")
        if kind == "attachment" and isinstance(attachment, dict) and attachment.get("type") == "queued_command":
            text = _text(attachment.get("prompt")) or _text(obj.get("rendered"))
            contents.append({"kind": "notification", "uuid": uid, "source": src, "bytes": len(text.encode("utf-8")),
                             "content_fingerprint": P.fingerprint(key, text), "preview": sanitizer.text(text)[0]})
        elif kind == "attachment" and isinstance(attachment, dict):
            subtype = attachment.get("type")
            if isinstance(subtype, str) and subtype in CONTEXT_ATTACHMENTS:
                # Never export prompt snapshots or thinking text; explicit counters,
                # identifiers and byte sizes are sufficient to locate and explain them.
                values = {k: attachment[k] for k in ("requestId", "newlyDropped", "thinkingBlocksSent", "thinkingTurnsSent",
                          "firstReportForThreadInProcess", "clientChange", "querySource", "model", "blockHashes",
                          "changed", "reason", "identity") if k in attachment}
                if subtype == "environment":
                    snapshot = attachment.get("snapshot")
                    values["snapshot"] = {k: snapshot[k] for k in ("workingDirectory", "additionalWorkingDirectories", "isGitRepo",
                                          "isWorktree", "platform", "osVersion", "shell") if k in snapshot} if isinstance(snapshot, dict) else None
                    changes = attachment.get("changes")
                    values["changes_count"] = len(changes) if isinstance(changes, list) else None
                    values["changes"] = [{k: sanitizer.text(v[k])[0] for k in ("field", "from", "to") if k in v}
                                         for v in changes if isinstance(v, dict)] if isinstance(changes, list) else None
                if subtype == "instructions":
                    files = attachment.get("files")
                    values["files"] = [{"path": sanitizer.text(v.get("path"))[0], "type": sanitizer.text(v.get("type"))[0],
                                        "content_bytes": len(v["content"].encode("utf-8")) if isinstance(v.get("content"), str) else None,
                                        "content_fingerprint": P.fingerprint(key, v["content"]) if isinstance(v.get("content"), str) else None}
                                       for v in files if isinstance(v, dict)] if isinstance(files, list) else None
                sizes = {k: len(json.dumps(v, ensure_ascii=False).encode("utf-8")) for k, v in attachment.items() if k != "type"}
                contents.append({"kind": "context_change", "uuid": uid, "source": src, "subtype": subtype,
                                 "request_id": _string(attachment.get("requestId")), "observed_fields": sanitizer._mask_tree(values),
                                 "field_json_bytes": sizes, "bytes": sum(sizes.values()),
                                 "content_fingerprint": P.fingerprint(key, json.dumps(attachment, sort_keys=True)),
                                 "retention_scope": "evenement seulement, pas maintien dans chaque requete suivante"})
    return {"path": path, "parsed": parsed, "graph": graph, "by_line": by_line,
            "contents": contents, "calls": calls, "compactions": compactions, "metadata": metadata, "line_types": dict(counts)}


def analyse(cfg: dict, home: str, sessions: list[str]) -> dict:
    started = time.perf_counter()
    paths = source_files(cfg, sessions)
    key = P.ensure_key(home)
    sanitizer = I.ClaudeExporter(io.StringIO(), key, max_chars=240)
    scans = [_scan(path, sanitizer, key) for path in paths]
    comp_by_id, observations, content_by_id, calls_by_id = {}, defaultdict(list), {}, {}
    for scan in scans:
        path = scan["path"]
        for compact in scan["compactions"]:
            entry = comp_by_id.setdefault(compact["id"], {**compact, "sources": [], "observations": [], "preserved": []})
            entry["sources"].append(compact["source"])
            entry["observations"].append(compact)
        for req in scan["parsed"]["requests"]:
            node = scan["by_line"].get(req["first_line"], {})
            ns = I._ns(req.get("timestamp"))
            boundary, limits, graph_boundary, basis = _boundary(scan, node.get("uuid"), req["first_line"], ns)
            src = {"path": str(path), "file": path.name, "line": req["first_line"], "lines": req["source_lines"], "ts": req["timestamp"]}
            observations[req["request_id"]].append({"req": req, "boundary": boundary, "limits": limits, "source": src,
                                                     "config": node.get("config", {}), "uuid": node.get("uuid"),
                                                     "graph_boundary": graph_boundary, "boundary_basis": basis})
        for content in scan["contents"]:
            ident = (content["uuid"] or str(path) + ":" + str(content["source"]["line"]),
                     content["source"].get("block"), content["kind"], content["content_fingerprint"])
            entry = content_by_id.setdefault(ident, {**content, "sources": [], "occurrences": []})
            boundary, *_ = _boundary(scan, content["uuid"], content["source"]["line"], content["source"]["ns"])
            entry["sources"].append(content["source"])
            entry["occurrences"].append({"boundary_id": boundary, "source": content["source"]})
        for call in scan["calls"]:
            ident = call["call_id"] or str(path) + ":" + str(call["source"]["line"])
            entry = calls_by_id.setdefault(ident, {**call, "sources": []})
            entry["sources"].append(call["source"])
    requests = []
    for rid, rows in observations.items():
        usage, conflicts = {}, []
        conflict_sources = [{"source": r["source"], "conflict": c} for r in rows for c in r["req"].get("usage_conflicts", [])]
        for k, _ in T._USAGE_KEYS:
            usage[k], conflict = _consensus([r["req"]["usage"].get(k) for r in rows])
            if conflict or any(c["conflict"].get("field") == k for c in conflict_sources):
                usage[k] = None
                conflicts.append(k)
        model, model_conflict = _consensus([r["req"].get("model") for r in rows])
        timestamp, timestamp_conflict = _consensus([r["req"].get("timestamp") for r in rows])
        config, config_conflict = _consensus([r["config"] for r in rows])
        boundaries = sorted({r["boundary"] for r in rows if r["boundary"]})
        boundary = boundaries[0] if len(boundaries) == 1 else None
        limits = sorted({v for r in rows for v in r["limits"]})
        if len(boundaries) > 1:
            limits.append("frontieres_concurrentes:" + ",".join(boundaries))
        requests.append({"request_id": rid, "usage": usage, "usage_conflicts": conflicts, "usage_conflict_sources": conflict_sources,
                         "usage_observations": [{"usage": r["req"]["usage"], "basis": r["req"].get("usage_basis"),
                                                  "conflicts": r["req"].get("usage_conflicts"), "source": r["source"]} for r in rows],
                         "model": model, "model_conflict": model_conflict, "configuration": config,
                         "configuration_conflict": config_conflict, "timestamp": timestamp, "timestamp_conflict": timestamp_conflict,
                         "boundary_id": boundary, "boundary_limits": limits, "sources": [r["source"] for r in rows],
                         "boundary_observations": [{"boundary": r["boundary"], "graph_boundary": r["graph_boundary"],
                                                    "basis": r["boundary_basis"], "source": r["source"]} for r in rows],
                         "tool_uses": sorted({t for r in rows for t in r["req"]["tool_uses"]}),
                         "candidate_results": sorted({t for r in rows for t in r["req"]["consumed"]}),
                         "candidate_results_basis": "ordre transcript, pas preuve du contenu exact envoye a l'API",
                         "input_total": _input(usage), "input_delta": None, "residual_after_previous_output": None})
    requests.sort(key=lambda r: (I._ns(r["timestamp"]) is None, I._ns(r["timestamp"]) or 0, r["request_id"]))
    windows = {}
    previous = {}
    segments = Counter()
    for req in requests:
        boundary = req["boundary_id"]
        who = boundary or "indetermine:" + req["sources"][0]["file"]
        prev = previous.get(who)
        if prev and (prev["model"] != req["model"] or prev["configuration"] != req["configuration"]):
            segments[who] += 1
        group = (who, segments[who])
        window = windows.setdefault(group, {"boundary_id": boundary, "model": req["model"], "configuration": req["configuration"],
                                            "requests": [], "content_counts": {}, "largest_results": [], "notifications": [], "useful_result_candidates": []})
        ns, prev_ns = I._ns(req["timestamp"]), I._ns(prev["timestamp"]) if prev else None
        comparable = (prev and boundary and ns is not None and prev_ns is not None and ns > prev_ns
                      and prev["model"] == req["model"] and prev["configuration"] == req["configuration"])
        if comparable and req["input_total"] is not None and prev["input_total"] is not None:
            req["input_delta"] = req["input_total"] - prev["input_total"]
            if isinstance(prev["usage"].get("output_tokens"), int):
                req["residual_after_previous_output"] = req["input_delta"] - prev["usage"]["output_tokens"]
        previous[who] = req
        window["requests"].append(req)
    # The first segment includes its initial prompt; the last includes terminal
    # blocks after the timestamp of its last request. A configuration change
    # splits observed content at the first response carrying the new setting.
    for (who, segment), window in windows.items():
        next_window = windows.get((who, segment + 1))
        window["content_interval"] = {
            "since_ns": I._ns(window["requests"][0]["timestamp"]) if segment else None,
            "until_ns": I._ns(next_window["requests"][0]["timestamp"]) if next_window else None,
            "basis": "meme_frontiere_observee_et_intervalle_temporel_semi_ouvert; pas contenu exact de requete"}
    # Content references are attached to observed occurrences, not claimed retained in every request.
    for compact in comp_by_id.values():
        compact["metadata_conflicts"] = []
        for field in ("preTokens", "postTokens", "trigger", "preserved_ids"):
            compact[field], conflict = _consensus([o[field] for o in compact["observations"]])
            if conflict:
                compact["metadata_conflicts"].append(field)
        compact["preserved_ids_available"] = compact["preserved_ids"] is not None
        for uid in compact["preserved_ids"] or []:
            matches = [c for c in content_by_id.values() if c["uuid"] == uid]
            compact["preserved"].append({"uuid": uid, "contents": [{"kind": c["kind"], "bytes": c["bytes"], "sources": c["sources"]} for c in matches],
                                          "found": bool(matches)})
        compact["preservation_scope"] = "UUID explicitement listes a cette compaction seulement; contenu exact des requetes suivantes indetermine"
    for window in windows.values():
        rows = window.pop("requests")
        points = [r for r in rows if r["input_total"] is not None]
        window.update({"request_ids": [r["request_id"] for r in rows], "first_time": rows[0]["timestamp"], "last_time": rows[-1]["timestamp"],
                       "first_source": rows[0]["sources"][0], "last_source": rows[-1]["sources"][0],
                       "usage": T.totals({"requests": rows}), "input_initial": rows[0]["input_total"], "input_last": rows[-1]["input_total"],
                       "input_peak": max((r["input_total"] for r in points), default=None),
                       "growth_first_to_last": rows[-1]["input_total"] - rows[0]["input_total"] if rows[0]["input_total"] is not None and rows[-1]["input_total"] is not None else None,
                       "boundary": comp_by_id.get(window["boundary_id"]),
                       "negative_deltas": sum(r["input_delta"] is not None and r["input_delta"] < 0 for r in rows)})
        lo, hi = window["content_interval"]["since_ns"], window["content_interval"]["until_ns"]
        candidates = []
        for content in content_by_id.values():
            matched = [o for o in content["occurrences"] if window["boundary_id"] and o["boundary_id"] == window["boundary_id"]
                       and o["source"]["ns"] is not None and (lo is None or lo <= o["source"]["ns"])
                       and (hi is None or o["source"]["ns"] < hi)]
            if matched:
                candidates.append({**content, "window_sources": [o["source"] for o in matched]})
        candidates.sort(key=lambda c: min(s["ns"] for s in c["window_sources"]))
        first_ns = I._ns(window["first_time"])
        window["content_before_first_request"] = sum(first_ns is not None and any(s["ns"] < first_ns for s in c["window_sources"]) for c in candidates)
        window["content_counts"] = dict(Counter(c["kind"] for c in candidates))
        window["content_bytes"] = {kind: sum(c["bytes"] for c in candidates if c["kind"] == kind and c["bytes"] is not None) for kind in window["content_counts"]}
        window["content_bytes_unknown_counts"] = dict(Counter(c["kind"] for c in candidates if c["bytes"] is None))
        window["largest_results"] = sorted((c for c in candidates if c["kind"] == "tool_result" and c["bytes"] is not None), key=lambda c: c["bytes"], reverse=True)[:8]
        window["non_text_results"] = [c for c in candidates if c["kind"] == "tool_result" and c["bytes"] is None]
        window["notifications"] = sorted((c for c in candidates if c["kind"] == "notification"), key=lambda c: c["bytes"], reverse=True)[:5]
        window["context_changes"] = [c for c in candidates if c["kind"] == "context_change"]
        window["useful_result_candidates"] = [c for c in candidates if c["kind"] == "assistant_text"][-3:]
        ids = {cid for r in rows for cid in r["tool_uses"]}
        window["reads"] = [c for cid, c in calls_by_id.items() if cid in ids and c["category"] in ("read", "search", "shell", "mcp")]
    return sanitizer._mask_tree({"version": 1, "client": "claude-code", "sessions": sessions,
            "retention_limit": RETENTION_LIMIT, "usage": T.totals({"requests": requests}), "requests": requests,
            "windows": list(windows.values()), "compactions": list(comp_by_id.values()),
            "duplicates": {"request_observations_removed": sum(len(v) - 1 for v in observations.values()),
                           "compaction_observations_removed": sum(len(c["sources"]) - 1 for c in comp_by_id.values())},
            "coverage": [{"path": str(s["path"]), "bytes": s["parsed"]["bytes"], "parser": s["parsed"]["coverage"],
                           "line_types": s["line_types"], "warnings": s["parsed"]["warnings"]} for s in scans],
            "metadata_observations": [m for s in scans for m in s["metadata"]],
            "context_changes": [c for c in content_by_id.values() if c["kind"] == "context_change"],
            "content_unassigned": dict(Counter(c["kind"] for c in content_by_id.values()
                                                if not any(o["boundary_id"] and o["source"]["ns"] is not None for o in c["occurrences"]))),
            "analysis_cost": {"wall_seconds": time.perf_counter() - started, "files": len(paths),
                              "source_bytes": sum(s["parsed"]["bytes"] for s in scans), "model_tokens": None}})
