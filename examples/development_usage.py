"""Bounded, numeric token usage for one recorded Codex development tree.

This example is deliberately a reader rather than an importer.  It scans only
the rollout files selected by a root thread, a physical root line boundary and
an explicit child agent-path prefix.  It records numbers, identifiers,
locations and hashes; it never includes prompts, tool output, or other
conversation text in the report.

The reusable entry point is :func:`build_report`.  ``main`` exposes it as::

    python examples/development_usage.py --rollouts-dir DIR \
        --root-thread THREAD --root-start-line N --agent-prefix /root/cycle3_ \
        --out REPORT.json

Missing token fields stay ``None`` and make strict totals incomplete.  A zero
is retained only when the source wrote zero.  The defined zero origin used for
an entire child is a verification reference, not a source observation.
``thread_token_usage`` is used as a cumulative cross-check: the root baseline
is the last eligible sample before the requested line and a selected child
starts at an explicit zero.  Duplicate response ids are collapsed; an id with
conflicting numeric data is cleared to ``None`` and its conflicting fields
remain unknown.  Metadata conflicts for one response id use the same
conservative rule: the id is cleared, the first source anchors the row, and
numeric fields remain known only when all colliding records agree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentwatch.collector import rollouts as R


USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_MISSING = object()


def _empty_numbers(value: Any = None) -> dict[str, int | None]:
    return {key: value for key in USAGE_FIELDS}


def _numeric_snapshot(value: Any) -> tuple[dict[str, int | None], list[str]]:
    """Use the shared normaliser while retaining absent/null fields as unknown."""

    if not isinstance(value, dict):
        # Calling the shared helper here keeps the source's normalisation
        # contract in one place, while the returned completeness information
        # prevents its historical missing->0 behaviour from leaking out.
        R._usage_numbers(value)
        return _empty_numbers(), list(USAGE_FIELDS)
    cleaned: dict[str, int] = {}
    for key in USAGE_FIELDS:
        raw = value.get(key, _MISSING)
        if raw is _MISSING or raw is None or isinstance(raw, bool):
            continue
        candidate: int | None = None
        if isinstance(raw, int):
            candidate = raw if raw >= 0 else None
        elif isinstance(raw, float):
            candidate = int(raw) if raw.is_integer() and raw >= 0 else None
        elif isinstance(raw, str) and re.fullmatch(r"\d+", raw):
            candidate = int(raw)
        if candidate is not None:
            cleaned[key] = candidate
    # Run the shared helper only on already validated fields.  It still
    # supplies the project's normal integer conversion, but cannot turn an
    # absent or invalid field into a visible zero.
    normalised = R._usage_numbers(cleaned)
    out: dict[str, int | None] = {}
    missing: list[str] = []
    for key in USAGE_FIELDS:
        if key not in cleaned:
            out[key] = None
            missing.append(key)
            continue
        try:
            out[key] = int(normalised[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            out[key] = None
            missing.append(key)
    return out, missing


def _source(path: Path, line: int) -> dict[str, Any]:
    return {"file": str(path), "line": line}


def _strict_sum(rows: Iterable[dict[str, Any]], field: str) -> int | None:
    values = [row["usage"].get(field) for row in rows]
    if not values:
        return None
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def _totals(rows: Iterable[dict[str, Any]]) -> dict[str, int | None]:
    rows = list(rows)
    return {field: _strict_sum(rows, field) for field in USAGE_FIELDS}


def _compare_usage(left: dict[str, int | None], right: dict[str, int | None]) -> tuple[bool, set[str]]:
    differing: set[str] = set()
    for field in USAGE_FIELDS:
        if left.get(field) != right.get(field):
            differing.add(field)
    return not differing, differing


def _valid_subset(row: dict[str, Any]) -> list[str]:
    """Return invariant violations without deriving a replacement number."""

    usage = row["usage"]
    violations: list[str] = []
    cached, input_tokens = usage.get("cached_input_tokens"), usage.get("input_tokens")
    reasoning, output = usage.get("reasoning_output_tokens"), usage.get("output_tokens")
    if cached is not None and input_tokens is not None and cached > input_tokens:
        violations.append("cached_input_gt_input")
    if reasoning is not None and output is not None and reasoning > output:
        violations.append("reasoning_output_gt_output")
    if all(usage.get(field) is not None for field in ("input_tokens", "output_tokens", "total_tokens")):
        if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
            violations.append("total_ne_input_plus_output")
    return violations


def _normalise_record(path: Path, line: int, meta: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    usage, missing = _numeric_snapshot(payload.get("usage"))
    thread_usage, thread_missing = _numeric_snapshot(payload.get("thread_token_usage"))
    row = {
        "thread_id": meta.get("thread_id"),
        "root_id": meta.get("root_id"),
        "parent_id": meta.get("parent_id"),
        "agent_path": meta.get("agent_path"),
        "source": _source(path, line),
        "response_id": payload.get("response_id") if isinstance(payload.get("response_id"), str) else None,
        "root_turn_id": payload.get("root_turn_id") if isinstance(payload.get("root_turn_id"), str) else None,
        "turn_id": payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None,
        "usage": usage,
        "thread_token_usage": thread_usage,
        "missing_usage_fields": missing,
        "missing_thread_usage_fields": thread_missing,
        "complete": not missing and not thread_missing,
        "usage_complete": not missing,
        "thread_usage_complete": not thread_missing,
    }
    violations = _valid_subset(row)
    row["invariant_violations"] = violations
    if violations:
        row["complete"] = False
    return row


def _deduplicate(rows: list[dict[str, Any]], counters: dict[str, int]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    output: list[dict[str, Any]] = []
    for row in rows:
        rid = row.get("response_id")
        if rid is None:
            counters["missing_response_id"] += 1
            output.append(row)
            continue
        previous = by_id.get(rid)
        if previous is None:
            by_id[rid] = row
            output.append(row)
            continue

        counters["duplicate_response_id"] += 1
        same_usage, differing_usage = _compare_usage(previous["usage"], row["usage"])
        same_thread, differing_thread = _compare_usage(previous["thread_token_usage"], row["thread_token_usage"])
        metadata_fields = ("root_turn_id", "turn_id", "thread_id", "root_id", "parent_id", "agent_path")
        differing_meta = [field for field in metadata_fields if previous.get(field) != row.get(field)]
        same_meta = not differing_meta
        if same_usage and same_thread and same_meta:
            continue

        counters["conflicting_response_id"] += 1
        previous["response_id"] = None
        previous["complete"] = False
        previous["conflict"] = True
        previous.setdefault("conflicting_sources", []).append(row["source"])
        if differing_meta:
            counters["conflicting_metadata"] += 1
            previous.setdefault("conflicting_metadata_fields", [])
            for field in differing_meta:
                if field not in previous["conflicting_metadata_fields"]:
                    previous["conflicting_metadata_fields"].append(field)
        for field in differing_usage:
            previous["usage"][field] = None
            if field not in previous["missing_usage_fields"]:
                previous["missing_usage_fields"].append(field)
        for field in differing_thread:
            previous["thread_token_usage"][field] = None
            if field not in previous["missing_thread_usage_fields"]:
                previous["missing_thread_usage_fields"].append(field)
        if differing_thread:
            previous["thread_usage_complete"] = False
        # Keep the first source as the stable location for the conflicted
        # response; the later source is retained only as a location.
    return output


def _delta(last: dict[str, int | None] | None, baseline: dict[str, int | None] | None) -> dict[str, int | None]:
    if last is None or baseline is None:
        return _empty_numbers()
    out: dict[str, int | None] = {}
    for field in USAGE_FIELDS:
        left, right = last.get(field), baseline.get(field)
        out[field] = left - right if left is not None and right is not None and left >= right else None
    return out


def _verify_file(rows: list[dict[str, Any]], baseline: dict[str, int | None] | None,
                 last: dict[str, int | None] | None) -> dict[str, Any]:
    summed = _totals(rows)
    delta = _delta(last, baseline)
    comparisons = [summed[field] == delta[field] for field in USAGE_FIELDS
                   if summed[field] is not None and delta[field] is not None]
    all_known = len(comparisons) == len(USAGE_FIELDS)
    full_match = all(comparisons) if all_known else None
    return {"sum_by_response": summed, "thread_delta": delta,
            "matches": full_match, "full_match": full_match,
            "checked_fields": [field for field in USAGE_FIELDS if summed[field] is not None and delta[field] is not None]}


def _scan_file(path: Path, meta: dict[str, Any], root_thread: str, root_start_line: int,
               is_root: bool, counters: dict[str, int], aggregate_digest: Any = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    digest = hashlib.sha256()
    read_bytes = 0
    eligible: list[dict[str, Any]] = []
    baseline_candidates: list[dict[str, int | None]] = []
    file_stats = {"records_seen": 0, "records_eligible": 0, "records_excluded": 0,
                  "invalid_json_lines": 0, "token_count_excluded": 0}
    with path.open("rb") as handle:
        for line, raw in enumerate(handle, 1):
            digest.update(raw)
            if aggregate_digest is not None:
                aggregate_digest.update(raw)
            read_bytes += len(raw)
            try:
                obj = json.loads(raw)
            except (UnicodeDecodeError, ValueError):
                file_stats["invalid_json_lines"] += 1
                counters["invalid_json_lines"] += 1
                continue
            if not isinstance(obj, dict):
                continue
            top_type = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if top_type == "token_count" or payload.get("type") == "token_count":
                file_stats["token_count_excluded"] += 1
                counters["token_count_excluded"] += 1
                continue
            if top_type != "token_usage_record":
                continue
            file_stats["records_seen"] += 1
            counters["token_usage_records_seen"] += 1
            thread_id = payload.get("thread_id")
            session_id = payload.get("session_id")
            if thread_id != meta.get("thread_id"):
                counters["excluded_wrong_thread"] += 1
                if thread_id == root_thread:
                    counters["excluded_parent_copy"] += 1
                file_stats["records_excluded"] += 1
                continue
            if session_id != root_thread:
                counters["excluded_wrong_session"] += 1
                file_stats["records_excluded"] += 1
                continue
            if is_root and line < root_start_line:
                file_stats["records_excluded"] += 1
                counters["excluded_before_root_start"] += 1
                row = _normalise_record(path, line, meta, payload)
                baseline_candidates.append(row["thread_token_usage"])
                continue
            row = _normalise_record(path, line, meta, payload)
            eligible.append(row)
            file_stats["records_eligible"] += 1
            counters["eligible_records"] += 1
    file_info = {"path": str(path), "thread_id": meta.get("thread_id"), "root_id": meta.get("root_id"),
                 "agent_path": meta.get("agent_path"), "is_root": is_root,
                 "read_prefix": {"bytes": read_bytes, "sha256": digest.hexdigest()}, "stats": file_stats}
    return file_info, eligible, baseline_candidates


def _select_files(rollouts_dir: Path, root_thread: str, agent_prefix: str) -> tuple[list[tuple[Path, dict[str, Any], bool]], dict[str, int]]:
    counters = {"files_discovered": 0, "files_with_metadata": 0, "metadata_missing": 0,
                "files_selected": 0, "files_excluded_other_agent": 0}
    rows: list[tuple[Path, dict[str, Any], bool]] = []
    for path in sorted(rollouts_dir.rglob("*.jsonl")):
        counters["files_discovered"] += 1
        meta = R.read_thread_meta(str(path))
        if not meta:
            counters["metadata_missing"] += 1
            continue
        counters["files_with_metadata"] += 1
        is_root = meta.get("thread_id") == root_thread and meta.get("root_id") == root_thread
        is_child = (meta.get("root_id") == root_thread and meta.get("thread_id") != root_thread and
                    isinstance(meta.get("agent_path"), str) and meta["agent_path"].startswith(agent_prefix))
        if is_root or is_child:
            rows.append((path, meta, is_root))
        elif meta.get("root_id") == root_thread:
            counters["files_excluded_other_agent"] += 1
    if not any(is_root for _, _, is_root in rows):
        raise ValueError(f"root thread absent: {root_thread}")
    rows.sort(key=lambda item: (0 if item[2] else 1, str(item[0])))
    counters["files_selected"] = len(rows)
    return rows, counters


def build_report(rollouts_dir: str | os.PathLike[str], root_thread: str, root_start_line: int,
                 agent_prefix: str) -> dict[str, Any]:
    """Read a bounded root/child tree and return a numeric-only report."""

    if root_start_line < 1:
        raise ValueError("root_start_line doit etre positif")
    directory = Path(rollouts_dir).expanduser().resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"rollouts-dir absent: {directory}")
    if not root_thread or not agent_prefix:
        raise ValueError("root_thread et agent_prefix sont requis")

    selected, counters = _select_files(directory, root_thread, agent_prefix)
    for key in ("token_usage_records_seen", "eligible_records", "excluded_before_root_start", "excluded_wrong_thread",
                "excluded_parent_copy", "excluded_wrong_session", "token_count_excluded", "duplicate_response_id",
                "conflicting_response_id", "missing_response_id", "invalid_json_lines", "incomplete_response",
                "invariant_violation", "missing_thread_usage", "conflicting_metadata", "missing_usage_fields"):
        counters.setdefault(key, 0)

    file_infos: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    baselines: dict[str, dict[str, int | None] | None] = {}
    lasts: dict[str, dict[str, int | None] | None] = {}
    first_source = last_source = None
    all_prefix = hashlib.sha256()
    for path, meta, is_root in selected:
        info, eligible, baseline_candidates = _scan_file(path, meta, root_thread, root_start_line, is_root, counters,
                                                         aggregate_digest=all_prefix)
        file_infos.append(info)
        file_key = str(path)
        candidates.extend(eligible)
        if eligible:
            if first_source is None:
                first_source = eligible[0]["source"]
            last_source = eligible[-1]["source"]
            lasts[file_key] = eligible[-1]["thread_token_usage"]
        else:
            lasts[file_key] = None
        if is_root:
            baselines[file_key] = baseline_candidates[-1] if baseline_candidates else None
            baseline_basis = ("last_observed_before_root_start" if baseline_candidates
                              else "unobserved_before_root_start")
        else:
            baselines[file_key] = _empty_numbers(0)
            baseline_basis = "defined_child_thread_origin_zero"
        info["baseline_basis"] = baseline_basis
        info["baseline_observed"] = bool(is_root and baseline_candidates)

    unique = _deduplicate(candidates, counters)
    for row in unique:
        if not row["complete"]:
            counters["incomplete_response"] += 1
        if row["invariant_violations"]:
            counters["invariant_violation"] += len(row["invariant_violations"])
        if not row["thread_usage_complete"]:
            counters["missing_thread_usage"] += 1

    root_id = next((row["thread_id"] for row in unique if row["thread_id"] == root_thread), root_thread)
    # Root/child subtotals need the root id even when no root response survived
    # a conflict, so group explicitly rather than inferring from first row.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in unique:
        grouped.setdefault(str(row.get("thread_id")), []).append(row)
    root_rows = grouped.get(root_id, [])
    child_rows = [row for row in unique if row.get("thread_id") != root_id]
    by_thread = {thread: {"responses": len(rows), "complete_responses": sum(row["complete"] for row in rows),
                          "incomplete_responses": sum(not row["complete"] for row in rows), "totals": _totals(rows)}
                 for thread, rows in grouped.items()}

    verifications = []
    for info in file_infos:
        path = info["path"]
        # Deduplication assigns a response to its first source.  A conflict is
        # still present in that first row with unknown conflicting fields.
        assigned = [row for row in unique if row["source"]["file"] == path]
        verification = _verify_file(assigned, baselines.get(path), lasts.get(path))
        verifications.append({"file": path, "thread_id": info["thread_id"], "is_root": info["is_root"],
                              "baseline": baselines.get(path), "baseline_basis": info["baseline_basis"],
                              "baseline_observed": info["baseline_observed"], "last": lasts.get(path), **verification})

    delta_fields = [v["thread_delta"] for v in verifications]
    aggregate_delta: dict[str, int | None] = {}
    for field in USAGE_FIELDS:
        values = [d[field] for d in delta_fields]
        aggregate_delta[field] = sum(values) if values and all(value is not None for value in values) else None
    totals = _totals(unique)
    comparisons = [totals[field] == aggregate_delta[field] for field in USAGE_FIELDS
                   if totals[field] is not None and aggregate_delta[field] is not None]
    aggregate_all_known = len(comparisons) == len(USAGE_FIELDS)
    aggregate_match = all(comparisons) if aggregate_all_known else None
    for field in USAGE_FIELDS:
        if any(row["usage"].get(field) is None for row in unique):
            counters.setdefault("missing_usage_fields", 0)
            counters["missing_usage_fields"] += sum(row["usage"].get(field) is None for row in unique)

    root_turn_ids: list[str] = []
    for row in unique:
        turn = row.get("root_turn_id")
        if isinstance(turn, str) and turn not in root_turn_ids:
            root_turn_ids.append(turn)

    total_read_bytes = 0
    # The aggregate digest is fed while the selected files are read, so for a
    # single file it is exactly the SHA256 of the bytes consumed from source.
    for info in file_infos:
        total_read_bytes += int(info["read_prefix"]["bytes"])
    incomplete = sum(not row["complete"] for row in unique)
    report = {
        "schema": "agentwatch.development-usage.v1",
        "root_thread": root_thread,
        "root_start_line": root_start_line,
        "agent_prefix": agent_prefix,
        "selected_files": file_infos,
        "responses": unique,
        "response_count": len(unique),
        "root_turn_ids": root_turn_ids,
        "first_source": first_source,
        "last_source": last_source,
        "fingerprint": {"sha256": all_prefix.hexdigest(), "bytes": total_read_bytes,
                         "basis": "concatenated bytes read from selected files in selection order"},
        "totals": totals,
        "subtotals": {"root": {"responses": len(root_rows), "totals": _totals(root_rows)},
                      "children": {"responses": len(child_rows), "totals": _totals(child_rows)},
                      "by_thread": by_thread},
        "completeness": {"responses": len(unique), "complete_responses": sum(row["complete"] for row in unique),
                          "incomplete_responses": incomplete,
                          "all_usage_fields_known": (all(row["usage_complete"] for row in unique) if unique else None),
                          "all_thread_deltas_known": (all(v["matches"] is True and
                                                             all(v["sum_by_response"].get(field) is not None and
                                                                 v["thread_delta"].get(field) is not None
                                                                 for field in USAGE_FIELDS)
                                                         for v in verifications) if verifications else None),
                          "per_file": verifications},
        "verification": {"sum_by_response": totals, "thread_delta": aggregate_delta,
                          "matches": aggregate_match, "full_match": aggregate_match,
                          "per_file": verifications},
        "limits": [
            "Missing or invalid usage fields remain unknown; strict totals do not substitute zero.",
            "A child baseline of zero is a defined thread origin, not a source observation.",
            "A response_id collision with differing metadata clears response_id and keeps only agreed numeric fields.",
        ],
        "counters": counters,
    }
    return report


def write_report(report: dict[str, Any], out: str | os.PathLike[str], source_paths: Iterable[str] = ()) -> Path:
    destination = Path(out).expanduser().resolve()
    sources = [Path(path).expanduser() for path in source_paths]
    source_resolved = {path.resolve() for path in sources}
    if destination in source_resolved:
        raise ValueError(f"sortie refusee: le chemin est un rollout source: {destination}")
    if destination.exists() and destination.is_dir():
        raise ValueError(f"sortie invalide: dossier: {destination}")
    if destination.exists():
        for source in sources:
            try:
                if source.exists() and destination.samefile(source):
                    raise ValueError(f"sortie refusee: le chemin est lie a un rollout source: {destination}")
            except OSError:
                # A broken source path cannot identify an alias.  The exact
                # path check above still protects an existing source name.
                continue
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollouts-dir", required=True)
    parser.add_argument("--root-thread", required=True)
    parser.add_argument("--root-start-line", required=True, type=int)
    parser.add_argument("--agent-prefix", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    report = build_report(args.rollouts_dir, args.root_thread, args.root_start_line, args.agent_prefix)
    # Protect every existing rollout in the input tree, including files that
    # were excluded by the explicit agent prefix.
    source_paths = [str(path) for path in Path(args.rollouts_dir).expanduser().resolve().rglob("*.jsonl")]
    write_report(report, args.out, source_paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
