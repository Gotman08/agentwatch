"""Resolve recorded shell-wrapper launches from bounded transcript events.

This module deliberately reads only the event stream passed by the caller.  It
does not inspect the current filesystem and it does not attempt to implement a
general shell interpreter.  A wrapper is accepted only when its historical
``Write`` call and successful confirmation are both present before the launch,
and when the selected wrapper branch is statically understandable.
"""
from __future__ import annotations

import posixpath
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from agentwatch.collector import privacy as P


PathNormalizer = Callable[..., str]
TargetBuilder = Callable[[str, str], dict[str, Any]]

_TARGET_KEYS = ("workspace", "label", "artifact", "mode", "partial", "task_refs")
_SHELL_NAMES = {"sh", "bash", "dash", "zsh", "ash", "busybox"}
_PYTHON_NAMES = {"python", "python3", "python3.11", "python3.12", "python.exe"}
_CONTROL_WORDS = {
    "case", "do", "done", "elif", "else", "esac", "fi", "for", "function",
    "if", "in", "select", "then", "time", "until", "while", "!", "{", "}",
}
_MUTATING_TOOLS = re.compile(
    r"(?ix)"
    r"\b(?:sed|perl)\b[^;\n]*(?:\s-i(?:\s|$)|--in-place|-[^;\n]*p[i])"
    r"|\b(?:tee|truncate|rm|mv|cp|install)\b"
    r"|\b(?:python(?:3(?:\.\d+)?)?(?:\.exe)?)\b[^;\n]*(?:-c\b|-[^;\n]*<<|write_text\b|read_text\b|open\s*\(|replace\s*\(|shutil\b|pathlib\b)"
    r"|(?:^|[;\n])\s*(?:cat|echo|printf)\b[^;\n]*(?:>>?|\s-\s)"
)
_SUCCESS_RE = re.compile(r"(?i)file\s+created\s+successfully\s+at\s*:")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")


@dataclass
class _Token:
    value: str
    raw: str
    start: int
    end: int
    quoted: bool = False


@dataclass
class _Segment:
    text: str
    start: int
    end: int
    before: str | None = None
    after: str | None = None


@dataclass
class _ShellCandidate:
    segment: _Segment
    tokens: list[_Token]
    shell_index: int
    script: str | None
    arguments: list[str]
    reason: str | None = None


@dataclass
class _PendingWrite:
    scope: tuple[Any, Any]
    path: str
    content: str | None
    call_id: str | None
    source: dict[str, Any]
    raw_path: str | None


@dataclass
class _Version:
    path: str
    content: str
    write_source: dict[str, Any]
    confirmation_source: dict[str, Any]
    version: str
    definition_lines: list[int] = field(default_factory=list)


