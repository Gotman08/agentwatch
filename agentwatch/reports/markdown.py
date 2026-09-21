"""Rendu Markdown : trois opportunites prioritaires maximum, puis statistiques et couverture."""

from __future__ import annotations

from typing import Any

from agentwatch.detectors.base import Finding
from agentwatch.reports.labels import agent_cells, agent_label, other_tools_row, recon_label, tokens_cost_text


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
    if value is None:
        return "(sans cible)"
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
        parts.append(f"{cost['duration_reconstructed_ms_sum']} ms ({recon_label(cost)})")
    if cost.get("duration_unknown_for"):
        parts.append(f"duree inconnue pour {cost['duration_unknown_for']} appel(s)")
    parts.append(tokens_cost_text(cost))
    return " ; ".join(parts)


def _agent(report: dict[str, Any], agent_id: Any) -> str:
    return agent_label(report, agent_id)


def _cell(value: Any, limit: int = 60) -> str:
    s = str(value).replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "..."


def _counts(d: dict[str, int], labels: dict[str, str], limit: int = 4) -> str:
    items = sorted(((k, v) for k, v in (d or {}).items() if v), key=lambda kv: -kv[1])
    return ", ".join(f"{labels.get(k, k)} {v}" for k, v in items[:limit]) or "-"


