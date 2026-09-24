"""Statistiques descriptives et matrice de couverture d'une session."""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX
from agentwatch.core import schema as S
from agentwatch.core import normalize as N
from agentwatch.core.correlate import ORIGIN_ROLLOUT, RECONSTRUCTED_SOURCES, STATUS_OPEN, SessionView

# * Capacites documentees par client (source : docs officielles, voir docs/compatibility.md).
#   Valeurs : supported | partial | absent. "observed" / "not_tested" viennent des donnees.
CAPABILITIES: dict[str, dict[str, tuple[str, str]]] = {
    CLIENT_CLAUDE_CODE: {
        "tool_start": ("supported", "PreToolUse"),
        "tool_end": ("supported", "PostToolUse (succes uniquement)"),
        "tool_failure": ("supported", "PostToolUseFailure (erreur, refus, interruption)"),
        "exit_code": ("absent", "tool_response de Bash expose stdout/stderr/interrupted, pas de code de sortie"),
        "client_duration": ("partial", "PostToolUse.duration_ms (observe 2.1.270 ; documente comme `duration` a partir de 2.1.267) ; durationMs dans certaines reponses"),
        "output_size": ("supported", "taille du tool_response serialise tel que recu par le hook"),
        "result_fingerprint": ("supported", "empreinte HMAC du tool_response"),
        "mcp_calls": ("supported", "outils nommes mcp__<serveur>__<outil>"),
        "subagents": ("supported", "agent_id / agent_type dans l'entree des hooks"),
        "compaction": ("supported", "PreCompact / PostCompact"),
        "interrupts": ("supported", "PostToolUseFailure.is_interrupt"),
        "turn_boundaries": ("supported", "UserPromptSubmit / Stop (prompt_id a partir de v2.1.196)"),
        "background_commands": ("partial", "run_in_background : la fin reelle passe par BashOutput, non correlee"),
        "hosted_tools": ("partial", "WebFetch/WebSearch sont des outils locaux vus par les hooks ; pas de detail reseau"),
        "long_commands_polling": ("partial", "un appel long = un PreToolUse puis un PostToolUse ; le polling interne n'est pas visible"),
        "token_usage": ("partial", "sous-agents : usage rapporte dans la reponse de l'outil Agent (observe 2.1.270) ; fil principal : absent, import seulement"),
    },
    CLIENT_CODEX: {
        "tool_start": ("supported", "PreToolUse"),
        "tool_end": ("supported", "PostToolUse (y compris apres un code de sortie non nul pour Bash)"),
        "tool_failure": ("partial", "pas de PostToolUseFailure : l'echec doit etre lu dans tool_response"),
        "exit_code": ("partial", "hooks : depend de tool_response ; rollouts (import-rollouts) : code de sortie de chaque commande"),
        "client_duration": ("partial", "hooks : aucune duree ; rollouts : duree et horodatages de chaque action"),
        "output_size": ("supported", "taille du tool_response serialise tel que recu par le hook"),
        "result_fingerprint": ("supported", "empreinte HMAC du tool_response"),
        "mcp_calls": ("supported", "outils nommes mcp__<serveur>__<outil>"),
        "subagents": ("supported", "hooks : SubagentStart/SubagentStop ; rollouts : un fil par sous-agent (parent, role, surnom)"),
        "compaction": ("supported", "PreCompact / PostCompact"),
        "interrupts": ("partial", "evenement Interrupt de session ; pas de statut par appel"),
        "turn_boundaries": ("supported", "turn_id sur les evenements de tour ; UserPromptSubmit / Stop"),
        "background_commands": ("partial", "sessions exec/write_stdin non correlees a un appel unique"),
        "hosted_tools": ("partial", "hooks : absents (documente) ; rollouts : recherches web visibles (item Extension)"),
        "long_commands_polling": ("partial", "un appel long = un PreToolUse puis un PostToolUse"),
        "token_usage": ("partial", "hooks : absent ; rollouts : releve par reponse du modele (import-rollouts)"),
    },
}


