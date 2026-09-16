"""Detecteur B : boucles d'erreurs.

Regle : au moins `min_failures` echecs de meme signature d'erreur sur une operation
comparable (meme outil, meme cible, memes parametres) dans la fenetre, sans correction
observable des preconditions entre deux echecs.

Distinctions :
- persistent : aucun succes de la meme operation apres la serie ;
- transient_recovered : la meme operation finit par reussir sans correction observee
  (retry transitoire, eventuellement avec backoff) -> informatif, confiance basse ;
- interruption humaine (statut interrupted) : jamais comptee ;
- refus de permission (denied) : signale a part, ce n'est pas une erreur d'execution.
"""

from __future__ import annotations

from typing import Any

from agentwatch.core import normalize as N
from agentwatch.core import schema as S
from agentwatch.core.correlate import Call, SessionView
from agentwatch.detectors import base as B

RULE_ID = "B.error_loops"
RULE_VERSION = "1.0"
_FAIL = {S.STATUS_ERROR, S.STATUS_TIMEOUT}

_CAUSE_HINTS: list[tuple[str, str]] = [
    ("no such file", "precondition manquante : fichier ou chemin absent"),
    ("not found", "precondition manquante : commande, module ou fichier introuvable"),
    ("enoent", "precondition manquante : chemin absent"),
    ("permission", "permission ou sandbox insuffisante"),
    ("denied", "permission ou sandbox insuffisante"),
    ("timeout", "operation trop longue pour le delai configure"),
    ("timed out", "operation trop longue pour le delai configure"),
    ("syntax", "erreur de syntaxe dans l'entree construite par le modele"),
    ("modulenotfound", "dependance Python absente"),
    ("import", "dependance ou chemin d'import incorrect"),
    ("connection", "service reseau ou serveur MCP indisponible"),
    ("refused", "service reseau ou serveur MCP indisponible"),
    ("ssh", "connexion distante rompue : corriger la session (reconnexion, cle, VPN) plutot que relancer"),
    ("session", "session distante perdue : relancer ne la retablit pas"),
    ("interrompu", "service distant interrompu : attendre ou reconnecter, pas relancer a l'identique"),
    ("exit code", "commande en echec : voir le code de sortie"),
]


def _hypotheses(signature: str | None) -> list[str]:
    if not signature:
        return ["signature d'erreur absente : cause non determinable"]
    low = signature.lower()
    out = [text for needle, text in _CAUSE_HINTS if needle in low]
    return out or ["cause non reconnue a partir de la signature (hypothese a formuler manuellement)"]


def _is_failure(c: Call) -> bool:
    """Echec signale par le client, ou echec probable : statut non expose (Codex) ou reponse MCP
    'reussie' dont le texte ressemble a une erreur, dans les deux cas avec un indice textuel."""
    if c.status in _FAIL or c.status == S.STATUS_DENIED:
        return True
    if not (c.evidence.get("error_hint") and c.error_signature):
        return False
    return c.status == S.STATUS_UNKNOWN or (c.status == S.STATUS_SUCCESS and c.category == S.CAT_MCP)


def _loop_key(c: Call) -> str:
    return f"{c.agent_key}|{c.tool_name}|{c.target_key}|{c.params_key}|{c.error_signature}"


