"""Utilise les detecteurs et rapports existants, avec provenance et isolation de projet."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentwatch.config import load_config
from agentwatch.core.correlate import build_session
from agentwatch.detectors import run_detectors
from agentwatch.reports.json_report import build_report
from agentwatch.reports.markdown import render_markdown
from agentwatch.reports.stats import compute_stats, coverage_matrix

from .storage import Project, encode, identity, within


def analyse(project: Project, sid: str) -> bool:
    cfg = load_config(project.home)
    cfg["auto_compact_threshold"] = 0
    # Raw observations remain complete in SQLite. Expensive analysis has a visible cap.
    cap = max(100, int(cfg.get("console_analysis_event_limit", 50000)))
    selected, revision = project.event_rows(sid, cap)
    with project.db() as db:
        retained = db.execute("SELECT event_count FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        routing = dict(db.execute("SELECT route,COUNT(*) FROM events WHERE session=? GROUP BY route", (sid,)).fetchall())
    view = build_session(selected, cfg)
    view.project_dir = str(project.root)
    with project.db() as db:
        feedback = {r["id"]: {"mark": r["mark"], "note": r["note"]} for r in db.execute("SELECT * FROM feedback")}
    if (project.home / "feedback.json").exists():
        from agentwatch.reports.feedback import load_feedback
        feedback = load_feedback(project.home)
    report = build_report(view, compute_stats(view, cfg), coverage_matrix(view), run_detectors(view, cfg),
                          cfg, feedback, None)
    report["project_scope"] = {"root": str(project.root), "events_retained": retained,
                               "events_analysed": len(selected), "truncated": retained > cap,
                               "routing_bases": routing,
                               "note": "Rapport du sous-ensemble d'observations attribue a cette racine. "
                                       "Les tokens d'une session traversant plusieurs racines ne sont pas un cout par projet."}
    calls = []
    for call in view.calls:
        data = asdict(call)
        data.update(id=identity(sid + "\0" + call.key), session=sid, label=call.label, order_ns=call.order_ns)
        data["timeline_reliable"] = call.agent_key not in view.timing_unreliable_agents
        data["duration_reliable"] = data["timeline_reliable"] or not str(call.duration_source or "").startswith("reconstructed")
        calls.append(data)
    if not project.save_analysis(sid, revision, report, calls):
        return False
    # Latest human-readable reports are durable per project; no deletion or retention job.
    directory = project.home / "reports"
    directory.mkdir(exist_ok=True)
    if not within(directory, project.home):
        raise ValueError("Le dossier des rapports doit appartenir a ce projet.")
    for suffix, content in (("json", encode(report)), ("md", render_markdown(report))):
        dest = directory / f"{sid}.{suffix}"
        temp = dest.with_suffix(f".{suffix}.{uuid.uuid4().hex}.tmp")
        temp.write_text(content, "utf-8")
        temp.replace(dest)
    return True


def pending(project: Project, limit: int = 2) -> list[str]:
    with project.db() as db:
        return [r[0] for r in db.execute("SELECT id FROM sessions WHERE revision!=analysed_revision "
                                         "AND error IS NULL ORDER BY last_ns DESC LIMIT ?", (limit,))]


def filters(query: dict[str, str]) -> tuple[str, list[Any]]:
    clauses, args = [], []
    for field, col in (("session", "session"), ("client", "client"), ("status", "status"), ("mcp", "mcp"), ("tool", "tool")):
        if query.get(field):
            if field == "status" and query[field] == "failure":
                clauses.append("status IN ('error','timeout','denied','failure')")
                continue
            clauses.append(f"{col}=?")
            args.append(query[field])
    for field, op in (("since", ">="), ("until", "<=")):
        if query.get(field):
            if field == "since" and query.get("overlap") == "1":
                clauses.append("(end_ns IS NULL OR end_ns>=?)")
                args.append(int(query[field]))
                continue
            clauses.append(f"at_ns{op}?")
            args.append(int(query[field]))
    if query.get("search"):
        clauses.append("(tool LIKE ? ESCAPE '\\' OR data LIKE ? ESCAPE '\\')")
        pattern = "%" + query["search"][:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args.extend([pattern, pattern])
    return " AND ".join(clauses) or "1=1", args


def calls_page(project: Project, query: dict[str, str]) -> dict[str, Any]:
    where, args = filters(query)
    limit = min(200, max(1, int(query.get("limit", 75))))
    cursor = query.get("cursor")
    if cursor:
        ns, cid = cursor.split(":", 1)
        where += " AND (at_ns<? OR (at_ns=? AND id<?))"
        args += [int(ns), int(ns), cid]
    with project.db() as db:
        rows = db.execute(f"SELECT * FROM calls WHERE {where} ORDER BY at_ns DESC,id DESC LIMIT ?", args + [limit + 1]).fetchall()
        total_where, total_args = filters(query)
        total = db.execute(f"SELECT COUNT(*) FROM calls WHERE {total_where}", total_args).fetchone()[0]
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [json.loads(r["data"]) for r in rows], "total": total,
            "cursor": f"{rows[-1]['at_ns']}:{rows[-1]['id']}" if more else None}


def sessions(project: Project) -> list[dict[str, Any]]:
    with project.db() as db:
        rows = db.execute("SELECT id,client,native_id,revision,analysed_revision,first_ns,last_ns,event_count,error "
                          "FROM sessions ORDER BY last_ns DESC LIMIT 500").fetchall()
    return [dict(r) for r in rows]


def summary(project: Project) -> dict[str, Any]:
    with project.db() as db:
        rows = db.execute("SELECT status,COUNT(*) n,COALESCE(SUM(CASE WHEN COALESCE(json_extract(data,'$.duration_reliable'),1) "
                          "THEN json_extract(data,'$.duration_ms') END),0) duration_ms,"
                          "SUM(json_extract(data,'$.duration_ms') IS NULL OR json_extract(data,'$.duration_reliable')=0) unknown "
                          "FROM calls GROUP BY status").fetchall()
        mcp = db.execute("SELECT mcp,tool,COUNT(*) n,SUM(status IN ('error','timeout','denied','failure')) failures,"
                         "SUM(CASE WHEN COALESCE(json_extract(data,'$.duration_reliable'),1) THEN "
                         "json_extract(data,'$.duration_ms') END) duration_ms FROM calls WHERE mcp IS NOT NULL "
                         "GROUP BY mcp,tool ORDER BY n DESC").fetchall()
        recent = db.execute("SELECT data FROM calls ORDER BY at_ns DESC LIMIT 8").fetchall()
        reports = db.execute("SELECT id,json_extract(report,'$.findings') findings FROM sessions "
                             "WHERE report IS NOT NULL ORDER BY last_ns DESC LIMIT 100").fetchall()
        changes = db.execute("SELECT path,at_ns FROM changes ORDER BY at_ns DESC LIMIT 10").fetchall()
        bounds = db.execute("SELECT MIN(CASE WHEN at_ns>0 THEN at_ns END),"
                            "MAX(CASE WHEN at_ns>0 THEN MAX(at_ns,COALESCE(end_ns,at_ns)) END) FROM calls").fetchone()
    findings = []
    for row in reports:
        for f in json.loads(row["findings"] or '[]'):
            findings.append({**f, "session": row["id"]})
    findings.sort(key=lambda f: {"high": 3, "medium": 2, "low": 1}.get(f.get("confidence"), 0), reverse=True)
    return {"project": project.info(), "statuses": [dict(r) for r in rows], "mcp": [dict(r) for r in mcp],
            "recent": [json.loads(r[0]) for r in recent], "findings": findings[:30],
            "findings_scope": "Derniers 100 rapports analyses", "changes": [dict(r) for r in changes],
            "first_ns": bounds[0], "last_ns": bounds[1]}


def union_ns(intervals: list[tuple[int, int]]) -> int:
    total, end = 0, -1
    for a, b in sorted(intervals):
        total += max(0, b - max(a, end))
        end = max(end, b)
    return total


def timeline(project: Project, query: dict[str, str]) -> dict[str, Any]:
    where, args = filters({k: v for k, v in query.items() if k not in ("since", "until")})
    width = min(604800, max(1, int(query.get("seconds", 10)))) * 1_000_000_000
    with project.db() as db:
        bounds = db.execute("SELECT MIN(CASE WHEN at_ns>0 THEN at_ns END),"
                            "MAX(CASE WHEN at_ns>0 THEN MAX(at_ns,COALESCE(end_ns,at_ns)) END),"
                            f"COUNT(CASE WHEN at_ns<=0 THEN 1 END) FROM calls WHERE {where}", args).fetchone()
        if bounds[0] is None:
            return {"buckets": [], "seconds": width // 1_000_000_000, "bounds": [None, None], "calls": 0,
                    "undated_calls": bounds[2]}
        end = int(query.get("until") or bounds[1])
        start = int(query.get("since") or max(bounds[0], end - 3600_000_000_000))
        if start >= end:
            end = start + width
        bins = (end - start + width - 1) // width
        if bins > 720:
            raise ValueError("Cette plage depasse 720 intervalles. Augmentez le pas ou reduisez la plage.")
        buckets = [{"at_ns": start + i * width, "until_ns": min(end, start + (i + 1) * width),
                    "starts": 0, "failures": 0, "mcp": 0, "unknown_duration": 0, "unplaced_time": 0,
                    "tool_ms": 0.0, "active_ms": 0.0, "active_until_ns": -1} for i in range(bins)]
        # at_ns is the persisted start timestamp (or end when no start was
        # observed). Ordered streaming allows an exact interval union per bin
        # with constant memory, without parsing native parameters or outputs.
        rows = db.execute(f"SELECT at_ns,end_ns,status,mcp,"
                          "CASE WHEN COALESCE(json_extract(data,'$.duration_reliable'),1) "
                          "AND json_type(data,'$.duration_ms') IN ('integer','real') "
                          "AND json_extract(data,'$.duration_ms')>=0 "
                          "THEN json_extract(data,'$.duration_ms') END duration_ms,"
                          "COALESCE(json_extract(data,'$.timeline_reliable'),1) reliable "
                          f"FROM calls WHERE {where} AND at_ns>0 AND at_ns<? "
                          "AND COALESCE(end_ns,at_ns)>=? ORDER BY at_ns", args + [end, start])
        count = 0
        for c in rows:
            count += 1
            a, b = c["at_ns"], c["end_ns"]
            index = (a - start) // width
            if 0 <= index < bins:
                bucket = buckets[index]
                bucket["starts"] += 1
                bucket["failures"] += c["status"] in ("error", "timeout", "denied", "failure")
                bucket["mcp"] += bool(c["mcp"])
                bucket["unknown_duration"] += c["duration_ms"] is None
                bucket["unplaced_time"] += not c["reliable"]
            if b is None or b < a or c["duration_ms"] is None or not c["reliable"]:
                continue
            for i in range(max(0, (a - start) // width), min(bins, (b - start) // width + 1)):
                bucket = buckets[i]
                lo, hi = max(a, bucket["at_ns"]), min(b, bucket["until_ns"])
                if hi > lo:
                    bucket["tool_ms"] += (hi - lo) / 1e6
                    bucket["active_ms"] += max(0, hi - max(lo, bucket["active_until_ns"])) / 1e6
                    bucket["active_until_ns"] = max(hi, bucket["active_until_ns"])
    for bucket in buckets:
        bucket.pop("active_until_ns")
    return {"buckets": buckets, "seconds": width // 1_000_000_000, "since": start, "until": end,
            "bounds": list(bounds[:2]), "calls": count, "undated_calls": bounds[2],
            "note": "Temps actif = union des intervalles observes ; temps outils = somme, avec le parallelisme. "
                    "Les durees inconnues, les appels ouverts et les horaires non fiables ne sont pas mesures."}
