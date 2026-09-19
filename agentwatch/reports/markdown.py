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
    tok = cost.get("tokens")
    if isinstance(tok, dict):
        src = {"claude-code:transcript": "transcript", "codex:rollout": "rollout"}.get(str(tok.get("source")), str(tok.get("source")))
        parts.append(f"{tok['total']} tokens mesures ({tok['uncached_input']} d'entree non mise en cache + {tok['output']} de sortie ; "
                     f"{tok['known_for']}/{cost.get('calls')} appels, {src})")
    else:
        parts.append("tokens : non mesures (agentwatch import-transcripts ou import-rollouts)")
    return " ; ".join(parts)


def _cell(value: Any, limit: int = 60) -> str:
    s = str(value).replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "..."


def _counts(d: dict[str, int], labels: dict[str, str], limit: int = 4) -> str:
    items = sorted(((k, v) for k, v in (d or {}).items() if v), key=lambda kv: -kv[1])
    return ", ".join(f"{labels.get(k, k)} {v}" for k, v in items[:limit]) or "-"


def _render_repetitions(rep: dict[str, Any], top: int) -> list[str]:
    """Section G : chaque groupe d'appels repetes avec sa raison, son rythme, son apport et son verdict ; puis le
    rythme de chaque outil. Les groupes justifies y figurent aussi : ce qu'on ne peut pas ameliorer se lit ici."""
    from agentwatch.detectors import repeated_calls as G
    groups = rep.get("groups") or []
    rhythm = rep.get("rhythm") or []
    if not groups and not rhythm:
        return []
    out = ["## Appels repetes : pourquoi, a quel rythme", ""]
    t = rep.get("totals") or {}
    if groups:
        byv = t.get("round_trips_by_verdict") or {}
        out.append(f"{t.get('groups')} groupe(s) d'appels refaits a l'identique (au moins {rep.get('min_calls')} fois) : "
                   f"{t.get('calls')} appels, {t.get('round_trips')} reprise(s) avec aller-retour du modele. Reprises par verdict : "
                   + (", ".join(f"{G.VERDICT_LABELS.get(k, k)} {v}" for k, v in sorted(byv.items(), key=lambda kv: -kv[1])) or "-")
                   + ".")
        out.append("")
        out += ["| Agent | Outil | Cible | Appels (episodes) | Intervalle median (min) | Raisons observees | Raisons annoncees | "
                "Apport | Verdict | Suggestion |", "|---|---|---|---|---|---|---|---|---|---|"]
        for g in groups[:top]:
            iv = g.get("interval_s") or {}
            oc = g.get("outcomes") or {}
            gain = (f"change {oc.get('changed', 0)} / identique {oc.get('same', 0)}"
                    + (f" / inconnu {oc['unknown']}" if oc.get("unknown") else ""))
            rec = ((g.get("cadence") or {}).get("recommended") or {})
            sugg = g.get("suggestion") or ""
            if rec and not sugg:
                sugg = f"1 appel / {G.fmt_duration(rec['cooldown_s'])} : -{rec['avoided']} appels"
            agent = "principal" if g["agent"] == "main" else f"`{str(g['agent'])[:8]}`"
            out.append(f"| {agent} | {_cell(g['tool'], 40)} | {_cell(g.get('target') or '-', 50)} | {g['calls']} ({g['episodes']}) | "
                       f"{G.fmt_duration(iv.get('median')) if iv else '-'} ({G.fmt_duration(iv.get('min')) if iv else '-'}) | "
                       f"{_counts(g.get('reasons') or {}, G.REASON_LABELS)} | {_counts(g.get('declared') or {}, G.DECLARED_LABELS)} | "
                       f"{gain} | **{g['verdict_label']}** | {_cell(sugg, 120) or '-'} |")
        if len(groups) > top:
            out.append(f"| ... | {len(groups) - top} autre(s) groupe(s) dans l'export JSON | | | | | | | | |")
        out.append("")
        out.append("Raisons observees : ce qui precede chaque reprise (resultat precedent, actions et messages entre les deux). "
                   "Raisons annoncees : categories reconnues dans les commentaires de l'agent, sans texte conserve. "
                   "Apport : etat du resultat (horodatages neutralises) change ou identique.")
        out.append("")
        for g in [g for g in groups if g.get("cadence") and (g["cadence"].get("recommended") or {})][:3]:
            cad = g["cadence"]
            out.append(f"Cadence simulee pour {g['tool']} ({g['calls']} appels, attente typique {G.fmt_duration(cad['typical_wait_s'])}, "
                       f"{cad['basis']}) :")
            out.append("")
            out += ["| Delai minimal | Appels | Evites | Changements de phase | Retard max | Retard moyen |", "|---|---|---|---|---|---|"]
            for s in cad["simulation"]:
                mark = " (suggere)" if s["cooldown_s"] == cad["recommended"]["cooldown_s"] else ""
                out.append(f"| {G.fmt_duration(s['cooldown_s'])}{mark} | {s['calls']} | {s['avoided']} | {s['changes']} | "
                           f"{G.fmt_duration(s['max_delay_s'])} | {G.fmt_duration(s['mean_delay_s'])} |")
            out.append("")
    if rhythm:
        out += ["Rythme des outils (intervalle entre deux appels successifs d'un meme agent, hors appels d'une meme reponse ; "
                "pics tous agents confondus) :", "",
                "| Outil | Appels | Agents | Intervalle median | 10 % des intervalles sous | Max / 1 min | Max / 10 min | "
                "Identiques a un precedent | dont sans apport |", "|---|---|---|---|---|---|---|---|---|"]
        for r in rhythm:
            out.append(f"| {_cell(r['tool'], 50)} | {r['calls']} | {r['agents']} | {G.fmt_duration(r['interval_median_s'])} | "
                       f"{G.fmt_duration(r['interval_p10_s'])} | {r['max_per_min']} | {r['max_per_10min']} | "
                       f"{r['identical_repeats']} | {r['repeats_no_gain']} |")
        out.append("")
    return out


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
    cad = prop.get("cadence") if isinstance(prop.get("cadence"), dict) else None
    if cad and cad.get("recommended") and detailed:
        from agentwatch.detectors.repeated_calls import fmt_duration
        out.append("Cadence simulee (" + cad.get("basis", "") + ") : " + " ; ".join(
            f"{fmt_duration(s['cooldown_s'])} -> {s['calls']} appels (retard max {fmt_duration(s['max_delay_s'])})"
            for s in cad.get("simulation") or []))
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
    if report.get("collection_health"):
        lines.append("Sante de la collecte : " + " ; ".join(f"{h['client']} : {h['message']}" for h in report["collection_health"]))
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
              f"- Usage de tokens : {st['usage']['status']} ({st['usage']['note']})", ""]
    threads = st["usage"].get("threads") or []
    if threads:
        lines += ["| Fil | Agent | Requetes du modele (demandes de compaction comprises) | Tokens | Entree (dont en cache) | Sortie (dont raisonnement) | Fenetres de contexte (lignes compacted + 1) |",
                  "|---|---|---|---|---|---|---|"]
        for t in threads:
            who = "principal" if not t.get("agent_id") else f"{t.get('agent_nickname') or '?'} ({t.get('agent_type') or 'sous-agent'})"
            lines.append(f"| `{str(t['thread_id'])[:13]}` | {who} | {t['requests']} | {t['total_tokens']} | "
                         f"{t['input_tokens']} ({t['cached_input_tokens']}) | {t['output_tokens']} ({t['reasoning_output_tokens']}) | "
                         f"{_fmt(t.get('windows'))} |")
        lines.append("")
    lines += [
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
            target = str(w["target"]).replace("|", "\\|").replace("\n", " ")
            shown = target if len(target) <= 110 else target[:110] + "..."
            lines.append(f"| {w['op']} | {shown} | {w['calls']} | {', '.join(w['tools'])} | {w['agents']} | "
                         f"{_fmt(w['distinct_contents'])} |")
        lines.append("")
        lines.append("Un travail refait n'est pas forcement inutile : voir les signalements et leurs contre-indications.")
        lines.append("")
    lines += _render_repetitions(st.get("repetitions") or {}, int(report.get("repetitions_top", 15)))
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
            status_txt = a["classification"] + (f" (repris {a['resumes']}x)" if a.get("resumes") else "")
            lines.append(f"| `{a['agent_id'][:12]}` | {a.get('agent_type') or 'inconnu'} | {status_txt} | {a['calls']} | "
                         f"{a.get('start_time') or '?'} -> {a.get('stop_time') or 'en cours'} | {parent} | {a.get('link_basis') or '-'} | "
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
