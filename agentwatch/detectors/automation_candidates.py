"""Detecteur D : sequences candidates a une automatisation deterministe.

Regle : motif court (2 a `max_pattern_len` appels) qui se repete au moins
`min_occurrences` fois avec une structure stable (memes outils, memes formes de cibles
et de parametres) et des parametres variables (cibles differentes).

Les etapes sont separees en "mecaniques" (structure et parametres stables ou
simplement substitues) et "jugement" (contenu d'edition variable, commande variable,
statut different selon l'occurrence). Le resultat est un candidat a valider : rien
n'est genere ni execute.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

from agentwatch.core import schema as S
from agentwatch.core.correlate import Call, SessionView
from agentwatch.detectors import base as B

RULE_ID = "D.automation_candidates"
RULE_VERSION = "1.0"


def _shape(c: Call) -> str:
    """Signature structurelle d'un appel : OPERATION normalisee (pas l'outil), forme de la cible,
    noms des parametres. Read et `cat` ont la meme forme ; `pytest` et `python -m pytest` aussi."""
    if c.op in ("mcp", "mcp_read"):
        return f"{c.op}|{c.mcp_server}/{c.mcp_tool}|" + ",".join(sorted(k for k in c.op_params if not k.startswith("_")))
    if c.op not in ("unknown", "other"):
        t = str(c.op_target or "")
        base = os.path.basename(t)
        if c.op in ("read", "search", "list", "edit", "write", "run_script") or "/" in t or (base and "." in base):
            # * Forme de chemin : extension seulement, pour que `pytest tests/test_a.py` et
            #   `pytest tests/test_b.py` soient la meme etape a cible variable.
            ext = os.path.splitext(t)[1] or ("dir" if not base or "." not in base else "noext")
            tshape = f"path{ext}"
        else:
            tshape = t[:40]
        pkeys = ",".join(sorted(k for k in c.op_params if k not in ("cwd",)))
        return f"{c.op}|{tshape}|{pkeys}"
    if c.target_kind == "command":
        heads = c.params.get("shell_heads") or []
        return f"{c.tool_name}|shell|cmd:" + "/".join(heads[:3]) + f":{c.shell_kind}|"
    if c.target_kind == "mcp":
        return f"{c.tool_name}|mcp|{c.mcp_server}/{c.mcp_tool}|"
    pkeys = ",".join(sorted(k for k in c.params if not k.startswith("_") and k not in ("shell_heads", "shell_kind", "shell_paths", "command")))
    return f"{c.tool_name}|{c.category}|{c.target_kind}|{pkeys}"


def _mine(seq: list[Call], n_min: int, n_max: int, min_occ: int) -> list[tuple[tuple[str, ...], list[list[Call]]]]:
    shapes = [_shape(c) for c in seq]
    found: list[tuple[tuple[str, ...], list[list[Call]]]] = []
    for n in range(n_min, n_max + 1):
        index: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for i in range(len(shapes) - n + 1):
            index[tuple(shapes[i:i + n])].append(i)
        for gram, positions in index.items():
            if len(set(gram)) < 2:
                continue  # * repetition d'un seul appel : domaine des detecteurs A/C
            occ: list[list[Call]] = []
            last_end = -1
            for p in positions:                     # occurrences non chevauchantes
                if p > last_end:
                    occ.append(seq[p:p + n])
                    last_end = p + n - 1
            if len(occ) >= min_occ:
                found.append((gram, occ))
    # * Motifs fermes : on retire un motif inclus dans un motif plus long de meme frequence.
    closed: list[tuple[tuple[str, ...], list[list[Call]]]] = []
    for gram, occ in found:
        dominated = False
        for g2, occ2 in found:
            if len(g2) > len(gram) and len(occ2) >= len(occ):
                joined, joined2 = "\x00".join(gram), "\x00".join(g2)
                if joined in joined2:
                    dominated = True
                    break
        if not dominated:
            closed.append((gram, occ))
    closed.sort(key=lambda g: (len(g[1]), len(g[0])), reverse=True)
    return closed


def _analyse(gram: tuple[str, ...], occ: list[list[Call]]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    judgment = 0
    for pos in range(len(gram)):
        col = [o[pos] for o in occ]
        targets = {c.target for c in col}
        statuses = {c.status for c in col}
        first = col[0]
        variable = len(targets) > 1
        kind = "mechanical"
        reason = "structure stable" + (", cible variable" if variable else ", cible fixe")
        if first.category in (S.CAT_EDIT, S.CAT_WRITE):
            fps = {c.params.get("new_fp") or c.params.get("content_fp") or c.params.get("patch_fp") for c in col}
            if len(fps) > 1:
                kind, reason = "judgment", "contenu d'edition different a chaque occurrence"
        elif first.category == S.CAT_SHELL and variable:
            heads = {tuple(c.params.get("shell_heads") or []) for c in col}
            if len(heads) > 1:
                kind, reason = "judgment", "commande de structure variable"
            else:
                reason = "meme commande, chemins substitues"
        if len(statuses) > 1:
            kind, reason = "judgment", reason + " ; statut variable selon l'occurrence (branchement)"
        if kind == "judgment":
            judgment += 1
        steps.append({"position": pos + 1, "tool": first.tool_name, "category": first.category, "kind": kind,
                      "reason": reason, "example_target": first.target, "variable": variable,
                      "statuses": sorted(statuses)})
    return {"steps": steps, "judgment_steps": judgment,
            "inputs": [s["position"] for s in steps if s["variable"]],
            "all_success": all(c.status == S.STATUS_SUCCESS for o in occ for c in o)}


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    d = cfg.get("detectors", {}).get("automation_candidates", {})
    min_occ = int(d.get("min_occurrences", 3))
    n_min = max(2, int(d.get("min_pattern_len", 2)))
    n_max = max(n_min, int(d.get("max_pattern_len", 6)))
    max_calls = int(d.get("max_calls", 2000))
    findings: list[B.Finding] = []
    by_agent: dict[str, list[Call]] = defaultdict(list)
    for c in view.calls[-max_calls:]:
        by_agent[c.agent_key].append(c)
    for agent, seq in by_agent.items():
        for gram, occ in _mine(seq, n_min, n_max, min_occ)[:10]:
            info = _analyse(gram, occ)
            n = len(occ)
            if info["judgment_steps"] >= len(info["steps"]):
                continue  # * aucune etape mecanique : rien a automatiser, on ne signale pas
            for step in info["steps"]:
                if isinstance(step.get("example_target"), str) and len(step["example_target"]) > 80:
                    step["example_target"] = step["example_target"][:80] + "..."
            if info["judgment_steps"] == 0 and info["all_success"] and n >= min_occ + 1:
                conf, why = B.CONFIDENCE_HIGH, f"{n} occurrences, toutes reussies, aucune etape de jugement detectee"
            elif info["judgment_steps"] <= 1:
                conf, why = B.CONFIDENCE_MEDIUM, f"{n} occurrences ; {info['judgment_steps']} etape(s) de jugement ou occurrences au seuil"
            else:
                conf, why = B.CONFIDENCE_LOW, f"{n} occurrences mais {info['judgment_steps']} etapes exigent encore un jugement"
            members = [c for o in occ for c in o]
            recipe = {
                "name": " -> ".join(s["tool"] or "?" for s in info["steps"]),
                "inputs": [{"step": p, "example": info["steps"][p - 1]["example_target"]} for p in info["inputs"]],
                "preconditions": ["les cibles existent et sont accessibles", "les memes outils/serveurs MCP sont disponibles",
                                  "aucune etape ne depend d'une decision prise pendant l'execution (a verifier)"],
                "steps": info["steps"],
                "output": f"{info['steps'][-1]['category']} sur {info['steps'][-1]['example_target']!r} (derniere etape)",
                "tests": ["rejouer la recette sur les occurrences enregistrees (donnees seulement, sans executer les commandes)",
                          "comparer les empreintes de resultat de chaque etape avec celles observees"],
                "risks": ["les etapes de jugement restent a la charge du modele ou d'un humain",
                          "une commande shell peut avoir des effets non observes",
                          "un motif repete n'implique pas que la tache soit sans intelligence : candidat a valider"],
            }
            findings.append(B.Finding(
                rule_id=RULE_ID, rule_version=RULE_VERSION, kind="recurring_pattern",
                title=f"Motif recurrent ({n}x) : {recipe['name']}",
                confidence=conf, confidence_rationale=why + ". " + B.LIMIT_HEURISTIC,
                calls=[c.key for c in members], call_refs=B.refs(members),
                evidence={"agent": agent, "pattern": list(gram), "occurrences": n, "pattern_length": len(gram),
                          "occurrence_seqs": [[c.seq for c in o] for o in occ], "judgment_steps": info["judgment_steps"]},
                explanation=(f"La sequence {recipe['name']} apparait {n} fois avec la meme structure ; "
                             f"{len(info['inputs'])} position(s) ont une cible variable."),
                counter_indications=[
                    "Les decisions prises entre deux appels (lecture du resultat, choix de la cible suivante) ne sont pas observables.",
                    "Une structure stable n'implique pas une semantique stable (commandes shell notamment).",
                ],
                missing_data=["contenu des resultats et raisonnement du modele non observes"],
                observed_cost=B.observed_cost(members),
                proposal={"type": "deterministic_recipe_candidate", "recipe": recipe,
                          "text": "Candidat de script/recette a valider manuellement ; AgentWatch ne le genere ni ne l'execute."},
                validation_protocol=[
                    "Verifier sur 2 occurrences que les entrees variables suffisent a reproduire chaque etape.",
                    "Ecrire la recette, l'executer sur une copie, comparer les sorties avec les empreintes observees.",
                    "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
                ],
            ))
    return findings
