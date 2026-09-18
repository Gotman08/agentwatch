"""Rendu terminal du rapport avec Rich (dependance OPTIONNELLE, jamais chargee par le hook).

# * Rich n'est importe que si `report --format rich|html|svg` est demande (ou par defaut
#   dans un terminal interactif quand Rich est installe). Le Markdown et le JSON restent
#   les sorties canoniques : cette vue n'ajoute aucune information, seulement une mise en
#   forme (panneaux par confiance, tableaux, barres proportionnelles).
"""

from __future__ import annotations

import io
from typing import Any

_CONF_STYLE = {"high": "bold red", "medium": "bold yellow", "low": "dim"}
_COVER_STYLE = {"observed": "green", "not_observed": "yellow", "absent": "dim"}


def rich_available() -> bool:
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        return False


def rich_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("rich")
    except Exception:  # noqa: BLE001 - metadonnees absentes : la version reste inconnue
        return None


def install_command() -> str:
    """Commande d'installation pour L'INTERPRETEUR COURANT.

    # ! Une machine a souvent plusieurs Python (python.org, Microsoft Store, venv) : un
    #   `pip install rich` nu peut viser un autre interpreteur que celui qui lance AgentWatch.
    #   On nomme donc toujours l'executable exact.
    """
    import sys
    exe = sys.executable or "python"
    return f'& "{exe}" -m pip install rich' if sys.platform == "win32" else f'"{exe}" -m pip install rich'


def missing_rich_message() -> str:
    import sys
    ver = ".".join(str(x) for x in sys.version_info[:3])
    lines = [
        "le format demande necessite Rich, absent de l'interpreteur qui execute AgentWatch :",
        f"  {sys.executable} (Python {ver})",
        "Installez-le pour cet interpreteur precis (un `pip install rich` nu peut viser un autre Python) :",
        f"  {install_command()}",
        "Sans Rich : --format markdown (defaut hors terminal) ou --format json.",
    ]
    return "\n".join(lines)


def _bar(value: float, maximum: float, width: int = 28) -> str:
    if not maximum or value is None:
        return ""
    n = int(round(width * float(value) / float(maximum)))
    return "█" * max(0, min(width, n))


def _fmt(v: Any) -> str:
    if v is None:
        return "inconnu"
    if isinstance(v, bool):
        return "oui" if v else "non"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def _duration(median: Any, n: Any) -> str:
    if median is None or not n:
        return "-"
    return f"{_fmt(median)} ms ({n})"


def _short(value: Any, limit: int = 90) -> str:
    s = value if isinstance(value, str) else repr(value)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _finding_panel(f: dict[str, Any], detailed: bool):
    from rich.panel import Panel
    from rich.text import Text

    style = _CONF_STYLE.get(f["confidence"], "")
    body = Text()
    body.append(f"Regle {f['rule_id']} v{f['rule_version']} ({f['kind']})  id {f['finding_id']}\n", style="dim")
    body.append("Confiance : ", style="bold")
    body.append(f"{f['confidence']}", style=style)
    body.append(f" - {f['confidence_rationale']}\n")
    refs = ", ".join(f"#{r['seq']} {r['tool']} ({r['status']})" for r in f["call_refs"][:10])
    if len(f["call_refs"]) > 10:
        refs += f" ... (+{len(f['call_refs']) - 10})"
    body.append("Appels : ", style="bold")
    body.append(refs + "\n")
    cost = f["observed_cost"]
    parts = [f"{cost.get('calls')} appel(s)"]
    if cost.get("output_bytes_sum") is not None:
        parts.append(f"{cost['output_bytes_sum']} octets observes")
    if cost.get("duration_client_ms_sum") is not None:
        parts.append(f"{cost['duration_client_ms_sum']} ms (client)")
    if cost.get("duration_reconstructed_ms_sum") is not None:
        parts.append(f"{cost['duration_reconstructed_ms_sum']} ms (reconstruit)")
    tok = cost.get("tokens")
    parts.append(f"{tok['total']} tokens mesures ({tok['known_for']} appels, transcript)" if isinstance(tok, dict) else "tokens non mesures")
    body.append("Cout observe : ", style="bold")
    body.append(" ; ".join(parts) + "\n\n")
    body.append(f["explanation"] + "\n")
    if detailed:
        if f["evidence"].get("trigger_counts"):
            body.append("Declencheurs : ", style="bold")
            body.append(f"{f['evidence']['trigger_counts']}\n")
        body.append("\nContre-indications :\n", style="bold")
        for c in f["counter_indications"]:
            body.append(f"  - {c}\n")
        if f["missing_data"]:
            body.append("Donnees manquantes : ", style="bold")
            body.append(" ; ".join(f["missing_data"]) + "\n")
        body.append("\nProposition : ", style="bold")
        body.append(str(f["proposal"].get("text", "")) + "\n")
        if f["proposal"].get("hypotheses"):
            body.append("Hypotheses : " + " ; ".join(f["proposal"]["hypotheses"]) + "\n")
        if f["proposal"].get("tooling"):
            body.append("Cote outillage : " + " ".join(f["proposal"]["tooling"]) + "\n")
        body.append("\nValidation :\n", style="bold")
        for i, s in enumerate(f["validation_protocol"], 1):
            body.append(f"  {i}. {s}\n")
    if f.get("feedback"):
        body.append(f"\nRetour local : {f['feedback'].get('mark')}", style="italic")
    return Panel(body, title=f"[{style}]{f['title']}[/]", border_style=style or "white", expand=True)


