"""Normalisation conservative des outils, chemins, commandes et parametres.

# * Importe par le chemin chaud : uniquement stdlib legere (re, os, json).
# ! On ne pretend jamais demontrer l'equivalence semantique de deux commandes
#   shell : la classification ci-dessous sert a degrader la confiance, pas a
#   affirmer qu'une commande est sans effet.
"""

from __future__ import annotations

import json
import os
import re
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX
from agentwatch.core import schema as S

# --------------------------------------------------------------------- outils
_CLAUDE_TOOL_CATEGORY: dict[str, str] = {
    "Read": S.CAT_READ, "NotebookRead": S.CAT_READ,
    "Grep": S.CAT_SEARCH, "WebSearch": S.CAT_WEB, "ToolSearch": S.CAT_OTHER,
    "Glob": S.CAT_LIST, "LS": S.CAT_LIST,
    "Edit": S.CAT_EDIT, "MultiEdit": S.CAT_EDIT, "NotebookEdit": S.CAT_EDIT,
    "Write": S.CAT_WRITE,
    "Bash": S.CAT_SHELL, "PowerShell": S.CAT_SHELL, "BashOutput": S.CAT_OTHER, "KillShell": S.CAT_OTHER,
    "WebFetch": S.CAT_WEB,
    "Agent": S.CAT_AGENT, "Task": S.CAT_AGENT,
    "TodoWrite": S.CAT_OTHER, "Skill": S.CAT_OTHER, "ExitPlanMode": S.CAT_OTHER,
    "AskUserQuestion": S.CAT_OTHER, "TaskOutput": S.CAT_OTHER, "TaskStop": S.CAT_OTHER,
}
_CODEX_TOOL_CATEGORY: dict[str, str] = {
    "Bash": S.CAT_SHELL, "shell": S.CAT_SHELL, "local_shell": S.CAT_SHELL,
    "exec_command": S.CAT_SHELL, "shell_command": S.CAT_SHELL, "container.exec": S.CAT_SHELL,
    "write_stdin": S.CAT_OTHER,
    "apply_patch": S.CAT_EDIT, "Edit": S.CAT_EDIT, "Write": S.CAT_WRITE,
    "Read": S.CAT_READ, "read_file": S.CAT_READ, "view_image": S.CAT_READ,
    "Grep": S.CAT_SEARCH, "Glob": S.CAT_LIST, "list_dir": S.CAT_LIST,
    "web_search": S.CAT_WEB, "update_plan": S.CAT_OTHER, "spawn_agent": S.CAT_AGENT,
    "request_user_input": S.CAT_OTHER,
    # * Fonctions observees dans les rollouts (codex-cli 0.153 a 0.155, mode multi-agents et code) :
    "collaboration.spawn_agent": S.CAT_AGENT, "collaboration.send_message": S.CAT_OTHER,
    "collaboration.followup_task": S.CAT_OTHER, "collaboration.wait_agent": S.CAT_OTHER,
    "collaboration.list_agents": S.CAT_OTHER, "collaboration.interrupt_agent": S.CAT_OTHER,
    "wait": S.CAT_OTHER, "clock.sleep": S.CAT_OTHER, "request_user_input_async": S.CAT_OTHER,
}
_MCP_PREFIX = "mcp__"


def categorize_tool(client: str, tool_name: str | None) -> tuple[str, str | None, str | None]:
    """(categorie, serveur MCP, outil MCP). Inconnu -> CAT_UNKNOWN, jamais une devinette."""
    if not tool_name:
        return S.CAT_UNKNOWN, None, None
    if tool_name.startswith(_MCP_PREFIX):
        rest = tool_name[len(_MCP_PREFIX):]
        server, sep, tool = rest.partition("__")
        return S.CAT_MCP, (server or None), (tool if sep else None)
    table = _CLAUDE_TOOL_CATEGORY if client == CLIENT_CLAUDE_CODE else _CODEX_TOOL_CATEGORY
    if client == CLIENT_CODEX and tool_name in _CLAUDE_TOOL_CATEGORY and tool_name not in table:
        return _CLAUDE_TOOL_CATEGORY[tool_name], None, None
    return table.get(tool_name, S.CAT_UNKNOWN), None, None


