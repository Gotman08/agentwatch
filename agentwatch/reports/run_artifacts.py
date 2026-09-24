"""Resolve recorded Python runner paths without opening the observed project.

The resolver is deliberately narrower than a Python interpreter.  It joins
only successful, identifier-matched results for a runner file that was read
from the supplied event stream, and it never imports, evaluates, or opens the
runner.  The one supported mapping is::

    ROOT = pathlib.Path(__file__).resolve().parents[3]
    out = ROOT / 'Saved' / 'NYK175' / 'Runs' / args.label

The definitions may be split across recorded ``Read`` or literal ``sed`` /
``cat`` calls.  All joins are scoped to the transcript file and thread and are
cut at the physical launch source.
"""
from __future__ import annotations

import posixpath
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from agentwatch.collector import privacy as P
from agentwatch.reports.run_commands import command_segments
from agentwatch.reports.run_shell import _lex_simple, _strip_redirection


PathNormalizer = Callable[..., str]
TextRenderer = Callable[[Any], str]

RULE = "recorded_python_root_and_out_args_label"
LIMITS = [
    "Only recorded Read/sed/cat results at or before the launch are used; the live project file is never opened, imported, or executed.",
    "External edits between or after recorded events are unobservable.",
    "Recorded snippets are partial; the HMAC covers recorded fragments only, not the complete runner file.",
]

_ROOT_RE = re.compile(
    r"^\s*ROOT\s*=\s*pathlib\.Path\(__file__\)\.resolve\(\)\.parents\[\s*(?P<parent>\d+)\s*\]\s*(?:#.*)?$"
)
_ROOT_ASSIGN_RE = re.compile(r"^\s*ROOT\s*=")
_OUT_ASSIGN_RE = re.compile(r"^\s*out\s*=")
_OUT_PREFIX_RE = re.compile(r"^\s*out\s*=\s*ROOT")
_STRING_SEGMENT = r"(?:'[^'\\\r\n]*'|\"[^\"\\\r\n]*\")"
_OUT_RE = re.compile(
    rf"^\s*out\s*=\s*ROOT(?P<parts>(?:\s*/\s*{_STRING_SEGMENT})+)\s*/\s*args\.label\s*(?:#.*)?$"
)
_TRUNCATED_RE = re.compile(
    r"(?is)(?:<tool_use_error\b|<truncated\s*/?>|output\s+truncated|"
    r"\[?output\s+truncated\]?|ellipsis\s+omitted|truncated\s+output)"
)
_PARTIAL_PIPE_RE = re.compile(r"(?i)(?:^|\|)\s*(?:head|tail|cut)\b")
_MUTATION_RE = re.compile(
    r"(?ix)"
    r"\b(?:sed|perl)\b[^;\n]*(?:\s-i(?:\s|$)|--in-place|-[^;\n]*p[i])"
    r"|\b(?:tee|truncate|rm|mv|cp|install|apply_patch|patch)\b"
    r"|\bgit\s+apply\b"
    r"|\bpython(?:3(?:\.\d+)?)?(?:\.exe)?\b[^;\n]*(?:-c\b|-[^;\n]*<<|write_text\b|read_text\b|open\s*\(|replace\s*\(|shutil\b|pathlib\b)"
    r"|(?:^|[;\n])\s*(?:cat|echo|printf)\b[^;\n]*(?:>>?|\s-\s)"
)


def _point(source: dict[str, Any]) -> tuple[int, int]:
    """Return a comparable physical source point."""

    line = source.get("line")
    block = source.get("block")
    try:
        line_value = int(line)
    except (TypeError, ValueError):
        line_value = -1
    try:
        block_value = int(block) if block is not None else 0
    except (TypeError, ValueError):
        block_value = 0
    return line_value, block_value


def _source_file(source: dict[str, Any]) -> str | None:
    value = source.get("path")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.replace("\\", "/").casefold()


def _source_key(source: dict[str, Any]) -> tuple[Any, Any, Any]:
    return source.get("path"), source.get("line"), source.get("block")


def _scope(event: dict[str, Any]) -> tuple[Any, str | None]:
    return event.get("thread"), _source_file(event.get("source") or {})


def _data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def _input(event: dict[str, Any]) -> dict[str, Any]:
    value = _data(event).get("input")
    return value if isinstance(value, dict) else {}


def _call_id(event: dict[str, Any]) -> str | None:
    value = _data(event).get("call_id")
    return value if isinstance(value, str) and value else None