# * Session Codex lue dans ses rollouts (lecture passive, aucun hook) : ce que l'import sait lire, ligne par ligne.
#   Le format des rollouts n'est pas documente par l'editeur : la colonne dit "import", pas "documente".
CAPABILITIES_ROLLOUT: dict[str, tuple[str, str]] = {
    "tool_start": ("supported", "ligne function_call / custom_tool_call, ou started_at_ms de l'element item_completed"),
    "tool_end": ("supported", "element item_completed (CommandExecution, McpToolCall, FileChange...) ou sortie de la fonction"),
    "tool_failure": ("partial", "statut de l'element (failed, declined), code de sortie, isError MCP ; fonction sans statut : texte d'erreur (heuristique)"),
    "exit_code": ("supported", "exit_code de l'element CommandExecution"),
    "client_duration": ("supported", "duration de l'element, ecrite par Codex ; sinon ecart entre les horodatages du rollout"),
    "output_size": ("supported", "taille de la reponse reconstituee depuis le rollout (sortie et champs de statut)"),
    "result_fingerprint": ("supported", "empreinte HMAC de la reponse reconstituee depuis le rollout"),
    "mcp_calls": ("supported", "element McpToolCall (serveur, outil, arguments)"),
    "subagents": ("supported", "un rollout par sous-agent : session_meta.thread_spawn (parent, role, surnom) ; SubAgentActivity dans le fil parent"),
    "compaction": ("supported", "ligne compacted (et son identifiant de demande de compaction)"),
    "interrupts": ("partial", "event_msg turn_aborted : interruption du tour, pas de statut par appel"),
    "turn_boundaries": ("supported", "event_msg task_started / task_complete"),
    "background_commands": ("partial", "sessions exec/write_stdin non correlees a un appel unique"),
    "hosted_tools": ("partial", "recherches web visibles (elements WebSearch et Extension)"),
    "long_commands_polling": ("partial", "un appel long = un element avec sa duree ; le sondage interne n'est pas visible"),
    "token_usage": ("supported", "token_usage_record : un releve par reponse du modele ; la part par appel est calculee, pas mesuree"),
}


def compute_stats(view: SessionView, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    calls = view.calls
    by_tool: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "errors": 0, "unknown_status": 0, "open": 0, "output_bytes": 0, "output_known": 0,
        "client_durations": [], "reconstructed_durations": []})
    err_sig: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    for c in calls:
        row = by_tool[c.tool_name or "?"]
        row["calls"] += 1
        status_counter[c.status] += 1
        if c.status in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED):
            row["errors"] += 1
            if c.error_signature:
                err_sig[c.error_signature] += 1
        elif c.status == S.STATUS_UNKNOWN:
            row["unknown_status"] += 1
        elif c.status == STATUS_OPEN:
            row["open"] += 1
        # * Edit/Write enregistres avant le schema 1.1 : taille = fichier recopie, non mesuree.
        echo_only = c.category in (S.CAT_EDIT, S.CAT_WRITE) and (c.output_size_source or "") == "serialized_tool_response"
        if isinstance(c.output_size_bytes, int) and not echo_only:
            row["output_bytes"] += c.output_size_bytes
            row["output_known"] += 1
        if isinstance(c.duration_ms, int):
            if c.duration_source and c.duration_source.startswith("client"):
                row["client_durations"].append(c.duration_ms)
            elif c.duration_source in RECONSTRUCTED_SOURCES:
                row["reconstructed_durations"].append(c.duration_ms)
    tools = []
    for name, row in sorted(by_tool.items(), key=lambda kv: kv[1]["calls"], reverse=True):
        tools.append({
            "tool": name, "calls": row["calls"], "errors": row["errors"], "unknown_status": row["unknown_status"],
            "open": row["open"], "output_bytes": row["output_bytes"] if row["output_known"] else None,
            "output_known_for": row["output_known"],
            "client_duration_median_ms": int(median(row["client_durations"])) if row["client_durations"] else None,
            "client_duration_n": len(row["client_durations"]),
            "reconstructed_duration_median_ms": int(median(row["reconstructed_durations"])) if row["reconstructed_durations"] else None,
            "reconstructed_duration_n": len(row["reconstructed_durations"]),
        })
    # * Les Edit/Write enregistres avant la version 1.1 incluent le fichier recopie dans leur
    #   taille : on les ecarte du classement plutot que d'afficher un faux volume.
    biggest = sorted((c for c in calls if isinstance(c.output_size_bytes, int)
                      and not (c.category in (S.CAT_EDIT, S.CAT_WRITE) and (c.output_size_source or "") == "serialized_tool_response")),
                     key=lambda c: c.output_size_bytes or 0, reverse=True)[:5]
    longest = sorted((c for c in calls if isinstance(c.duration_ms, int)), key=lambda c: c.duration_ms or 0, reverse=True)[:5]
    return {
        "calls": len(calls),
        "events": view.counts.get("events", 0),
        "status": dict(status_counter),
        "tools": tools,
        "tools_total": {"tools": len(tools), "calls": sum(t["calls"] for t in tools), "errors": sum(t["errors"] for t in tools),
                        "unknown_status": sum(t["unknown_status"] for t in tools), "open": sum(t["open"] for t in tools)},
        "error_signatures": [{"signature": s, "count": n} for s, n in err_sig.most_common(10)],
        "errors": error_breakdown(view, cfg=cfg),
        "collection": view.collection, "origins": dict(view.origins),
        "cross_agent": cross_agent_note(view),
        "largest_outputs": [c.summary() for c in biggest],
        "longest_calls": [c.summary() for c in longest],
        "hook_overhead_ms": _describe(view.hook_ms_samples),
        "correlation": dict(view.counts),
        "turns": view.turns, "context_epochs": view.epochs, "agents": view.agents,
        "usage": _usage_summary(view),
        "work_units": work_units(view),
        "repetitions": _repetitions(view, cfg),
        "context": _context(view, cfg),
        "exchanges": _exchanges(view, cfg),
    }


