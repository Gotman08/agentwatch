"""Rendu Markdown : trois opportunites prioritaires maximum, puis statistiques et couverture."""

from __future__ import annotations

from typing import Any

from agentwatch.detectors.base import Finding


def _fmt(v: Any) -> str:
    if v is None:
        return "inconnu"
    if isinstance(v, bool):
        return "oui" if v else "non"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def _short(value: Any, limit: int = 100) -> str:
    """Cible bornee pour l'affichage : une commande longue reste lisible dans le JSON."""
    s = repr(value) if not isinstance(value, str) else value
    s = s.replace("\n", " ")
    return f"`{s}`" if len(s) <= limit else f"`{s[:limit]}...` ({len(s)} car., complet dans l'export JSON)"


def _cost_line(cost: dict[str, Any]) -> str:
    parts = [f"{cost.get('calls')} appel(s)"]
    if cost.get("output_bytes_sum") is not None:
        parts.append(f"{cost['output_bytes_sum']} octets de sortie observes ({cost.get('output_bytes_known_for')} appels mesures)")
    if cost.get("duration_client_ms_sum") is not None:
        parts.append(f"{cost['duration_client_ms_sum']} ms (duree client)")
    if cost.get("duration_reconstructed_ms_sum") is not None:
        parts.append(f"{cost['duration_reconstructed_ms_sum']} ms (reconstruite entre hooks, inclut la surcharge des hooks)")
    if cost.get("duration_unknown_for"):
        parts.append(f"duree inconnue pour {cost['duration_unknown_for']} appel(s)")
    parts.append("tokens : non mesures")
    return " ; ".join(parts)


def _render_finding(f: Finding, detailed: bool) -> list[str]:
    out = [f"### {f.title}", "",
           f"- Regle : `{f.rule_id}` v{f.rule_version} ({f.kind}) ; identifiant `{f.finding_id}`",
           f"- Confiance : **{f.confidence}** ; {f.confidence_rationale}",
           f"- Appels concernes : " + ", ".join(f"#{r['seq']} {r['tool']} ({r['status']})" for r in f.call_refs[:12])
           + (" ..." if len(f.call_refs) > 12 else ""),
           f"- Cout observe : {_cost_line(f.observed_cost)}"]
    if f.feedback:
        out.append(f"- Retour local : **{f.feedback.get('mark')}**" + (f" ({f.feedback.get('note')})" if f.feedback.get("note") else ""))
    out += ["", f"{f.explanation}", ""]
    if detailed:
        out.append("Preuve locale :")
        for k, v in f.evidence.items():
            if k in ("pair_verdicts",):
                continue
            out.append(f"- {k} : {_fmt(v) if not isinstance(v, (list, dict)) else v}")
        out.append("")
    out.append("Contre-indications :")
    out += [f"- {c}" for c in f.counter_indications]
    if f.missing_data:
        out.append("")
        out.append("Donnees manquantes : " + " ; ".join(f.missing_data))
    out.append("")
    prop = f.proposal
    out.append("Amelioration proposee : " + str(prop.get("text", "")))
    if prop.get("hypotheses"):
        out.append("Hypotheses de cause : " + " ; ".join(prop["hypotheses"]))
    if prop.get("grouped_tool"):
        gt = prop["grouped_tool"]
        out.append(f"Outil groupe : {gt.get('status')} - {gt.get('note')}" + (f" ({', '.join(gt['tools'])})" if gt.get("tools") else ""))
    if prop.get("recipe") and detailed:
        r = prop["recipe"]
        out.append("")
        out.append(f"Recette candidate `{r['name']}` :")
        out.append("- Entrees : " + (", ".join(f"etape {i['step']} (ex. {i['example']!r})" for i in r["inputs"]) or "aucune (motif fixe)"))
        out.append("- Preconditions : " + " ; ".join(r["preconditions"]))
        for s in r["steps"]:
            out.append(f"- Etape {s['position']} [{s['kind']}] {s['tool']} : {s['reason']} (ex. {s['example_target']!r})")
        out.append("- Sortie : " + r["output"])
        out.append("- Tests : " + " ; ".join(r["tests"]))
        out.append("- Risques : " + " ; ".join(r["risks"]))
    out.append("")
    out.append("Protocole de validation :")
    out += [f"{i}. {s}" for i, s in enumerate(f.validation_protocol, 1)]
    out.append("")
    return out


