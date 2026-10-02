"""Detecteur E : service externe manipule a la main alors qu'un outil ferait le travail en un appel.

Regle : au moins `min_calls` (3) commandes shell d'une meme famille de service (ssh/scp/rsync,
SLURM, curl/wget, gh, az/aws/gcloud/kubectl, docker) vers la meme cible (hote, URL, service),
par le meme agent, dans la session. Chaque commande coute un tour complet : la construire,
puis relire une sortie brute. Un outil MCP ou une skill qui expose l'operation rend la meme
information en un appel structure, avec moins de tokens et plus de contexte pour le modele
(exemple : chercher la cle, l'ecrire, `ssh` vers le calculateur, contre un appel du serveur
MCP du calculateur).

Confiance :
- high : un serveur MCP lie a la cible a ete observe dans la session (il etait disponible et
  n'a pas servi pour ces commandes) ;
- medium : au moins `strong_calls` (6) commandes, ou des echecs dans la serie ;
- low : sinon.
Rien n'est construit ici : le signalement nomme l'outil manquant ou inutilise et compte ce
qu'il remplacerait.
"""

from __future__ import annotations

import os
import re
from typing import Any

from agentwatch.core import schema as S
from agentwatch.core.correlate import Call, SessionView
from agentwatch.detectors import base as B

RULE_ID = "E.tool_gap"
RULE_VERSION = "1.1"
_FAILED = {S.STATUS_ERROR, S.STATUS_TIMEOUT, S.STATUS_DENIED}
_WRAPPERS = {"sudo", "doas", "time", "rtk", "env", "nohup", "cd"}
FAMILIES: dict[str, set[str]] = {
    "ssh": {"ssh", "scp", "sftp", "rsync", "ssh-keygen", "ssh-add", "ssh-copy-id", "ssh-keyscan"},
    "slurm": {"sbatch", "squeue", "srun", "sacct", "scancel", "sinfo", "scontrol", "salloc", "seff"},
    "http": {"curl", "wget", "http", "https", "invoke-webrequest", "iwr", "invoke-restmethod", "irm"},
    "github": {"gh"},
    "cloud": {"az", "aws", "gcloud", "kubectl", "helm", "terraform"},
    "container": {"docker", "podman", "docker-compose", "nerdctl"},
}
# * Mots qui, dans le nom d'un serveur MCP ou de ses outils, rattachent ce serveur a une famille.
FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ssh": ("ssh", "remote", "login", "upload", "download", "cluster"),
    "slurm": ("slurm", "job", "sbatch", "squeue", "cluster"),
    "http": ("fetch", "http", "web", "api"),
    "github": ("github", "pull", "issue", "repo"),
    "cloud": ("aws", "azure", "gcloud", "kube", "cloud"),
    "container": ("docker", "container", "compose"),
}
_URL_HOST_RE = re.compile(r"https?://([^/\s'\"]+)")
_SSH_VALUE_OPTS = {"-p", "-i", "-o", "-l", "-J", "-F", "-e", "-P"}
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _head(h: Any) -> str:
    return os.path.basename(str(h)).lower().removesuffix(".exe") if h is not None else ""


def family_of(heads: list[Any]) -> tuple[str | None, str | None]:
    """(famille, tete) de la premiere commande utile (les enveloppes sudo/rtk/cd sont ignorees)."""
    for h in heads:
        head = _head(h)
        if not head or head in _WRAPPERS:
            continue
        for fam, members in FAMILIES.items():
            if head in members:
                return fam, head
        return None, head   # * premiere commande utile hors famille : pas un service
    return None, None


def _tokens_after_head(command: str, head: str) -> list[str]:
    first = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command, maxsplit=1)[0] if command else ""
    toks = [t.strip("\"'") for t in _TOKEN_RE.findall(first)]
    for i, t in enumerate(toks):
        if _head(t) == head:
            return toks[i + 1:]
    return toks[1:]


def target_of(family: str, head: str, command: str) -> str:
    """Cible comparable d'une commande de service : hote, URL, ou nom du service."""
    toks = _tokens_after_head(command, head)
    if family == "ssh":
        skip = False
        for t in toks:
            if skip:
                skip = False
                continue
            if t in _SSH_VALUE_OPTS:
                skip = True
                continue
            if t.startswith("-"):
                continue
            if "@" in t or (":" in t and head in ("scp", "rsync", "sftp")):
                host = t.split("@", 1)[-1].split(":", 1)[0]
                if host:
                    return host.lower()
                continue
            if head in ("scp", "rsync"):
                continue   # ? chemin local : la cible distante vient plus loin
            return t.lower()
        return head
    if family == "http":
        m = _URL_HOST_RE.search(command)
        return m.group(1).lower() if m else head
    if family == "slurm":
        return "slurm"
    if family == "github":
        return "github"
    return head


def related_servers(family: str, target: str, servers: dict[str, set[str]]) -> list[dict[str, str]]:
    """Serveurs MCP dont le nom cite la cible, ou dont le nom / les outils portent un mot de la famille."""
    out: list[dict[str, str]] = []
    keywords = FAMILY_KEYWORDS.get(family, ())
    for server, tools in servers.items():
        name = server.lower()
        if target and target != family and (target in name or name in target):
            out.append({"server": server, "basis": f"nom du serveur lie a la cible {target!r}"})
            continue
        hit = next((k for k in keywords if k in name), None)
        if hit is None:
            hit = next((k for k in keywords for t in tools if k in (t or "").lower()), None)
            if hit is not None:
                out.append({"server": server, "basis": f"outil(s) du serveur portant le mot {hit!r} (famille {family})"})
        else:
            out.append({"server": server, "basis": f"nom du serveur portant le mot {hit!r} (famille {family})"})
    return out


