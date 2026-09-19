"""Vue multi-sessions : gaspillages recurrents par motif, projet, client et session.

# * Un signalement isole dans une session ne justifie rien : ce qui justifie un script, une
#   skill, un outil MCP ou une regle dans CLAUDE.md / AGENTS.md, c'est un MOTIF qui revient
#   dans plusieurs sessions. Ce module reanalyse chaque session de la fenetre avec les
#   detecteurs, regroupe les signalements par une cle de motif stable (independante de la
#   session, des identifiants d'appel et du client), puis compte les sessions, projets et
#   clients ou chaque motif apparait, et les tokens mesures quand les transcripts sont importes.
# * Rien de nouveau n'est detecte ici : ce sont les signalements par session, agreges. Les
#   contre-indications de chaque signalement restent valables ; `report --session` donne
#   la preuve.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Callable

from agentwatch import __version__
from agentwatch.collector.store import EventStore
from agentwatch.core import schema as S
from agentwatch.core.session import load_session
from agentwatch.detectors import run_detectors
from agentwatch.detectors.base import Finding, cost_tokens
from agentwatch.reports.stats import session_tokens
from agentwatch.detectors.tool_gap import mcp_servers_of, related_servers

TRENDS_VERSION = "1.1"
RULE_LETTERS = ("A", "B", "C", "D", "E")
UNKNOWN_PROJECT = "(inconnu)"
_ERRORS = (S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED)
_CONF_RANK = {"high": 3, "medium": 2, "low": 1}
Importer = Callable[[str, str], Any]


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    v = cfg.get(name)
    return v if isinstance(v, dict) else {}


# ---------------------------------------------------------------------------- cle de motif
def pattern_key(f: Finding) -> tuple[str, str]:
    """(cle stable, libelle) d'un signalement, independants de la session.

    # * A : operation + cible normalisee (chemin relatif au projet quand il est dessous : le
    #   meme fichier relu dans plusieurs projets est un seul motif, le compte de projets le dit).
    # * B : type de boucle + outil + signature d'erreur, pas la cible : la meme erreur sur des
    #   cibles differentes est le meme probleme.
    # * C : outil seulement, le motif etant l'habitude de lire en serie.
    # * D : la sequence de formes d'appels (la recette candidate).
    # * E : famille de service + cible (hote, URL, service) : l'outil qui manque ou qui n'a pas servi.
    """
    ev = f.evidence or {}
    letter = f.rule_id.split(".", 1)[0]
    if letter == "A":
        op, target = ev.get("operation") or "?", ev.get("target")
        if f.kind == "repeated_run":
            return f"A|repeated_run|{op}|{target}", f"{op} relance sans changement : {target!r}"
        return f"A|repeated_read|{op}|{target}", f"{op} de {target!r} refait"
    if letter == "B":
        kind = f.kind.removesuffix("_probable")
        tool = (f.call_refs[0].get("tool") if f.call_refs else None) or "?"
        sig = ev.get("error_signature") or "(sans signature)"
        suffix = "" if kind == "persistent" else f" ({kind})"
        return f"B|{kind}|{tool}|{sig}", f"{tool} en echec `{sig}`{suffix}"
    if letter == "C":
        tool = ev.get("tool") or "?"
        return f"C|{tool}", f"appels {tool} en serie sur des cibles differentes"
    if letter == "D":
        pattern = [str(p) for p in (ev.get("pattern") or [])]
        recipe = f.proposal.get("recipe") if isinstance(f.proposal, dict) else None
        name = recipe.get("name") if isinstance(recipe, dict) else None
        return "D|" + "\x1f".join(pattern), f"sequence {name or ' -> '.join(pattern)}"
    if letter == "E":
        family, target = ev.get("family") or "?", ev.get("target") or "?"
        return f"E|{family}|{target}", f"commandes {family} vers {target!r} faites a la main"
    return f"{f.rule_id}|{f.kind}|{f.title}", f.title


def _project_key(project_dir: str | None, case_insensitive: bool) -> str:
    if not project_dir:
        return UNKNOWN_PROJECT
    p = project_dir.replace("\\", "/").rstrip("/") or "/"
    return p.lower() if case_insensitive else p


def _rule_letter(f: Finding) -> str:
    return f.rule_id.split(".", 1)[0]


def _empty_by_rule() -> dict[str, int]:
    return {letter: 0 for letter in RULE_LETTERS}


# ---------------------------------------------------------------------------- collecte
def _collect_sessions(store: EventStore, cfg: dict[str, Any], client: str | None, project: str | None,
                      since_ns: int | None, importer: Importer | None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    case_insensitive = bool(cfg.get("case_insensitive_paths", False))
    rows: list[dict[str, Any]] = []
    skipped = {"client_filter": 0, "empty": 0, "out_of_window": 0, "project_filter": 0}
    for c, skey, _ in store.iter_sessions():
        if client and c != client:
            skipped["client_filter"] += 1
            continue
        view = load_session(store, c, skey, cfg)
        if not view.calls and not view.markers:
            skipped["empty"] += 1
            continue
        # * Fenetre sur le DERNIER evenement : une session longue encore active reste visible.
        if since_ns is not None and (view.last_ns or 0) < since_ns:
            skipped["out_of_window"] += 1
            continue
        if project and project.lower() not in (view.project_dir or "").replace("\\", "/").lower():
            skipped["project_filter"] += 1
            continue
        if importer is not None:
            # * Import d'usage (transcripts) seulement pour les sessions retenues, puis relecture.
            importer(c, skey)
            view = load_session(store, c, skey, cfg)
        findings = run_detectors(view, cfg)
        session_usage = session_tokens(view)
        rows.append({
            "client": c, "session_key": skey, "session_id": view.session_id or skey,
            "project_dir": view.project_dir, "project_key": _project_key(view.project_dir, case_insensitive),
            "first_time": view.first_time, "last_time": view.last_time, "last_ns": view.last_ns or 0,
            "calls": len(view.calls), "errors": sum(1 for x in view.calls if x.status in _ERRORS),
            "findings": findings, "mcp_servers": mcp_servers_of(view.calls),
            "tokens": int(session_usage.get("total_tokens") or 0) if session_usage else None,
        })
    rows.sort(key=lambda r: r["last_ns"], reverse=True)
    return rows, skipped


# ---------------------------------------------------------------------------- agregation
def _aggregate_patterns(rows: list[dict[str, Any]], feedback: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    patterns: dict[str, dict[str, Any]] = {}
    for row in rows:
        for f in row["findings"]:
            f.feedback = feedback.get(f.finding_id)
            key, label = pattern_key(f)
            p = patterns.setdefault(key, {
                "pattern_key": key, "label": label, "rule_id": f.rule_id, "kinds": set(),
                "sessions": {}, "projects": set(), "clients": set(),
                "occurrences": 0, "calls": 0, "output_bytes_sum": 0, "output_bytes_known_for": 0,
                "tokens_sum": 0, "tokens_known_for": 0,
                "confidence_counts": Counter(), "feedback": Counter(), "best": None, "examples": [],
            })
            p["kinds"].add(f.kind)
            false_positive = bool(f.feedback and f.feedback.get("mark") == "false-positive")
            if f.feedback:
                p["feedback"][f.feedback.get("mark")] += 1
            sess = p["sessions"].setdefault(row["session_key"], {
                "session_id": row["session_id"], "client": row["client"], "project_dir": row["project_dir"],
                "project_key": row["project_key"], "last_time": row["last_time"], "findings": 0, "active": False})
            sess["findings"] += 1
            p["occurrences"] += 1
            p["calls"] += len(f.calls)
            cost = f.observed_cost or {}
            if isinstance(cost.get("output_bytes_sum"), int):
                p["output_bytes_sum"] += cost["output_bytes_sum"]
                p["output_bytes_known_for"] += int(cost.get("output_bytes_known_for") or 0)
            tok = cost_tokens(cost)
            if tok is not None:
                p["tokens_sum"] += tok
                p["tokens_known_for"] += int((cost.get("tokens") or {}).get("known_for") or 0)
            p["examples"].append({"session_id": row["session_id"], "session_key": row["session_key"], "client": row["client"],
                                  "project_dir": row["project_dir"], "last_time": row["last_time"], "finding_id": f.finding_id,
                                  "confidence": f.confidence, "title": f.title, "false_positive": false_positive})
            if false_positive:
                continue   # * un faux positif marque reste compte, mais ne porte ni confiance ni session active
            sess["active"] = True
            p["projects"].add(row["project_key"])
            p["clients"].add(row["client"])
            p["confidence_counts"][f.confidence] += 1
            if p["best"] is None or f.confidence_rank > p["best"].confidence_rank:
                p["best"] = f
    return patterns


def _confidence_max(p: dict[str, Any]) -> str | None:
    counts = p["confidence_counts"]
    return max(counts, key=lambda c: _CONF_RANK.get(c, 0)) if counts else None


def _active_sessions(p: dict[str, Any], *, project_key: str | None = None, client: str | None = None) -> list[dict[str, Any]]:
    out = []
    for s in p["sessions"].values():
        if not s["active"]:
            continue
        if project_key is not None and s["project_key"] != project_key:
            continue
        if client is not None and s["client"] != client:
            continue
        out.append(s)
    return out


def _rank_key(p: dict[str, Any], sessions: int) -> tuple[int, int, int, int, int, int]:
    return (sessions, _CONF_RANK.get(_confidence_max(p) or "", 0), p["occurrences"], p["tokens_sum"], p["calls"], p["output_bytes_sum"])


def _pattern_row(p: dict[str, Any], sessions: list[dict[str, Any]], max_examples: int,
                 servers: dict[str, set[str]]) -> dict[str, Any]:
    best: Finding | None = p["best"]
    proposal = best.proposal.get("text") if best and isinstance(best.proposal, dict) else None
    examples = sorted(p["examples"], key=lambda e: e["last_time"] or "", reverse=True)
    row = {
        "pattern_key": p["pattern_key"], "label": p["label"], "rule_id": p["rule_id"], "kinds": sorted(p["kinds"]),
        "sessions": len(sessions), "projects": len({s["project_key"] for s in sessions}),
        "project_dirs": sorted({s["project_dir"] or UNKNOWN_PROJECT for s in sessions}),
        "clients": sorted({s["client"] for s in sessions}),
        "occurrences": p["occurrences"], "calls": p["calls"],
        "output_bytes_sum": p["output_bytes_sum"] if p["output_bytes_known_for"] else None,
        "output_bytes_known_for": p["output_bytes_known_for"],
        "tokens_sum": p["tokens_sum"] if p["tokens_known_for"] else None, "tokens_known_for": p["tokens_known_for"],
        "confidence_max": _confidence_max(p), "confidence_counts": dict(p["confidence_counts"]),
        "feedback": dict(p["feedback"]), "proposal": proposal,
        "examples": examples[:max_examples], "examples_total": len(examples),
    }
    if p["pattern_key"].startswith("E|") and best is not None:
        # * Un serveur MCP lie, vu dans n'importe quelle session de la fenetre : l'outil existe deja.
        ev = best.evidence or {}
        row["mcp_available_in_window"] = related_servers(str(ev.get("family") or ""), str(ev.get("target") or ""), servers)
    return row


def _group_rows(rows: list[dict[str, Any]], patterns: dict[str, dict[str, Any]], min_sessions: int, max_examples: int,
                by: str, servers: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Resume par projet (`by="project_key"`) ou par client (`by="client"`), avec les motifs
    recurrents A L'INTERIEUR du groupe (seuil `min_sessions` applique au groupe)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r[by], []).append(r)
    out: list[dict[str, Any]] = []
    for gkey, members in groups.items():
        by_rule = _empty_by_rule()
        for r in members:
            for f in r["findings"]:
                by_rule[_rule_letter(f)] = by_rule.get(_rule_letter(f), 0) + 1
        recurring, single = [], 0
        for p in patterns.values():
            sessions = _active_sessions(p, **({"project_key": gkey} if by == "project_key" else {"client": gkey}))
            if not sessions:
                continue
            if len(sessions) >= min_sessions:
                recurring.append((_rank_key(p, len(sessions)), _pattern_row(p, sessions, max_examples, servers)))
            else:
                single += 1
        recurring.sort(key=lambda t: t[0], reverse=True)
        label = (members[0]["project_dir"] or UNKNOWN_PROJECT) if by == "project_key" else gkey
        tokens = [r["tokens"] for r in members if isinstance(r["tokens"], int)]
        out.append({
            ("project" if by == "project_key" else "client"): label,
            **({"project_key": gkey} if by == "project_key" else {}),
            "sessions": len(members), "clients": sorted({r["client"] for r in members}),
            "projects": len({r["project_key"] for r in members}),
            "calls": sum(r["calls"] for r in members), "errors": sum(r["errors"] for r in members),
            "tokens": sum(tokens) if tokens else None, "tokens_sessions": len(tokens),
            "findings": sum(len(r["findings"]) for r in members), "findings_by_rule": by_rule,
            "recurring": [row for _, row in recurring], "single_session_patterns": single,
            "last_time": max((r["last_time"] or "" for r in members), default=""),
        })
    out.sort(key=lambda g: (g["sessions"], g["calls"]), reverse=True)
    return out


def build_trends(store: EventStore, cfg: dict[str, Any], feedback: dict[str, dict[str, Any]], *, days: int = 7,
                 client: str | None = None, project: str | None = None, min_sessions: int = 2,
                 now_ns: int | None = None, importer: Importer | None = None) -> dict[str, Any]:
    """Analyse toutes les sessions de la fenetre et agrege leurs signalements par motif.

    `days <= 0` = toutes les sessions enregistrees. `min_sessions` = nombre de sessions
    distinctes (hors faux positifs) a partir duquel un motif est dit recurrent. `importer`
    (client, cle de session) est appele avant l'analyse de chaque session retenue (usage).
    """
    tcfg = _section(cfg, "trends")
    max_top = int(tcfg.get("max_top", 5))
    max_examples = int(tcfg.get("max_examples", 8))
    min_sessions = max(1, int(min_sessions))
    now_ns = now_ns if now_ns is not None else time.time_ns()
    since_ns = now_ns - int(days) * 86_400 * 1_000_000_000 if days > 0 else None
    rows, skipped = _collect_sessions(store, cfg, client, project, since_ns, importer)
    patterns = _aggregate_patterns(rows, feedback)
    servers: dict[str, set[str]] = {}
    for r in rows:
        for name, tools in r["mcp_servers"].items():
            servers.setdefault(name, set()).update(tools)

    ranked: list[tuple[tuple[int, ...], dict[str, Any]]] = []
    single = false_only = 0
    for p in patterns.values():
        sessions = _active_sessions(p)
        if not sessions:
            false_only += 1
        elif len(sessions) >= min_sessions:
            ranked.append((_rank_key(p, len(sessions)), _pattern_row(p, sessions, max_examples, servers)))
        else:
            single += 1
    ranked.sort(key=lambda t: t[0], reverse=True)
    recurring = [row for _, row in ranked]
    recurring_keys = {row["pattern_key"] for row in recurring}

    by_rule_total = _empty_by_rule()
    by_session: list[dict[str, Any]] = []
    for r in rows:
        by_rule = _empty_by_rule()
        shared: dict[str, str] = {}
        for f in r["findings"]:
            letter = _rule_letter(f)
            by_rule[letter] = by_rule.get(letter, 0) + 1
            by_rule_total[letter] = by_rule_total.get(letter, 0) + 1
            key, label = pattern_key(f)
            if key in recurring_keys and not (f.feedback and f.feedback.get("mark") == "false-positive"):
                shared[key] = label
        by_session.append({
            "session_id": r["session_id"], "session_key": r["session_key"], "client": r["client"],
            "project_dir": r["project_dir"], "first_time": r["first_time"], "last_time": r["last_time"],
            "calls": r["calls"], "errors": r["errors"], "tokens": r["tokens"],
            "findings": len(r["findings"]), "findings_by_rule": by_rule,
            "recurring_patterns": len(shared), "recurring_labels": list(shared.values())[:3],
        })
    tokens_rows = [r["tokens"] for r in rows if isinstance(r["tokens"], int)]

    return {
        "trends_version": TRENDS_VERSION, "agentwatch_version": __version__,
        "window": {"days": days, "since": S.now_iso(since_ns / 1e9) if since_ns is not None else None,
                   "until": S.now_iso(now_ns / 1e9), "client": client, "project": project, "min_sessions": min_sessions},
        "sessions_analysed": len(rows), "sessions_skipped": skipped,
        "sessions_by_client": dict(Counter(r["client"] for r in rows)),
        "projects": len({r["project_key"] for r in rows}),
        "calls": sum(r["calls"] for r in rows), "errors": sum(r["errors"] for r in rows),
        "tokens": sum(tokens_rows) if tokens_rows else None, "tokens_sessions": len(tokens_rows),
        "findings": sum(len(r["findings"]) for r in rows), "findings_by_rule": by_rule_total,
        "mcp_servers_seen": {name: sorted(t for t in tools if t) for name, tools in sorted(servers.items())},
        "ranking_criteria": ["sessions distinctes", "confiance maximale (high > medium > low)", "occurrences",
                             "tokens mesures (transcripts ou rollouts importes)", "appels concernes", "octets de sortie observes"],
        "max_top": max_top,
        "recurring": recurring,
        "single_session_patterns": single, "false_positive_only_patterns": false_only,
        "by_project": _group_rows(rows, patterns, min_sessions, max_examples, "project_key", servers),
        "by_client": _group_rows(rows, patterns, min_sessions, max_examples, "client", servers),
        "by_session": by_session,
    }


# ---------------------------------------------------------------------------- rendu
def _n(v: Any) -> str:
    return "inconnu" if v is None else str(v)


def _day(t: str | None) -> str:
    return (t or "?")[:10]


def _cell(text: Any, limit: int = 90) -> str:
    s = str(text).replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "..."


def _conf_cell(row: dict[str, Any]) -> str:
    if not row["confidence_max"]:
        return "-"
    detail = ", ".join(f"{n} {c}" for c, n in sorted(row["confidence_counts"].items(), key=lambda kv: -_CONF_RANK.get(kv[0], 0)))
    return f"{row['confidence_max']} ({detail})"


def _feedback_cell(fb: dict[str, int]) -> str:
    parts = []
    if fb.get("relevant"):
        parts.append(f"{fb['relevant']} pertinent(s)")
    if fb.get("false-positive"):
        parts.append(f"{fb['false-positive']} faux positif(s)")
    return ", ".join(parts) or "-"


def _pattern_table(rows: list[dict[str, Any]], with_projects: bool = True) -> list[str]:
    head = ("| # | Motif | Regle | Sessions |" + (" Projets |" if with_projects else "")
            + " Clients | Occurrences | Appels | Tokens mesures | Sortie (octets) | Confiance max | Retours |")
    sep = "|---|---|---|---|" + ("---|" if with_projects else "") + "---|---|---|---|---|---|---|"
    out = [head, sep]
    for i, r in enumerate(rows, 1):
        out.append(f"| {i} | {_cell(r['label'])} | `{r['rule_id']}` | {r['sessions']} |" + (f" {r['projects']} |" if with_projects else "")
                   + f" {', '.join(r['clients'])} | {r['occurrences']} | {r['calls']} | {_n(r['tokens_sum'])} | {_n(r['output_bytes_sum'])} | "
                   f"{_conf_cell(r)} | {_feedback_cell(r['feedback'])} |")
    return out


def render_trends_markdown(report: dict[str, Any]) -> str:
    w = report["window"]
    fb_rule = report["findings_by_rule"]
    by_client = ", ".join(f"{c} {n}" for c, n in sorted(report["sessions_by_client"].items())) or "aucune"
    window_txt = f"{w['days']} dernier(s) jour(s), depuis {w['since']}" if w["since"] else "toutes les sessions enregistrees"
    filters = f"client = {w['client'] or 'tous'} ; projet = {w['project'] or 'tous'}"
    rules_txt = ", ".join(f"{k} {fb_rule.get(k, 0)}" for k in RULE_LETTERS)
    tokens_txt = (f"{report['tokens']} tokens mesures sur {report['tokens_sessions']} session(s) (transcripts ou rollouts)"
                  if report.get("tokens") is not None else "tokens non mesures (agentwatch import-transcripts ou import-rollouts)")
    lines = ["# AgentWatch - gaspillages recurrents", "",
             f"- Fenetre : {window_txt} (jusqu'a {w['until']}) ; filtres : {filters}",
             f"- Sessions analysees : {report['sessions_analysed']} ({by_client}) sur {report['projects']} projet(s) ; "
             f"{report['calls']} appels ; {report['errors']} en erreur ; {tokens_txt} ; {report['findings']} signalement(s) ({rules_txt})",
             f"- Motif recurrent = meme motif signale dans au moins {w['min_sessions']} session(s) distincte(s) ; "
             f"{len(report['recurring'])} recurrent(s), {report['single_session_patterns']} vu(s) dans une seule session (non listes), "
             f"{report['false_positive_only_patterns']} marque(s) faux positif(s) partout",
             f"- Serveurs MCP observes dans la fenetre : {', '.join(report['mcp_servers_seen']) or 'aucun'}",
             f"- AgentWatch {report['agentwatch_version']} ; sessions ignorees : {report['sessions_skipped']}",
             ""]
    if report.get("collection_health"):
        lines.append("Sante de la collecte : " + " ; ".join(f"{h['client']} : {h['message']}" for h in report["collection_health"]))
        lines.append("")
    if report["sessions_analysed"] == 0:
        lines += ["Aucune session dans la fenetre : elargir avec `--days N` (0 = toutes) ou retirer les filtres.", ""]
        return "\n".join(lines)

    lines += ["## Top des gaspillages recurrents", "",
              "Classement : " + " > ".join(report["ranking_criteria"]) + ". Aucun score global ; un motif recurrent est un "
              "candidat, les contre-indications de chaque signalement restent valables.", ""]
    recurring = report["recurring"]
    if not recurring:
        lines += [f"Aucun motif ne revient dans {w['min_sessions']} sessions ou plus : rien ne justifie encore un script, une "
                  "skill, un outil MCP ou une regle de projet. Les signalements isoles restent visibles avec `agentwatch report --session <id>`.", ""]
    else:
        lines += _pattern_table(recurring)
        lines.append("")
        for i, r in enumerate(recurring[: report["max_top"]], 1):
            lines += [f"### {i}. {r['label']}", "",
                      f"- Regle `{r['rule_id']}` ({', '.join(r['kinds'])}) ; {r['sessions']} session(s) sur {report['sessions_analysed']} ; "
                      f"{r['projects']} projet(s) : " + ", ".join(f"`{p}`" for p in r["project_dirs"]) + f" ; client(s) : {', '.join(r['clients'])}"]
            if r["tokens_sum"] is not None:
                lines.append(f"- Cout mesure : {r['tokens_sum']} tokens sur {r['tokens_known_for']} appel(s) (transcripts ou rollouts) ; "
                             "c'est ce qu'un outil ou une regle eviterait a chaque fois que le motif revient")
            ex = [e for e in r["examples"] if not e["false_positive"]]
            if ex:
                lines.append("- Sessions les plus recentes : " + ", ".join(
                    f"`{e['session_id'][:12]}` ({_day(e['last_time'])}, {e['client']}, {e['confidence']})" for e in ex[:6])
                    + (f" ... ({r['examples_total']} signalements au total)" if r["examples_total"] > len(ex[:6]) else ""))
            if r.get("mcp_available_in_window"):
                lines.append("- Outil deja disponible : serveur(s) MCP " + ", ".join(
                    f"`{m['server']}` ({m['basis']})" for m in r["mcp_available_in_window"]) + " observe(s) dans la fenetre")
            if r["proposal"]:
                lines.append(f"- Proposition (du signalement le plus sur) : {r['proposal']}")
            lines.append("- Signalements a marquer (`agentwatch feedback --finding <id> --mark relevant|false-positive`) : "
                         + ", ".join(f"`{e['finding_id']}`" for e in r["examples"][:6]))
            lines.append("")

    lines += ["## Par projet", "",
              "| Projet | Sessions | Clients | Appels | Erreurs | Tokens mesures | Signalements (A/B/C/D/E) | Motifs recurrents dans le projet | Derniere activite |",
              "|---|---|---|---|---|---|---|---|---|"]
    for g in report["by_project"]:
        br = g["findings_by_rule"]
        lines.append(f"| `{_cell(g['project'], 70)}` | {g['sessions']} | {', '.join(g['clients'])} | {g['calls']} | {g['errors']} | {_n(g['tokens'])} | "
                     f"{g['findings']} ({'/'.join(str(br.get(k, 0)) for k in RULE_LETTERS)}) | {len(g['recurring'])} | {_day(g['last_time'])} |")
    lines.append("")
    for g in report["by_project"]:
        lines.append(f"### Projet `{g['project']}`")
        lines.append("")
        if g["sessions"] < w["min_sessions"]:
            lines.append(f"- {g['sessions']} session(s) dans la fenetre : la recurrence n'est pas mesurable (seuil {w['min_sessions']}) ; "
                         f"{g['single_session_patterns']} motif(s) vu(s) une fois, voir `agentwatch report`.")
        elif not g["recurring"]:
            lines.append(f"- Aucun motif recurrent dans ce projet ({g['single_session_patterns']} motif(s) vu(s) dans une seule session).")
        else:
            lines += _pattern_table(g["recurring"], with_projects=False)
        lines.append("")

    lines += ["## Par client", "",
              "| Client | Sessions | Projets | Appels | Erreurs | Tokens mesures | Signalements (A/B/C/D/E) | Motifs recurrents chez ce client |",
              "|---|---|---|---|---|---|---|---|"]
    for g in report["by_client"]:
        br = g["findings_by_rule"]
        lines.append(f"| {g['client']} | {g['sessions']} | {g['projects']} | {g['calls']} | {g['errors']} | {_n(g['tokens'])} | "
                     f"{g['findings']} ({'/'.join(str(br.get(k, 0)) for k in RULE_LETTERS)}) | {len(g['recurring'])} |")
    lines.append("")
    for g in report["by_client"]:
        if g["recurring"]:
            lines += [f"### Client {g['client']}", ""] + _pattern_table(g["recurring"]) + [""]

    lines += ["## Par session", "",
              "| Debut | Client | Projet | Session | Appels | Erreurs | Tokens | A | B | C | D | E | Motifs recurrents |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in report["by_session"]:
        br = s["findings_by_rule"]
        labels = " ; ".join(_cell(x, 50) for x in s["recurring_labels"])
        shared = f"{s['recurring_patterns']}" + (f" ({labels})" if labels else "")
        lines.append(f"| {_day(s['first_time'])} | {s['client']} | `{_cell(s['project_dir'] or UNKNOWN_PROJECT, 50)}` | `{s['session_id'][:12]}` | "
                     f"{s['calls']} | {s['errors']} | {_n(s['tokens'])} | " + " | ".join(str(br.get(k, 0)) for k in RULE_LETTERS) + f" | {shared} |")
    lines += ["", "La colonne 'Motifs recurrents' compte les motifs de cette session qui reviennent ailleurs dans la fenetre ; "
              "le detail de chaque session : `agentwatch report --session <id>`.", ""]
    return "\n".join(lines)
