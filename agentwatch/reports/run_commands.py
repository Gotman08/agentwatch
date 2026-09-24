"""Literal command targets needed by run inspection, without shell evaluation."""
from __future__ import annotations

import re

from agentwatch.reports.run_shell import _lex_simple, _split_segments, _strip_redirection


def command_segments(command: str):
    # A heredoc is input to its command, not a set of outer shell invocations.
    # Preserve newlines and refuse an unfinished block instead of interpreting it.
    lines, terminator = [], None
    for line in command.splitlines(keepends=True):
        if terminator:
            if line.strip() == terminator:
                terminator = None
            lines.append("\n")
            continue
        match = re.search(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1", line)
        lines.append(line)
        if match:
            terminator = match[2]
    segments, malformed = _split_segments("".join(lines))
    return [] if malformed or terminator else segments


def target(command: str, cwd: str, normalize_path) -> dict:
    workspace = normalize_path(cwd)
    targets, task_refs, candidate_modes = [], [], []
    partial = bool(re.search(r"\|\s*(?:head|tail|cut|grep)\b", command))
    unresolved = None
    previous_name = None
    previous_cd_unconditional = False
    for segment in command_segments(command):
        tokens = _lex_simple(_strip_redirection(segment.text))
        if not tokens:
            continue
        values = [t.value for t in tokens]
        name = values[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
        prior_name, previous_name = previous_name, name
        prior_cd_unconditional, previous_cd_unconditional = previous_cd_unconditional, False
        if name == "cd" and len(values) > 1:
            previous_cd_unconditional = segment.before not in {"&&", "||", "|", "&"} and segment.after not in {"&", "|"}
            if not previous_cd_unconditional:
                unresolved = "conditional_workspace"
            if segment.after in {"&", "|"}:
                continue
            directory = values[2] if values[1] == "/d" and len(values) > 2 else values[1]
            if not any(c in directory for c in "$`"):
                workspace = normalize_path(directory, workspace)
            else:
                unresolved = "dynamic_workspace"
            continue
        if name in {"cat", "tail", "head", "cut", "type", "get-content"}:
            for value in values[1:]:
                if not any(c in value for c in "$`"):
                    task_refs.extend(re.findall(r"[/\\]tasks[/\\]([\w-]+)\.output\b", value))
        if not re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", name):
            continue
        argv = values[1:]
        while argv and argv[0] in {"-u", "-B"}:
            argv = argv[1:]
        if not argv:
            continue
        script = argv[0].replace("\\", "/").rsplit("/", 1)[-1]
        modes = {"run_game_scenario.py": "launch", "watch_run.py": "watch", "t5_view.py": "view"}
        if script not in modes:
            continue
        candidate_modes.append(modes[script])
        conditional = segment.before in {"||", "|"} or (segment.before == "&&" and not (prior_name == "cd" and prior_cd_unconditional))
        value = None
        if modes[script] == "launch":
            if "--label" in argv and argv.index("--label") + 1 < len(argv):
                value = argv[argv.index("--label") + 1]
        elif len(argv) > 1:
            value = argv[1]
        if not value or any(c in value for c in "$`"):
            unresolved = unresolved or "dynamic_or_missing_run_argument"
            continue
        label = re.split(r"[/\\]", value.rstrip("/\\"))[-1]
        targets.append({"workspace": workspace, "label": label,
                        "runner_script": normalize_path(argv[0], workspace),
                        "artifact": normalize_path(value, workspace) if modes[script] == "watch" or "/" in value or "\\" in value else None,
                        "mode": modes[script]})
        if conditional:
            unresolved = unresolved or "conditional_python_invocation"
    if len(targets) > 1:
        unresolved = "multiple_run_targets_in_command"
    out = targets[0] if len(targets) == 1 and not unresolved else {"workspace": workspace, "label": None, "artifact": None, "mode": None}
    return {**out, "partial": partial, "task_refs": sorted(set(task_refs)), "unresolved_target": unresolved,
            "candidate_modes": candidate_modes, "candidate_targets": targets if unresolved else []}