# --------------------------------------------------------------------- chemins
def to_posix(path: str) -> str:
    return path.replace("\\", "/")


def _is_abs(path: str) -> bool:
    return os.path.isabs(path) or re.match(r"^[A-Za-z]:[\\/]", path) is not None or path.startswith("/")


_MSYS_RE = re.compile(r"^/(?:mnt/)?([A-Za-z])(/|$)")


def _expand_tilde(path: str) -> str:
    """`~/x` -> dossier utilisateur : les evenements stockent le projet et le cwd masques par `~`
    (confidentialite), mais les chemins cites dans les commandes sont absolus ; a l'analyse,
    les deux doivent se comparer."""
    if path == "~" or path.startswith("~/"):
        return to_posix(os.path.expanduser("~")) + path[1:]
    return path


def _unmsys(path: str) -> str:
    """Sous Windows, `/c/Users/x` (Git Bash, MSYS) et `/mnt/c/Users/x` (WSL) designent `C:/Users/x`.

    # * Observe en direct : les commandes emises depuis Git Bash citent le projet sous cette
    #   forme, ce qui empechait de rattacher leurs chemins au projet.
    """
    if os.name != "nt":
        return path
    m = _MSYS_RE.match(path)
    if not m:
        return path
    return m.group(1).upper() + ":/" + path[m.end():]


def normalize_path(path: str | None, project_dir: str | None, cwd: str | None) -> str | None:
    """Chemin en separateurs '/', relatif au projet s'il est dessous, sinon absolu.

    # ? On ne resout pas les liens symboliques ni la casse reelle du disque :
    #   cout borne et pas d'acces disque dans le hook.
    """
    if not path or not isinstance(path, str):
        return None
    p = _unmsys(_expand_tilde(to_posix(path.strip().strip('"').strip("'"))))
    if not p:
        return None
    cwd = _unmsys(_expand_tilde(to_posix(cwd))) if cwd else cwd
    if not _is_abs(p) and cwd:
        p = to_posix(os.path.normpath(os.path.join(cwd, p)))
    elif _is_abs(p):
        p = to_posix(os.path.normpath(p))
    root = to_posix(os.path.normpath(_unmsys(_expand_tilde(to_posix(project_dir))))) if project_dir else None
    if root:
        fold = os.name == "nt"
        a, b = (p.lower(), root.lower()) if fold else (p, root)
        if a == b:
            return "."
        if a.startswith(b.rstrip("/") + "/"):
            return p[len(root.rstrip("/")) + 1:]
    return p


def path_key(target: str | None, case_insensitive: bool) -> str | None:
    """Cle de comparaison d'un chemin normalise."""
    if target is None:
        return None
    return target.lower() if case_insensitive else target


# --------------------------------------------------------------------- shell
_READ_HEADS = {
    "cat", "head", "tail", "less", "more", "type", "wc", "stat", "file", "ls", "dir", "tree",
    "find", "fd", "rg", "grep", "egrep", "fgrep", "ag", "ack", "which", "where", "whereis",
    "pwd", "echo", "printf", "true", "jq", "cut", "sort", "uniq", "tr", "awk", "nl", "od",
    "xxd", "hexdump", "basename", "dirname", "realpath", "readlink", "date", "uname", "id",
    "whoami", "hostname", "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "column", "test",
    "get-content", "gc", "get-childitem", "gci", "select-string", "sls", "get-item", "gi",
    "get-location", "gl", "resolve-path", "test-path", "get-command", "gcm", "measure-object",
    "select-object", "format-table", "ft", "out-string", "write-output", "write-host",
    "get-process", "gps",
}
_GIT_READ_SUBS = {
    "status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "blame", "describe",
    "remote", "tag", "config", "cat-file", "ls-tree", "grep", "shortlog", "reflog", "worktree",
    "rev-list", "name-rev", "check-ignore", "stash list",
}
_WRITE_HEADS = {
    "rm", "mv", "cp", "mkdir", "rmdir", "touch", "tee", "ln", "chmod", "chown", "truncate",
    "dd", "install", "patch", "unzip", "tar", "zip", "curl", "wget", "pip", "pip3", "uv",
    "npm", "pnpm", "yarn", "npx", "cargo", "make", "cmake", "ninja", "msbuild", "dotnet",
    "go", "gradle", "mvn", "docker", "kubectl", "terraform", "remove-item", "ri", "del",
    "move-item", "mi", "copy-item", "ci", "new-item", "ni", "set-content", "sc", "add-content",
    "ac", "out-file", "rename-item", "rni", "invoke-webrequest", "iwr", "invoke-restmethod",
    "irm", "start-process", "saps", "clear-content", "clc", "rtk",
}
_RUN_HEADS = {"python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php", "java",
              "bash", "sh", "zsh", "pwsh", "powershell", "cmd", "xargs", "env", "timeout", "nohup",
              "pytest", "jest", "vitest", "mocha", "tox", "nox", "ctest", "sudo", "doas", "time"}