def render(report: dict[str, Any], console: Any) -> None:
    """Ecrit le rapport sur une console Rich."""
    from rich import box
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    s = report["session"]
    st = report["stats"]
    head = Text()
    head.append(f"Client {s['client']}  session {s['session_id']}\n", style="bold")
    head.append(f"Modele : {s['model'] or 'non observe'} ({s['model_source']})\n")
    head.append(f"Projet : {s['project_dir'] or 'inconnu'}   Periode : {s['first_time']} -> {s['last_time']}\n")
    head.append(f"Tours : {s['turns']}   Epoques de contexte : {s['context_epochs']}   Agents : {len(s['agents'])}   "
                f"Evenements : {st['events']}   Appels : {st['calls']}\n")
    head.append(f"AgentWatch {report['agentwatch_version']}  schema {report['schema_version']}  "
                f"executable {s['client']} du PATH a la configuration : {s.get('client_version_at_configure') or 'inconnu'}", style="dim")
    console.print(Panel(head, title="AgentWatch - rapport de session", border_style="cyan"))

    findings = report["findings"]
    top_ids = set(report["top_findings"])
    console.rule("[bold]Opportunites prioritaires")
    console.print(Text("Classement : " + " > ".join(report["ranking_criteria"]) + ". Aucun score global.", style="dim"))
    top = [f for f in findings if f["finding_id"] in top_ids]
    if not top:
        console.print(Panel(report.get("no_issue_statement") or "Aucun probleme demontre dans les donnees couvertes.", border_style="green"))
    for f in top:
        console.print(_finding_panel(f, detailed=True))
    rest = [f for f in findings if f["finding_id"] not in top_ids]
    if rest:
        console.rule("[bold]Autres signalements")
        t = Table(box=box.SIMPLE, expand=True)
        t.add_column("Regle"); t.add_column("Confiance"); t.add_column("Titre"); t.add_column("Appels", justify="right"); t.add_column("Id")
        cap = int(report.get("max_listed_per_rule", 15))
        by_rule: dict[str, list[dict[str, Any]]] = {}
        for f in rest:
            by_rule.setdefault(f["rule_id"], []).append(f)
        for rule, items in by_rule.items():
            for f in items[:cap]:
                t.add_row(rule, Text(f["confidence"], style=_CONF_STYLE.get(f["confidence"], "")), _short(f["title"], 80), str(len(f["calls"])), f["finding_id"])
            if len(items) > cap:
                t.add_row(rule, "", f"... et {len(items) - cap} autre(s) dans l'export JSON", "", "")
        console.print(t)

    console.rule("[bold]Statistiques")
    tools = st["tools"]
    max_calls = max((x["calls"] for x in tools), default=0)
    max_bytes = max((x["output_bytes"] or 0 for x in tools), default=0)
    t = Table(title="Appels par outil - barres : appels (cyan), octets de sortie (magenta) ; durees medianes, n = appels connus",
              box=box.SIMPLE_HEAD, width=min(console.width, 118))
    for col, just, width in (("Outil", "left", 10), ("Appels", "right", 6), ("", "left", 10), ("Err.", "right", 4),
                             ("Ouv.", "right", 4), ("Sortie (n)", "right", 12), ("", "left", 8),
                             ("Client (n)", "right", 13), ("Reconstr. (n)", "right", 13)):
        t.add_column(col, justify=just, min_width=width, no_wrap=True, overflow="ellipsis")  # type: ignore[arg-type]
    for x in tools[:15]:
        err_style = "red" if x["errors"] else ""
        t.add_row(_short(x["tool"], 24), str(x["calls"]), Text(_bar(x["calls"], max_calls, 10), style="cyan"),
                  Text(str(x["errors"]), style=err_style), str(x["open"]),
                  f"{_fmt(x['output_bytes'])} ({x['output_known_for']})", Text(_bar(x["output_bytes"] or 0, max_bytes, 8), style="magenta"),
                  _duration(x["client_duration_median_ms"], x["client_duration_n"]),
                  _duration(x["reconstructed_duration_median_ms"], x["reconstructed_duration_n"]))
    console.print(t)
    console.print(Text(f"Statuts : {st['status']}   Correlation : {st['correlation']}", style="dim"))
    ho = st["hook_overhead_ms"]
    console.print(Text(f"Surcharge des hooks (dans le processus) : mediane {ho.get('median')} ms, p90 {ho.get('p90')} ms, max {ho.get('max')} ms sur {ho.get('n')} evenements", style="dim"))
    console.print(Text(f"Tokens : {st['usage']['status']}", style="dim"))

    wu = [w for w in st.get("work_units", []) if w["repeated"]]
    if wu:
        t = Table(title="Unites de travail refaites (meme operation, meme cible, tous outils confondus)", box=box.SIMPLE_HEAD, expand=True)
        t.add_column("Operation"); t.add_column("Cible"); t.add_column("Fois", justify="right"); t.add_column("Outils"); t.add_column("Agents", justify="right"); t.add_column("Contenus distincts", justify="right")
        max_w = max(w["calls"] for w in wu)
        for w in wu[:15]:
            t.add_row(w["op"], _short(str(w["target"]), 70), Text(f"{w['calls']} {_bar(w['calls'], max_w, 12)}", style="yellow"),
                      ", ".join(w["tools"]), str(w["agents"]), _fmt(w["distinct_contents"]))
        console.print(t)
    if st["error_signatures"]:
        t = Table(title="Signatures d'erreur", box=box.SIMPLE_HEAD, expand=True)
        t.add_column("Fois", justify="right"); t.add_column("Signature")
        for e in st["error_signatures"]:
            t.add_row(str(e["count"]), _short(e["signature"], 110))
        console.print(t)
    if st["longest_calls"]:
        t = Table(title="Appels les plus longs", box=box.SIMPLE_HEAD, expand=True)
        t.add_column("#", justify="right"); t.add_column("Outil"); t.add_column("Cible"); t.add_column("Duree", justify="right"); t.add_column("Source")
        mx = max(c["duration_ms"] or 0 for c in st["longest_calls"])
        for c in st["longest_calls"]:
            t.add_row(str(c["seq"]), c["tool"], _short(str(c["target"]), 60), Text(f"{c['duration_ms']} ms {_bar(c['duration_ms'] or 0, mx, 12)}", style="blue"), str(c["duration_source"]))
        console.print(t)

    agents = report.get("agents") or []
    if agents:
        console.rule("[bold]Agents")
        t = Table(box=box.SIMPLE_HEAD, expand=True)
        for col in ("Agent", "Type", "Statut", "Appels", "Debut -> fin", "Lance par", "Lien", "Modele", "Tokens rapportes"):
            t.add_column(col, no_wrap=col in ("Agent", "Appels", "Debut -> fin", "Lance par"), overflow="ellipsis")
        internal = 0
        for a in agents:
            if a["classification"] == "stop_only":
                internal += 1
                continue
            u = a.get("usage") or {}
            tokens = f"in {_fmt(u.get('input_tokens'))} / out {_fmt(u.get('output_tokens'))} / total {_fmt(u.get('total_tokens'))}" if u else "non rapportes"
            status = a["classification"] + (f" (repris {a['resumes']}x)" if a.get("resumes") else "")
            t.add_row(a["agent_id"][:12], a.get("agent_type") or "inconnu", status, str(a["calls"]),
                      f"{(a.get('start_time') or '?')[11:19]} -> {(a.get('stop_time') or 'en cours')[11:19] if a.get('stop_time') else 'en cours'}",
                      f"#{a['parent_call_seq']}" if a.get("parent_call_seq") is not None else "-", _short(a.get("link_basis") or "-", 40),
                      a.get("model") or "inconnu", tokens)
        console.print(t)
        if internal:
            console.print(Text(f"{internal} agent(s) interne(s) : seulement un SubagentStop, sans type ni appel (details dans le JSON).", style="dim"))

    console.rule("[bold]Couverture")
    t = Table(box=box.SIMPLE_HEAD, expand=True)
    t.add_column("Capacite"); t.add_column("Documente"); t.add_column("Observe"); t.add_column("Base")
    for r in report["coverage"]:
        t.add_row(r["capability"], r["documented"], Text(r["observed_in_session"], style=_COVER_STYLE.get(r["observed_in_session"], "")), _short(r["basis"], 90))
    console.print(t)
    console.print(Text("Aucun pourcentage de couverture globale : le nombre total d'actions possibles n'est pas connu.", style="dim"))


def render_to_terminal(report: dict[str, Any]) -> None:
    from rich.console import Console
    render(report, Console())


def export(report: dict[str, Any], fmt: str, width: int = 120) -> str:
    """Rendu enregistre puis exporte en 'html', 'svg' ou 'text' (sans terminal)."""
    from rich.console import Console
    console = Console(record=True, width=width, force_terminal=fmt != "text", color_system="truecolor" if fmt != "text" else None, file=io.StringIO())
    render(report, console)
    if fmt == "html":
        return console.export_html(inline_styles=True)
    if fmt == "svg":
        return console.export_svg(title="AgentWatch - rapport de session")
    return console.export_text()
