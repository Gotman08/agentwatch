"""Rejoue une recherche de consigne Claude sur des copies locales figees.

Le script ne lit les transcripts qu'en lecture seule et ne modifie jamais les
sources cycle1. Les copies, exports JSONL et le rapport sont ecrits dans un
dossier local ignore par Git (``_logs/cycle2`` par defaut). La recherche utile
reutilise ``inspect_claude.export_session``; ce module ne reimplemente pas le
parseur des transcripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentwatch.collector import privacy as P
from agentwatch.reports.inspect_claude import export_session


DEFAULT_MAIN_SOURCE = (WORKSPACE_ROOT / "agentwatch-cycle1" / "_logs" / "cycle1" / "projects" /
                       "G--UnrealEngine-Unearthed" / "91ae2d65-1278-4cff-b732-77df8bf3947b.jsonl")
DEFAULT_OLD_SOURCE = (WORKSPACE_ROOT / "agentwatch-cycle1" / "_logs" / "cycle1" / "projects" /
                      "G--UnrealEngine-Unearthed" / "bb09a2ec-f68c-455d-9aa1-143cb748565b.jsonl")
DEFAULT_OUTPUT_DIR = REPO_ROOT / "_logs" / "cycle2"
DEFAULT_REFERENCE_LINE = 11793
DEFAULT_REFERENCE_UUID = "a2295306-4954-4dfd-9908-421617230836"
DEFAULT_REFERENCE_SHA256 = "e55bfa4be83d43142279405a3b16608cb93a8423b262c0dc133892b42707c6ad"
DEFAULT_REFERENCE_BYTES = 20304
DEFAULT_CONTAINS = "T5"
DEFAULT_UNIQUE_CONTAINS = "server-streaming"
DEFAULT_OLD_START = 411
DEFAULT_OLD_END = 429
_CHUNK = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_frozen(source: Path, target: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"transcript source absent: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_stat = source.stat()
    source_hash = _sha256_file(source)
    if target.exists():
        target_stat = target.stat()
        target_hash = _sha256_file(target)
        if source_stat.st_size != target_stat.st_size or source_hash != target_hash:
            raise OSError(f"copie figee differente de la source: {target}")
    else:
        shutil.copyfile(source, target)
        target_stat = target.stat()
        target_hash = _sha256_file(target)
        if source_stat.st_size != target_stat.st_size or source_hash != target_hash:
            raise OSError(f"copie incomplete ou non identique: {source} -> {target}")
    return {"original": str(source), "copy": str(target), "bytes": source_stat.st_size,
            "sha256": target_hash}


def _safe_receipt(receipt: dict[str, Any], key: bytes) -> dict[str, Any]:
    return {**receipt, "original": P.mask_secrets(str(receipt["original"]), key),
            "copy": P.mask_secrets(str(receipt["copy"]), key)}


def _read_source_line(path: Path, line_number: int) -> dict[str, Any]:
    if line_number < 1:
        raise ValueError("reference_line doit etre positif")
    with path.open("rb") as fh:
        for current, raw in enumerate(fh, 1):
            if current != line_number:
                continue
            try:
                obj = json.loads(raw)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f"ligne de reference JSON invalide: {path}:{line_number}") from exc
            message = obj.get("message") if isinstance(obj, dict) and isinstance(obj.get("message"), dict) else {}
            text = _message_text(message)
            return {"line": line_number, "uuid": obj.get("uuid"), "type": obj.get("type"),
                    "role": message.get("role"), "text_chars": len(text) if text is not None else None,
                    "text_bytes": len(text.encode("utf-8")) if text is not None else None,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None}
    raise ValueError(f"ligne de reference absente: {path}:{line_number}")


def _message_text(message: Any) -> str | None:
    """Extrait le texte utile d'un message simple ou de blocs textuels."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text") for block in content
                 if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
        return "\n\n".join(parts) if parts else None
    return None


def _validate_reference(reference: dict[str, Any], expected: dict[str, Any] | None) -> None:
    if expected is None:
        return
    mismatches = []
    for key in ("uuid", "text_sha256", "text_bytes"):
        if key in expected and expected[key] is not None and reference.get(key) != expected[key]:
            mismatches.append(f"{key}: attendu {expected[key]!r}, obtenu {reference.get(key)!r}")
    if reference.get("type") != "user" or reference.get("role") != "user":
        mismatches.append(f"type/role: attendu user/user, obtenu {reference.get('type')!r}/{reference.get('role')!r}")
    if mismatches:
        raise ValueError("reference historique inattendue: " + "; ".join(mismatches))


def _load_export(path: Path) -> list[tuple[dict[str, Any], bytes]]:
    rows: list[tuple[dict[str, Any], bytes]] = []
    with path.open("rb") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            rows.append((json.loads(raw), raw))
    return rows


