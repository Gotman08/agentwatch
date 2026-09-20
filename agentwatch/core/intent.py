"""Unites de travail : operation normalisee, independante de l'outil et de la forme de commande.

# * Objectif : reconnaitre le meme TRAVAIL fait par des moyens differents (outil Read vs
#   `cat` vs `sed -n`, outil Grep vs `rg`, `pytest` vs `python -m pytest`) pour comparer des
#   taches, pas seulement des appels identiques.
# ! Reste conservateur : seules des formes de commande reconnues (liste blanche) sont
#   traduites. Toute commande hors liste reste `unknown` et n'est comparee qu'a elle-meme
#   (texte normalise). Aucune equivalence semantique generale n'est pretendue.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from agentwatch.core import normalize as N
from agentwatch.core import schema as S
from agentwatch.core.correlate import Call

OP_READ = "read"
OP_SEARCH = "search"
OP_LIST = "list"
OP_VCS_READ = "vcs_read"
OP_RUN_TESTS = "run_tests"
OP_BUILD = "build"
OP_RUN_SCRIPT = "run_script"
OP_EDIT = "edit"
OP_WRITE = "write"
OP_MCP = "mcp"
OP_AGENT = "agent"
OP_WEB = "web"
OP_OTHER = "other"
OP_UNKNOWN = "unknown"

OP_MCP_READ = "mcp_read"
READ_LIKE_OPS = {OP_READ, OP_SEARCH, OP_LIST, OP_VCS_READ, OP_MCP_READ}
# * Outils MCP dont le nom annonce une lecture : comparables entre eux, avec une confiance
#   plafonnee (un serveur peut renvoyer un etat volatil ou avoir des effets non declares).
_MCP_READ_VERBS = re.compile(r"(?i)(^|_)(get|list|read|search|find|status|show|info|quota|check|selfcheck|self_check|health|ping|"
                             r"version|whoami|query|describe|fetch|lookup|explore|diagnose|forecast|efficiency|output)(_|$)")
RUN_LIKE_OPS = {OP_RUN_TESTS, OP_BUILD, OP_RUN_SCRIPT}

_READ_HEADS = {"cat", "type", "get-content", "gc", "less", "more", "nl", "head", "tail", "sed", "bat"}
_SEARCH_HEADS = {"rg", "grep", "egrep", "fgrep", "ag", "ack", "select-string", "sls", "findstr"}
_LIST_HEADS = {"ls", "dir", "get-childitem", "gci", "tree", "find", "fd"}
_TEST_RUNNERS = {"pytest", "py.test", "unittest", "jest", "vitest", "mocha", "ctest", "tox", "nox", "cargo test", "go test",
                 "npm test", "pnpm test", "yarn test", "dotnet test", "make test", "python -m pytest", "python -m unittest",
                 "python3 -m pytest", "python3 -m unittest", "py -m pytest", "py -m unittest"}
_SCRIPT_HEADS = {"python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php"}
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
_RANGE_RE = re.compile(r"^(\d+),(\d+)p$")


@dataclass
class Intent:
    op: str
    target: str | None          # chemin normalise, motif, ou texte de commande (unknown)
    params: dict[str, Any]
    source: str                 # tool | shell
    key: str                    # cle de comparaison


def _tokens(command: str) -> list[str]:
    toks = [t.strip("\"'") for t in _TOKEN_RE.findall(command)]
    return [t for t in toks if t]


def _norm_path(p: str, call: Call, cwd: str | None = None) -> str | None:
    return N.path_key(N.normalize_path(p, call.project_dir, cwd or call.cwd), os.name == "nt")


def _shell_intent(call: Call) -> Intent:
    command = str(call.params.get("command") or call.target or "")
    segments = [s.strip() for s in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command) if s.strip()]
    unknown = Intent(OP_UNKNOWN, command, {}, "shell", f"cmd:{call.cwd or ''}::{command}")
    if not segments:
        return unknown
    # * Prefixe `cd <dossier> &&` (tres courant chez les agents) : il fixe le dossier de travail
    #   effectif de la commande suivante, sans etre une operation en soi.
    eff_cwd = call.cwd
    first = _tokens(segments[0])
    while len(segments) > 1 and first and os.path.basename(first[0]).lower() in ("cd", "set-location", "pushd") and len(first) == 2:
        raw = first[1]
        eff_cwd = raw if N._is_abs(raw) else os.path.join(eff_cwd or "", raw)
        segments = segments[1:]
        first = _tokens(segments[0])
    if eff_cwd != call.cwd:
        call = _with_cwd(call, eff_cwd)
        residual = " && ".join(segments)
        unknown = Intent(OP_UNKNOWN, residual, {}, "shell", f"cmd:{eff_cwd or ''}::{residual}")
    if not first:
        return unknown
    head = os.path.basename(first[0]).lower().removesuffix(".exe")
    if head in {"sudo", "doas", "time", "rtk"} and len(first) > 1:
        first = first[1:]
        head = os.path.basename(first[0]).lower().removesuffix(".exe")
    two = " ".join([head, *first[1:3]]).lower()
    rest = first[1:]
    opts = [t for t in rest if t.startswith("-")]
    args = [t for t in rest if not t.startswith("-")]
    pipeline = [_tokens(s)[0].lower() for s in segments[1:] if _tokens(s)]
    if pipeline and not all(os.path.basename(p).removesuffix(".exe") in (_READ_HEADS | _SEARCH_HEADS | {"wc", "sort", "uniq", "cut", "tr", "jq", "column", "awk"}) for p in pipeline):
        return unknown  # ? un filtre inconnu dans le tube : pas de traduction

    if head in _READ_HEADS and args:
        params: dict[str, Any] = {}
        if head == "sed":
            if "-n" not in opts and not any(o.startswith("-n") for o in opts):
                return unknown  # sed sans -n : potentiellement une transformation
            m = _RANGE_RE.match(args[0]) if args else None
            if m:
                params["range"] = [int(m.group(1)), int(m.group(2))]
                args = args[1:]
            else:
                return unknown
        if head in ("head", "tail"):
            for o in opts:
                if o.startswith("-n"):
                    params[head] = o[2:] or (args.pop(0) if args and args[0].isdigit() else None)
                elif o[1:].isdigit():
                    params[head] = o[1:]
        if not args:
            return unknown
        target = _norm_path(args[0], call)
        if len(args) > 1:
            params["extra_paths"] = [_norm_path(a, call) for a in args[1:6]]
        if pipeline:
            params["pipeline"] = pipeline
        return Intent(OP_READ, target, params, "shell", f"{OP_READ}:{target}:{N.canonical_json(params)}")

    if head in _SEARCH_HEADS and args:
        pattern = args[0]
        path = _norm_path(args[1], call) if len(args) > 1 else _norm_path(".", call)
        params = {"pattern": pattern, "flags": sorted(o for o in opts if len(o) <= 3)}
        if pipeline:
            params["pipeline"] = pipeline
        return Intent(OP_SEARCH, path, params, "shell", f"{OP_SEARCH}:{path}:{N.canonical_json(params)}")

    if head in _LIST_HEADS:
        path = _norm_path(args[0], call) if args else _norm_path(".", call)
        params = {"flags": sorted(opts)}
        if head in ("find", "fd") and len(args) > 1:
            params["pattern"] = args[1]
        if pipeline:
            params["pipeline"] = pipeline
        return Intent(OP_LIST, path, params, "shell", f"{OP_LIST}:{path}:{N.canonical_json(params)}")

    if head == "git" and rest and rest[0].lower() in {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "blame", "describe", "remote", "tag"}:
        sub = rest[0].lower()
        params = {"args": rest[1:6], "cwd": call.cwd}
        return Intent(OP_VCS_READ, f"git {sub}", params, "shell", f"{OP_VCS_READ}:git {sub}:{N.canonical_json(params)}")

    lowered = command.lower()
    if two in _TEST_RUNNERS or head in {"pytest", "py.test", "jest", "vitest", "mocha", "ctest", "tox", "nox"} or lowered.startswith(("npm test", "pnpm test", "yarn test", "dotnet test", "cargo test", "go test", "make test")):
        target = next((a for a in args if not a.startswith("-") and a not in ("-m", "pytest", "unittest", "test")), None)
        params = {"args": [t for t in rest if t not in ("-m", "pytest", "unittest")][:8], "cwd": call.cwd}
        return Intent(OP_RUN_TESTS, target or "(suite)", params, "shell", f"{OP_RUN_TESTS}:{call.cwd or ''}:{N.canonical_json(params)}")

    if head in {"make", "cmake", "ninja", "msbuild", "tsc", "gradle", "mvn"} or two.startswith(("cargo build", "npm run build", "pnpm build", "dotnet build", "python -m build", "go build")):
        params = {"args": rest[:8], "cwd": call.cwd}
        return Intent(OP_BUILD, head, params, "shell", f"{OP_BUILD}:{call.cwd or ''}:{N.canonical_json(params)}")

    if head in _SCRIPT_HEADS and args and not args[0].startswith("-") and "." in os.path.basename(args[0]):
        script = _norm_path(args[0], call)
        after = rest[rest.index(args[0]) + 1:]          # * options comprises : elles font partie de la tache
        params = {"args": after[:8], "cwd": call.cwd}
        return Intent(OP_RUN_SCRIPT, script, params, "shell", f"{OP_RUN_SCRIPT}:{script}:{N.canonical_json(params)}")
    if head in {"python", "python3", "py"} and len(rest) >= 2 and rest[0] == "-m" and not rest[1].startswith("-"):
        # * `python -m paquet.module ...` : un module executable est une tache au meme titre qu'un script.
        module = f"module:{rest[1]}"
        params = {"args": rest[2:10], "cwd": call.cwd}
        return Intent(OP_RUN_SCRIPT, module, params, "shell", f"{OP_RUN_SCRIPT}:{call.cwd or ''}:{module}:{N.canonical_json(params)}")
    return unknown


def _with_cwd(call: Call, cwd: str | None) -> Call:
    """Copie legere de l'appel avec un autre dossier de travail (pour la normalisation des chemins)."""
    import copy
    clone = copy.copy(call)
    clone.cwd = cwd
    return clone


