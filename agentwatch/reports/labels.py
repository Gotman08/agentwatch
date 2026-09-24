"""Etiquettes d'affichage : agents designes sans ambiguite, provenance dite telle qu'elle est.

# * Constate le 2026-09-20 sur une session Codex : deux sous-agents, `01a0bf98-91ee...` et `01a0bf98-d0cd...`,
#   s'affichaient tous deux `01a0bf98` (8 premiers caracteres). Les identifiants de fil Codex sont horodates : ceux
#   de sous-agents lances a quelques secondes d'ecart partagent leur debut.
"""

from __future__ import annotations

from typing import Any, Iterable

MIN_PREFIX = 8


def unique_prefixes(ids: Iterable[str], minimum: int = MIN_PREFIX) -> dict[str, str]:
    """Plus court prefixe (au moins `minimum` caracteres) qui distingue chaque identifiant des autres."""
    ids = sorted({str(i) for i in ids if i})
    out: dict[str, str] = {}
    for i in ids:
        n = minimum
        while n < len(i) and any(o != i and o.startswith(i[:n]) for o in ids):
            n += 1
        if n > minimum:
            # * Coupe a la fin du segment entame (`01a0bf98-91ee`, pas `01a0bf98-9`) : plus lisible, toujours unique.
            nxt = i.find("-", n)
            n = nxt if nxt != -1 else len(i)
        out[i] = i[:n].rstrip("-")
    return out