def render_markdown(report: dict[str, Any]) -> str:
    s = report["session"]
    findings = report["findings"]
    top_ids = set(report["top_findings"])
    lines = [f"# AgentWatch - rapport de session", "",
             f"- Client : `{s['client']}` ; session `{s['session_id']}` ; modele : `{s['model'] or 'non observe'}` ({s['model_source']})",
             f"- Projet : `{s['project_dir'] or 'inconnu'}` ; periode : {s['first_time']} -> {s['last_time']}",
             f"- Tours : {s['turns']} ; epoques de contexte : {s['context_epochs']} ; agents : {', '.join(s['agents']) or 'aucun'}",
             f"- Version de l'executable `{s['client']}` du PATH lors de `configure` : {s.get('client_version_at_configure') or 'inconnue'} "
             f"(la version reellement executee n'est pas transmise aux hooks) ; AgentWatch {report['agentwatch_version']} ; schema {report['schema_version']}",
             ""]
    if s.get("warnings"):
        lines.append("Avertissements de lecture : " + " ; ".join(s["warnings"][:5]))
        lines.append("")
    lines.append("## Opportunites prioritaires")
    lines.append("")
    lines.append("Classement : " + " > ".join(report["ranking_criteria"]) + ". Aucun score global.")
    lines.append("")
    if not findings:
        lines.append(report["no_issue_statement"] or "Aucun probleme demontre dans les donnees couvertes.")
        lines.append("")
    else:
        top = [f for f in findings if f["finding_id"] in top_ids]
        if not top:
            lines.append(report.get("no_issue_statement") or "Tous les signalements ont ete marques comme faux positifs localement.")
            lines.append("")
        for fd in top:
            lines += _render_finding(Finding(**fd), detailed=True)
        rest = [f for f in findings if f["finding_id"] not in top_ids]
        if rest:
            cap = int(report.get("max_listed_per_rule", 15))
            lines.append("## Autres signalements")
            lines.append("")
            by_rule: dict[str, list[dict[str, Any]]] = {}
            for fd in rest:
                by_rule.setdefault(fd["rule_id"], []).append(fd)
            for rule_id, items in by_rule.items():
                lines.append(f"### {rule_id} : {len(items)} signalement(s)")
                lines.append("")
                for fd in items[:cap]:
                    f = Finding(**fd)
                    fb = f" ; retour local : {f.feedback['mark']}" if f.feedback else ""
                    lines.append(f"- `{f.finding_id}` {f.title} - confiance {f.confidence}{fb}")
                if len(items) > cap:
                    lines.append(f"- ... et {len(items) - cap} autre(s) (tous presents dans l'export JSON)")
                lines.append("")
    st = report["stats"]
    lines += ["## Statistiques descriptives", "",
              f"- Evenements : {st['events']} ; appels correles : {st['calls']} ; statuts : {st['status']}",
              f"- Correlation : {st['correlation']}",
              f"- Surcharge des hooks (dans le processus, hors demarrage de l'interpreteur) : {st['hook_overhead_ms']}",
              f"- Usage de tokens : {st['usage']['status']} ({st['usage']['note']})", "",
              "| Outil | Appels | Erreurs | Statut inconnu | Ouverts | Sortie (octets, mesures) | Duree client mediane (n) | Duree reconstruite mediane (n) |",
              "|---|---|---|---|---|---|---|---|"]
    for t in st["tools"][:15]:
        lines.append(f"| {t['tool']} | {t['calls']} | {t['errors']} | {t['unknown_status']} | {t['open']} | "
                     f"{_fmt(t['output_bytes'])} ({t['output_known_for']}) | {_fmt(t['client_duration_median_ms'])} ({t['client_duration_n']}) | "
                     f"{_fmt(t['reconstructed_duration_median_ms'])} ({t['reconstructed_duration_n']}) |")
    lines.append("")
    wu = [w for w in st.get("work_units", []) if w["repeated"]]
    if wu:
        lines += ["Unites de travail refaites (meme operation, meme cible, tous outils confondus) :", "",
                  "| Operation | Cible | Fois | Outils | Agents | Contenus distincts obtenus |", "|---|---|---|---|---|---|"]
        for w in wu[:15]:
            lines.append(f"| {w['op']} | {str(w['target'])[:60]} | {w['calls']} | {', '.join(w['tools'])} | {w['agents']} | "
                         f"{_fmt(w['distinct_contents'])} |")
        lines.append("")
        lines.append("Un travail refait n'est pas forcement inutile : voir les signalements et leurs contre-indications.")
        lines.append("")
    if st["error_signatures"]:
        lines.append("Signatures d'erreur les plus frequentes :")
        lines += [f"- {e['count']}x `{e['signature']}`" for e in st["error_signatures"]]
        lines.append("")
    if st["largest_outputs"]:
        lines.append("Sorties les plus volumineuses (une sortie volumineuse n'est pas, seule, un gaspillage) :")
        lines += [f"- #{c['seq']} {c['tool']} {_short(c['target'])} : {c['output_size_bytes']} octets" for c in st["largest_outputs"]]
        lines.append("")
    if st["longest_calls"]:
        lines.append("Appels les plus longs (source de la duree indiquee) :")
        lines += [f"- #{c['seq']} {c['tool']} {_short(c['target'])} : {c['duration_ms']} ms ({c['duration_source']})" for c in st["longest_calls"]]
        lines.append("")
    agents = report.get("agents") or []
    if agents:
        internal = [a for a in agents if a["classification"] == "stop_only"]
        shown = [a for a in agents if a["classification"] != "stop_only"]
        lines += ["## Agents", "",
                  "| Agent | Type | Statut | Appels | Debut -> fin | Lance par | Base du lien | Modele | Tokens (rapportes par le client) |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for a in shown:
            u = a.get("usage") or {}
            tokens = (f"in {_fmt(u.get('input_tokens'))} / out {_fmt(u.get('output_tokens'))} / cache lu {_fmt(u.get('cache_read_tokens'))}"
                      f" / total {_fmt(u.get('total_tokens'))}") if u else "non rapportes"
            parent = f"#{a['parent_call_seq']} (Agent)" if a.get("parent_call_seq") is not None else "aucun lien"
            lines.append(f"| `{a['agent_id'][:12]}` | {a.get('agent_type') or 'inconnu'} | {a['classification']} | {a['calls']} | "
                         f"{a.get('start_time') or '?'} -> {a.get('stop_time') or '?'} | {parent} | {a.get('link_basis') or '-'} | "
                         f"{a.get('model') or 'inconnu'} | {tokens} |")
        lines.append("")
        if internal:
            lines.append(f"- {len(internal)} agent(s) interne(s) non detailles : seulement un `SubagentStop`, sans type ni appel "
                         f"(identifiants dans l'export JSON) ; agent interne du client ou demarre avant l'installation des hooks.")
        if shown:
            lines.append("- sous-agent observe : demarrage et/ou appels d'outils ; les detecteurs ne comparent jamais deux agents entre eux.")
        lines.append("")
    lines += ["## Couverture", "", "| Capacite | Documente | Observe dans cette session | Base |", "|---|---|---|---|"]
    for r in report["coverage"]:
        lines.append(f"| {r['capability']} | {r['documented']} | {r['observed_in_session']} | {r['basis']} |")
    lines.append("")
    lines.append("Les capacites non listees ne sont pas observees : aucun pourcentage de couverture globale n'est calcule.")
    lines.append("")
    return "\n".join(lines)
