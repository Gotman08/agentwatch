"""Extract conservative facts from the historical run watcher outputs.

The public contract is :func:`parse_facts`.  It is a pure parser: it never
opens a file, imports a script from the observed project, or executes a
command.  The result is JSON-serialisable and has exactly these top-level
keys::

    {"facts": [{"field": str, "value": JSON, "role": str | None,
                "at": float | None,  # stream facts also carry ``phase``
                }],
     "unparsed_lines": int, "truncated": bool}

Facts keep the order in which the parser sees them.  Semantic de-duplication
and provenance belong to the caller.  ``at`` is the scenario-relative time
when the source line contains one.  ``stream.<key>`` fields retain the names
used by ``t5_view``; a numeric ``maxFrame=...ms`` token is exposed as
``stream.maxFrame_ms``.  A map/admission arrival is represented by a small
JSON object, while an event which cannot be classified safely is retained as
an ``event_text`` fact.  An observed scenario ``END`` marker is only a
``run_end`` fact; the separate ``run finished`` watcher line is
``watcher_end``.  Neither implies success.  Likewise, an exit code of zero is
recorded only when the source explicitly writes it.

The parser accepts the two historical line formats (``Monitor`` and
``t5_view``), complete JSON summary objects, and the ``event``, ``status``
and explicit ``exit_code`` part of XML task notifications.  Text outside
those XML elements is deliberately ignored.  The parser recognises the
truncation markers ``<truncated>``, ``output truncated`` and
``ellipsis omitted`` (plus their common ellipsis/token variants): visible
facts are retained, but ``truncated`` is true and completeness is not
claimed.  Unknown non-empty selected lines increment ``unparsed_lines``;
blank lines and XML wrapper/instruction text do not.
"""

from __future__ import annotations

import json
import re
from typing import Any


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NUMBER_RE = re.compile(rf"\A{_NUMBER}\Z")
_TRUNCATED_RE = re.compile(
    r"(?is)<truncated>|output\s+truncated|ellipsis\s+omitted|"
    r"(?:\.\.\.|…)\s*(?:\[?\d+\s*)?(?:lines?|lignes?|chars?|caracteres?|tokens?)?\s*"
    r"(?:truncated|omitted|tronque)"
)
_ROLE_HEADER_RE = re.compile(r"^\s*=+\s*(?P<role>[^=\s][^\r\n]*?)\s*$")
_MONITOR_EVENT_RE = re.compile(
    rf"^\s*(?P<role>\S+)(?:\s+(?P<at>{_NUMBER}|\.\.\.))?\s+"
    r"(?P<kind>hitch|ok|fail|end|stream|pending)\b(?P<detail>.*)$", re.IGNORECASE
)
_T5_EVENT_RE = re.compile(
    rf"^\s*(?P<at>{_NUMBER}|\.\.\.)\s+(?P<kind>hitch|ok|fail|end|stream|pending)\b(?P<detail>.*)$",
    re.IGNORECASE,
)
_HITCH_RE = re.compile(rf"\bhitch\s+(?P<ms>{_NUMBER})\s*ms\b", re.IGNORECASE)
_MAP_ARRIVAL_RE = re.compile(
    rf"\bcarte\s+(?P<map>\S+)\s+apres\s*(?P<after>{_NUMBER})\s*s?"
    r"(?:\s*\((?P<mode>[^)]*)\))?",
    re.IGNORECASE,
)
_ADMISSION_RE = re.compile(
    rf"\barrive(?:e|ée)\s+admise\b.*?\bapres\s*(?P<after>{_NUMBER})\s*s?",
    re.IGNORECASE,
)
_STREAM_TOKEN_RE = re.compile(r"(?P<key>[A-Za-z][A-Za-z0-9_.-]*)=(?P<value>[^\s]+)")
_COUNT_RE = re.compile(
    rf"(?:\b(?:failures|failed|tests_failed)\s*[:=]\s*(?P<value_kw>{_NUMBER})\b|"
    rf"\b(?P<value_fr>{_NUMBER})\s+echec(?:s|\(s\))?)",
    re.IGNORECASE,
)
_SUMMARY_EXIT_RE = re.compile(
    rf"(?:[\"']?exit(?:_code|\s+code)[\"']?|ExitCode)"
    rf"(?:\s*[:=]\s*|\s+)(?P<value>{_NUMBER})\b",
    re.IGNORECASE,
)
_XML_BLOCK_RE = re.compile(
    r"(?is)<(?P<tag>event|status|summary)\b[^>]*>(?P<body>.*?)</(?P=tag)\s*>"
)