def agent_labels(agent_ids: Iterable[str], agents: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Identifiant d'agent -> etiquette : « principal », ou « Surnom (`prefixe unique`) », ou le prefixe seul."""
    ids = [a for a in agent_ids if a and a != "main"]
    ids += [str(a["agent_id"]) for a in (agents or []) if a.get("agent_id") and a["agent_id"] not in ids]
    prefixes = unique_prefixes(ids)
    names = {str(a["agent_id"]): a.get("nickname") for a in (agents or []) if a.get("agent_id")}
    out = {"main": "principal"}
    for i, p in prefixes.items():
        out[i] = f"{names[i]} (`{p}`)" if names.get(i) else f"`{p}`"
    return out


def agent_label(report: dict[str, Any], agent_id: Any) -> str:
    """Etiquette non ambigue d'un agent, lue dans le rapport (`agent_labels`)."""
    key = "main" if agent_id in (None, "main") else str(agent_id)
    return (report.get("agent_labels") or {}).get(key) or ("principal" if key == "main" else f"`{key}`")


def _n(v: Any) -> str:
    return "inconnu" if v is None else str(v)


def agent_cells(report: dict[str, Any], a: dict[str, Any]) -> dict[str, str]:
    """Cellules d'une ligne du tableau des agents, reconciliees avec ce qui est deja connu (surnom, fil parent,
    modele, tokens du fil). Ce qui n'est pas ecrit dans les donnees reste « inconnu » : rien n'est deduit.
    Partage par les vues Markdown et Rich."""
    u = a.get("usage") or {}
    if u and u.get("source") == "codex:rollout":
        tokens = (f"total {_n(u.get('total_tokens'))} ; entree {_n(u.get('input_tokens'))} dont {_n(u.get('cached_input_tokens'))} "
                  f"en cache ; sortie {_n(u.get('output_tokens'))} ; {_n(u.get('requests'))} requetes ({a.get('usage_basis')})")
    elif u:
        tokens = (f"in {_n(u.get('input_tokens'))} / out {_n(u.get('output_tokens'))} / cache lu {_n(u.get('cache_read_tokens'))}"
                  f" / total {_n(u.get('total_tokens'))} ({a.get('usage_basis') or 'rapportes par le client'})")
    else:
        tokens = "non rapportes"
    if a.get("parent_call_seq") is not None:
        parent, basis = f"#{a['parent_call_seq']} (Agent)", a.get("link_basis") or "-"
    elif a.get("parent_agent_id"):
        # * Le FIL parent est ecrit par Codex ; l'APPEL de lancement, lui, n'est relie par aucun identifiant.
        parent = f"fil {agent_label(report, a['parent_agent_id'])} ; appel de lancement non etabli"
        basis = a.get("parent_basis") or "-"
    else:
        parent, basis = "inconnu", a.get("link_basis") or "-"
    if a.get("stop_time"):
        end = f"{a['stop_time']} (dernier de {a.get('stops') or 1} arret(s) observe(s))"
    elif a.get("origin") == "codex:rollout":
        # * Rollouts : « termine » d'un sous-agent Codex veut dire « tache rendue » ; il peut en recevoir une autre. Sans
        #   arret apres son dernier appel, sa fin n'est pas etablie. (Hooks Claude Code : libelle « en cours » conserve.)
        if a.get("end_state") == "resumed_after_stop":
            end = (f"inconnue : repris apres son dernier arret observe ({a.get('last_stop_time')}) ; dernier appel "
                   f"{a.get('last_call_time') or '?'}")
        else:
            end = f"inconnue : aucun arret observe ; dernier appel {a.get('last_call_time') or '?'}"
    else:
        end = "en cours"
    model = a.get("model") or "inconnu"
    if a.get("model_basis"):
        model += f" ({a['model_basis']})"
    return {"label": agent_label(report, a["agent_id"]), "id": str(a["agent_id"]),
            "type": (a.get("agent_type") or "inconnu") + (f" ; {a['agent_path']}" if a.get("agent_path") else ""),
            "status": a["classification"] + (f" (repris {a['resumes']}x)" if a.get("resumes") else ""),
            "calls": str(a["calls"]), "start": a.get("start_time") or "?", "end": end, "parent": parent, "basis": basis,
            "model": model, "tokens": tokens}


# * La base d'une duree reconstruite depend de la source des appels : aucun hook dans une session lue dans ses rollouts.
RECON_LABELS = {
    "hooks": "reconstruite entre hooks, inclut la surcharge des hooks",
    "rollout": "reconstruite entre les horodatages de debut et de fin ecrits dans le rollout, sans hook",
    "hooks+rollout": "reconstruite, selon l'appel, entre hooks ou entre horodatages du rollout",
}


def recon_label(cost: dict[str, Any]) -> str:
    return RECON_LABELS.get(str(cost.get("duration_reconstructed_basis") or "hooks"), RECON_LABELS["hooks"])


def tokens_cost_text(cost: dict[str, Any]) -> str:
    """Tokens d'un signalement : une part CALCULEE par AgentWatch a partir des releves par reponse, jamais presentee
    comme une mesure par appel, et jamais comme une somme facturee."""
    tok = cost.get("tokens")
    if not isinstance(tok, dict):
        return "tokens : aucun releve (agentwatch import-transcripts ou import-rollouts)"
    src = {"claude-code:transcript": "transcript", "codex:rollout": "rollout"}.get(str(tok.get("source")), str(tok.get("source")))
    if tok.get("total") is None:
        return (f"repartition partielle : {tok.get('observed_total')} tokens sur les seules composantes connues ; total non releve "
                f"({tok.get('complete_for', 0)}/{cost.get('calls')} appels avec entree et sortie ; releves par reponse : {src})")
    return (f"{tok['total']} tokens repartis par calcul sur ces appels ({tok['uncached_input']} d'entree non mise en cache + "
            f"{tok['output']} de sortie ; {tok['known_for']}/{cost.get('calls')} appels ; releves par reponse : {src})")


def other_tools_row(tools: list[dict[str, Any]], shown: int = 15) -> dict[str, Any] | None:
    """Outils au-dela des `shown` premiers, additionnes : le tableau retombe sur le nombre d'appels annonce."""
    rest = tools[shown:]
    if not rest:
        return None
    return {"tools": len(rest), "calls": sum(t["calls"] for t in rest), "errors": sum(t["errors"] for t in rest),
            "unknown_status": sum(t["unknown_status"] for t in rest), "open": sum(t["open"] for t in rest),
            "names": ", ".join(f"{t['tool']} ({t['calls']})" for t in rest[:12]) + (" ..." if len(rest) > 12 else "")}


def provenance(collection: str, origins: dict[str, int], client: str) -> dict[str, Any]:
    """D'ou viennent les evenements de la session, en une phrase lisible et des drapeaux pour le rendu."""
    hooks = int(origins.get("hook", 0))
    rollout = int(origins.get("codex:rollout", 0))
    other = {k: v for k, v in origins.items() if k not in ("hook", "codex:rollout")}
    if collection == "rollout":
        text = (f"rollouts Codex lus passivement ({rollout} evenement(s)) ; aucun hook dans cette session : ni surcout de hook, "
                "ni duree « entre hooks »")
    elif collection == "hooks":
        text = f"hooks du client {client} ({hooks} evenement(s))"
    elif collection == "mixed":
        text = f"hooks ({hooks} evenement(s)) et rollouts Codex ({rollout} evenement(s))"
    else:
        text = "origine des appels non etablie"
    if other:
        text += " ; imports complementaires : " + ", ".join(f"{k} ({v})" for k, v in sorted(other.items()))
    return {"collection": collection, "hook_events": hooks, "rollout_events": rollout, "other_imports": other, "text": text}