def derive(call: Call) -> Intent:
    """Intention d'un appel. Les outils natifs sont exacts ; le shell est traduit par liste blanche."""
    cat = call.category
    tkey = call.target_key or ""
    # * Meme forme que les chemins traduits du shell : sans le prefixe "path:<projet>::".
    path_key = tkey[len("path:"):].split("::", 1)[-1] if tkey.startswith("path:") else None
    if cat == S.CAT_SHELL:
        return _shell_intent(call)
    if cat == S.CAT_READ:
        params = {k: call.params[k] for k in ("offset", "limit", "pages") if k in call.params}
        return Intent(OP_READ, path_key, params, "tool", f"{OP_READ}:{path_key}:{N.canonical_json(params)}")
    if cat == S.CAT_SEARCH:
        params = {k: v for k, v in call.params.items() if k not in ("_fp",) and not k.startswith("_")}
        return Intent(OP_SEARCH, path_key, params, "tool", f"{OP_SEARCH}:{path_key}:{N.canonical_json(params)}")
    if cat == S.CAT_LIST:
        params = {k: v for k, v in call.params.items() if not k.startswith("_")}
        return Intent(OP_LIST, path_key, params, "tool", f"{OP_LIST}:{path_key}:{N.canonical_json(params)}")
    if cat in (S.CAT_EDIT, S.CAT_WRITE):
        op = OP_EDIT if cat == S.CAT_EDIT else OP_WRITE
        return Intent(op, path_key or call.target, {}, "tool", f"{op}:{path_key or call.target}:{call.params_key}")
    if cat == S.CAT_MCP:
        op = OP_MCP_READ if _MCP_READ_VERBS.search(call.mcp_tool or "") else OP_MCP
        return Intent(op, call.target, dict(call.params), "tool", f"{op}:{call.tool_name}:{tkey}:{call.params_key}")
    if cat == S.CAT_AGENT:
        return Intent(OP_AGENT, call.params.get("subagent_type"), {}, "tool", f"{OP_AGENT}:{call.key}")
    if cat == S.CAT_WEB:
        return Intent(OP_WEB, call.target, {}, "tool", f"{OP_WEB}:{call.target}:{call.params_key}")
    if cat == S.CAT_OTHER:
        return Intent(OP_OTHER, call.tool_name, {}, "tool", f"{OP_OTHER}:{call.tool_name}:{call.key}")
    return Intent(OP_UNKNOWN, call.target, {}, "tool", f"{OP_UNKNOWN}:{call.tool_name}:{tkey}:{call.params_key}")