def _fact(field: str, value: Any, role: str | None, at: float | None) -> dict[str, Any]:
    return {"field": field, "value": value, "role": role, "at": at}


def _parse_at(value: str | None) -> float | None:
    if not value or value == "...":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _scalar(value: str) -> Any:
    """Parse a scalar used by the human-oriented ``key=value`` lines.

    Unit suffixes remain represented by the field name where that distinction
    matters.  Other unknown values stay strings rather than being guessed.
    """

    value = value.strip().rstrip(",;)]")
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    if _NUMBER_RE.fullmatch(value):
        try:
            return int(value) if re.fullmatch(r"[+-]?\d+", value) else float(value)
        except ValueError:
            pass
    if value.startswith(("\"", "[", "{")):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            pass
    return value


def _summary_facts(obj: dict[str, Any], role: str | None = None, at: float | None = None) -> list[dict[str, Any]]:
    """Turn only explicitly named summary keys into facts."""

    obj_role = obj.get("role") if isinstance(obj.get("role"), str) else role
    obj_at = at
    for key in ("at", "t"):
        candidate = obj.get(key)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            obj_at = float(candidate)
            break
    facts: list[dict[str, Any]] = []
    for key in ("failures", "failed", "tests_failed"):
        if key in obj:
            value = obj[key]
            # A failures array is detail, not a numeric count.  Treating it
            # as ``tests_failed`` would turn an observed list into a made-up
            # quantity and would also make empty lists look like zero tests.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                facts.append(_fact("tests_failed", value, obj_role, obj_at))
            else:
                facts.append(_fact("failure_details", value, obj_role, obj_at))
    if "failure_details" in obj:
        facts.append(_fact("failure_details", obj["failure_details"], obj_role, obj_at))
    if "end" in obj:
        facts.append(_fact("run_end", obj["end"], obj_role, obj_at))
    if "ended" in obj:
        facts.append(_fact("run_end", obj["ended"], obj_role, obj_at))
    if "status" in obj:
        facts.append(_fact("process_status", obj["status"], obj_role, obj_at))
    if "exit_code" in obj:
        facts.append(_fact("process_exit", obj["exit_code"], obj_role, obj_at))
    if "exitcode" in obj:
        facts.append(_fact("process_exit", obj["exitcode"], obj_role, obj_at))
    return facts