def _correction_between(a: Call, b: Call, calls: list[Call], case_insensitive: bool) -> tuple[list[str], list[str]]:
    """(corrections observees, indices d'effet inconnu) entre deux echecs."""
    observed: list[str] = []
    unknown: list[str] = []
    paths = set()
    if a.target_key and a.target_key.startswith("path:"):
        paths.add(a.target_key[len("path:"):].split("::", 1)[-1])
    for p in a.params.get("shell_paths") or []:
        if isinstance(p, str):
            k = N.path_key(N.normalize_path(p, a.project_dir, a.cwd), case_insensitive)
            if k:
                paths.add(k)
    for w in calls[a.seq + 1: b.seq]:
        if w.agent_key != a.agent_key:
            continue
        if a.category == S.CAT_MCP:
            # * Echec d'un outil MCP (ressource distante) : une ecriture locale ne le corrige pas ;
            #   seul un autre appel MCP non-lecture vers le meme serveur peut avoir agi.
            if w.category == S.CAT_MCP and w.mcp_server == a.mcp_server and w.op != "mcp_read" and w.tool_name != a.tool_name:
                unknown.append(f"#{w.seq} {w.tool_name} (action MCP sur le meme serveur)")
            continue
        if w.is_write_like:
            touched = set()
            if w.target_key and w.target_key.startswith("path:"):
                touched.add(w.target_key[len("path:"):].split("::", 1)[-1])
            for key in ("patch_paths", "shell_paths"):
                for p in w.params.get(key) or []:
                    if isinstance(p, str):
                        k = N.path_key(N.normalize_path(p, w.project_dir, w.cwd), case_insensitive)
                        if k:
                            touched.add(k)
            if paths & touched or (w.category == S.CAT_SHELL and not paths):
                observed.append(f"#{w.seq} {w.tool_name} sur {w.target!r}")
            elif w.category == S.CAT_SHELL:
                unknown.append(f"#{w.seq} commande d'ecriture sans cible reconnue")
        elif w.unknown_effect:
            unknown.append(f"#{w.seq} {w.tool_name} (effet inconnu)")
    return observed, unknown