def _source(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("source")
    return value if isinstance(value, dict) else {}


def _source_key(source: dict[str, Any]) -> tuple[Any, Any, Any]:
    return source.get("path"), source.get("line"), source.get("block")


def _scope(event: dict[str, Any]) -> tuple[Any, Any]:
    source = _source(event)
    return event.get("thread"), source.get("path")


def _data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def _input(event: dict[str, Any]) -> dict[str, Any]:
    value = _data(event).get("input")
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    """Flatten the bounded result shapes used by Claude transcript exports."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _text(item)))
    if isinstance(value, dict):
        for key in ("text", "content", "output", "message", "result"):
            if key in value:
                rendered = _text(value[key])
                if rendered:
                    return rendered
    return ""


def _normalise(normalize_path: PathNormalizer, value: Any, cwd: str = "") -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = normalize_path(value, cwd)
    except TypeError:
        result = normalize_path(value, cwd=cwd)
    except (OSError, ValueError, RuntimeError):
        return None
    return result if isinstance(result, str) and result else None


def _basename(value: str) -> str:
    value = value.replace("\\", "/").rstrip("/")
    return value.rsplit("/", 1)[-1].casefold()


def _strip_redirection(text: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char in "<>" or (char == "&" and index + 1 < len(text) and text[index + 1] == ">"):
            return text[:index]
    return text


def _lex_simple(text: str) -> list[_Token]:
    """Tokenise one simple command without expanding shell syntax."""
    tokens: list[_Token] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        value: list[str] = []
        quoted = False
        quote: str | None = None
        escaped = False
        while index < length:
            char = text[index]
            if escaped:
                if char != "\n":
                    value.append(char)
                escaped = False
                index += 1
                continue
            if char == "\\" and quote != "'":
                escaped = True
                index += 1
                continue
            if quote:
                if char == quote:
                    quote = None
                else:
                    value.append(char)
                index += 1
                continue
            if char in "'\"`":
                quote = char
                quoted = True
                index += 1
                continue
            if char.isspace():
                break
            value.append(char)
            index += 1
        if quote is not None:
            # Retain the token; the caller reports the unsupported syntax.
            quoted = True
        if index == start:
            index += 1
            continue
        tokens.append(_Token("".join(value), text[start:index], start, index, quoted))
    return tokens


def _split_segments(text: str) -> tuple[list[_Segment], bool]:
    """Split at unquoted command separators and retain separator classes."""
    segments: list[_Segment] = []
    start = 0
    before: str | None = None
    quote: str | None = None
    escaped = False
    index = 0
    malformed = False
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            # A backslash-newline continuation is part of one command.
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
            index += 1
            continue
        separator: str | None = None
        width = 1
        if text.startswith("&&", index):
            separator, width = "&&", 2
        elif text.startswith("||", index):
            separator, width = "||", 2
        elif char == "&" and (index > 0 and text[index - 1] == ">" or
                               index + 1 < len(text) and text[index + 1] == ">"):
            # ``2>&1`` and ``&>file`` are redirections, not command
            # separators.  The redirection itself is removed later.
            index += 1
            continue
        elif char in ";|&\n":
            separator = char
        if separator is not None:
            end = index
            if text[start:end].strip():
                segments.append(_Segment(text[start:end], start, end, before=before, after=separator))
            start = index + width
            before = separator
            index += width
            continue
        index += 1
    if quote is not None:
        malformed = True
    if text[start:].strip():
        segments.append(_Segment(text[start:], start, len(text), before=before, after=None))
    if segments:
        for left, right in zip(segments, segments[1:]):
            left.after = right.before
    return segments, malformed


def _mask_heredoc(command: str) -> tuple[str, bool]:
    """Hide heredoc bodies so embedded script text is never parsed as shell."""
    lines: list[str] = []
    terminator: str | None = None
    for line in command.splitlines(keepends=True):
        if terminator is not None:
            lines.append("\n" if line.endswith(("\n", "\r")) else "")
            if line.strip() == terminator:
                terminator = None
            continue
        lines.append(line)
        marker = _HEREDOC_RE.search(line)
        if marker:
            terminator = marker.group(2)
    return "".join(lines), terminator is not None


def _is_shell_token(token: _Token) -> bool:
    return _basename(token.value) in _SHELL_NAMES or _basename(token.value).endswith("/sh") or _basename(token.value).endswith("/bash")


def _is_python_token(token: _Token) -> bool:
    value = _basename(token.value)
    return value in _PYTHON_NAMES or bool(re.fullmatch(r"python3(?:\.\d+)?(?:\.exe)?", value))


def _dynamic_token(token: _Token) -> bool:
    return "$" in token.raw or "`" in token.raw or "$((" in token.raw


def _shell_candidate(segment: _Segment) -> _ShellCandidate | None:
    prefix = _strip_redirection(segment.text)
    tokens = _lex_simple(prefix)
    if not tokens:
        return None
    shell_index = next((i for i, token in enumerate(tokens) if _is_shell_token(token)), None)
    if shell_index is None:
        return None
    # A shell name inside a quoted argument was kept as one token and cannot be
    # selected here.  A control word before it is a conditional invocation.
    reason = None
    if shell_index != 0:
        if tokens[0].value.casefold() not in _CONTROL_WORDS:
            # ``echo sh wrapper.sh`` and ``rg 'sh wrapper.sh'`` are ordinary
            # text commands, not launches.  Do not create phantom entries.
            return None
        reason = "conditional_shell_invocation"
    if segment.before in {"&&", "||", "|", "&"} or segment.after in {"&&", "||", "|", "&"}:
        reason = "conditional_shell_invocation"
    rest = tokens[shell_index + 1:]
    if not rest:
        return _ShellCandidate(segment, tokens, shell_index, None, [], reason or "missing_script_path")
    option_index = 0
    while option_index < len(rest) and rest[option_index].value.startswith("-"):
        option = rest[option_index].value
        if option in {"--", "-"}:
            option_index += 1
            break
        # ``sh -c`` executes a command string, never a recorded script path.
        if "c" in option.lstrip("-") or option in {"-l", "-lc", "-cl"}:
            return _ShellCandidate(segment, tokens, shell_index, None,
                                    [token.value for token in rest[option_index + 1:]],
                                    reason or "shell_command_string_unsupported")
        option_index += 1
    if option_index >= len(rest):
        return _ShellCandidate(segment, tokens, shell_index, None, [], reason or "missing_script_path")
    script_token = rest[option_index]
    script = None if _dynamic_token(script_token) else script_token.value
    args = [token.value for token in rest[option_index + 1:]]
    if any(_dynamic_token(token) for token in rest[option_index + 1:]):
        reason = reason or "dynamic_arguments"
    if script is None:
        reason = reason or "dynamic_script_path"
    return _ShellCandidate(segment, tokens, shell_index, script, args, reason)


def _shell_candidates(command: str) -> tuple[list[_ShellCandidate], bool]:
    masked, unfinished = _mask_heredoc(command)
    segments, malformed = _split_segments(masked)
    return [candidate for segment in segments if (candidate := _shell_candidate(segment))], malformed or unfinished


def _arg_literal(token: _Token, arguments: list[str]) -> tuple[str | None, int | None, str | None]:
    raw = token.raw.strip()
    value = token.value
    match = re.fullmatch(r"\$(\d+)|\$\{(\d+)\}", value)
    if match:
        number = int(match.group(1) or match.group(2))
        if number < 1 or number > len(arguments):
            return None, number, "missing_parameter"
        return arguments[number - 1], number, None
    if "$" in raw or "`" in raw or "$((" in raw:
        return None, None, "dynamic_label"
    return value, None, None


def _line_numbers(text: str, start: int, end: int) -> list[int]:
    if end < start:
        start, end = end, start
    return list(range(text.count("\n", 0, start) + 1, text.count("\n", 0, end) + 2))


@dataclass
class _Wrapper:
    cd: str | None
    launch: str | None
    label: str | None
    label_parameter: int | None
    case_parameter: int | None
    definition_lines: list[int]
    rule: str
    reason: str | None = None


def _find_cd(script: str) -> tuple[str | None, list[int], str | None]:
    segments, malformed = _split_segments(script)
    if malformed:
        return None, [], "malformed_script"
    found: list[tuple[str, _Segment]] = []
    for segment in segments:
        tokens = _lex_simple(_strip_redirection(segment.text))
        if not tokens:
            continue
        first = tokens[0].value.casefold()
        if first == "cd":
            if len(tokens) != 2 or _dynamic_token(tokens[1]) or tokens[1].value.startswith("~"):
                return None, [], "dynamic_cd"
            found.append((tokens[1].value, segment))
        elif first in {"if", "for", "while", "until", "function", "eval", "source", "."}:
            # The selected case cannot be trusted when an outer control-flow
            # construct changes whether the cd/case is reached.
            return None, [], "unsupported_controlflow"
    if len(found) != 1:
        return None, [], "missing_or_dynamic_cd"
    value, segment = found[0]
    return value, _line_numbers(script, segment.start, segment.end), None


def _case_parts(script: str) -> tuple[int, int, int, list[tuple[str, int, int, str]], str | None]:
    matches = list(re.finditer(r"(?im)^\s*case\s+(?:\"|')?\$(\d+)(?:\"|')?\s+in\s*$", script))
    if len(matches) != 1:
        return 0, 0, 0, [], "unsupported_case"
    header = matches[0]
    end_match = re.search(r"(?im)^\s*esac\b", script[header.end():])
    if end_match is None:
        return 0, 0, 0, [], "incomplete_case"
    body_start = header.end()
    body_end = header.end() + end_match.start()
    body = script[body_start:body_end]
    arms: list[tuple[str, int, int, str]] = []
    arm_matches = list(re.finditer(r"(?im)^\s*([^\n()]+?)\)\s*", body))
    for index, arm in enumerate(arm_matches):
        arm_start = body_start + arm.end()
        arm_end = body_start + (arm_matches[index + 1].start() if index + 1 < len(arm_matches) else len(body))
        arms.append((arm.group(1).strip(), arm_start, arm_end, script[arm_start:arm_end]))
    if not arms:
        return 0, 0, 0, [], "unsupported_case"
    return header.start(), body_end, int(header.group(1)), arms, None


def _launch_from_body(body: str) -> tuple[str | None, list[_Token], str | None, int, int]:
    segments, malformed = _split_segments(body)
    if malformed:
        return None, [], "malformed_case_arm", 0, 0
    launches: list[tuple[_Segment, list[_Token]]] = []
    for segment in segments:
        prefix = _strip_redirection(segment.text)
        tokens = _lex_simple(prefix)
        if not tokens:
            continue
        # Ignore the arm terminator and harmless shell assignments/comments.
        first = tokens[0].value
        if (tokens[0].value in {";;", ":", "true", "cd", "exit", "set"}
                or first.startswith("#")):
            continue
        if _is_python_token(tokens[0]):
            if any(token.value.casefold().endswith("run_game_scenario.py") for token in tokens[1:]):
                launches.append((segment, tokens))
            else:
                return None, [], "unsupported_python_command", segment.start, segment.end
        elif tokens[0].value.endswith("=") or "=" in tokens[0].value and not tokens[0].value.startswith("--"):
            # Static variable assignments are allowed in a wrapper arm, but
            # their values are intentionally not evaluated.
            continue
        else:
            return None, [], "unsupported_controlflow", segment.start, segment.end
    if len(launches) != 1:
        return None, [], "multiple_or_missing_launches", 0, 0
    segment, tokens = launches[0]
    if segment.before in {"&&", "||", "|", "&"} or segment.after in {"&&", "||", "|", "&"}:
        return None, [], "conditional_launch", segment.start, segment.end
    return segment.text, tokens, None, segment.start, segment.end


def _label_from_tokens(tokens: list[_Token], arguments: list[str]) -> tuple[str | None, int | None, str | None]:
    found: list[tuple[str | None, int | None, str | None]] = []
    index = 0
    while index < len(tokens):
        value = tokens[index].value
        if value == "--label":
            if index + 1 >= len(tokens):
                return None, None, "missing_label"
            found.append(_arg_literal(tokens[index + 1], arguments))
            index += 2
            continue
        if value.startswith("--label="):
            raw_value = value.split("=", 1)[1]
            synthetic = _Token(raw_value, tokens[index].raw.split("=", 1)[1], tokens[index].start, tokens[index].end,
                               tokens[index].quoted)
            found.append(_arg_literal(synthetic, arguments))
        index += 1
    if len(found) != 1:
        return None, None, "missing_or_ambiguous_label"
    return found[0]


def _substitute_parameters(text: str, arguments: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        number = int(match.group(1) or match.group(2))
        return arguments[number - 1] if 1 <= number <= len(arguments) else match.group(0)

    return re.sub(r"\$(\d+)|\$\{(\d+)\}", replace, text.replace("\\\r\n", " ").replace("\\\n", " "))


def _shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:+@%=-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _parse_wrapper(script: str, arguments: list[str]) -> _Wrapper:
    if not isinstance(script, str) or not script.strip():
        return _Wrapper(None, None, None, None, None, [], "", "empty_script")
    cd, cd_lines, cd_reason = _find_cd(script)
    if cd_reason:
        return _Wrapper(cd, None, None, None, None, cd_lines, "", cd_reason)
    header_start, case_end, case_parameter, arms, case_reason = _case_parts(script)
    if case_reason:
        # A tiny straight-line wrapper is safe when it has one launch and no
        # case statement at all.  It still needs a literal label.
        launch, tokens, reason, start, end = _launch_from_body(script)
        if reason:
            return _Wrapper(cd, None, None, None, None, cd_lines, "", case_reason)
        label, label_parameter, label_reason = _label_from_tokens(tokens, arguments)
        if label_reason:
            return _Wrapper(cd, None, None, None, None, cd_lines, "straightline", label_reason)
        return _Wrapper(cd, _substitute_parameters(launch or "", arguments), label, label_parameter,
                        None, sorted(set(cd_lines + _line_numbers(script, start, end))),
                        "shell.wrapper.straightline_literal")
    selected: list[tuple[str, int, int, str]] = []
    if case_parameter < 1 or case_parameter > len(arguments):
        return _Wrapper(cd, None, None, None, case_parameter, cd_lines, "", "missing_case_parameter")
    case_value = arguments[case_parameter - 1]
    for pattern, start, end, body in arms:
        cleaned = pattern.strip().strip("\"'")
        if cleaned == case_value:
            selected.append((pattern, start, end, body))
    if len(selected) != 1:
        return _Wrapper(cd, None, None, None, case_parameter, cd_lines, "", "unknown_case_arm")
    pattern, arm_start, arm_end, body = selected[0]
    if any(char in pattern for char in "*?[|$"):
        return _Wrapper(cd, None, None, None, case_parameter, cd_lines, "", "non_literal_case_arm")
    launch, tokens, launch_reason, start, end = _launch_from_body(body)
    if launch_reason:
        return _Wrapper(cd, None, None, None, case_parameter, cd_lines, "", launch_reason)
    label, label_parameter, label_reason = _label_from_tokens(tokens, arguments)
    if label_reason:
        return _Wrapper(cd, None, None, label_parameter, case_parameter, cd_lines, "", label_reason)
    lines = sorted(set(cd_lines + _line_numbers(script, header_start, case_end) +
                      _line_numbers(script, arm_start + start, arm_start + end)))
    return _Wrapper(cd, _substitute_parameters(launch or "", arguments), label, label_parameter,
                    case_parameter, lines, "shell.wrapper.case_literal_static")


def _target_shape(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = list(_TARGET_KEYS)
    if "runner_script" in value:
        keys.append("runner_script")
    return {key: value.get(key) for key in keys}


def _confirmation_path(text: str, expected: str, normalize_path: PathNormalizer, cwd: str) -> bool:
    match = _SUCCESS_RE.search(text)
    if not match:
        return False
    remainder = text[match.end():].splitlines()[0].strip()
    remainder = remainder.lstrip("\"'")
    candidates = [remainder]
    if " " in remainder:
        parts = remainder.split()
        candidates.extend(" ".join(parts[:index]) for index in range(1, len(parts) + 1))
    if remainder.endswith("."):
        candidates.append(remainder[:-1])
    return any(_normalise(normalize_path, candidate.strip("\"'"), cwd) == expected for candidate in candidates)


def _path_forms(path: str) -> set[str]:
    normal = path.replace("\\", "/").casefold().rstrip("/")
    forms = {normal}
    drive = re.fullmatch(r"([a-z]):/(.*)", normal)
    if drive:
        forms.add("/" + drive.group(1) + "/" + drive.group(2))
    if normal.startswith("/"):
        forms.add(normal[1:])
    components = [part for part in normal.split("/") if part]
    if components:
        forms.add(components[-1])
    if len(components) >= 2:
        forms.add("/".join(components[-2:]))
    return forms


def _mutation_references(command: str, path: str) -> bool:
    if not _MUTATING_TOOLS.search(command):
        return False
    command_forms = command.replace("\\", "/").casefold()
    return any(form and form in command_forms for form in _path_forms(path))


def _event_result_success(data: dict[str, Any]) -> bool:
    return data.get("is_error") in (None, False)


def _script_path(event: dict[str, Any], normalize_path: PathNormalizer) -> tuple[str | None, str | None]:
    input_data = _input(event)
    raw = next((input_data.get(name) for name in ("file_path", "path", "filename")
                if isinstance(input_data.get(name), str)), None)
    if not isinstance(raw, str):
        return None, None
    return _normalise(normalize_path, raw, str(event.get("cwd") or "")), raw


def _source_ref(role: str, source: dict[str, Any]) -> dict[str, Any]:
    copied = dict(source)
    # ``source`` is convenient for direct consumers; ``sources`` lets the
    # bounded run graph collect proof refs without interpreting this record.
    return {"kind": "source_ref", "role": role, "source": copied, "sources": [dict(copied)]}


def _version_evidence(version: _Version, invocation_source: dict[str, Any], wrapper: _Wrapper,
                      path: str, arguments: list[str]) -> list[dict[str, Any]]:
    return [
        _source_ref("invocation", invocation_source),
        _source_ref("definition_write", version.write_source),
        _source_ref("definition_confirmation", version.confirmation_source),
        {"kind": "rule", "rule": wrapper.rule},
        {"kind": "script_version", "version_hmac": version.version,
         "version": version.version, "version_basis": "privacy.fingerprint(masked_recorded_script)"},
        {"kind": "definition", "script": path, "lines": list(wrapper.definition_lines)},
        {"kind": "parameter_mapping", "case_parameter": wrapper.case_parameter,
         "label_parameter": wrapper.label_parameter, "arguments": list(arguments),
         "label": wrapper.label,
         "mapping": {"case_parameter": wrapper.case_parameter,
                     "label_parameter": wrapper.label_parameter,
                     "label": wrapper.label}},
    ]


def _candidate(invocation: _ShellCandidate, script: str | None, normalize_path: PathNormalizer,
               cwd: str, call_id: Any, source: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalise(normalize_path, script, cwd) if script is not None else None
    return {"script": normalized, "arguments": list(invocation.arguments), "call_id": call_id,
            "source": dict(source)}


def _result(invocation: _ShellCandidate, source: dict[str, Any], call_id: Any,
            *, status: str, reason: str, target: dict[str, Any] | None = None,
            evidence: list[dict[str, Any]] | None = None,
            candidates: list[dict[str, Any]] | None = None,
            normalized_script: str | None = None) -> dict[str, Any]:
    return {"status": status,
            "invocation": {"script": normalized_script, "arguments": list(invocation.arguments),
                           "call_id": call_id, "source": dict(source)},
            "target": target, "reason": reason, "evidence": list(evidence or []),
            "candidates": list(candidates or [])}


def resolve_shell_calls(events: list[dict[str, Any]], key: bytes, *,
                        target_for_command: TargetBuilder,
                        normalize_path: PathNormalizer) -> dict[tuple[Any, Any, Any], dict[str, Any]]:
    """Resolve historical ``sh``/``bash`` wrapper launches.

    Events are consumed in their supplied physical order.  A version becomes
    usable only after its matching successful result has been seen, so a later
    result can never retroactively resolve an earlier launch.
    """
    active: dict[tuple[Any, Any], dict[str, _Version]] = {}
    known_paths: dict[tuple[Any, Any], set[str]] = {}
    invalid: dict[tuple[Any, Any], dict[str, str]] = {}
    pending: dict[tuple[tuple[Any, Any], str], _PendingWrite] = {}
    pending_conflicts: set[tuple[tuple[Any, Any], str]] = set()
    completed: dict[tuple[tuple[Any, Any], str], tuple[str, str | None, str]] = {}
    signatures: dict[tuple, str] = {}
    ambiguous_ids: set[tuple] = set()
    output: dict[tuple[Any, Any, Any], dict[str, Any]] = {}

    def invalidate(scope: tuple[Any, Any], path: str, reason: str) -> None:
        known_paths.setdefault(scope, set()).add(path)
        active.setdefault(scope, {}).pop(path, None)
        invalid.setdefault(scope, {})[path] = reason

    def record_write(event: dict[str, Any]) -> None:
        data = _data(event)
        name = str(data.get("name") or "").casefold()
        if name != "write":
            return
        path, raw_path = _script_path(event, normalize_path)
        call_id = data.get("call_id")
        if path is None:
            return
        invalidate(_scope(event), path, "historical_write_unconfirmed")
        content = _input(event).get("content")
        if not isinstance(content, str):
            content = None
        if isinstance(call_id, str) and call_id:
            identity = (_scope(event), call_id)
            previous = pending.get(identity)
            if previous and (previous.path != path or previous.content != content):
                invalidate(previous.scope, previous.path, "historical_write_contradictory")
                pending_conflicts.add(identity)
            pending[identity] = _PendingWrite(_scope(event), path, content, call_id,
                                              dict(_source(event)), raw_path)

    def record_result(event: dict[str, Any]) -> None:
        data = _data(event)
        call_id = data.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        identity = (_scope(event), call_id)
        write = pending.pop(identity, None)
        text = _text(data.get("content"))
        if identity in ambiguous_ids:
            if write:
                invalidate(write.scope, write.path, "historical_write_contradictory")
            return
        if write is None:
            previous = completed.get(identity)
            if previous and ((not _event_result_success(data)) or not _confirmation_path(text, previous[1] or "", normalize_path,
                                                                                         str(event.get("cwd") or ""))):
                invalidate(identity[0], previous[1] or "", "historical_write_contradictory")
            return
        if identity in pending_conflicts:
            pending_conflicts.discard(identity)
            invalidate(write.scope, write.path, "historical_write_contradictory")
            completed[identity] = ("failed", write.path, "")
            return
        if write.content is None or not _event_result_success(data):
            invalidate(write.scope, write.path, "historical_write_failed")
            completed[identity] = ("failed", write.path, "")
            return
        if not _confirmation_path(text, write.path, normalize_path, str(event.get("cwd") or "")):
            invalidate(write.scope, write.path, "historical_write_contradictory")
            completed[identity] = ("failed", write.path, "")
            return
        version = P.fingerprint(key, write.content)
        active.setdefault(write.scope, {})[write.path] = _Version(
            path=write.path, content=write.content, write_source=write.source,
            confirmation_source=dict(_source(event)), version=version,
        )
        known_paths.setdefault(write.scope, set()).add(write.path)
        invalid.setdefault(write.scope, {}).pop(write.path, None)
        completed[identity] = ("success", write.path, version)

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind == "appel":
            data = _data(event)
            call_id = data.get("call_id")
            call_id = call_id if isinstance(call_id, str) and call_id else None
            identity = (_scope(event), call_id)
            signature = json.dumps([data.get("name"), data.get("input")], sort_keys=True)
            previous = signatures.get(identity)
            if call_id:
                signatures.setdefault(identity, signature)
                if previous is not None and previous != signature:
                    ambiguous_ids.add(identity)
            if previous != signature:
                record_write(event)
            if identity in ambiguous_ids:
                write = pending.get(identity)
                old = completed.get(identity)
                for path in (write.path if write else None, old[1] if old else None):
                    if path:
                        invalidate(_scope(event), path, "historical_write_contradictory")
            name = str(data.get("name") or "").casefold()
            if name == "edit":
                path, _ = _script_path(event, normalize_path)
                if path:
                    invalidate(_scope(event), path, "historical_edit_invalidated")
            input_data = _input(event)
            command = input_data.get("command")
            if not isinstance(command, str):
                continue
            scope = _scope(event)
            for path in known_paths.get(scope, set()):
                if _mutation_references(command, path):
                    invalidate(scope, path, "historical_shell_mutation")
            candidates, malformed = _shell_candidates(command)
            if not candidates:
                continue
            source = _source(event)
            call_id = data.get("call_id")
            ref = _source_key(source)
            cwd = str(event.get("cwd") or "")
            if malformed:
                candidate = candidates[0]
                output[ref] = _result(candidate, source, call_id, status="unresolved",
                                       reason="malformed_shell_command",
                                       candidates=[_candidate(c, c.script, normalize_path, cwd, call_id, source) for c in candidates])
                continue
            if len(candidates) != 1:
                first = candidates[0]
                output[ref] = _result(first, source, call_id, status="unresolved",
                                       reason="multiple_shell_invocations",
                                       candidates=[_candidate(c, c.script, normalize_path, cwd, call_id, source) for c in candidates])
                continue
            invocation = candidates[0]
            normalized_script = _normalise(normalize_path, invocation.script, cwd) if invocation.script is not None else None
            candidate_info = [_candidate(invocation, invocation.script, normalize_path, cwd, call_id, source)]
            if invocation.reason:
                output[ref] = _result(invocation, source, call_id, status="unresolved", reason=invocation.reason,
                                       candidates=candidate_info, normalized_script=normalized_script)
                continue
            if normalized_script is None:
                output[ref] = _result(invocation, source, call_id, status="unresolved", reason="script_path_unresolved",
                                       candidates=candidate_info)
                continue
            version = active.get(scope, {}).get(normalized_script)
            if version is None:
                reason = invalid.get(scope, {}).get(normalized_script, "no_historical_script_version")
                output[ref] = _result(invocation, source, call_id, status="unresolved", reason=reason,
                                       candidates=candidate_info, normalized_script=normalized_script)
                continue
            wrapper = _parse_wrapper(version.content, invocation.arguments)
            if wrapper.reason:
                evidence = [_source_ref("invocation", source), _source_ref("definition_write", version.write_source),
                            _source_ref("definition_confirmation", version.confirmation_source),
                            {"kind": "script_version", "version_hmac": version.version,
                             "version": version.version, "version_basis": "privacy.fingerprint(masked_recorded_script)"},
                            {"kind": "definition", "script": normalized_script, "lines": wrapper.definition_lines}]
                output[ref] = _result(invocation, source, call_id, status="unresolved", reason=wrapper.reason,
                                       evidence=evidence, candidates=candidate_info, normalized_script=normalized_script)
                continue
            target_command = "cd " + _shell_quote(wrapper.cd or "") + " && " + (wrapper.launch or "")
            if re.search(r"\|\s*(?:head|tail|cut|grep)\b", command, re.I):
                target_command += " | cut -c1-3000"
            try:
                raw_target = target_for_command(target_command, cwd)
            except (OSError, ValueError, RuntimeError, TypeError):
                raw_target = None
            target = _target_shape(raw_target)
            if target is None or target.get("mode") != "launch":
                output[ref] = _result(invocation, source, call_id, status="unresolved", reason="target_unresolved",
                                       evidence=_version_evidence(version, source, wrapper, normalized_script, invocation.arguments),
                                       candidates=candidate_info, normalized_script=normalized_script)
                continue
            output[ref] = _result(invocation, source, call_id, status="established", reason="historical_static_wrapper",
                                   target=target,
                                   evidence=_version_evidence(version, source, wrapper, normalized_script, invocation.arguments),
                                   candidates=candidate_info, normalized_script=normalized_script)
        elif kind == "resultat":
            record_result(event)
    return output


__all__ = ["resolve_shell_calls"]