def _text_bytes(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _text_sha256(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_ref(row: dict[str, Any]) -> dict[str, Any]:
    source = row.get("source")
    return dict(source) if isinstance(source, dict) else {"available": False}


def _message_records(rows: Iterable[tuple[dict[str, Any], bytes]]) -> list[tuple[dict[str, Any], bytes]]:
    return [(row, raw) for row, raw in rows if row.get("kind") == "message"]


def _measure_messages(rows: list[tuple[dict[str, Any], bytes]]) -> dict[str, Any]:
    messages = _message_records(rows)
    return {"results": len(messages),
            "rendered_text_bytes": sum(_text_bytes(row.get("data", {}).get("text")) for row, _ in messages),
            "selected_envelope_bytes": sum(len(raw) for row, raw in messages),
            "artifact_bytes": sum(len(raw) for _, raw in rows),
            "truncated_results": sum("[tronque]" in flag for row, _ in messages for flag in row.get("flags", [])),
            "references": [{"source": _source_ref(row), "text_bytes": _text_bytes(row.get("data", {}).get("text"))}
                           for row, _ in messages]}


def _measure_old_sequence(rows: list[tuple[dict[str, Any], bytes]], start: int, end: int) -> dict[str, Any]:
    interval = [(row, raw) for row, raw in rows
                if isinstance(row.get("source"), dict) and isinstance(row["source"].get("line"), int)
                and start <= row["source"]["line"] <= end]
    results = [(row, raw) for row, raw in interval if row.get("kind") == "resultat"]
    calls = [(row, raw) for row, raw in interval if row.get("kind") == "appel"]
    internal_hints: list[dict[str, Any]] = []
    for row, _ in results:
        content = row.get("data", {}).get("content")
        rendered = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        terms = [term for term in ("cp1252", "nonetype") if term in rendered.casefold()]
        if terms:
            internal_hints.append({"source": _source_ref(row), "terms": terms})
    return {"interval": {"start": start, "end": end}, "interval_records": len(interval),
            "calls": len(calls), "results": len(results),
            "last_result_source_line": max((row["source"]["line"] for row, _ in results), default=None),
            "rendered_result_text_bytes": sum(_text_bytes(row.get("data", {}).get("content")) for row, _ in results),
            "result_envelope_bytes": sum(len(raw) for _, raw in results),
            "interval_envelope_bytes": sum(len(raw) for _, raw in interval),
            "error_results": sum(row.get("data", {}).get("is_error") is True for row, _ in results),
            "internal_error_hint_count": len(internal_hints),
            "internal_error_hint_text": internal_hints,
            "call_references": [{"name": row.get("data", {}).get("name"), "source": _source_ref(row)}
                                for row, _ in calls],
            "references": [{"kind": row.get("kind"), "name": row.get("data", {}).get("name"),
                            "is_error": row.get("data", {}).get("is_error"), "source": _source_ref(row),
                            "text_bytes": _text_bytes(row.get("data", {}).get("content"))}
                           for row, _ in results]}


def _same_path(left: Path, right: Path) -> bool:
    if left.expanduser().resolve() == right.expanduser().resolve():
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _timed_export(path: Path, cfg: dict[str, Any], home: Path, session: str,
                  protected_paths: Iterable[Path] = (), **filters: Any) -> float:
    path = path.expanduser().resolve()
    if any(_same_path(path, protected) for protected in protected_paths):
        raise ValueError(f"export refuse: la sortie est un transcript source: {path}")
    if path.exists() and path.is_dir():
        raise ValueError(f"export refuse: la sortie est un dossier: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        export_session(cfg, str(home), session, fh, fmt="jsonl", max_chars=1_000_000,
                       conclusions=False, **filters)
    return (time.perf_counter() - started) * 1000


def run_replay(*, source: Path = DEFAULT_MAIN_SOURCE, old_source: Path = DEFAULT_OLD_SOURCE,
               output_dir: Path = DEFAULT_OUTPUT_DIR, home: Path | None = None,
               session: str | None = None, old_session: str | None = None,
               reference_line: int = DEFAULT_REFERENCE_LINE, contains: str = DEFAULT_CONTAINS,
               unique_contains: str | None = DEFAULT_UNIQUE_CONTAINS,
               old_start: int = DEFAULT_OLD_START, old_end: int = DEFAULT_OLD_END,
               expected_reference: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copie, exporte et mesure la comparaison historique.

    ``expected_reference=None`` est utile pour fixtures synthétiques; le CLI
    utilise les invariants cycle1 par défaut. L'intervalle historique est
    inclusif et ses résultats d'outils, erreurs comprises, sont additionnés.
    """
    output_dir = Path(output_dir).expanduser().resolve()
    source = Path(source).expanduser().resolve()
    old_source = Path(old_source).expanduser().resolve()
    if output_dir.exists() and output_dir.is_file():
        raise ValueError(f"output_dir doit etre un dossier: {output_dir}")
    home = Path(home).expanduser().resolve() if home is not None else output_dir / "spec-replay-home"
    projects = output_dir / "spec-replay-projects"
    main_session = session or Path(source).stem
    prior_session = old_session or Path(old_source).stem
    main_copy = projects / "main" / source.name
    old_copy = main_copy if source == old_source else projects / "old" / old_source.name
    copy_started = time.perf_counter()
    main_receipt = _copy_frozen(Path(source), main_copy)
    old_receipt = dict(main_receipt) if source == old_source else _copy_frozen(Path(old_source), old_copy)
    copy_ms = (time.perf_counter() - copy_started) * 1000
    reference = _read_source_line(Path(source), reference_line)
    _validate_reference(reference, expected_reference)

    cfg = {"transcripts": {"claude_projects_dir": str(projects)}}
    output_dir.mkdir(parents=True, exist_ok=True)
    search_path = output_dir / "spec-replay-search.jsonl"
    unique_path = output_dir / "spec-replay-unique.jsonl"
    filtered_path = output_dir / "spec-replay-filtered.jsonl"
    old_path = output_dir / "spec-replay-old.jsonl"
    protected = (source, old_source, main_copy, old_copy)
    search_ms = _timed_export(search_path, cfg, home, main_session, protected_paths=protected,
                              kinds=["message"], role="user", contains=contains)
    search_rows = _load_export(search_path)
    unique_ms = None
    unique_rows: list[tuple[dict[str, Any], bytes]] = []
    if unique_contains is not None:
        unique_ms = _timed_export(unique_path, cfg, home, main_session, protected_paths=protected,
                                   kinds=["message"], role="user", contains=unique_contains)
        unique_rows = _load_export(unique_path)
    exact_ms = _timed_export(filtered_path, cfg, home, main_session, protected_paths=protected,
                             kinds=["message"], role="user", contains=contains, source_line=reference_line)
    exact_rows = _load_export(filtered_path)
    old_ms = _timed_export(old_path, cfg, home, prior_session, protected_paths=protected)
    old_rows = _load_export(old_path)
    search_measure = _measure_messages(search_rows)
    unique_measure = _measure_messages(unique_rows) if unique_contains is not None else None
    if unique_measure is not None:
        unique_measure["reference_line_matches"] = sum(ref.get("source", {}).get("line") == reference_line
                                                        for ref in unique_measure["references"])
    exact_measure = _measure_messages(exact_rows)
    old_measure = _measure_old_sequence(old_rows, old_start, old_end)
    exact_messages = _message_records(exact_rows)
    exact_texts = [row.get("data", {}).get("text") for row, _ in exact_messages]
    content_identical = len(exact_texts) == 1 and exact_texts[0] == _reference_text(Path(source), reference_line)
    unique_messages = _message_records(unique_rows)
    unique_texts = [row.get("data", {}).get("text") for row, _ in unique_messages]
    unique_content_identical = len(unique_texts) == 1 and unique_texts[0] == _reference_text(Path(source), reference_line)
    key = P.ensure_key(home)
    safe_contains = P.mask_secrets(contains, key)
    safe_unique_contains = P.mask_secrets(unique_contains, key) if unique_contains is not None else None
    safe_main_receipt = _safe_receipt(main_receipt, key)
    safe_old_receipt = _safe_receipt(old_receipt, key)
    report = {
        "schema": "claude_spec_replay/1",
        "command": f"{Path(sys.argv[0]).name} [arguments redacted]",
        "inputs": {"main": safe_main_receipt, "old": safe_old_receipt, "home": P.mask_secrets(str(home), key),
                    "projects": P.mask_secrets(str(projects), key), "copy_duration_ms": round(copy_ms, 3),
                    "copy_duration_scope": "copy or frozen-copy hash verification",
                    "shared_copy": source == old_source},
        "reference": reference,
        "new": {"contains": safe_contains,
                "filters": {"kinds": ["message"], "role": "user", "contains": safe_contains},
                "search": {"path": P.mask_secrets(str(search_path), key), "duration_ms": round(search_ms, 3),
                           "duration_scope": "output open + export; copy separate", **search_measure},
                "unique": ({"contains": safe_unique_contains, "path": P.mask_secrets(str(unique_path), key),
                            "duration_ms": round(unique_ms, 3), "duration_scope": "output open + export; copy separate",
                            **unique_measure,
                            "content_useful_identical": unique_content_identical,
                            "text_sha256": _text_sha256(unique_texts[0]) if len(unique_texts) == 1 else None}
                           if unique_contains is not None else None),
                "exact": {"path": P.mask_secrets(str(filtered_path), key), "duration_ms": round(exact_ms, 3),
                          "duration_scope": "output open + export; copy separate", **exact_measure,
                          "source_line": reference_line, "mode": "contains+source_line",
                          "source_line_preknown": True, "content_useful_identical": content_identical,
                          "text_sha256": _text_sha256(exact_texts[0]) if len(exact_texts) == 1 else None},
                "total_workflow": {"steps": ["contains search", "contains + preknown source_line"],
                                   "duration_ms": round(search_ms + exact_ms, 3),
                                   "artifact_bytes": search_measure["artifact_bytes"] + exact_measure["artifact_bytes"],
                                   "rendered_text_bytes": search_measure["rendered_text_bytes"] + exact_measure["rendered_text_bytes"]},
                "calibration_workflow": {"steps": ["contains search", "unique contains search", "contains + preknown source_line"],
                                         "duration_ms": round(search_ms + (unique_ms or 0) + exact_ms, 3),
                                         "artifact_bytes": search_measure["artifact_bytes"] +
                                         (unique_measure["artifact_bytes"] if unique_measure is not None else 0) +
                                         exact_measure["artifact_bytes"]}},
        "old": {"session": prior_session, "path": P.mask_secrets(str(old_path), key), "duration_ms": round(old_ms, 3),
                "duration_scope": "output open + export; copy separate", **old_measure},
        "comparison": {"new_search_text_bytes": search_measure["rendered_text_bytes"],
                       "new_exact_text_bytes": exact_measure["rendered_text_bytes"],
                       "old_result_text_bytes": old_measure["rendered_result_text_bytes"],
                       "new_search_envelope_bytes": search_measure["selected_envelope_bytes"],
                       "new_exact_envelope_bytes": exact_measure["selected_envelope_bytes"],
                       "new_search_artifact_bytes": search_measure["artifact_bytes"],
                       "new_exact_artifact_bytes": exact_measure["artifact_bytes"],
                       "old_result_envelope_bytes": old_measure["result_envelope_bytes"],
                       "new_workflow_duration_ms": round(search_ms + exact_ms, 3),
                       "new_workflow_artifact_bytes": search_measure["artifact_bytes"] + exact_measure["artifact_bytes"],
                       "new_search_results": search_measure["results"],
                       "new_unique_results": unique_measure["results"] if unique_measure is not None else None,
                       "new_exact_results": exact_measure["results"],
                       "token_savings_claimed": False,
                       "limits": ["durees mesurees sur export local, ouverture incluse, copie mesuree separement",
                                  "octets de texte et enveloppe JSONL ne sont pas des tokens ni une facturation",
                                  "source refs pointent vers les copies figees"]},
    }
    report_path = output_dir / "spec-replay-report.json"
    if any(_same_path(report_path, protected_path) for protected_path in protected):
        raise ValueError(f"export refuse: le rapport est un transcript source: {report_path}")
    report["report_path"] = P.mask_secrets(str(report_path), key)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _reference_text(path: Path, line_number: int) -> str | None:
    with path.open("rb") as fh:
        for current, raw in enumerate(fh, 1):
            if current == line_number:
                obj = json.loads(raw)
                message = obj.get("message") if isinstance(obj, dict) else None
                return _message_text(message)
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_MAIN_SOURCE)
    parser.add_argument("--old-source", type=Path, default=DEFAULT_OLD_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--old-session")
    parser.add_argument("--reference-line", type=int, default=DEFAULT_REFERENCE_LINE)
    parser.add_argument("--contains", default=DEFAULT_CONTAINS)
    parser.add_argument("--unique-contains", default=DEFAULT_UNIQUE_CONTAINS)
    parser.add_argument("--old-start", type=int, default=DEFAULT_OLD_START)
    parser.add_argument("--old-end", type=int, default=DEFAULT_OLD_END)
    parser.add_argument("--skip-reference-check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected = None if args.skip_reference_check else {"uuid": DEFAULT_REFERENCE_UUID,
                                                         "text_sha256": DEFAULT_REFERENCE_SHA256,
                                                         "text_bytes": DEFAULT_REFERENCE_BYTES}
    report = run_replay(source=args.source, old_source=args.old_source, output_dir=args.output_dir,
                        home=args.home, session=args.session, old_session=args.old_session,
                        reference_line=args.reference_line, contains=args.contains,
                        unique_contains=args.unique_contains,
                        old_start=args.old_start, old_end=args.old_end, expected_reference=expected)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