_FAILED_STATUSES = (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED)


def _exit_code_basis(c: Any) -> str:
    if c.exit_code is None:
        return "aucun code de sortie fourni"
    if c.evidence.get("exit_code_source"):
        return str(c.evidence["exit_code_source"])
    return "exit_code de l'element du rollout Codex" if c.origin == ORIGIN_ROLLOUT else "tool_response du hook"


MAX_SOURCE_LOOKUPS = 200          # * garde-fou : nombre de relectures de source par rapport


def _recover_from_source(cfg: dict[str, Any] | None, unclassified: list[Any]) -> dict[str, dict[str, Any]]:
    """Cause d'une erreur retrouvee dans la source, pour les seuls appels qu'aucune forme connue ne classe.

    Le resume conserve la FIN de la sortie (`max_error_chars`) : quand l'echec s'affiche au DEBUT d'une longue
    sortie (une commande qui echoue, puis d'autres qui reussissent), sa cause n'y est plus. La ligne du rollout
    qui a produit l'appel est deja referencee (`evidence.source_end`) : on y relit un debut de sortie borne,
    secrets masques, et on ne classe que si une forme connue s'y trouve. Rien n'est force, rien n'est reecrit.

    # ! Lecture seule et tolerante : dossier introuvable, fichier deplace, offset perime -> l'appel reste non classe.
    """
    if not cfg or not unclassified:
        return {}
    from agentwatch.collector import rollouts as R
    key = R.source_key(cfg)                            # * None si aucune cle n'existe deja : on ne relit pas
    if key is None:
        return {}
    cache: dict[str, str | None] = {}
    out: dict[str, dict[str, Any]] = {}
    for c in unclassified[:MAX_SOURCE_LOOKUPS]:
        src = c.evidence.get("source_end")
        head, _complete = R.read_call_source(cfg, src, key, cache)
        if not head:
            continue
        k = N.classify_error(head)
        if k["kind"] == "unclassified":
            continue                                   # * la source ne montre pas davantage : on n'invente rien
        k["detail_source"] = f"debut de la sortie relu dans {src.get('file')}:{src.get('line')}"
        out[c.key] = k
    return out