def attach_intents(calls: list[Call], cfg: dict[str, Any] | None = None) -> None:
    for c in calls:
        it = derive(c)
        c.op, c.op_target, c.op_params, c.op_key, c.op_source = it.op, it.target, it.params, it.key, it.source
    reinterpret_exit_status(calls, cfg)


_NO_MATCH_HEADS = {"rg", "grep", "egrep", "fgrep", "ag", "ack", "findstr", "select-string", "sls"}
_REAL_ERROR_RE = re.compile(r"(?i)no such file|cannot find|cannot access|not found|introuvable|\berror\b|erreur|invalid|"
                            r"unrecognized|permission denied|is not recognized|regex parse|syntax")
_DIFF_PROBLEM_RE = re.compile(r"(?i)trailing whitespace|space before tab|conflict marker|new blank line at eof|indent with")
_EXIT_IN_SUMMARY_RE = re.compile(r"^\s*Exit code (\d+)\b")


MAX_SOURCE_LOOKUPS = 200


def reinterpret_exit_status(calls: list[Call], cfg: dict[str, Any] | None = None) -> None:
    """Un code de sortie non nul n'est pas toujours un echec (a l'analyse, sur les appels shell).

    # * Constate sur une session Codex reelle (2026-09-19) : 25 des 179 "echecs" shell n'en etaient pas :
    #   `rg` sans correspondance (code 1, aucune sortie) et `git diff --no-index` (code 1 = les fichiers
    #   different, ce que le modele voulait voir). Compter ces cas gonflait les erreurs et pouvait fabriquer
    #   de fausses boucles d'erreurs. Tout autre texte d'erreur laisse l'echec en place.
    # ! Le resume d'erreur ne conserve que la FIN de la sortie : une vraie erreur affichee au debut n'y figure
    #   pas, et l'echec etait alors requalifie en succes a tort (12 fois sur 50 dans la session du 2026-09-20).
    #   Quand la source de l'appel est disponible, son debut est relu avant de requalifier quoi que ce soit.
    """
    lookups = 0
    key = None
    cache: dict[str, str | None] = {}
    if cfg:
        from agentwatch.collector import rollouts as R
        key = R.source_key(cfg)
    for c in calls:
        if c.category != S.CAT_SHELL or c.status != S.STATUS_ERROR:
            continue
        code = c.exit_code
        summary = c.error_summary or ""
        if code is None:
            m = _EXIT_IN_SUMMARY_RE.match(summary)      # * Claude Code : "Exit code 1" dans le message d'echec
            code = int(m.group(1)) if m else None
            summary = summary[m.end():] if m else summary
        if code != 1:
            continue
        heads = [str(h).lower() for h in (c.params.get("shell_heads") or [])]
        command = str(c.params.get("command") or c.target or "").lower()
        meaning = None
        if heads and heads[0] in _NO_MATCH_HEADS and not _REAL_ERROR_RE.search(summary):
            # ? `rg a; rg b` : la derniere recherche seule peut etre vide ; le compte n'est nul que sans aucune sortie.
            meaning = "code 1 d'une recherche = aucune correspondance (pour au moins un motif), pas un echec"
            if not summary.strip():
                c.evidence["result_count"] = 0
        elif heads[:1] == ["git"] and " diff" in f" {command}" and any(o in command for o in ("--no-index", "--exit-code", "--quiet")) \
                and not _DIFF_PROBLEM_RE.search(summary) and not _REAL_ERROR_RE.search(summary.replace("warning:", "")):
            meaning = "code 1 = differences trouvees (git diff), pas un echec"
        if meaning and key is not None and lookups < MAX_SOURCE_LOOKUPS:
            # ! Le resume ne montre que la fin de la sortie : verifier dans la source qu'aucune erreur reelle ne
            #   s'affiche avant elle. Source indisponible -> on s'en tient au resume, comme avant.
            from agentwatch.collector import rollouts as R
            lookups += 1
            head = R.read_call_source(cfg, c.evidence.get("source_end"), key, cache)
            if head and _REAL_ERROR_RE.search(head):
                c.evidence["exit_status_meaning"] = ("code 1 conserve comme echec : une erreur figure au debut de la sortie, "
                                                     "hors du resume (relue dans la source)")
                meaning = None
        if meaning:
            c.status = S.STATUS_SUCCESS
            c.evidence["exit_status_meaning"] = meaning
            c.evidence["status_basis"] = "reinterpreted_exit_code"
            c.error_signature = None
