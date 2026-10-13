"""Mesures MCP exactes sur les appels du projet, sans charger leurs parametres."""
from __future__ import annotations

from typing import Any

from .analysis import filters
from .storage import Project


def overview(project: Project, query: dict[str, str]) -> dict[str, Any]:
    selection = {k: str(v) for k, v in query.items() if v and k in
                 {"session", "client", "status", "mcp", "tool", "search", "since", "until", "overlap"}}
    where, args = filters(selection)
    # SQL performs the sorts/percentiles; Python receives one row per group. In
    # particular, large native parameters and captured output are never decoded.
    sql = f"""
        WITH selected AS (
            SELECT mcp,tool,client,session,status,at_ns first_ns,
                CASE WHEN at_ns>0 THEN MAX(at_ns,COALESCE(end_ns,at_ns)) END last_ns,
                CASE WHEN COALESCE(json_extract(data,'$.duration_reliable'),1)
                    AND json_type(data,'$.duration_ms') IN ('integer','real')
                    AND json_extract(data,'$.duration_ms')>=0
                    THEN json_extract(data,'$.duration_ms') END duration,
                CASE WHEN json_type(data,'$.output_size_bytes')='integer'
                    AND json_extract(data,'$.output_size_bytes')>=0
                    THEN json_extract(data,'$.output_size_bytes') END output_bytes,
                COALESCE(json_extract(data,'$.output_truncated'),0) truncated
            FROM calls WHERE mcp IS NOT NULL AND ({where})
        ), expanded AS (
            SELECT 'tool' kind,mcp,tool group_tool,client,session,status,first_ns,last_ns,duration,output_bytes,truncated FROM selected
            UNION ALL
            SELECT 'server',mcp,NULL,client,session,status,first_ns,last_ns,duration,output_bytes,truncated FROM selected
            UNION ALL
            SELECT 'total',NULL,NULL,client,session,status,first_ns,last_ns,duration,output_bytes,truncated FROM selected
        ), ranked AS (
            SELECT kind,mcp,group_tool,duration,
                ROW_NUMBER() OVER(PARTITION BY kind,mcp,group_tool ORDER BY duration) rank,
                COUNT(*) OVER(PARTITION BY kind,mcp,group_tool) n
            FROM expanded WHERE duration IS NOT NULL
        ), quantiles AS (
            SELECT kind,mcp,group_tool,
                AVG(CASE WHEN rank IN ((n+1)/2,(n+2)/2) THEN duration END) median_ms,
                MAX(CASE WHEN rank=(95*n+99)/100 THEN duration END) p95_ms
            FROM ranked GROUP BY kind,mcp,group_tool
        ), totals AS (
            SELECT kind,mcp,group_tool,COUNT(*) calls,COUNT(DISTINCT client) clients,
                COUNT(DISTINCT session) sessions,
                COUNT(CASE WHEN status='success' THEN 1 END) successes,
                COUNT(CASE WHEN status IN ('error','failure','timeout','denied') THEN 1 END) failures,
                COUNT(CASE WHEN status='open' THEN 1 END) open_calls,
                COUNT(duration) measured_calls,SUM(duration) total_ms,MAX(duration) max_ms,
                COUNT(output_bytes) output_observed_calls,SUM(output_bytes) output_bytes,
                COUNT(CASE WHEN truncated THEN 1 END) truncated_calls,MAX(last_ns) last_ns,
                MIN(CASE WHEN first_ns>0 THEN first_ns END) first_ns,
                COUNT(CASE WHEN first_ns<=0 THEN 1 END) undated_calls
            FROM expanded GROUP BY kind,mcp,group_tool
        )
        SELECT totals.*,quantiles.median_ms,quantiles.p95_ms FROM totals
        LEFT JOIN quantiles ON totals.kind=quantiles.kind AND totals.mcp IS quantiles.mcp
            AND totals.group_tool IS quantiles.group_tool
        ORDER BY calls DESC,mcp,group_tool
    """
    with project.db() as db:
        rows = db.execute(sql, args).fetchall()
    servers, tools, total = [], [], _empty()
    for row in rows:
        record = dict(row)
        kind = record.pop("kind")
        record["tool"] = record.pop("group_tool")
        record["unknown_duration_calls"] = record["calls"] - record["measured_calls"]
        record["unknown_output_calls"] = record["calls"] - record["output_observed_calls"]
        record["other_status_calls"] = record["calls"] - record["successes"] - record["failures"] - record["open_calls"]
        closed = record["successes"] + record["failures"]
        record["failure_rate_pct"] = record["failures"] * 100 / closed if closed else None
        if kind == "server":
            servers.append(record)
        elif kind == "tool":
            tools.append(record)
        else:
            total = record
    return {"project_id": project.id, "selection": selection,
            "scope": "Filtres choisis" if selection else "Tout l'historique du projet",
            "total": total, "servers": servers, "tools": tools,
            "method": {"latency": "Durees observees et fiables uniquement, erreurs incluses. Mediane exacte ; "
                                  "P95 par rang le plus proche vers le haut (ceil(0.95*n)).",
                       "failure_rate": "Echecs / (reussites + echecs). Appels ouverts et autres etats exclus du taux.",
                       "output": "Octets observes, sans estimation des tailles absentes. Une sortie tronquee reste signalee.",
                       "scope": "Appels correles de cette racine, a la revision d'analyse disponible. "
                                "Un serveur observe ne prouve pas qu'il est connecte maintenant."}}


def _empty() -> dict[str, Any]:
    return {"calls": 0, "clients": 0, "sessions": 0, "successes": 0, "failures": 0, "open_calls": 0,
            "other_status_calls": 0, "measured_calls": 0, "unknown_duration_calls": 0,
            "total_ms": None, "max_ms": None, "median_ms": None, "p95_ms": None,
            "output_observed_calls": 0, "unknown_output_calls": 0, "output_bytes": None,
            "truncated_calls": 0, "first_ns": None, "last_ns": None, "undated_calls": 0, "failure_rate_pct": None}