def error_breakdown(view: SessionView, limit: int = 12, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Erreurs en trois niveaux qui ne se confondent pas : STATUT de l'outil (ce que le client dit de l'appel), CODE DE
    SORTIE de la commande (avec sa provenance), et RESULTAT ecrit par le script dans sa sortie (`"ExitCode"` d'une
    compilation, exception Python). Chaque groupe garde des exemples (#appel) et leur source (fichier et ligne du rollout).

    # * Remplace les « signatures » prises sur la premiere ligne de la fin de sortie (fragments de code).
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    failed = [c for c in view.calls if c.status in _FAILED_STATUSES]
    script_results = 0
    # * La cause peut se trouver au debut d'une longue sortie, hors du resume : on la relit dans la source
    #   pour les seuls appels qu'aucune forme connue ne classe, avant de regrouper.
    classified = {c.key: N.classify_error(c.error_summary) for c in failed}
    recovered = _recover_from_source(cfg, [c for c in failed if classified[c.key]["kind"] == "unclassified"])
    classified.update(recovered)
    for c in failed:
        k = classified[c.key]
        # * Ne compte que ce qui ETABLIT un echec interne : code ecrit non nul, ou compteur d'echecs non nul.
        if k["script_exit_code"] or k.get("script_failed"):
            script_results += 1
        tool_status = c.evidence.get("item_status") or c.evidence.get("collab_status")
        if tool_status and c.origin == ORIGIN_ROLLOUT:
            basis = "statut de l'element du rollout Codex"
        elif c.evidence.get("status_basis"):
            basis = "statut deduit par AgentWatch : " + str(c.evidence.get("status_basis"))
        else:
            basis = None
        key = (c.tool_name, c.status, tool_status, c.exit_code, k["kind"], k["detail"], k["script_exit_code"],
               k.get("script_failed"))
        g = groups.setdefault(key, {"tool": c.tool_name, "status": c.status, "tool_status": tool_status,
                                    "tool_status_basis": basis,
                                    "exit_code": c.exit_code, "exit_code_basis": _exit_code_basis(c),
                                    "kind": k["kind"], "kind_label": N.ERROR_KIND_LABELS.get(k["kind"], k["kind"]),
                                    "detail": k["detail"], "script_exit_code": k["script_exit_code"],
                                    "script_failed": k.get("script_failed"), "detail_source": k.get("detail_source"),
                                    "count": 0, "seqs": [], "sources": [], "heads": Counter()})
        g["count"] += 1
        if len(g["seqs"]) < 5:
            g["seqs"].append(c.seq)
        src = c.evidence.get("source_end")
        if isinstance(src, dict) and len(g["sources"]) < 3:
            g["sources"].append({"seq": c.seq, "file": src.get("file"), "line": src.get("line")})
        head = (c.params.get("shell_heads") or [None])[0]
        if head:
            g["heads"][str(head)[:30]] += 1
    rows = sorted(groups.values(), key=lambda g: -g["count"])
    for g in rows:
        g["heads"] = [h for h, _ in g["heads"].most_common(3)]
    reinterpreted = [c for c in view.calls if c.evidence.get("status_basis") == "reinterpreted_exit_code"]
    # * Cas insuffisamment documentes : code de sortie non nul, sortie tronquee, aucune erreur reconnue. Ni comptes
    #   en erreurs, ni requalifies en succes : ils se lisent a part.
    undetermined = [c for c in view.calls if c.evidence.get("status_basis") == "undetermined_truncated_output"]
    # * Decompte par nature sur TOUS les groupes : le tableau est borne, le compte rendu ne doit pas l'etre.
    by_kind: Counter[str] = Counter()
    for g in rows:
        by_kind[g["kind"]] += g["count"]
    return {
        "failed_calls": len(failed), "groups": rows[:limit], "groups_total": len(rows),
        "unclassified": sum(g["count"] for g in rows if g["kind"] == "unclassified"),
        "by_kind": [{"kind": k, "label": N.ERROR_KIND_LABELS.get(k, k), "count": n} for k, n in by_kind.most_common()],
        "recovered_from_source": len(recovered),
        "with_exit_code": sum(1 for c in failed if c.exit_code is not None),
        "with_script_result": script_results,
        "undetermined": {"count": len(undetermined), "seqs": [c.seq for c in undetermined[:8]],
                         "note": ("code de sortie non nul, sortie tronquee et aucune forme d'erreur reconnue : ni echec "
                                  "confirme, ni succes ; l'absence d'erreur dans un extrait ne demontre rien")},
        "reinterpreted_as_success": {"count": len(reinterpreted), "seqs": [c.seq for c in reinterpreted[:5]],
                                     "note": ("code de sortie non nul mais resultat normal (recherche sans correspondance, git diff "
                                              "avec differences) : comptes en succes, voir evidence.exit_status_meaning")},
    }


def cross_agent_note(view: SessionView) -> dict[str, Any]:
    """Dit en clair ce qui n'est PAS compare : chaque detecteur raisonne agent par agent."""
    n = len(view.agents)
    if n > 1:
        note = ("les detecteurs raisonnent agent par agent : aucun signalement ne compare deux agents. Ce qui se passe entre "
                "eux (messages rapproches de leur reception, requetes qui n'emettent que des messages, ressources lues ou ecrites "
                "par plusieurs) est decrit et chiffre, sans verdict, dans la section « Entre agents ».")
    else:
        note = "un seul agent dans la session : aucune comparaison entre agents a faire"
    return {"agents": n, "performed": False, "described": n > 1, "note": note}


def _exchanges(view: SessionView, cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    from agentwatch.reports.exchanges import agent_exchanges
    return agent_exchanges(view, cfg)


def _context(view: SessionView, cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    from agentwatch.reports.context import context_costs
    return context_costs(view, cfg)


def _repetitions(view: SessionView, cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Appels repetes (pourquoi, a quel rythme, verdict) et rythme des outils : analyse du detecteur G."""
    from agentwatch.config import DEFAULTS
    from agentwatch.detectors import repeated_calls as G
    return G.analyse(view, cfg if cfg is not None else DEFAULTS)


def _unit_target(c: Any) -> Any:
    """Cible AFFICHEE d'une unite de travail : ce qui la distingue deja dans sa cle de regroupement doit se lire.
    Sans cela deux lignes differentes portent le meme libelle (`mcp:linear/get_issue`, `module:py_compile`)."""
    if c.target_kind == "mcp" and c.label:
        return c.label
    target = c.op_target or c.target
    if c.op == "run_script" and isinstance(target, str) and target.startswith("module:"):
        args = [str(a) for a in (c.op_params.get("args") or [])[:2]]
        return f"{target} {' '.join(args)}".strip()
    return target


def work_units(view: SessionView, limit: int = 25) -> list[dict[str, Any]]:
    """Unites de travail : meme operation normalisee sur la meme cible, tous outils confondus.

    # * Repond a « a-t-on refait le meme travail ? » plutot qu'a « a-t-on refait le meme appel ? ».
    """
    groups: dict[str, dict[str, Any]] = {}
    for c in view.calls:
        if c.op in ("other", "agent") or not c.op_key:
            continue
        g = groups.setdefault(c.op_key, {"op": c.op, "target": _unit_target(c), "calls": 0,
                                         "tools": set(), "agents": set(), "statuses": {}, "content_fps": set(), "seqs": []})
        g["calls"] += 1
        g["tools"].add(c.tool_name or "?")
        g["agents"].add(c.agent_key)
        g["statuses"][c.status] = g["statuses"].get(c.status, 0) + 1
        if c.content_fingerprint:
            g["content_fps"].add(c.content_fingerprint)
        g["seqs"].append(c.seq)
    rows = []
    for g in groups.values():
        rows.append({"op": g["op"], "target": g["target"], "calls": g["calls"], "tools": sorted(g["tools"]),
                     "agents": len(g["agents"]), "statuses": g["statuses"],
                     "distinct_contents": len(g["content_fps"]) if g["content_fps"] else None,
                     "seqs": g["seqs"][:20], "repeated": g["calls"] >= 2})
    rows.sort(key=lambda r: (r["calls"], len(r["tools"])), reverse=True)
    return rows[:limit]


_TRANSCRIPT_SOURCE = "claude-code:transcript"
_ROLLOUT_SOURCE = "codex:rollout"
_ROLLOUT_KEYS = ("requests", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def compaction_request_ids(view: SessionView) -> set[str]:
    """Identifiants des demandes de compaction (voir `compaction_requests`)."""
    return set(compaction_requests(view))


def compaction_requests(view: SessionView) -> dict[str, str]:
    """Demandes de compaction -> base du lien avec la ligne `compacted`. Une demande de compaction est la requete au
    modele par laquelle Codex resume l'historique ; son releve de tokens precede la ligne `compacted`. Elle consomme des
    tokens reels mais ne repond pas a la conversation. Seule la ligne `compacted` etablit que l'historique a ete
    remplace : une demande n'est jamais presentee comme une compaction reussie sans elle.

    Base : "identifiant" (compaction_response_id ecrit par Codex dans la ligne `compacted`) ou "position" (imports
    anterieurs sans cet identifiant : releve du meme fil qui precede la ligne `compacted` de 5 s au plus).

    # * Constate le 2026-09-19 : 180 demandes sur 180 designees par leur identifiant sont le releve juste avant la ligne.
    """
    per_agent: dict[str, tuple[list[int], list[str]]] = {}
    for m in view.markers:
        u = m.meta.get("usage") if m.phase == S.PHASE_USAGE else None
        if isinstance(u, dict) and u.get("scope") == "response" and u.get("response_id"):
            ns_list, ids = per_agent.setdefault(m.agent_id or "main", ([], []))
            ns_list.append(m.ns)
            ids.append(str(u["response_id"]))
    for ns_list, ids in per_agent.values():
        order = sorted(range(len(ns_list)), key=lambda i: ns_list[i])
        ns_list[:], ids[:] = [ns_list[i] for i in order], [ids[i] for i in order]
    out: dict[str, str] = {}
    for m in view.markers:
        if m.phase != S.PHASE_COMPACT_END:
            continue
        rid = m.meta.get("compaction_response_id")
        if isinstance(rid, str) and rid:
            out[rid] = "identifiant"
            continue
        ns_list, ids = per_agent.get(m.agent_id or "main", ([], []))
        i = bisect.bisect_right(ns_list, m.ns) - 1
        if i >= 0 and m.ns - ns_list[i] <= 5_000_000_000:
            out.setdefault(ids[i], "position")
    return out


def session_tokens(view: SessionView) -> dict[str, Any] | None:
    """Usage mesure de la session, None si aucun import.

    # * Transcript Claude Code : plusieurs imports d'un transcript qui grandit laissent plusieurs marqueurs ; le
    #   plus complet fait foi. Rollouts Codex : un marqueur par fil (principal et sous-agents) et par lecture ;
    #   le plus complet de chaque fil fait foi, puis les fils sont sommes.
    """
    usages = [m.meta["usage"] for m in view.markers if m.phase == S.PHASE_USAGE and isinstance(m.meta.get("usage"), dict)
              and m.meta["usage"].get("scope") != "response"]      # * releves par reponse : detail, pas un total
    if not usages:
        return None
    rollout = [u for u in usages if u.get("source") == _ROLLOUT_SOURCE and u.get("scope") != "response"]
    if not rollout:
        # A nombre de requetes egal, un reimport corrige peut completer ou invalider un ancien releve.
        # Preferer les imports qui distinguent absence/zero, puis le plus recent ; garder les anciens evenements.
        _, best = max(enumerate(usages), key=lambda item: (int(item[1].get("requests") or 0),
                                                         bool(item[1].get("usage_coverage")), item[0]))
        return dict(best)
    per_thread: dict[str, dict[str, Any]] = {}
    for u in rollout:
        tid = str(u.get("thread_id") or "?")
        if tid not in per_thread or int(u.get("requests") or 0) > int(per_thread[tid].get("requests") or 0):
            per_thread[tid] = u
    names = {m.meta.get("agent_id"): m.meta for m in view.markers if m.phase == S.PHASE_SUBAGENT_START and m.meta.get("agent_id")}
    threads = []
    for tid, u in per_thread.items():
        meta = names.get(u.get("agent_id")) or {}
        threads.append({"thread_id": tid, "agent_id": u.get("agent_id"), "agent_type": meta.get("agent_type"),
                        "agent_nickname": meta.get("agent_nickname"), "windows": u.get("windows"),
                        **{k: int(u.get(k) or 0) for k in _ROLLOUT_KEYS}})
    threads.sort(key=lambda r: -r["total_tokens"])
    tot: dict[str, Any] = {k: sum(r[k] for r in threads) for k in _ROLLOUT_KEYS}
    tot["uncached_input_tokens"] = tot["input_tokens"] - tot["cached_input_tokens"]
    return {"source": _ROLLOUT_SOURCE, "scope": "session", **tot, "threads": threads}


def _usage_summary(view: SessionView) -> dict[str, Any]:
    """Usage en tokens : ce que le client rapporte ou ce qu'un import (transcript, rollout, JSONL) a fourni, avec source et perimetre."""
    rows = []
    for a in view.agent_infos:
        if a.usage:
            rows.append({"agent_id": a.agent_id, "agent_type": a.agent_type, "model": a.model, **a.usage})
    imported = [c.usage for c in view.calls if c.usage and c.usage.get("source") not in
                (None, "claude-code:Agent.tool_response", _TRANSCRIPT_SOURCE, _ROLLOUT_SOURCE)]
    transcript_calls = sum(1 for c in view.calls if isinstance(c.usage, dict) and c.usage.get("source") in (_TRANSCRIPT_SOURCE, _ROLLOUT_SOURCE))
    session = session_tokens(view)
    if session and session.get("source") == _ROLLOUT_SOURCE:
        comp = compaction_requests(view)
        by_id = sum(1 for b in comp.values() if b == "identifiant")
        return {
            "status": (f"mesure depuis les rollouts Codex : {session['requests']} requetes du modele, dont "
                       f"{len(comp)} demande(s) de compaction ({by_id} reliee(s) a leur ligne compacted par l'identifiant "
                       f"ecrit par Codex, {len(comp) - by_id} par position), {session['total_tokens']} tokens "
                       f"(entree {session['input_tokens']} dont {session['cached_input_tokens']} en cache, soit "
                       f"{session['uncached_input_tokens']} non mis en cache ; sortie {session['output_tokens']} dont "
                       f"{session['reasoning_output_tokens']} de raisonnement) ; {len(session['threads'])} fil(s)"),
            "note": "releves token_usage_record ecrits par Codex, un par reponse du modele, additionnes sans conversion",
            "kinds": {
                "measured": "par reponse du modele : releves ecrits par le client (token_usage_record), sommes par fil et par session",
                "allocated": (f"par appel d'outil : {transcript_calls}/{len(view.calls)} appels ont une part CALCULEE par AgentWatch "
                              "(entree non mise en cache de la reponse qui a consomme la sortie, au prorata des tailles, + part de "
                              "la sortie de la reponse emettrice) ; ce n'est pas une mesure par appel"),
                "estimated": "aucune estimation : rien n'est deduit d'octets ni d'un tarif ; aucune donnee de facturation n'est lue",
            },
            "session": session, "transcript_calls": transcript_calls, "agents": rows, "imported": imported,
            "threads": session["threads"],
        }
    if session:
        coverage = session.get("usage_coverage") or {}
        if coverage.get("status") in ("missing", "partial"):
            observed = session.get("observed_tokens")
            detail = (f"somme partielle des champs releves : {observed} tokens" if isinstance(observed, int)
                      else "aucun champ d'usage releve")
            status = (f"usage non releve completement dans le transcript : {session.get('requests')} requetes API, "
                      f"total non releve ; {detail} ; {coverage.get('complete_requests', 0)}/{session.get('requests')} "
                      "requetes avec les quatre champs d'usage")
        else:
            status = (f"mesure depuis le transcript : {session.get('requests')} requetes API, {session.get('total_tokens')} tokens "
                      f"(entree {session.get('input_tokens')}, creation de cache {session.get('cache_creation_tokens')}, "
                      f"lecture de cache {session.get('cache_read_tokens')}, sortie {session.get('output_tokens')})")
        return {
            "status": status,
            "note": "comptes tels qu'ecrits par le client dans son transcript, dedoublonnes par requete",
            "kinds": {
                "measured": "par requete API : comptes ecrits par le client dans son transcript, sommes pour la session",
                "allocated": (f"par appel d'outil : {transcript_calls}/{len(view.calls)} appels ont une part CALCULEE par AgentWatch "
                              "(entree non mise en cache de la requete qui a consomme le resultat + part de la sortie de la requete "
                              "emettrice) ; ce n'est pas une mesure par appel"),
                "estimated": "aucune estimation : rien n'est deduit d'octets ni d'un tarif ; aucune donnee de facturation n'est lue",
            },
            "session": session, "transcript_calls": transcript_calls, "agents": rows, "imported": imported,
        }
    if not rows and not imported:
        return {"status": "non mesure", "note": "aucune donnee d'usage fournie par les hooks ; voir agentwatch import-transcripts (Claude Code) ou import-usage",
                "session": None, "transcript_calls": 0, "agents": [], "imported": []}
    return {
        "status": f"rapporte par le client pour {len(rows)} sous-agent(s) (portee : agent) ; non mesure pour le fil principal",
        "note": "valeurs telles que fournies dans la reponse de l'outil Agent ; aucune conversion ni estimation de cout",
        "session": None, "transcript_calls": transcript_calls, "agents": rows, "imported": imported,
    }


def _describe(samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {"n": 0}
    s = sorted(samples)
    return {"n": len(s), "median": round(s[len(s) // 2], 1), "p90": round(s[min(len(s) - 1, int(len(s) * 0.9))], 1),
            "max": round(s[-1], 1), "note": "temps dans le processus du hook (hors demarrage de l'interpreteur)"}


def coverage_matrix(view: SessionView) -> list[dict[str, Any]]:
    """Ligne par capacite : statut documente + observation dans cette session.

    # * La base citee est celle de la SOURCE reelle des appels : une session Codex lue dans ses rollouts ne cite
    #   aucun hook (constate le 2026-09-20 : la table annoncait PreToolUse / PostToolUse pour une session sans hook).
    """
    rollout = view.collection == "rollout"
    caps = CAPABILITIES_ROLLOUT if rollout else CAPABILITIES.get(view.client, {})
    calls = view.calls
    marker_phases = {m.phase for m in view.markers}
    observed: dict[str, bool] = {
        "tool_start": any(c.has_start for c in calls),
        "tool_end": any(c.has_end and c.status not in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED) for c in calls),
        "tool_failure": any(c.status in (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED, S.STATUS_INTERRUPTED) for c in calls),
        "exit_code": any(c.exit_code is not None for c in calls),
        "client_duration": any(c.duration_source and c.duration_source.startswith("client") for c in calls),
        "output_size": any(c.output_size_bytes is not None for c in calls),
        "result_fingerprint": any(c.result_fingerprint for c in calls),
        "mcp_calls": any(c.category == S.CAT_MCP for c in calls),
        "subagents": any(c.agent_id for c in calls) or bool({S.PHASE_SUBAGENT_START, S.PHASE_SUBAGENT_STOP} & marker_phases),
        "compaction": bool({S.PHASE_COMPACT_START, S.PHASE_COMPACT_END} & marker_phases),
        "interrupts": any(c.status == S.STATUS_INTERRUPTED for c in calls) or S.PHASE_INTERRUPT in marker_phases,
        "turn_boundaries": view.turns > 0,
        "background_commands": any(c.params.get("run_in_background") for c in calls),
        "hosted_tools": any(c.category == S.CAT_WEB for c in calls),
        "long_commands_polling": any(isinstance(c.duration_ms, int) and c.duration_ms > 30000 for c in calls),
        "token_usage": any(c.usage for c in calls),
    }
    rows = []
    for cap, (status, basis) in caps.items():
        seen = observed.get(cap, False)
        rows.append({"capability": cap, "documented": status, "basis": basis, "source": "rollout" if rollout else "hooks",
                     "observed_in_session": "observed" if seen else ("absent" if status == "absent" else "not_observed")})
    return rows