def _summary_from_text(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _stream_facts(detail: str, role: str | None, at: float | None) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    phase: str | None = None
    # ``t5_view`` writes a phase label immediately before its first
    # key/value metric (for example ``carte_chargee prof=...``).  Keep that
    # label so two samples rounded to the same scenario time remain distinct.
    for token in detail.split():
        if "=" in token:
            break
        if phase is None:
            phase = token
    for match in _STREAM_TOKEN_RE.finditer(detail):
        key = match.group("key")
        raw = match.group("value")
        unit = ""
        if raw.lower().endswith("ms") and _NUMBER_RE.fullmatch(raw[:-2]):
            raw = raw[:-2]
            unit = "_ms"
        field = f"stream.{key}{unit}"
        fact = _fact(field, _scalar(raw), role, at)
        fact["phase"] = phase
        facts.append(fact)
    return facts


def _event_facts(line: str, current_role: str | None) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Parse one selected event line.

    The boolean reports whether the line was a recognised event/context line;
    it is separate from the returned facts because a ``STREAM`` line may be
    valid while containing no key/value metrics.
    """

    stripped = line.strip()
    if not stripped:
        return [], True, current_role

    header = _ROLE_HEADER_RE.match(stripped)
    if header:
        role = header.group("role").strip()
        return [], True, role or current_role

    if re.fullmatch(r"run\s+finished", stripped, re.IGNORECASE):
        # This line is emitted by ``watch_run.py`` after it stops polling.  It
        # does not establish that every scenario/process role ended.
        return [_fact("watcher_end", True, None, None)], True, current_role

    # A t5_view line starts with the scenario timestamp and inherits the role
    # from its header.  Try it first so that ``169.8 HITCH ...`` is not
    # mistaken for a Monitor line whose role happens to be ``169.8``.
    match = _T5_EVENT_RE.match(stripped)
    role = current_role
    at: float | None = None
    kind: str | None = None
    detail = ""
    if match:
        at = _parse_at(match.group("at"))
        kind = match.group("kind").lower()
        detail = match.group("detail").strip()
    else:
        match = _MONITOR_EVENT_RE.match(stripped)
        if match:
            role = match.group("role")
            at = _parse_at(match.group("at"))
            kind = match.group("kind").lower()
            detail = match.group("detail").strip()

    if not match or kind is None:
        return [], False, current_role

    if kind == "hitch":
        hitch = _HITCH_RE.search(f"hitch {detail}")
        if hitch:
            return [_fact("hitch_ms", _scalar(hitch.group("ms")), role, at)], True, current_role
        return [_fact("event_text", stripped, role, at)], True, current_role

    if kind == "stream":
        return _stream_facts(detail, role, at), True, current_role

    if kind == "pending":
        facts = _stream_facts(detail, role, at)
        for fact in facts:
            fact["field"] = fact["field"].replace("stream.", "pending.", 1)
        return facts, True, current_role

    if kind == "ok":
        map_arrival = _MAP_ARRIVAL_RE.search(detail)
        if map_arrival:
            mode = map_arrival.group("mode")
            value = {
                "kind": "map_loaded",
                "map": map_arrival.group("map").rstrip(",;"),
                "after_s": float(map_arrival.group("after")),
                "mode": mode.strip() if mode is not None else None,
            }
            return [_fact("arrival", value, role, at)], True, current_role
        admission = _ADMISSION_RE.search(detail)
        if admission:
            return [_fact("arrival", {"kind": "arrival_admitted", "after_s": float(admission.group("after"))}, role, at)], True, current_role
        return [_fact("event_text", stripped, role, at)], True, current_role

    if kind == "end":
        facts = [_fact("run_end", True, role, at)]
        for counter in _COUNT_RE.finditer(detail):
            value = counter.group("value_kw") or counter.group("value_fr")
            facts.append(_fact("tests_failed", _scalar(value), role, at))
        # Retain end details when they carry text beyond the marker.  The
        # marker itself is represented by run_end and never means success.
        if detail and detail not in {"...", "."} and not _COUNT_RE.fullmatch(detail):
            facts.append(_fact("event_text", stripped, role, at))
        return facts, True, current_role

    # A FAIL event is evidence of this event, never a terminal test count:
    # a later END can report zero in these historical scenarios.
    return [_fact("failure_event", detail, role, at)], True, current_role


def _process_lines(lines: list[str], *, initial_role: str | None = None) -> tuple[list[dict[str, Any]], int]:
    facts: list[dict[str, Any]] = []
    unparsed = 0
    role = initial_role
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        obj = _summary_from_text(stripped)
        if obj is not None:
            summary = _summary_facts(obj)
            facts.extend(summary)
            if not summary:
                unparsed += 1
            continue
        parsed, consumed, role = _event_facts(line, role)
        facts.extend(parsed)
        if not consumed:
            # The marker itself is metadata about completeness, not an
            # unknown run event.  ``truncated`` is already set globally.
            if _TRUNCATED_RE.search(stripped):
                continue
            unparsed += 1
    return facts, unparsed


def _process_xml(text: str) -> tuple[list[dict[str, Any]], int]:
    facts: list[dict[str, Any]] = []
    unparsed = 0
    role: str | None = None
    for block in _XML_BLOCK_RE.finditer(text):
        tag = block.group("tag").lower()
        body = block.group("body")
        if tag == "event":
            parsed, count = _process_lines(body.splitlines(), initial_role=role)
            facts.extend(parsed)
            unparsed += count
            # A role header in this block is context for later event blocks.
            for line in body.splitlines():
                header = _ROLE_HEADER_RE.match(line.strip())
                if header:
                    role = header.group("role").strip() or role
        elif tag == "status":
            for line in body.splitlines():
                value = line.strip()
                if value:
                    facts.append(_fact("process_status", value, None, None))
        else:  # summary: only an explicitly written exit code is permitted.
            for match in _SUMMARY_EXIT_RE.finditer(body):
                facts.append(_fact("process_exit", _scalar(match.group("value")), None, None))
    return facts, unparsed


def parse_facts(text: str) -> dict[str, Any]:
    """Parse historical run output without inferring outcome or completeness."""

    text = text if isinstance(text, str) else str(text)
    truncated = bool(_TRUNCATED_RE.search(text))

    # A complete JSON object is a summary, including a pretty-printed object.
    whole = _summary_from_text(text.strip()) if text.strip() else None
    if whole is not None:
        facts = _summary_facts(whole)
        return {"facts": facts, "unparsed_lines": 0 if facts else sum(bool(x.strip()) for x in text.splitlines()),
                "truncated": truncated}

    if _XML_BLOCK_RE.search(text):
        facts, unparsed = _process_xml(text)
    else:
        facts, unparsed = _process_lines(text.splitlines())
    return {"facts": facts, "unparsed_lines": unparsed, "truncated": truncated}


__all__ = ["parse_facts"]