def mcp_servers_of(calls: list[Call]) -> dict[str, set[str]]:
    servers: dict[str, set[str]] = {}
    for c in calls:
        if c.category == S.CAT_MCP and c.mcp_server:
            servers.setdefault(c.mcp_server, set()).add(c.mcp_tool or "")
    return servers


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    d = cfg.get("detectors", {}).get("tool_gap", {})
    min_calls = int(d.get("min_calls", 3))
    strong_calls = int(d.get("strong_calls", 6))
    servers = mcp_servers_of(view.calls)
    clusters: dict[str, dict[str, Any]] = {}
    for c in view.calls:
        if c.category != S.CAT_SHELL:
            continue
        heads = c.params.get("shell_heads")
        if not isinstance(heads, list):
            continue
        family, head = family_of(heads)
        if family is None or head is None:
            continue
        target = target_of(family, head, str(c.params.get("command") or c.target or ""))
        key = f"{c.agent_key}|{family}|{target}"
        cl = clusters.setdefault(key, {"family": family, "target": target, "calls": [], "heads": set()})
        cl["calls"].append(c)
        cl["heads"].add(head)
    findings: list[B.Finding] = []
    for cl in clusters.values():
        members: list[Call] = cl["calls"]
        n = len(members)
        if n < min_calls:
            continue
        family, target = cl["family"], cl["target"]
        related = related_servers(family, target, servers)
        failures = sum(1 for c in members if c.status in _FAILED)
        if related:
            conf = B.CONFIDENCE_HIGH
            why = (f"un serveur MCP lie ({', '.join(r['server'] for r in related)}) a ete utilise dans cette session, "
                   f"mais pas pour ces {n} commandes")
        elif n >= strong_calls or failures:
            conf = B.CONFIDENCE_MEDIUM
            why = f"{n} commandes vers la meme cible" + (f", dont {failures} en echec" if failures else "") + " ; aucun serveur MCP lie observe dans la session"
        else:
            conf = B.CONFIDENCE_LOW
            why = f"{n} commandes vers la meme cible, sans echec ; aucun serveur MCP lie observe dans la session"
        names = ", ".join(r["server"] for r in related)
        title = f"{n} commandes {family} vers {target!r} faites a la main" + (f" ; serveur MCP apparente observe : {names}" if related else "")
        if related:
            text = (f"Verifier si un outil du serveur MCP {names} couvre ces operations avec les memes resultats, droits, erreurs "
                    f"et effets. La presence d'un serveur apparente ne prouve ni cette capacite ni sa disponibilite au moment requis.")
        else:
            text = (f"Candidat pour un outil MCP ou une skill {family!r} vers {target!r} : {n} commandes observees. "
                    f"Definir puis verifier le contrat de remplacement ; une skill ne reduit pas automatiquement le nombre "
                    f"d'appels. Disponibilite de la capacite exacte inconnue. AgentWatch ne cree rien.")
        findings.append(B.Finding(
            rule_id=RULE_ID, rule_version=RULE_VERSION, kind="manual_service_access", title=title,
            confidence=conf, confidence_rationale=why + ". " + B.LIMIT_HEURISTIC,
            calls=[c.key for c in members], call_refs=B.refs(members),
            evidence={"family": family, "target": target, "heads": sorted(cl["heads"]), "agent": members[0].agent_key,
                      "call_seqs": [c.seq for c in members][:40], "statuses": [c.status for c in members][:40], "failures": failures,
                      "mcp_servers_seen": sorted(servers), "related_servers": related},
            explanation=(f"{n} commandes de la famille {family} ({', '.join(sorted(cl['heads']))}) visent {target!r} dans la session. "
                         "Un remplacement structure exige de verifier chaque operation et les informations a conserver."),
            counter_indications=[
                "Une commande shell ponctuelle reste legitime : le signalement porte sur la repetition vers une meme cible.",
                "Un outil MCP a un cout fixe (demarrage, schema dans le contexte) : rentable seulement si l'operation revient.",
                "La disponibilite du serveur MCP dans les autres sessions n'est pas observee ici (voir `agentwatch trends`).",
                "Les operations exactes faites par ces commandes ne sont connues que par leur texte normalise.",
            ],
            missing_data=(["tokens non mesures : importer les transcripts pour chiffrer le cout de ces commandes"]
                          if not any(isinstance(c.usage, dict) for c in members) else []),
            observed_cost=B.observed_cost(members),
            proposal={"type": "use_or_build_tool", "steps_replaced": n, "family": family, "target": target,
                      "tool_candidates": [r["server"] for r in related], "text": text,
                      "steps_replaced_basis": "candidate_scope_not_demonstrated_reduction",
                      "capability_availability": "unknown", "capability_equivalence": "unknown"},
            validation_protocol=[
                "Lister les operations distinctes faites par ces commandes (verbes) et verifier qu'un outil MCP ou une skill les couvre.",
                "Transcripts importes : comparer les tokens de ces commandes avec ceux d'un appel MCP equivalent.",
                "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
            ],
        ))
    return findings