def _render_repetitions(rep: dict[str, Any], top: int, report: dict[str, Any] | None = None) -> list[str]:
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
        out += ["| Agent | Outil | Cible | Appels (episodes) | Delai demande a l'outil | Intervalle observe entre deux appels : "
                "median (minimum) | Raisons observees | Raisons annoncees | Resultat suivant | Verdict | Suggestion |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        for g in groups[:top]:
            req = (g.get("wait_timing") or {}).get("requested_s") or []
            req_txt = ", ".join(G.fmt_duration(float(r)) for r in req[:3]) or "-"
            iv = g.get("interval_s") or {}
            oc = g.get("outcomes") or {}
            gain = (f"change {oc.get('changed', 0)} / identique {oc.get('same', 0)}"
                    + (f" / inconnu {oc['unknown']}" if oc.get("unknown") else ""))
            rec = ((g.get("cadence") or {}).get("recommended") or {})
            sugg = g.get("suggestion") or ""
            if rec and not sugg:
                sugg = f"1 appel / {G.fmt_duration(rec['cooldown_s'])} : -{rec['avoided']} appels"
            agent = _agent(report or {}, g["agent"])
            out.append(f"| {agent} | {_cell(g['tool'], 40)} | {_cell(g.get('target') or '-', 70)} | {g['calls']} ({g['episodes']}) | "
                       f"{req_txt} | "
                       f"{G.fmt_duration(iv.get('median')) if iv else '-'} ({G.fmt_duration(iv.get('min')) if iv else '-'}) | "
                       f"{_counts(g.get('reasons') or {}, G.REASON_LABELS)} | {_counts(g.get('declared') or {}, G.DECLARED_LABELS)} | "
                       f"{gain} | **{g['verdict_label']}** | {_cell(sugg, 120) or '-'} |")
        if len(groups) > top:
            out.append(f"| ... | {len(groups) - top} autre(s) groupe(s) dans l'export JSON | | | | | | | | | |")
        out.append("")
        out.append("Delai demande : parametre de delai passe a l'outil (`-` s'il n'y en a pas). Intervalle observe : ecart reel entre "
                   "le debut de deux appels successifs (duree de l'appel + temps de reponse du modele) ; les deux ne se confondent pas. "
                   "Raisons observees : ce qui precede chaque reprise (resultat precedent, actions et messages entre les deux). "
                   "Raisons annoncees : categories reconnues dans les commentaires de l'agent, sans texte conserve. "
                   "Resultat suivant : etat du resultat (horodatages neutralises) change ou identique au precedent. "
                   "Un resultat identique, ou une reprise apres compaction, ne prouve pas un gaspillage : verifier qu'un etat n'a pas "
                   "bouge, ou relire apres le remplacement du contexte, sont des travaux normaux.")
        out.append("")
        for g in [g for g in groups if g.get("cadence") and (g["cadence"].get("recommended") or {})][:3]:
            cad = g["cadence"]
            out.append(f"Cadence simulee pour {g['tool']} ({g['calls']} appels, attente typique {G.fmt_duration(cad['typical_wait_s'])}, "
                       f"{cad['basis']}) :")
            out.append("")
            out += ["| Delai minimal | Appels restants | Appels en moins | Changements de phase observes | Retard max | "
                    "Retard moyen |", "|---|---|---|---|---|---|"]
            for s in cad["simulation"]:
                mark = " (suggere)" if s["cooldown_s"] == cad["recommended"]["cooldown_s"] else ""
                # * Sans changement de phase observe, le retard n'est pas estimable : on l'ecrit, on n'affiche pas 0 ms.
                delay_max = G.fmt_duration(s["max_delay_s"]) if s["max_delay_s"] is not None else "non estimable"
                delay_mean = G.fmt_duration(s["mean_delay_s"]) if s["mean_delay_s"] is not None else "non estimable"
                out.append(f"| {G.fmt_duration(s['cooldown_s'])}{mark} | {s['calls']} | {s['avoided']} | {s['changes']} | "
                           f"{delay_max} | {delay_mean} |")
            out.append("")
            out.append("Simulation : ce qu'aurait donne un delai minimal entre deux appels, sur les appels deja enregistres. "
                       + ("Aucun changement de phase n'a ete observe dans ce groupe : le retard de detection n'est pas "
                          "estimable, et rien n'etablit que les appels en moins auraient ete retirables sans consequence "
                          "sur le travail attendu."
                          if not cad.get("phase_changes_seen") else
                          "Le retard indique est celui qu'aurait pris la detection des changements de phase observes."))
            out.append("")
    if rhythm:
        out += ["Rythme des outils (intervalle entre deux appels successifs d'un meme agent, hors appels d'une meme reponse ; "
                "pics tous agents confondus) :", "",
                "| Outil | Appels | Agents | Intervalle median | 10 % des intervalles sous | Max / 1 min | Max / 10 min | "
                "Identiques a un precedent | dont au resultat identique |", "|---|---|---|---|---|---|---|---|---|"]
        for r in rhythm:
            out.append(f"| {_cell(r['tool'], 50)} | {r['calls']} | {r['agents']} | {G.fmt_duration(r['interval_median_s'])} | "
                       f"{G.fmt_duration(r['interval_p10_s'])} | {r['max_per_min']} | {r['max_per_10min']} | "
                       f"{r['identical_repeats']} | {r['repeats_no_gain']} |")
        out.append("")
    return out


def _agent_row(report: dict[str, Any], a: dict[str, Any]) -> str:
    c = agent_cells(report, a)
    return (f"| {c['label']} | `{c['id']}` | {c['type']} | {c['status']} | {c['calls']} | {c['start']} | {c['end']} | "
            f"{c['parent']} | {c['basis']} | {c['model']} | {c['tokens']} |")


def _render_errors(st: dict[str, Any]) -> list[str]:
    """Erreurs en trois niveaux sources : statut de l'outil, code de sortie de la commande, resultat ecrit par le script.
    Remplace la liste de « signatures » (premiere ligne de la fin de sortie, souvent un fragment de code)."""
    err = st.get("errors") or {}
    if not err.get("failed_calls"):
        return []
    natures = ", ".join(f"{k['label']} {k['count']}" for k in err.get("by_kind") or [])
    out = ["## Erreurs : statut de l'outil, code de sortie, resultat du script", "",
           f"{err['failed_calls']} appel(s) en erreur, en {err['groups_total']} groupe(s) ; {err['with_exit_code']} ont un code de sortie ; "
           f"{err['with_script_result']} portent un resultat ecrit par le script lui-meme ; {err['unclassified']} ont une sortie "
           f"qu'aucune forme connue ne classe (laissee non classee, rien n'est devine).", ""]
    if natures:
        # * Decompte sur TOUS les groupes : le tableau est borne, ce total ne l'est pas.
        out += [f"Natures, sur la totalite des {err['failed_calls']} appels en erreur (le tableau ci-dessous est borne aux "
                f"{len(err.get('groups') or [])} premiers groupes) : {natures}.", ""]
    if err.get("recovered_from_source"):
        out += [f"{err['recovered_from_source']} cause(s) ont ete retrouvees en relisant le debut de la sortie dans le rollout "
                "source : le resume conserve la fin de la sortie, or l'echec peut s'y afficher avant des lignes normales. "
                "Ces lignes portent la mention « relu dans la source ».", ""]
    out += [
           "| Fois | Outil (commande) | Statut de l'outil | Code de sortie de la commande | Ce que le script ecrit de son "
           "propre resultat | Nature de l'erreur | Exemples (#appel) | Source |", "|---|---|---|---|---|---|---|---|"]
    groups = err.get("groups") or []
    for g in groups:
        heads = f" ({', '.join(g['heads'])})" if g.get("heads") else ""
        tool_status = str(g["tool_status"]) if g.get("tool_status") else "non fourni"
        code = str(g["exit_code"]) if g.get("exit_code") is not None else "aucun"
        # * La colonne cite l'element qui ETABLIT l'echec interne. Un code ecrit nul n'en est pas un : il est montre
        #   comme tel, sans etre presente comme un echec.
        if g.get("script_exit_code"):
            script = f"ExitCode {g['script_exit_code']} dans la sortie"
        elif g.get("script_failed"):
            script = f"champ Failed : {g['script_failed']} dans la sortie"
        elif g.get("script_exit_code") == 0:
            script = "ExitCode 0 : le script s'est termine normalement, aucun echec interne rapporte"
        else:
            script = "-"
        nature = g["kind_label"] + (f" : `{_cell(g['detail'], 110)}`" if g.get("detail") and g["kind"] != "script_result" else "")
        if g.get("detail_source"):
            nature += f" (relu dans la source : {_cell(g['detail_source'], 90)})"
        srcs = [x for x in (g.get("sources") or []) if x.get("file")]
        src = f"#{srcs[0]['seq']} : {srcs[0]['file']}:{srcs[0]['line']}" if srcs else "-"
        out.append(f"| {g['count']} | {_cell(g['tool'], 40)}{_cell(heads, 50)} | {tool_status} | {code} | {script} | "
                   f"{nature} | {', '.join('#' + str(x) for x in g['seqs'])} | {_cell(src, 120)} |")
    if err["groups_total"] > len(groups):
        out.append(f"| ... | {err['groups_total'] - len(groups)} autre(s) groupe(s) dans l'export JSON | | | | | | |")
    out.append("")
    status_bases = sorted({str(g["tool_status_basis"]) for g in groups if g.get("tool_status_basis")})
    code_bases = sorted({str(g["exit_code_basis"]) for g in groups if g.get("exit_code_basis")})
    out.append("Statut de l'outil : ce que le client ecrit de l'appel" + (f" (base : {' ; '.join(status_bases)})" if status_bases else "")
               + ". Code de sortie : celui de la commande" + (f" (base : {' ; '.join(code_bases)})" if code_bases else "")
               + ". Ce que le script ecrit de son propre resultat : le code ou le compte d'echecs qu'il imprime lui-meme (par "
               "exemple `\"ExitCode\": 6` d'une compilation, ou `\"Failed\": 2` d'une serie de tests). Il differe du code de la "
               "commande, qui vaut 1 des qu'une etape echoue. Un `\"ExitCode\": 0` ecrit par le script n'etablit aucun echec "
               "interne : il dit que le script s'est termine normalement, et la cause de l'echec est alors ailleurs. Source : "
               "fichier et ligne du rollout du premier exemple ; les autres sont dans l'export JSON.")
    if err.get("unclassified"):
        out.append("Sortie non classee : seule la fin de la sortie est conservee (400 caracteres, deja masques). Dans une chaine de "
                   "commandes, l'etape en echec peut preceder cette fin : la nature de l'erreur n'est alors pas lisible et reste non "
                   "classee plutot que devinee.")
    und = err.get("undetermined") or {}
    if und.get("count"):
        out.append(f"{und['count']} appel(s) restent **indetermines** : {und['note']}. Ils ne sont comptes ni dans les erreurs "
                   "ci-dessus, ni dans les succes. Exemples : "
                   + ", ".join("#" + str(x) for x in und["seqs"]) + (" ..." if und["count"] > len(und["seqs"]) else "") + ".")
    rein = err.get("reinterpreted_as_success") or {}
    if rein.get("count"):
        out.append(f"{rein['count']} autre(s) appel(s) ont un code de sortie non nul sans etre des erreurs, et sont comptes en succes : "
                   "recherche sans correspondance, `git diff` avec differences (voir evidence.exit_status_meaning). Exemples : "
                   + ", ".join("#" + str(x) for x in rein["seqs"]) + (" ..." if rein["count"] > len(rein["seqs"]) else "") + ".")
    out.append("")
    return out


def _pct(v: Any) -> str:
    return "inconnu" if v is None else f"{100 * float(v):.1f} %"


_REREAD_STATUS = {"identical": "contenu identique (empreinte)", "no_change_observed": "aucune modification observee",
                  "different_or_other_excerpt": "contenu different ou autre extrait", "modified_between": "modifie entre-temps"}


def _render_context(ctx: dict[str, Any] | None, report: dict[str, Any]) -> list[str]:
    """Section « Contexte » : ce que les sorties ajoutent, combien de requetes les relisent, ce qui revient apres chaque
    compaction, quota et tours coupes. Descriptive : des mesures et un partage calcule, jamais un verdict."""
    if not ctx:
        return []
    out = ["## Contexte : sorties relues, reprises apres compaction, quota", ""]
    lim = ctx.get("limits") or {}
    cut = lim.get("turns_cut") or {}
    if cut.get("total"):
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(cut["by_kind"].items()))
        out.append(f"- **Tours coupes par le client : {cut['total']}** ({kinds} ; agents : "
                   f"{', '.join(_agent(report, a) for a in cut['agents'])} ; dernier a {cut.get('last_time')}). Un tour coupe "
                   f"porte la meme ligne de fin qu'un tour termine : il n'est pas compte comme un travail termine.")
    q = lim.get("quota")
    if q:
        window = (f"fenetre de {int(q['window_minutes']) // 1440} j" if isinstance(q.get("window_minutes"), (int, float))
                  else "fenetre inconnue")
        out.append(f"- Quota du compte ecrit par le client ({window}) : {_fmt(q['first_percent'])} % a {q['first_time']} -> "
                   f"{_fmt(q['last_percent'])} % a {q['last_time']} (maximum {_fmt(q['max_percent'])} % ; {q['readings']} releves).")
    out.append(f"- Entree de la session : {ctx['session_input_tokens']} tokens sur {ctx['responses']} requetes. Chaque requete relit "
               f"tout son contexte : une sortie d'outil est relue par chaque requete suivante de sa fenetre, jusqu'a la compaction.")
    floor = ctx.get("floor") or {}
    if floor.get("thread_first_input"):
        firsts = ", ".join(f"{_agent(report, a)} {n}" for a, n in sorted(floor["thread_first_input"].items(), key=lambda kv: kv[0] != "main"))
        out.append(f"- Socle relu par chaque requete (entree de la premiere requete de chaque fil : instructions, outils, skills, "
                   f"consigne) : {firsts} ; multiplie par les requetes de chaque fil, soit "
                   f"{_pct(floor.get('thread_first_share_of_session_input'))} de l'entree de ces fils"
                   + (f" ({floor['threads_started_before_view']} fil(s) commence(s) avant la tranche : socle non releve)"
                      if floor.get("threads_started_before_view") else "") + ". Premiere requete d'une fenetre "
                   f"apres compaction (socle + resume) : {_fmt(floor.get('window_first_input_median'))} tokens (mediane).")
    out.append(f"- Tokens ajoutes au contexte par les sorties d'outils (mesure : {ctx['basis']}) : {ctx['added_tokens']} sur "
               f"{ctx['calls_measured']} appels ; relus ensuite {ctx['reread_tokens']} fois-tokens, soit "
               f"{_pct(ctx.get('reread_share_of_session_input'))} de l'entree de la session (en cache pour l'essentiel, mais compte "
               f"dans l'entree et le quota).")
    mixed = ctx.get("mixed") or {}
    out.append(f"- Limites : {ctx['calls_unmeasured']} appel(s) sans reponse precedente comparable (debut de fenetre), "
               f"{ctx['calls_without_usage']} sans releve ; {mixed.get('calls', 0)} appel(s) ({mixed.get('added_tokens', 0)} tokens) ou un "
               f"message est aussi entre entre les deux reponses : leur part est surestimee d'autant. Le partage entre les sorties "
               f"d'une meme reponse est un calcul (prorata des tailles), pas une mesure.")
    tr = ctx.get("truncation") or {}
    if tr.get("known"):
        out.append(f"- Sorties coupees par le client avant d'atteindre le modele : {tr['truncated_calls']} commande(s) "
                   f"({tr['original_tokens_of_truncated_calls']} tokens d'origine, {tr['added_tokens_of_truncated_calls']} tokens "
                   f"entres quand meme) ; {tr['execs_cut_in_the_middle']} exec coupe(s) au milieu ({tr['tokens_cut_from_execs']} tokens "
                   f"retires : la fin d'une sortie et le debut de la suivante sont perdus).")
    else:
        out.append("- Sorties coupees par le client : non relevees pour cette session (import anterieur a ce releve ; "
                   "`import-rollouts` les releve pour les lignes lues depuis).")
    out.append("")
    fams = ctx.get("families") or []
    if fams:
        out += ["| Famille d'outil | Appels | Tokens ajoutes | Relus ensuite (fois-tokens) | Part de l'entree de la session | Sorties coupees |",
                "|---|---|---|---|---|---|"]
        for f in fams:
            out.append(f"| {_cell(f['family'])} | {f['calls']} | {f['added_tokens']} | {f['reread_tokens']} | "
                       f"{_pct(f.get('share_of_session_input'))} | {f['truncated_calls'] if tr.get('known') else '-'} |")
        out.append("")
    tops = ctx.get("top_outputs") or []
    if tops:
        out.append("Sorties qui ont pese le plus (tokens ajoutes x requetes suivantes de la fenetre) :")
        for o in tops:
            src = o.get("source") or {}
            where = f" ; source {src.get('file')} L{src.get('line')}" if src.get("line") else ""
            cutnote = f" ; coupee (origine {o['original_token_count']} tokens)" if o.get("truncated") else ""
            out.append(f"- #{o['seq']} {_agent(report, o['agent'])}, fenetre {o['window']} : {o['tool']} {_short(o.get('label'), 90)} : "
                       f"{o['added_tokens']} tokens x {o['later_requests_in_window']} requetes = {o['reread_tokens']}{cutnote}{where}")
        out.append("")
    ac = ctx.get("after_compaction") or {}
    ws = ac.get("window_start") or {}
    if ws.get("windows"):
        out.append(f"Reprises apres compaction ({ws['windows']} fenetre(s)) : dans les {ac['recovery_responses']} premieres reponses d'une "
                   f"fenetre, les sorties ajoutent {_fmt(ws['added_tokens_median'])} tokens (mediane ; 9e decile {_fmt(ws['added_tokens_p90'])}), "
                   f"soit {ws['added_tokens_total']} au total et {_pct(ws.get('share_of_added_tokens'))} des tokens ajoutes par les sorties.")
        by_status = sorted((ac.get("by_status") or {}).items(), key=lambda kv: -kv[1]["rereads"])
        out.append(f"Relectures d'une ressource deja lue par le meme agent dans une fenetre anterieure : {ac['rereads']} "
                   f"({ac['added_tokens']} tokens ajoutes, {ac['reread_tokens']} relus ensuite), dont {(ac.get('early') or {}).get('rereads', 0)} "
                   f"en debut de fenetre. Par etat : "
                   + " ; ".join(f"{_REREAD_STATUS.get(k, k)} {v['rereads']} ({v['added_tokens']} tokens)" for k, v in by_status) + ".")
        out.append("")
        res = ac.get("resources") or []
        if res:
            out += ["| Ressource relue apres compaction | Relectures | Agents | Fenetres | Tokens ajoutes | Relus ensuite | Etat des relectures | Exemples |",
                    "|---|---|---|---|---|---|---|---|"]
            for r in res:
                status = ", ".join(f"{_REREAD_STATUS.get(k, k)} {n}" for k, n in sorted(r["status"].items(), key=lambda kv: -kv[1]))
                refs = ", ".join(f"#{x['seq']}" for x in r["refs"][:3])
                out.append(f"| {_cell(r['resource'], 90)} | {r['rereads']} | {len(r['agents'])} | {r['windows']} | {r['added_tokens']} | "
                           f"{r['reread_tokens']} | {status} | {refs} |")
            out.append("")
        out.append("Relire apres une compaction est attendu : le contexte a ete remplace. Ce tableau dit ce que cela coute et ce qui "
                   "revient a chaque fenetre ; « aucune modification observee » ne prouve pas un contenu identique (une modification "
                   "hors session n'est pas observable, et une commande qui lit plusieurs fichiers n'a qu'une empreinte).")
        out.append("")
    return out


