"""Detecteur F : consignes rappelees a la main (ou le modele se fait redire la meme chose).

Regle : un meme paragraphe (empreinte HMAC du texte normalise, >= 40 caracteres) revient dans plusieurs
messages DISTINCTS :
- de l'utilisateur, dans le fil principal (`user_repeated_guidance`, au moins `min_user_messages`) ;
- de l'orchestrateur a ses sous-agents : argument `message` de send_message, followup_task, spawn_agent
  (`orchestrator_repeated_guidance`, au moins `min_instructions`).
Une consigne qu'il faut redire est une consigne qui manque ailleurs : AGENTS.md, une skill, la definition du
sous-agent. Les paragraphes repetes ensemble dans les memes messages forment un seul signalement.

# ! Le texte n'est jamais conserve : empreintes, longueurs, tours et identifiants seulement. Un sous-agent
#   recopie l'historique de son parent (memes identifiants de message) : les messages sont dedoublonnes par
#   identifiant, et seuls ceux de l'utilisateur dans le fil principal comptent comme rappels humains.
# * Source : marqueurs `message` (import des rollouts Codex). Sans import, rien a analyser.
"""

from __future__ import annotations

from typing import Any

from agentwatch.core import schema as S
from agentwatch.core.correlate import SessionView
from agentwatch.detectors import base as B

RULE_ID = "F.repeated_guidance"
RULE_VERSION = "1.0"


def _messages(view: SessionView, role: str) -> list[dict[str, Any]]:
    """Messages distincts d'un role (dedoublonnes par identifiant), dans l'ordre."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in view.markers:
        if m.phase != S.PHASE_MESSAGE or m.meta.get("role") != role:
            continue
        if role == "user" and m.agent_id:
            continue   # * copie de l'historique du parent, ou tache de sous-agent : pas un rappel de l'utilisateur
        mid = m.meta.get("message_id") or f"{m.ns}|{m.agent_id}"
        if mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "time": m.time, "turn": m.meta.get("turn_id"), "agent": m.agent_id,
                    "paragraphs": list(m.meta.get("paragraphs") or []), "sizes": list(m.meta.get("paragraph_chars") or []),
                    "target": m.meta.get("target"), "tool": m.meta.get("tool")})
    return out


def _groups(msgs: list[dict[str, Any]], minimum: int) -> list[dict[str, Any]]:
    """Paragraphes presents dans au moins `minimum` messages distincts, regroupes par ensemble de messages."""
    where: dict[str, list[int]] = {}
    size: dict[str, int] = {}
    for i, m in enumerate(msgs):
        for j, fp in enumerate(m["paragraphs"]):
            where.setdefault(fp, [])
            if not where[fp] or where[fp][-1] != i:
                where[fp].append(i)
            if j < len(m["sizes"]):
                size[fp] = m["sizes"][j]
    by_set: dict[tuple[int, ...], list[str]] = {}
    for fp, idx in where.items():
        if len(idx) >= minimum:
            by_set.setdefault(tuple(idx), []).append(fp)
    groups = [{"messages": list(k), "paragraphs": v, "chars": sum(size.get(fp, 0) for fp in v)} for k, v in by_set.items()]
    groups.sort(key=lambda g: (len(g["messages"]), g["chars"]), reverse=True)
    return groups


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    d = cfg.get("detectors", {}).get("repeated_guidance", {})
    findings: list[B.Finding] = []
    for role, kind, minimum in (("user", "user_repeated_guidance", int(d.get("min_user_messages", 2))),
                                ("agent_instruction", "orchestrator_repeated_guidance", int(d.get("min_instructions", 3)))):
        msgs = _messages(view, role)
        groups = [g for g in _groups(msgs, max(2, minimum)) if g["chars"] >= int(d.get("min_chars", 80))]
        for g in groups[: int(d.get("max_findings", 10))]:
            members = [msgs[i] for i in g["messages"]]
            n = len(members)
            turns = sorted({str(m["turn"]) for m in members if m["turn"]})
            wasted = g["chars"] * (n - 1)
            if role == "user":
                conf = B.CONFIDENCE_HIGH if n >= 3 else B.CONFIDENCE_MEDIUM
                who, fix = "l'utilisateur", ("Inscrire cette consigne dans AGENTS.md (ou une skill chargee pour ce type de tache) : "
                                              "elle a ete redonnee a la main a chaque fois.")
            else:
                conf = B.CONFIDENCE_MEDIUM
                who, fix = "l'orchestrateur a ses sous-agents", ("Placer cette consigne dans les instructions des sous-agents (role, "
                                                                 "AGENTS.md du dossier) plutot que dans chaque message.")
            findings.append(B.Finding(
                rule_id=RULE_ID, rule_version=RULE_VERSION, kind=kind,
                title=(f"Consigne redonnee {n} fois par {who} ({len(g['paragraphs'])} paragraphe(s), {g['chars']} caracteres)"),
                confidence=conf,
                confidence_rationale=(f"meme(s) paragraphe(s) a l'identique (empreinte) dans {n} messages distincts"
                                      + (f", sur {len(turns)} tours" if turns else "") + ". " + B.LIMIT_HEURISTIC),
                calls=[f"message|{m['id']}" for m in members], call_refs=[],
                evidence={"role": role, "paragraph_fingerprints": g["paragraphs"], "paragraph_chars": g["chars"],
                          "messages": n, "message_ids": [m["id"] for m in members][:20], "turns": turns[:20],
                          "times": [m["time"] for m in members][:20], "repeated_chars": wasted,
                          "targets": sorted({m["target"] for m in members if m.get("target")})[:10]},
                explanation=(f"Le meme texte ({g['chars']} caracteres) a ete ecrit {n} fois par {who} ; "
                             f"{wasted} caracteres de consignes auraient pu etre donnes une seule fois."),
                counter_indications=[
                    "Un rappel volontaire (consigne critique a chaque etape) peut etre voulu.",
                    "Le texte n'est pas conserve : relire les messages concernes pour juger (identifiants et heures fournis).",
                ],
                missing_data=["texte des consignes non conserve (empreintes seulement)"],
                observed_cost={"calls": 0, "repeated_chars": wasted, "tokens": "non mesure",
                               "note": "caracteres repetes ; les tokens correspondants ne sont pas deduits des caracteres"},
                proposal={"type": "move_guidance_to_persistent_instructions", "text": fix,
                          "requires_judgment": "Verifier que la consigne vaut pour toutes les taches de ce type."},
                validation_protocol=[
                    "Retrouver les messages par leur heure dans le client et confirmer qu'il s'agit bien d'une consigne.",
                    "La deplacer dans AGENTS.md ou la skill, puis verifier qu'elle n'est plus redonnee dans les sessions suivantes (trends).",
                    "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
                ],
            ))
    return findings