_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_REDIRECT_RE = re.compile(r"(?<![<>\d])>{1,2}(?!&\d)|\d>{1,2}(?!&)")
_PATHLIKE_RE = re.compile(r"""(?<![\w@:])(?:~|\.{1,2})?[/\\]?(?:[\w.\-+@]+[/\\])+[\w.\-+@*?]*|[\w.\-+]+\.[A-Za-z0-9]{1,8}(?![\w/\\])""")
_OPTION_RE = re.compile(r"^-{1,2}[\w\-]*(=.*)?$")
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
# * Parametre de delai (attente, intervalle, echeance) : un nombre, jamais un contenu. Garde en clair pour dire
#   quel delai l'agent a demande ; exclu de la cle de comparaison (deux attentes aux delais differents attendent
#   la meme chose).
TIMING_PARAM_RE = re.compile(r"(?i)(?:^|_)(?:timeout|wait|delay|interval|yield|sleep|poll|backoff|retry_after)(?:_|$)"
                             r"|(?:_ms|_s|_sec|_secs|_seconds)$")


def is_timing_param(key: str, value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and bool(TIMING_PARAM_RE.search(str(key)))


_URL_HEAD_RE = re.compile(r"^[\"']?[A-Za-z][A-Za-z0-9+.\-]*://")


def normalize_command(command: str) -> str:
    """Collapse des espaces, suppression des retours a la ligne superflus."""
    return re.sub(r"\s+", " ", command.strip())


def _strip_quotes(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def classify_shell(command: str) -> dict[str, Any]:
    """Classification conservative d'une commande shell.

    Retourne {"kind": read|write|run|unknown, "heads": [...], "paths": [...], "segments": n}.
    # ! "read" signifie seulement : aucun segment reconnu comme ecrivant ; ce n'est pas
    #   une preuve d'absence d'effet (un binaire inconnu ou un alias peut ecrire).
    """
    if not command or not isinstance(command, str):
        return {"kind": "unknown", "heads": [], "paths": [], "segments": 0}
    segments = [s for s in _SPLIT_RE.split(command.strip()) if s]
    heads: list[str] = []
    paths: list[str] = []
    kinds: list[str] = []
    for seg in segments:
        toks = [_strip_quotes(t) for t in _TOKEN_RE.findall(seg)]
        toks = [t for t in toks if t and not _ENV_ASSIGN_RE.match(t)]
        if not toks:
            continue
        # ! Un segment qui commence par une URL (ligne d'un script) : sa "tete" serait la fin de l'URL, requete
        #   et signature comprises. Constate le 2026-09-19 (URL presignees GCS dans les tetes de commande).
        head = "<url>" if _URL_HEAD_RE.match(toks[0]) else os.path.basename(toks[0]).lower()
        if head.endswith(".exe"):
            head = head[:-4]
        if head in {"sudo", "doas", "time", "nice", "rtk"} and len(toks) > 1:
            toks = toks[1:]
            head = os.path.basename(toks[0]).lower()
        heads.append(head)
        kind = "unknown"
        if _REDIRECT_RE.search(seg):
            kind = "write"
        elif head == "git" and len(toks) > 1:
            sub = toks[1].lower()
            kind = "read" if sub in _GIT_READ_SUBS else "write"
        elif head == "sed":
            kind = "write" if any(t in ("-i", "--in-place") or t.startswith("-i") for t in toks[1:]) else "read"
        elif head in _READ_HEADS:
            kind = "read"
        elif head in _WRITE_HEADS:
            kind = "write"
        elif head in _RUN_HEADS:
            kind = "run"
        kinds.append(kind)
        for tok in toks[1:]:
            if _OPTION_RE.match(tok):
                continue
            if _PATHLIKE_RE.fullmatch(tok) and len(paths) < 20:
                paths.append(tok)
    if not kinds:
        overall = "unknown"
    elif all(k == "read" for k in kinds):
        overall = "read"
    elif any(k == "write" for k in kinds):
        overall = "write"
    elif any(k == "run" for k in kinds):
        overall = "run"
    else:
        overall = "unknown"
    return {"kind": overall, "heads": heads[:10], "paths": paths, "segments": len(segments)}


# --------------------------------------------------------------------- patchs
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", re.M)
_DIFF_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$", re.M)


def extract_patch_paths(text: str | None, limit: int = 50) -> list[str]:
    """Chemins cibles d'un patch apply_patch (ou diff unifie). Sans lecture du contenu."""
    if not text or not isinstance(text, str):
        return []
    found = _PATCH_FILE_RE.findall(text) or _DIFF_FILE_RE.findall(text)
    return [f.strip() for f in found[:limit]]


# --------------------------------------------------------------------- divers
def canonical_json(obj: Any) -> str:
    """JSON canonique (cles triees, sans espaces) pour empreintes stables."""
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(obj)


def strip_url(url: str | None) -> str | None:
    """Conserve scheme://hote/chemin ; supprime requete et fragment (jetons, PII)."""
    if not url or not isinstance(url, str):
        return None
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://[^/?#]+)([^?#]*)", url)
    if not m:
        return url.split("?", 1)[0][:200]
    return (m.group(1) + m.group(2))[:300]


_ERROR_LINE_RE = re.compile(r"(?i)\b(error|erreur|exception|traceback|no such file|not found|cannot|can't|failed|denied|"
                            r"unexpected|invalid|refused|timed? ?out|introuvable|impossible)\b")


def make_error_signature(text: str | None, limit: int = 160) -> str | None:
    """Signature d'erreur : premiere ligne significative, nombres/hex/chemins remplaces."""
    if not text:
        return None
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)   # caracteres de controle invisibles
    lines = [c.strip() for c in text.strip().splitlines() if c.strip()]
    if not lines:
        return None
    line = lines[0]
    # * Observe en direct (Claude Code) : l'erreur de Bash commence par "Exit code N" seul ;
    #   on y joint la premiere ligne significative de stderr, sinon toutes les erreurs se
    #   confondraient dans la meme signature.
    if re.match(r"(?i)^exit code \d+$", line) and len(lines) > 1:
        # ? Le texte melange stdout et stderr : on prend la premiere ligne qui ressemble a une
        #   erreur, sinon la derniere (stderr arrive generalement en fin).
        detail = next((c for c in lines[1:] if _ERROR_LINE_RE.search(c)), lines[-1])
        line = line + " | " + detail
    line = re.sub(r"[A-Za-z]:[\\/][^\s:'\"]+|(?:/[\w.\-]+){2,}", "<path>", line)
    line = re.sub(r"\b[0-9a-f]{7,}\b", "<hex>", line)
    line = re.sub(r"\d+", "<n>", line)
    line = re.sub(r"\s+", " ", line)
    return line[:limit]


def size_of(obj: Any) -> int:
    """Taille en octets de la representation JSON (mesure 'serialized')."""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj.encode("utf-8", "surrogateescape"))
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8", "surrogateescape"))
    except (TypeError, ValueError):
        return len(repr(obj))