def _where(src: Any) -> str:
    return f"{src.get('file')} L{src.get('line')}" if isinstance(src, dict) and src.get("line") else "source inconnue"


def _render_exchanges(ex: dict[str, Any] | None, report: dict[str, Any]) -> list[str]:
    """Section « Entre agents » : messages rapproches, requetes qui n'emettent que des messages, debut de fenetre face
    aux messages recus, ressources partagees. Libelles observables ; des faits et des mesures, jamais un verdict."""
    if not ex or not (ex.get("messages") or (ex.get("resources") or {}).get("shared_write_total")
                      or ((ex.get("resources") or {}).get("handoff") or {}).get("reads")):
        return []
    out = ["## Entre agents : messages, requetes qui n'emettent que des messages, ressources partagees", ""]
    m = ex.get("messages")
    if m:
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(m["received_by_kind"].items(), key=lambda kv: -kv[1]))
        out.append(f"- Messages : {m['sent']} envoyes, {m['received']} entres dans un fil ({kinds}) ; contenu chiffre par le "
                   f"fournisseur pour {m['encrypted_sent']} envoi(s) : leur texte n'est pas comparable d'un message a l'autre "
                   f"(une consigne redonnee n'y est pas detectable).")
        pg = m["pairing"]
        if pg["available"]:
            d = m["delivery_delay"]
            out.append(f"- Rapprochement envoi -> reception ({pg['basis']}) : {pg['paired']} rapproches ; "
                       f"{pg['undelivered_total']} envoi(s) jamais entre(s) chez le destinataire ; {pg['received_without_send']} "
                       f"reception(s) sans envoi (reponse finale d'un tour). Delai avant d'entrer chez le destinataire : mediane "
                       f"{_fmt(d['median_s'])} s, 9e decile {_fmt(d['p90_s'])} s, maximum {_fmt(d['max_s'])} s ; {d['slow']} au-dela de "
                       f"{d['slow_threshold_s']} s (un message n'entre qu'a la requete suivante du destinataire) ; {m['crossed']} "
                       f"envoye(s) alors qu'un message du destinataire etait en route.")
            for u in pg["undelivered"]:
                out.append(f"  - jamais entre : {_agent(report, u['from'])} -> {_agent(report, u['to'])} a {u['time']} "
                           f"({u.get('tool')} ; {_where(u.get('source'))})")
        else:
            out.append("- Rapprochement envoi -> reception : indisponible (messages importes avant l'ecriture de l'empreinte du "
                       "contenu transmis ; reimporter la session). L'ordre seul ne rapproche pas sans erreur : rien n'est devine.")
        out += ["", "| De | Vers | Envoyes | Entres | Rapproches | Caracteres transmis | Delai median (s) | 9e decile (s) |",
                "|---|---|---|---|---|---|---|---|"]
        for r in m["routes"]:
            out.append(f"| {_agent(report, r['from'])} | {_agent(report, r['to'])} | {r['sent']} | {r['received']} | {r['paired']} | "
                       f"{r['payload_chars']} | {_fmt(r['delay_median_s'])} | {_fmt(r['delay_p90_s'])} |")
        out.append("")
    rq = ex.get("message_requests")
    if rq:
        out.append(f"- **Requetes qui n'emettent que des messages : {rq['message_only']} sur {rq['responses']}**, soit "
                   f"{rq['message_only_input_tokens']} tokens d'entree ({_pct(rq.get('share_of_session_input'))} de l'entree de la "
                   f"session) : chaque message envoye est une requete entiere qui relit tout le contexte de son agent. Par agent : "
                   + " ; ".join(f"{_agent(report, a)} {r['message_only']} requetes, {_pct(r.get('share_of_agent_input'))} de son entree"
                                for a, r in sorted(rq["by_agent"].items(), key=lambda kv: kv[0] != "main")) + ".")
        labels = {"tool_call": "emet un appel d'outil hors messagerie",
                  "message_to_sender": "n'emet que des messages, dont un a l'emetteur",
                  "message_to_other_agent": "n'emet que des messages, aucun a l'emetteur", "text_only": "texte seul",
                  "no_later_response": "aucune reponse ensuite"}
        if rq.get("reactions"):
            out.append("- Premiere reponse du destinataire apres l'entree d'un message, par ce qu'elle emet (enchainement dans le "
                       "temps : ni « repond » ni « relaie le meme contenu » ne sont etablis) : "
                       + " ; ".join(f"{_agent(report, a)} : " + _counts(r, labels, limit=5)
                                    for a, r in sorted(rq["reactions"].items(), key=lambda kv: kv[0] != "main")) + ".")
        ch = rq.get("chains")
        if ch and ch["chains"]:
            shapes = " ; ".join(f"{' > '.join(_agent(report, a) for a in s['agents'])} x{s['count']}" for s in ch["shapes"][:4])
            out.append(f"- Messages enchaines sans appel d'outil hors messagerie entre deux (un message entre, la premiere reponse "
                       f"du destinataire n'emet que des messages ; cela ne dit pas « sans travail ») : {ch['chains']} chaine(s), "
                       f"{ch['messages']} messages, {ch['intermediate_input_tokens']} tokens d'entree pour les reponses "
                       f"intermediaires. Formes les plus frequentes : {shapes}.")
            for c in ch["longest"][:1]:
                # * L<n> : ligne de l'envoi dans le rollout de l'emetteur (nom du fichier dans l'export JSON).
                steps = " ; ".join(f"{s['sent_time'][11:19]} {_agent(report, s['from'])} -> {_agent(report, s['to'])} "
                                   f"(L{(s.get('sent_source') or {}).get('line')})" for s in c["steps"])
                out.append(f"  - la plus longue, {c['messages']} messages de {c['start_time']} a {c['end_time']} : {steps}")
    kept = [r for r in ex.get("resident_messages") or [] if r.get("correlation") is not None]
    if kept:
        out.append("- Premiere requete de chaque fenetre apres compaction, face au cumul des messages recus (une pente nette et "
                   "une correlation proche de 1 sont compatibles avec des messages recus conserves a travers les compactions) : "
                   + " ; ".join(f"{_agent(report, r['agent'])} {r['first']['input_tokens']} -> {r['last']['input_tokens']} tokens "
                                f"apres {r['last']['messages']} messages ({r['tokens_per_payload_char']:.3f} token par caractere "
                                f"transmis, r = {r['correlation']:.3f})" for r in kept) + ".")
    est = ex.get("retained_context_estimate") or {}
    if est.get("agents"):
        out.append(f"- ESTIMATION du contexte conserve ({est['basis']} ; correlation >= {est['min_correlation']}) : "
                   f"{est['tokens']} tokens relus sur {est['requests']} requetes ("
                   + ", ".join(_agent(report, a) for a in est["agents"]) + "). Un calcul, pas un releve ; il recouvre en partie "
                   "l'entree des requetes d'envoi ci-dessus : les deux nombres ne s'additionnent pas et aucun n'est un gain.")
    res = ex.get("resources") or {}
    ref, hand = res.get("shared_reference") or {}, res.get("handoff") or {}
    if ref.get("resources") or hand.get("reads") or res.get("shared_write_total"):
        out.append(f"- Ressources partagees (tokens ajoutes mesures : {res.get('added_tokens_measured')}) : {ref.get('resources', 0)} "
                   f"lue(s) par plusieurs agents sans etre modifiee(s) ({ref.get('reads_by_others', 0)} lectures par un autre que le "
                   f"premier lecteur, {ref.get('added_tokens_by_others', 0)} tokens, {_pct(ref.get('share_of_added'))}) ; "
                   f"{hand.get('reads', 0)} lecture(s) apres la modification d'un AUTRE agent ({hand.get('added_tokens', 0)} tokens, "
                   f"{_pct(hand.get('share_of_added'))}) ; {res.get('shared_write_total', 0)} fichier(s) modifie(s) par plusieurs agents.")
        out.append("")
        if hand.get("routes"):
            out += ["| Auteur de la modification | Lecteur | Ressources | Lectures | Tokens ajoutes | Lectures apres un message de l'auteur |",
                    "|---|---|---|---|---|---|"]
            for r in hand["routes"][:12]:
                out.append(f"| {_agent(report, r['author'])} | {_agent(report, r['reader'])} | {r['resources']} | {r['reads']} | "
                           f"{r['added_tokens']} | {r['reads_after_author_message']} |")
            out.append("")
        for w in res.get("shared_write") or []:
            runs: list[list[Any]] = []                 # * ecritures consecutives d'un meme agent : une seule mention
            for s in w["sequence"]:
                if runs and runs[-1][0] == s["agent"]:
                    runs[-1][2], runs[-1][3] = s["seq"], runs[-1][3] + 1
                else:
                    runs.append([s["agent"], s["seq"], s["seq"], 1])
            seq = ", puis ".join(f"{_agent(report, a)} x{n} (#{lo}" + (f" a #{hi})" if n > 1 else ")") for a, lo, hi, n in runs)
            out.append(f"- Modifie par plusieurs agents : {_short(w['label'], 90)} : {seq}")
        for r in (ref.get("top") or [])[:5]:
            who = ", ".join(f"{_agent(report, a)} {n}" for a, n in sorted(r["reads"].items(), key=lambda kv: -kv[1]))
            out.append(f"- Lue par plusieurs agents : {_short(r['label'], 90)} ({who} ; {r['added_tokens']} tokens, dont "
                       f"{r['added_tokens_by_others']} par d'autres que le premier lecteur ; contenus distincts : {_fmt(r['distinct_contents'])})")
    out.append("- Limites : " + " ; ".join(ex.get("limits") or []) + ".")
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
    prov = s.get("provenance") or {}
    rollout_only = prov.get("collection") == "rollout"
    if rollout_only:
        # * Session lue dans ses rollouts : la version est celle que Codex ecrit dans son session_meta, aucun hook en jeu.
        version_line = (f"- Version de `{s['client']}` : {s.get('client_version_observed') or 'inconnue'} "
                        f"(ecrite par le client dans le session_meta du rollout) ; AgentWatch {report['agentwatch_version']} ; "
                        f"schema {report['schema_version']}")
    else:
        version_line = (f"- Version de l'executable `{s['client']}` du PATH lors de `configure` : "
                        f"{s.get('client_version_at_configure') or 'inconnue'} (la version reellement executee n'est pas transmise "
                        f"aux hooks) ; AgentWatch {report['agentwatch_version']} ; schema {report['schema_version']}")
    lines = [f"# AgentWatch - rapport de session", "",
             f"- Client : `{s['client']}` ; session `{s['session_id']}` ; modele : `{s['model'] or 'non observe'}` ({s['model_source']})",
             f"- Projet : `{s['project_dir'] or 'inconnu'}` ; periode : {s['first_time']} -> {s['last_time']}",
             f"- Tours : {s['turns']} ; epoques de contexte : {s['context_epochs']} ; agents : "
             f"{', '.join(_agent(report, a) for a in s['agents']) or 'aucun'}",
             version_line]
    if prov.get("text"):
        lines.append(f"- Source des evenements : {prov['text']}")
    lines.append("")
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
    cross = (report.get("stats") or {}).get("cross_agent") or {}
    if cross.get("agents", 0) > 1:
        lines.append(f"Portee des signalements : {cross['note']}")
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
              f"- Correlation : {st['correlation']}"]
    if rollout_only and not (st.get("hook_overhead_ms") or {}).get("n"):
        lines.append("- Surcharge des hooks : sans objet, aucun hook dans cette session (lecture passive des rollouts)")
    else:
        lines.append(f"- Surcharge des hooks (dans le processus, hors demarrage de l'interpreteur) : {st['hook_overhead_ms']}")
    lines.append(f"- Tokens, releves mesures : {st['usage']['status']} ({st['usage']['note']})")
    kinds = st["usage"].get("kinds") or {}
    if kinds:
        # * Trois natures de nombres, jamais melangees : mesure par reponse, part calculee par appel, estimation (aucune).
        lines += [f"- Tokens, mesure : {kinds['measured']}",
                  f"- Tokens, repartition calculee : {kinds['allocated']}",
                  f"- Tokens, estimation : {kinds['estimated']}"]
    lines.append("")
    threads = st["usage"].get("threads") or []
    if threads:
        lines += ["| Fil | Agent | Requetes du modele (demandes de compaction comprises) | Tokens releves | Entree (dont en cache) | Sortie (dont raisonnement) | Fenetres de contexte (lignes compacted + 1) |",
                  "|---|---|---|---|---|---|---|"]
        for t in threads:
            who = _agent(report, t.get("agent_id")) + (f" ({t['agent_type']})" if t.get("agent_id") and t.get("agent_type") else "")
            lines.append(f"| `{str(t['thread_id'])}` | {who} | {t['requests']} | {t['total_tokens']} | "
                         f"{t['input_tokens']} ({t['cached_input_tokens']}) | {t['output_tokens']} ({t['reasoning_output_tokens']}) | "
                         f"{_fmt(t.get('windows'))} |")
        lines.append("")
    recon_header = ("Duree reconstruite entre horodatages du rollout, mediane (n)" if rollout_only
                    else "Duree reconstruite mediane (n)")
    client_header = "Duree ecrite par le client, mediane (n)" if rollout_only else "Duree client mediane (n)"
    lines += [
              f"| Outil | Appels | Erreurs | Statut inconnu | Ouverts | Sortie brute de l'outil (octets, mesures) | {client_header} | {recon_header} |",
              "|---|---|---|---|---|---|---|---|"]
    shown_tools = st["tools"][:15]
    for t in shown_tools:
        lines.append(f"| {t['tool']} | {t['calls']} | {t['errors']} | {t['unknown_status']} | {t['open']} | "
                     f"{_fmt(t['output_bytes'])} ({t['output_known_for']}) | {_fmt(t['client_duration_median_ms'])} ({t['client_duration_n']}) | "
                     f"{_fmt(t['reconstructed_duration_median_ms'])} ({t['reconstructed_duration_n']}) |")
    # * Le tableau se limite aux 15 premiers outils : le reste est additionne sur une ligne pour que le total retombe
    #   sur le nombre d'appels annonce (constate le 2026-09-20 : 2 857 appels affiches pour 2 870).
    other = other_tools_row(st["tools"], 15)
    if other:
        lines.append(f"| *{other['tools']} autre(s) outil(s)* : {other['names']} | {other['calls']} | {other['errors']} | "
                     f"{other['unknown_status']} | {other['open']} | - | - | - |")
    tt = st.get("tools_total") or {}
    if tt:
        lines.append(f"| **Total : {tt['tools']} outil(s)** | **{tt['calls']}** | **{tt['errors']}** | **{tt['unknown_status']}** | "
                     f"**{tt['open']}** | | | |")
    lines.append("")
    wu = [w for w in st.get("work_units", []) if w["repeated"]]
    if wu:
        lines += ["Unites de travail refaites (meme operation, meme cible, tous outils confondus) :", "",
                  "| Operation | Cible | Fois | Outils | Agents | Contenus distincts obtenus |", "|---|---|---|---|---|---|"]
        for w in wu[:15]:
            target = ("(sans cible)" if w["target"] is None else str(w["target"])).replace("|", "\\|").replace("\n", " ")
            shown = target if len(target) <= 110 else target[:110] + "..."
            lines.append(f"| {w['op']} | {shown} | {w['calls']} | {', '.join(w['tools'])} | {w['agents']} | "
                         f"{_fmt(w['distinct_contents'])} |")
        lines.append("")
        lines.append("Un travail refait n'est pas forcement inutile : voir les signalements et leurs contre-indications."
                     + (" La colonne Agents additionne des agents differents a titre descriptif : aucun signalement ne compare "
                        "deux agents entre eux." if cross.get("agents", 0) > 1 else ""))
        lines.append("")
    lines += _render_repetitions(st.get("repetitions") or {}, int(report.get("repetitions_top", 15)), report)
    lines += _render_errors(st)
    lines += _render_context(st.get("context"), report)
    lines += _render_exchanges(st.get("exchanges"), report)
    if st["largest_outputs"]:
        # * Taille BRUTE produite par l'outil : le modele peut en recevoir beaucoup moins (sortie coupee par le client,
        #   image encodee) ; ce qui entre dans le contexte est dans la section « Contexte ».
        lines.append("Sorties brutes les plus volumineuses, telles que produites par l'outil (le modele peut en recevoir moins : "
                     "sortie coupee par le client, image encodee ; voir « Contexte ») :")
        lines += [f"- #{c['seq']} {c['tool']} {_short(c.get('label') or c['target'])} : {c['output_size_bytes']} octets"
                  for c in st["largest_outputs"]]
        lines.append("")
    if st["longest_calls"]:
        lines.append("Appels les plus longs (source de la duree indiquee) :")
        lines += [f"- #{c['seq']} {c['tool']} {_short(c.get('label') or c['target'])} : {c['duration_ms']} ms ({c['duration_source']})"
                  for c in st["longest_calls"]]
        lines.append("")
    agents = report.get("agents") or []
    if agents:
        internal = [a for a in agents if a["classification"] == "stop_only"]
        shown = [a for a in agents if a["classification"] != "stop_only"]
        lines += ["## Agents", "",
                  "| Agent | Identifiant | Type | Statut | Appels | Debut | Fin | Lance par | Base du lien | Modele | Tokens (et leur source) |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for a in shown:
            lines.append(_agent_row(report, a))
        lines.append("")
        lines.append("« inconnu » et « non etabli » sont gardes tels quels : un parent, un modele ou une fin ne sont affiches que "
                     "s'ils sont ecrits dans les donnees, jamais deduits.")
        lines.append("")
        if internal:
            lines.append(f"- {len(internal)} agent(s) interne(s) non detailles : seulement un `SubagentStop`, sans type ni appel "
                         f"(identifiants dans l'export JSON) ; agent interne du client ou demarre avant l'installation des hooks.")
        if shown:
            lines.append("- sous-agent observe : demarrage et/ou appels d'outils ; les detecteurs ne comparent jamais deux agents entre eux.")
        lines.append("")
    cov_rollout = any(r.get("source") == "rollout" for r in report["coverage"])
    if cov_rollout:
        lines += ["## Couverture", "",
                  "Session lue dans les rollouts Codex : la base citee est la ligne ou l'element du rollout, aucun hook n'intervient. "
                  "Le format des rollouts n'est pas documente par l'editeur : la colonne dit ce que l'import sait lire.", "",
                  "| Capacite | Lue par l'import des rollouts | Observe dans cette session | Base (rollout) |", "|---|---|---|---|"]
    else:
        lines += ["## Couverture", "", "| Capacite | Documente | Observe dans cette session | Base |", "|---|---|---|---|"]
    for r in report["coverage"]:
        lines.append(f"| {r['capability']} | {r['documented']} | {r['observed_in_session']} | {r['basis']} |")
    lines.append("")
    lines.append("Les capacites non listees ne sont pas observees : aucun pourcentage de couverture globale n'est calcule.")
    lines.append("")
    return "\n".join(lines)