def _normalise(normalize_path: PathNormalizer, value: Any, cwd: str = "") -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        try:
            result = normalize_path(value, cwd)
        except TypeError:
            result = normalize_path(value, cwd=cwd)
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    return result if isinstance(result, str) and result else None


def _render(text_of: TextRenderer, value: Any) -> str:
    try:
        rendered = text_of(value)
    except (TypeError, ValueError, RuntimeError):
        return ""
    return rendered if isinstance(rendered, str) else ""


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        key = _source_key(source)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(source))
    return result


def _literal_parts(match: re.Match[str]) -> list[str] | None:
    """Extract the already-validated literal path segments from ``out``."""

    parts: list[str] = []
    tail = match.group("parts")
    cursor = 0
    segment_re = re.compile(rf"\s*/\s*(?P<literal>{_STRING_SEGMENT})")
    while cursor < len(tail):
        item = segment_re.match(tail, cursor)
        if not item:
            return None
        literal = item.group("literal")
        # The expression is intentionally narrower than literal_eval: no
        # escape processing, calls, concatenation, or interpolation.
        value = literal[1:-1]
        if not value or any(char in value for char in "\x00\r\n"):
            return None
        parts.append(value)
        cursor = item.end()
    return parts or None


def _assignment_lines(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, list[str]]], bool]:
    """Find safe assignments and flag unsupported assignment-like lines.

    The scanner only examines lines which begin with an assignment.  It tracks
    triple-quoted strings so a docstring example cannot become a definition.
    A dynamic assignment is a reason to reject the fragment rather than an
    invitation to infer a replacement expression.
    """

    roots: list[tuple[int, int]] = []
    outs: list[tuple[int, list[str]]] = []
    unsupported = False
    triple: str | None = None

    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        in_triple = triple is not None
        if not in_triple:
            root_match = _ROOT_RE.fullmatch(line)
            if root_match:
                roots.append((number, int(root_match.group("parent"))))
            elif _ROOT_ASSIGN_RE.match(line):
                unsupported = True
            out_match = _OUT_RE.fullmatch(line)
            if out_match:
                parts = _literal_parts(out_match)
                if parts is None:
                    unsupported = True
                else:
                    outs.append((number, parts))
            elif _OUT_ASSIGN_RE.match(line):
                # A line which starts with an executable-looking ``out``
                # assignment but is not the safe expression is unsupported.
                unsupported = True

        # Track only triple quotes outside comments and ordinary quoted text.
        # This is not a Python lexer; it is a conservative guard against
        # treating documentation/examples as executable assignments.
        index = 0
        quote: str | None = None
        escaped = False
        while index < len(line):
            char = line[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if char == "\\" and quote != "'":
                escaped = True
                index += 1
                continue
            if triple:
                if line.startswith(triple, index):
                    triple = None
                    index += 3
                else:
                    index += 1
                continue
            if quote:
                if char == quote:
                    quote = None
                index += 1
                continue
            if char == "#":
                break
            if char in "'\"":
                if line.startswith(char * 3, index):
                    triple = char * 3
                    index += 3
                    continue
                quote = char
            index += 1

    return roots, outs, unsupported


@dataclass
class _Fragment:
    path: str
    scope: tuple[Any, str | None]
    call: dict[str, Any]
    result: dict[str, Any]
    text: str
    partial: bool
    truncated: bool


_AMBIGUOUS_THREAD = object()


class RunArtifactResolver:
    """Evidence-only resolver returned by :func:`resolve_run_artifacts`."""

    def __init__(self, events: list[dict[str, Any]], key: bytes, *, normalize_path: PathNormalizer,
                 text_of: TextRenderer):
        try:
            self.events = list(events)
        except TypeError:
            self.events = []
        self.key = key
        self.normalize_path = normalize_path
        self.text_of = text_of

    def _launch_thread(self, source: dict[str, Any]) -> Any:
        explicit = source.get("thread")
        if explicit is not None:
            return explicit
        path = _source_file(source)
        point = _point(source)
        threads = {
            event.get("thread")
            for event in self.events
            if isinstance(event, dict)
            and _source_file(event.get("source") or {}) == path
            and _point(event.get("source") or {}) == point
        }
        if len(threads) == 1:
            return next(iter(threads))
        return _AMBIGUOUS_THREAD if len(threads) > 1 else None

    def _prefix(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        path = _source_file(source)
        point = _point(source)
        if path is None or point[0] < 0:
            return []
        thread = self._launch_thread(source)
        if thread is _AMBIGUOUS_THREAD:
            return []
        events: list[tuple[int, dict[str, Any]]] = []
        for order, event in enumerate(self.events):
            if not isinstance(event, dict):
                continue
            event_source = event.get("source")
            if not isinstance(event_source, dict) or _source_file(event_source) != path:
                continue
            if _point(event_source) > point:
                continue
            if thread is not None and event.get("thread") != thread:
                continue
            events.append((order, event))
        events.sort(key=lambda pair: (_point(pair[1].get("source") or {}), pair[0]))
        return [event for _, event in events]

    def _read_path(self, event: dict[str, Any]) -> tuple[str | None, bool]:
        input_data = _input(event)
        raw = next((input_data.get(name) for name in ("file_path", "path", "filename")
                    if isinstance(input_data.get(name), str)), None)
        if raw is None:
            return None, False
        path = _normalise(self.normalize_path, raw, str(event.get("cwd") or ""))
        partial = bool(input_data.get("offset") or input_data.get("limit"))
        return path, partial

    def _command_paths(self, event: dict[str, Any]) -> list[tuple[str, bool]]:
        """Return literal sed/cat target paths, retaining command partiality."""

        command = _input(event).get("command")
        if not isinstance(command, str) or not command.strip():
            return []
        cwd = str(event.get("cwd") or "")
        paths: list[tuple[str, bool]] = []
        try:
            segments = command_segments(command)
        except (TypeError, ValueError, RuntimeError):
            return []
        for segment in segments:
            tokens = _lex_simple(_strip_redirection(segment.text))
            if not tokens:
                continue
            name = tokens[0].value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            if name == "cd":
                if len(tokens) >= 2 and not any(char in tokens[1].value for char in "$`"):
                    new_cwd = _normalise(self.normalize_path, tokens[1].value, cwd)
                    if new_cwd:
                        cwd = new_cwd
                continue
            if name not in {"sed", "cat"} or tokens[0].quoted:
                continue
            values = [token.value for token in tokens[1:]]
            if name == "sed":
                # Literal sed reads use a final path after a range/expression.
                candidates = [token for token in tokens[1:] if token.value and not token.value.startswith("-")]
                if not candidates:
                    continue
                candidates = candidates[-1:]
            else:
                candidates = [token for token in tokens[1:] if token.value and not token.value.startswith("-")]
            partial = bool(_PARTIAL_PIPE_RE.search(command))
            for token in candidates:
                if any(char in token.value for char in "$`\n"):
                    continue
                path = _normalise(self.normalize_path, token.value, cwd)
                if path:
                    paths.append((path, partial))
        return paths

    def _is_mutating_command(self, command: str, cwd: str, script: str) -> bool:
        try:
            segments = command_segments(command)
        except (TypeError, ValueError, RuntimeError):
            segments = []
        current = cwd
        mutation_cwds: list[str] = []
        for segment in segments:
            tokens = _lex_simple(_strip_redirection(segment.text))
            if not tokens:
                continue
            name = tokens[0].value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            if name == "cd" and len(tokens) >= 2 and not any(char in tokens[1].value for char in "$`"):
                new_cwd = _normalise(self.normalize_path, tokens[1].value, current)
                if new_cwd:
                    current = new_cwd
                continue
            # A compound command may mutate one unrelated file and launch the
            # runner in another segment.  Keep the mutation test local to the
            # segment which names the candidate path.
            if not _MUTATION_RE.search(segment.text):
                continue
            mutation_cwds.append(current)
            for token in tokens:
                value = token.value
                if any(char in value for char in "$`\n"):
                    continue
                candidate = _normalise(self.normalize_path, value, current)
                if candidate == script:
                    return True
            # Python -c and Path('...').write_text forms keep a relative path
            # inside one token; inspect only full path-like quoted literals.
            for match in re.finditer(r"[\"']([^\"']+\.(?:py|sh|txt))[\"']", segment.text, re.I):
                candidate = _normalise(self.normalize_path, match.group(1), current)
                if candidate == script:
                    return True
            for match in re.finditer(r"\*\*\*\s+(?:Update|Delete|Add)\s+File:\s*([^\s\r\n]+)", segment.text, re.I):
                candidate = _normalise(self.normalize_path, match.group(1).strip("'\""), current)
                if candidate == script:
                    return True
            for match in re.finditer(r"(?:>>?|<)\s*([\"']?)([^\s>\r\n]+)\1", segment.text):
                candidate = _normalise(self.normalize_path, match.group(2), current)
                if candidate == script:
                    return True
        # ``command_segments`` intentionally removes heredoc bodies because
        # they are not outer shell commands.  A recorded Python heredoc can
        # still mutate the runner, so inspect only its quoted path literals;
        # this remains path-exact and does not treat a basename as a match.
        if _MUTATION_RE.search(command):
            for mutation_cwd in mutation_cwds or [current]:
                for match in re.finditer(r"[\"']([^\"']+\.(?:py|sh|txt))[\"']", command, re.I):
                    candidate = _normalise(self.normalize_path, match.group(1), mutation_cwd)
                    if candidate == script:
                        return True
            for match in re.finditer(r"\*\*\*\s+(?:Update|Delete|Add)\s+File:\s*([^\s\r\n]+)", command, re.I):
                for mutation_cwd in mutation_cwds or [current]:
                    candidate = _normalise(self.normalize_path, match.group(1).strip("'\""), mutation_cwd)
                    if candidate == script:
                        return True
        # Absolute forms can be escaped or represented with a Git-Bash drive.
        forms = {script.replace("\\", "/").casefold()}
        drive = re.fullmatch(r"([a-z]):/(.*)", script.replace("\\", "/").casefold())
        if drive:
            forms.add("/" + drive.group(1) + "/" + drive.group(2))
        for segment in segments:
            if _MUTATION_RE.search(segment.text):
                normalized_segment = segment.text.replace("\\", "/").casefold()
                if any(form in normalized_segment for form in forms if form):
                    return True
        return False

    def _candidate_call(self, event: dict[str, Any]) -> tuple[str | None, bool]:
        """Return one exact path for a supported recorded read call."""

        data = _data(event)
        name = str(data.get("name") or "").casefold()
        if name == "read":
            path, partial = self._read_path(event)
            return path, partial
        if name in {"bash", "shell", "powershell", "cmd", "terminal"}:
            paths = self._command_paths(event)
            if len(paths) == 1:
                return paths[0]
        # Historical Claude Bash calls use name="Bash"; accept any tool name
        # when the command itself contains one unambiguous literal sed/cat.
        if isinstance(_input(event).get("command"), str):
            paths = self._command_paths(event)
            if len(paths) == 1:
                return paths[0]
        return None, False

    @staticmethod
    def _call_signature(event: dict[str, Any]) -> str:
        data = _data(event)
        try:
            return json.dumps([data.get("name"), data.get("input")], sort_keys=True,
                              ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return repr([data.get("name"), data.get("input")])

    def _fragments(self, script: str, source: dict[str, Any]) -> tuple[list[_Fragment], bool]:
        prefix = self._prefix(source)
        if not prefix:
            return [], False
        pending: dict[tuple[tuple[Any, str | None], str], tuple[dict[str, Any], bool]] = {}
        seen_calls: dict[tuple[tuple[Any, str | None], str], str] = {}
        conflicted: set[tuple[tuple[Any, str | None], str]] = set()
        results: dict[tuple[tuple[Any, str | None], str], list[tuple[dict[str, Any], str, bool, bool]]] = {}
        invalid = False

        for event in prefix:
            kind = event.get("kind")
            scope = _scope(event)
            if kind != "appel":
                if kind == "resultat":
                    call_id = _call_id(event)
                    if call_id is None:
                        continue
                    key = (scope, call_id)
                    if key not in pending:
                        continue
                    data = _data(event)
                    text = _render(self.text_of, data.get("content"))
                    truncated = bool(data.get("truncated") or _TRUNCATED_RE.search(text))
                    successful = data.get("is_error") is not True and not truncated
                    partial = bool(data.get("partial"))
                    if successful and text:
                        results.setdefault(key, []).append((event, text, False, partial))
                    else:
                        results.setdefault(key, []).append((event, text, True, partial))
                continue

            data = _data(event)
            call_id = _call_id(event)
            input_data = _input(event)
            name = str(data.get("name") or "").casefold()
            direct_path = next((input_data.get(item) for item in ("file_path", "path", "filename")
                                if isinstance(input_data.get(item), str)), None)
            touches = False
            if direct_path is not None:
                touches = _normalise(self.normalize_path, direct_path, str(event.get("cwd") or "")) == script
            if name in {"write", "edit", "multiedit", "notebookedit", "applypatch", "patch"} and touches:
                invalid = True
            command = input_data.get("command")
            if isinstance(command, str) and self._is_mutating_command(command, str(event.get("cwd") or ""), script):
                invalid = True
            if call_id is None:
                continue
            identity = (scope, call_id)
            signature = self._call_signature(event)
            previous_signature = seen_calls.get(identity)
            if previous_signature is not None and previous_signature != signature:
                # A call ID is a join key only when every recorded call with
                # that scope/ID has the identical payload.  Unsupported calls
                # participate in this check too: otherwise an ``echo`` call
                # could be silently ignored before a runner Read reuses its ID.
                conflicted.add(identity)
                pending.pop(identity, None)
                continue
            if identity in conflicted:
                continue
            seen_calls.setdefault(identity, signature)
            path, partial = self._candidate_call(event)
            if path is None:
                continue
            if path == script:
                # Identical duplicate call copies retain the first physical
                # call as the owner, so a result cannot be joined backward to
                # a later copy of that call.
                pending.setdefault(identity, (event, partial))

        fragments: list[_Fragment] = []
        for key, (call, partial) in pending.items():
            if key in conflicted:
                continue
            rows = results.get(key, [])
            successful = [row for row in rows if row[1] and not row[2]]
            failed = any(not row[1] or row[2] for row in rows)
            # A failed or truncated paired result makes the recorded version
            # ambiguous.  Do not let a later duplicate result repair it.
            if failed or len(successful) != 1:
                continue
            result, text, _, result_partial = successful[0]
            fragments.append(_Fragment(
                path=script, scope=key[0], call=call, result=result, text=text,
                partial=partial or result_partial, truncated=False,
            ))
        fragments.sort(key=lambda item: (_point(item.call.get("source") or {}), _point(item.result.get("source") or {})))
        return fragments, invalid

    def resolve(self, script: str, label: str, source: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve one launch's runner script to its recorded output folder."""

        if not isinstance(source, dict) or not isinstance(label, str) or not label.strip():
            return None
        if any(char in label for char in "/\\\x00\r\n$`") or label in {".", ".."}:
            return None
        normalized_script = _normalise(self.normalize_path, script, "")
        if normalized_script is None:
            return None
        fragments, invalid = self._fragments(normalized_script, source)
        if invalid or not fragments:
            return None

        root_values: list[tuple[_Fragment, int, int]] = []
        out_values: list[tuple[_Fragment, int, list[str]]] = []
        unsupported = False
        contributing: set[int] = set()
        for fragment in fragments:
            roots, outs, bad = _assignment_lines(fragment.text)
            unsupported = unsupported or bad
            for line, parent in roots:
                root_values.append((fragment, line, parent))
                contributing.add(id(fragment))
            for line, parts in outs:
                out_values.append((fragment, line, parts))
                contributing.add(id(fragment))
        if unsupported or not root_values or not out_values:
            return None
        # Reads of the same file which contain no supported definition are
        # retained for neither the proof refs nor the fragment HMAC.  This
        # keeps the edge grounded in the exact ROOT/out lines rather than in
        # incidental grep/cat output from the same command family.
        used_fragments = [fragment for fragment in fragments if id(fragment) in contributing]

        parent_values = {item[2] for item in root_values}
        path_values = {tuple(item[2]) for item in out_values}
        if len(parent_values) != 1 or len(path_values) != 1:
            return None
        parent_index = next(iter(parent_values))
        parts = list(next(iter(path_values)))
        if parent_index < 0:
            return None

        root_fragment, root_line, _ = root_values[0]
        out_fragment, out_line, _ = out_values[0]
        root_point = (_point(root_fragment.result.get("source") or {}), root_line)
        out_point = (_point(out_fragment.result.get("source") or {}), out_line)
        if root_point > out_point:
            return None
        if root_fragment is out_fragment and root_line >= out_line:
            return None

        try:
            root_path = PurePosixPath(normalized_script).parents[parent_index]
        except (IndexError, TypeError):
            return None
        raw_artifact = posixpath.join(str(root_path), *parts, label)
        artifact = _normalise(self.normalize_path, raw_artifact, "")
        if artifact is None:
            return None

        recorded = "\n".join(fragment.text for fragment in used_fragments)
        sources: list[dict[str, Any]] = [dict(source)]
        for fragment in used_fragments:
            sources.extend([fragment.call.get("source") or {}, fragment.result.get("source") or {}])
        return {
            "artifact": artifact,
            "sources": _dedupe_sources(sources),
            "rule": RULE,
            "version": P.fingerprint(self.key, recorded),
            "limits": list(LIMITS),
        }


def resolve_run_artifacts(events: list[dict[str, Any]], key: bytes, *,
                          normalize_path: PathNormalizer, text_of: TextRenderer) -> RunArtifactResolver:
    """Build an evidence-only runner-to-artifact resolver."""

    return RunArtifactResolver(events, key, normalize_path=normalize_path, text_of=text_of)


__all__ = ["RunArtifactResolver", "resolve_run_artifacts"]