def detect(view: SessionView, cfg: dict[str, Any]) -> list[B.Finding]:
    d = cfg.get("detectors", {}).get("error_loops", {})
    min_failures = int(d.get("min_failures", 3))
    window_calls = int(d.get("window_calls", 40))
    window_seconds = int(d.get("window_seconds", 1800))
    case_insensitive = bool(cfg.get("case_insensitive_paths", False))
    calls = view.calls
    chains: dict[str, list[Call]] = {}
    closed: list[tuple[str, list[Call]]] = []
    findings: list[B.Finding] = []

    for c in calls:
        if c.status == S.STATUS_INTERRUPTED:
            continue  # * annulation humaine ou du client : jamais une boucle du modele
        if _is_failure(c):
            key = _loop_key(c)
            chain = chains.get(key)
            if chain:
                prev = chain[-1]
                observed, _ = _correction_between(prev, c, calls, case_insensitive)
                if not B.within_window(prev, c, window_calls, window_seconds) or observed:
                    closed.append((key, chain))
                    chains[key] = [c]
                    continue
                chain.append(c)
            else:
                chains[key] = [c]
    closed.extend(chains.items())

    for key, chain in closed:
        if len(chain) < min_failures:
            continue
        first, last = chain[0], chain[-1]
        # ? Succes ulterieur de la meme operation (sans correction) => retry transitoire.
        later_success = next((c for c in calls[last.seq + 1:] if c.agent_key == first.agent_key and c.tool_name == first.tool_name
                              and c.target_key == first.target_key and c.params_key == first.params_key
                              and c.status == S.STATUS_SUCCESS and B.within_window(last, c, window_calls, window_seconds)), None)
        unknown_hints: list[str] = []
        for a, b in zip(chain, chain[1:]):
            _, unk = _correction_between(a, b, calls, case_insensitive)
            unknown_hints.extend(unk)
        gaps = [(b.order_ns - a.order_ns) / 1e9 for a, b in zip(chain, chain[1:]) if a.order_ns and b.order_ns]
        backoff = len(gaps) >= 2 and all(g2 >= g1 * 1.5 for g1, g2 in zip(gaps, gaps[1:]))
        denied = first.status == S.STATUS_DENIED
        if later_success:
            kind = "transient_recovered"
            conf = B.CONFIDENCE_LOW
            why = ("la meme operation a fini par reussir sans correction observee : retry transitoire probable"
                   + (" avec backoff croissant" if backoff else ""))
        elif denied:
            kind = "repeated_denial"
            conf = B.CONFIDENCE_MEDIUM
            why = "refus repetes de la meme operation ; un refus n'est pas une erreur d'execution"
        elif unknown_hints:
            kind = "persistent"
            conf = B.CONFIDENCE_MEDIUM
            why = "echecs identiques repetes, mais un appel a effet inconnu s'est intercale (correction possible non observable)"
        else:
            kind = "persistent"
            conf = B.CONFIDENCE_HIGH
            why = f"{len(chain)} echecs de meme signature sans aucune modification observee entre eux"
        if any(c.status == S.STATUS_UNKNOWN for c in chain):
            # ? Codex : le code de sortie n'est pas expose ; l'echec est infere du texte de sortie.
            kind = kind + "_probable" if kind == "persistent" else kind
            if conf == B.CONFIDENCE_HIGH:
                conf = B.CONFIDENCE_MEDIUM
            why += " ; statut d'execution non expose par le client, echec infere du texte de sortie (indice heuristique)"
        elif any(c.status == S.STATUS_SUCCESS and c.evidence.get("error_hint") for c in chain):
            # ? Serveur MCP qui renvoie une panne comme un resultat normal (isError=false).
            kind = kind + "_probable" if kind == "persistent" else kind
            if conf == B.CONFIDENCE_HIGH:
                conf = B.CONFIDENCE_MEDIUM
            why += (" ; le serveur MCP a renvoye ces reponses comme des succes (isError=false) alors que leur texte "
                    "decrit une panne : echec infere du texte (indice heuristique)")
        members = chain + ([later_success] if later_success else [])
        findings.append(B.Finding(
            rule_id=RULE_ID, rule_version=RULE_VERSION, kind=kind,
            title=f"{len(chain)} echecs identiques : {first.tool_name} sur {first.target!r}",
            confidence=conf, confidence_rationale=why + ". " + B.LIMIT_HEURISTIC,
            calls=[c.key for c in members], call_refs=B.refs(members),
            evidence={
                "error_signature": first.error_signature, "error_summary_first": first.error_summary,
                "statuses": [c.status for c in chain], "exit_codes": [c.exit_code for c in chain],
                "gaps_seconds": [round(g, 1) for g in gaps], "backoff_pattern": backoff,
                "unknown_effect_calls_between": unknown_hints, "later_success_seq": later_success.seq if later_success else None,
                "target": first.target, "params": first.params,
            },
            explanation=(f"La meme operation ({first.tool_name}, cible {first.target!r}) a echoue {len(chain)} fois avec la "
                         f"signature {first.error_signature!r}. " +
                         ("Elle a ensuite reussi sans modification observee." if later_success else
                          "Aucun succes ulterieur ni correction observable des preconditions.")),
            counter_indications=[
                "Un test intentionnel (verifier qu'une commande echoue) produirait le meme motif.",
                "Une correction hors des outils observes (edition manuelle, service externe) n'est pas visible.",
                "Les causes proposees sont des hypotheses etayees par la signature, pas des certitudes.",
            ],
            missing_data=[m for m in (
                "code de sortie absent" if all(c.exit_code is None for c in chain) else None,
                "durees inconnues" if all(c.duration_ms is None for c in chain) else None,
            ) if m],
            observed_cost=B.observed_cost(chain),
            proposal={
                "type": "fix_precondition_before_retry",
                "hypotheses": _hypotheses(first.error_signature),
                "text": ("Verifier la precondition (chemin, dependance, permission, delai) avant de relancer, "
                         "ou limiter le nombre de tentatives identiques dans les instructions du projet."),
                "tooling": ((["Cote serveur MCP : renvoyer les pannes avec isError=true et un code stable (ex. SSH_SESSION_LOST) ; "
                              "fournir un outil d'attente/reconnexion avec delai plutot que de laisser le client interroger l'etat ; "
                              "apres N echecs identiques, le dire explicitement dans la reponse."])
                            if first.category == S.CAT_MCP else []),
            },
            validation_protocol=[
                "Relire le resume d'erreur masque et confirmer que la signature designe bien la meme cause.",
                "Verifier dans le transcript si une correction a eu lieu hors des outils observes.",
                "Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.",
            ],
        ))
    return findings
