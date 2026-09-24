"""Replay rendering of bounded run investigations on frozen transcripts.

No historical command is executed. Acquisitions remain in both paths: this
compares a field projection AFTER the recorded result with the original output,
not a claim that a file could have been read before that version existed.
Case manifests define questions, expected facts and decision bounds explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentwatch.collector import transcripts as T
from agentwatch.reports import inspect_runs as R


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def preserve_result(obs: dict, retained_lines=()) -> bool:
    """A successful tool envelope can still contain a traceback or a run failure.

    This is a conservative rendering guard, not a new error attribution. Unknown
    structured evidence (e.g. clipped lifecycle JSON) also stays in the answer.
    """
    return bool(obs["kind"] == "notification" or obs["is_error"] or obs["transport_receipt"]
                or not obs["facts"] or obs["source"]["line"] in retained_lines
                or any(f["field"] in {"failure_event", "failure_details"} for f in obs["facts"])
                or re.search(r"(?im)Traceback \(most recent call last\)|\b\w*(?:Error|Exception):|<tool_use_error>|\bFAIL\b|\{|^\s*\[", obs["text"]))


def _selected_view(base: dict, run: dict, observations: list[dict], fields: list[str]) -> dict:
    selected = {(o["source"]["path"], o["source"]["line"], o["source"].get("block")) for o in observations}
    return {**base, "view": R.read_run(run, fields=fields, selected=selected)}


def _ref(obs: dict) -> str:
    return f"{obs['source']['line']}.{obs['source'].get('block', 0)}:{obs['version'][:12]}"


def _already_notified(field: str, summary: dict, notifications: list[dict]) -> bool:
    """Skip only a summary whose exact role/time/phase/value was already shown."""
    samples = [(f, o) for o in notifications for f in o["facts"] if f["field"] == field]
    for role, values in summary.get("roles", {}).items():
        if values["conflicting_keys"]:
            return False
        for value in values.values():
            if not isinstance(value, dict) or "value" not in value:
                continue
            if not any((f.get("role") or (o["process_scope"] if field in {"process_exit", "process_status"} else "unspecified")) == role
                       and all(f.get(k) == value.get(k) for k in ("value", "at", "phase")) for f, o in samples):
                return False
    return bool(summary.get("roles"))


def adapt_sequence(*, run: dict, observations: list[dict], original: list[dict],
                   discovery: list[dict], base: dict, fields: list[str],
                   retained_lines: list[int], session: str, thread: str, home: str,
                   manual_proofs: dict | None = None) -> dict:
    """Choose direct output or an existing source-line field view, without an oracle.

    Only fields, recorded evidence and encoded sizes choose a route. Test expected
    values, later events and decision text never participate. All acquisitions,
    notifications, errors and manually retained evidence stay in physical order.
    The full original and all candidate views remain in the replay audit artifact.
    """
    started = time.perf_counter()
    path = list(original)
    candidates, proofs, choices = [], dict(manual_proofs or {}), []
    prefix = R.compact_answer(base)
    scoped = R.compact_answer(_selected_view(base, run, observations, fields))
    for obs in observations:
        src = obs["source"]
        line = src["line"]
        # Source identity is shared rather than repeated in each fact.
        ref = _ref(obs)
        proofs[ref] = {"kind": obs["kind"], "observed_at": src["ts"], "version": obs["version"],
                       "partial": obs["partial"], "unparsed_lines": obs["unparsed_lines"],
                       "roles": sorted({f["role"] or (obs["process_scope"] if f["field"] in {"process_exit", "process_status"} else "unspecified")
                                        for f in obs["facts"]}),
                       "max_chars": obs["full_result"]["max_chars_required"]}
        present = [f for f in fields if any(p["field"] == f for p in obs["facts"])]
        choice = {"source": ref, "route": "direct", "reason": "preuve_conservee"}
        # More than one observation on a line requires block-aware export. The
        # CLI source-line filter intentionally exports all blocks on that line.
        entries = [i for i, e in enumerate(path) if e.get("line") == line and e.get("kind") == obs["kind"]]
        unique = sum(o["source"]["line"] == line for o in observations) == 1
        if present and unique and len(entries) == 1 and not preserve_result(obs, retained_lines):
            view = R.compact_answer(_selected_view(base, run, [obs], present))
            earlier_notifications = [p for p in observations if p["kind"] == "notification" and p["source"]["line"] < line]
            present = [f for f in present if not _already_notified(f, view["fields"][f], earlier_notifications)]
            # No new requested field: the short output remains directly visible.
            if not present:
                choices.append(choice)
                continue
            view["fields"] = {f: view["fields"][f] for f in present}
            query = {"run": run["id"], "fields": present, "thread": thread,
                     "through_line": base["period"]["through_line_inclusive"],
                     "source_line": line, "kinds": [obs["kind"]], "fields_only": True}
            # Same field view, same evidence IDs. Context and proofs are shared
            # by the sequence instead of repeating them for every complement.
            candidate = {"kind": "projection", "line": line, "input": query,
                         "output": {"fields": view["fields"], "missing": view["missing"]}}
            old = path[entries[0]]
            sizes = {"direct": len(encoded(old)), "fields": len(encoded(candidate))}
            candidates.append({"source": ref, "candidate": candidate, "full_inspect_view": view, "utf8_bytes": sizes})
            choice.update(reason="direct_plus_court", utf8_bytes=sizes)
            if sizes["fields"] < sizes["direct"]:
                path[entries[0]] = candidate
                choice.update(route="fields", reason="champs_demandes_plus_courts")
        choices.append(choice)
    # Freshness is the recorded timestamp plus source order, never an assertion
    # that the run was still alive when an old watcher output was reread.
    context = {"run": run["id"], "label": run["label"], "source_file": run["scope"],
               "through_line": base["period"]["through_line_inclusive"], "requested_fields": fields,
               "missing_in_sequence": scoped["missing"], "missing_in_known_prefix": prefix["missing"],
               "missing_means": "non_extrait; consulter les lignes non interpretees, pas une preuve d absence",
               "version_basis": "masked_recorded_output_not_artifact_hash", "evidence": proofs,
               "limits": "Echantillons visibles; notification != entree API; fin watcher != fin scenario != reussite."}
    answer = {"discovery": discovery, "path": path, "context": context}
    direct = {"discovery": discovery, "path": original, "context": context}
    # Validation consumes the rendered union, not the hidden audit projection.
    visible = []
    for obs in observations:
        choice = next(c for c in choices if c["source"] == _ref(obs))
        if choice["route"] == "direct":
            for fact in obs["facts"]:
                f = dict(fact)
                if f["field"] in {"process_exit", "process_status"}:
                    f["role"] = f.get("role") or obs["process_scope"]
                visible.append({**f, "evidence": choice["source"]})
        else:
            entry = next(e for e in path if e.get("line") == obs["source"]["line"] and e["kind"] == "projection")
            for field, data in entry["output"]["fields"].items():
                for role, values in data.get("roles", {}).items():
                    for value in values.values():
                        if isinstance(value, dict) and "value" in value:
                            visible.append({"field": field, "role": role, **value})
    projections = sum(c["route"] == "fields" for c in choices)
    details = {ref: ["python", "-m", "agentwatch", "--home", home, "inspect", "--client", "claude-code",
                     "--session", session, "--thread", thread, "--kind", p["kind"], "--source-line", ref.split(".")[0],
                     "--max-chars", str(p["max_chars"]), "--format", "jsonl", "--out", f"detail-{ref.split(':')[0]}.jsonl"]
               for ref, p in proofs.items()}
    return {"answer": answer, "choices": choices, "candidate_audit": candidates, "visible_facts": visible,
            "detail_commands": details,
            "rendered_utf8_bytes": {"adapted": len(encoded(answer)), "direct_with_same_context": len(encoded(direct)),
                                    "difference_same_context": len(encoded(answer)) - len(encoded(direct))},
            "local_processing_ms": (time.perf_counter() - started) * 1000,
            "steps": {"projection_calls": projections, "historical_acquisitions_avoided": 0,
                      "manual_field_extractions_replaced": sum(len(c["candidate"]["input"]["fields"]) for c in candidates
                                                               if any(q["source"] == c["source"] and q["route"] == "fields" for q in choices)),
                      "necessary_recorded_complements": [o["source"]["line"] for o in observations if o["kind"] == "resultat"
                          and any(f["field"] in fields for f in o["facts"])
                          and not all(any(f == p for prior in observations if prior["source"]["line"] < o["source"]["line"]
                                          for p in prior["facts"]) for f in o["facts"] if f["field"] in fields)]}}


def replay(cfg: dict, home: str, session: str, thread: str, cases: list[dict]) -> dict:
    started = time.perf_counter()
    if not cases:
        raise ValueError("au moins un cas avec attentes explicites est requis")
    events, coverage, key = R.load_events(cfg, home, session, thread=thread)
    index = R.build_index(events, key)
    source = Path(events[0]["source"]["path"])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    parsed = T.parse_transcript(source)
    results = []
    for case in cases:
        start, end = case["start_line"], case["end_line"]
        if not (type(start) is int and type(end) is int and 1 <= start <= end):
            raise ValueError("bornes physiques positives et ordonnees requises")
        for name in ("common_lines", "retain_result_lines", "decision_lines"):
            if any(type(n) is not int or not start <= n <= end for n in case.get(name, [])):
                raise ValueError(f"{name}: references hors de la sequence")
        if not (case.get("expected") or case.get("expected_missing")):
            raise ValueError("attentes explicites requises pour verifier la conservation")
        # Index and answer are rebuilt at each historical physical frontier.
        available, _, _ = R.load_events(cfg, home, session, thread=thread, through_line=end)
        known = R.build_index(available, key)
        runs = [r for r in known["runs"] if case["run"] in [r["id"], r["label"], *r["task_ids"]]]
        if len(runs) != 1:
            raise ValueError(f"run absent/ambigu dans le cas {case['id']}")
        run = runs[0]
        if run["anchor"]["line"] > start:
            raise ValueError("le run doit etre identifiable au debut du cas")
        calls = [c for c in run["calls"] if start <= c["source"]["line"] <= end]
        observations = [o for o in run["observations"] if start <= o["source"]["line"] <= end]
        original = []
        common = []
        acquisitions = []
        manual_proofs = {}
        for call in calls:
            entry = {"kind": "appel", "line": call["source"]["line"], "call_id": call["data"]["call_id"], "input": call["data"]["input"]}
            original.append(entry)
            acquisitions.append(entry)
        for obs in observations:
            entry = {"kind": obs["kind"], "line": obs["source"]["line"], "content": obs["text"], "is_error": obs["is_error"]}
            original.append(entry)
            # Notifications and failed/transport-only results cannot be hidden
            # as though successful measurements replaced them.
            if preserve_result(obs, case.get("retain_result_lines", [])):
                common.append(entry)
        indexed_sources = {(e["source"]["line"], e["source"].get("block"), e["kind"]) for e in [*calls, *observations]}
        for event in available:
            if event["source"]["line"] in case.get("common_lines", []) and event["kind"] in {"appel", "resultat", "message"}:
                if (event["source"]["line"], event["source"].get("block"), event["kind"]) in indexed_sources:
                    continue
                entry = {"kind": event["kind"], "line": event["source"]["line"], "data": event["data"]}
                original.append(entry)
                common.append(entry)
                src = event["source"]
                version = R.P.fingerprint(key, encoded(event["data"]).decode("utf-8"))
                ref = f"{src['line']}.{src.get('block', 0)}:{version[:12]}"
                manual_proofs[ref] = {"kind": event["kind"], "observed_at": src["ts"], "version": version,
                                     "version_basis": "masked_recorded_event", "association": "manifest_common_line",
                                     "partial": None, "roles": [], "max_chars": len(encoded(event["data"])) + 2048}
        retained = set(case.get("retain_result_lines", []))
        found = {e["line"] for e in original if e["kind"] == "resultat"}
        if retained - found:
            raise ValueError(f"preuves demandees sans resultat associe: {sorted(retained - found)}; verifier le lien puis declarer common_lines explicitement")
        if set(case.get("common_lines", [])) - {e["line"] for e in original}:
            raise ValueError("common_lines contient une preuve absente ou non exportable")
        decisions = [e for e in available if e["kind"] == "message" and e["source"]["line"] in case.get("decision_lines", [])]
        decision_entries = [{"kind": "decision_observee", "line": e["source"]["line"], "text": e["data"].get("text")} for e in decisions]
        original.extend(decision_entries)
        common.extend(decision_entries)
        query = {"run": case["run"], "fields": case["fields"], "thread": thread, "through_line": end, "fields_only": True}
        view = R.analyse(cfg, home, session, run=case["run"], fields=case["fields"], thread=thread, through_line=end)
        answer = R.compact_answer(view)
        checks = []
        for expected in case.get("expected", []):
            field = answer["fields"].get(expected["field"], {})
            hits = [v for role, values in field.get("roles", {}).items() for v in values.values()
                    if isinstance(v, dict) and v.get("value") == expected["value"]
                    and ("role" not in expected or role == expected["role"])
                    and ("at" not in expected or v.get("at") == expected["at"])]
            checks.append({"expected": expected, "preserved": bool(hits), "evidence": sorted({h["evidence"] for h in hits})})
        for field in case.get("expected_missing", []):
            checks.append({"expected_missing": field, "preserved": field in answer["missing"]})
        discovery = []
        if case.get("discovery"):
            discovery_query = {"thread": thread, "through_line": start, "limit": 20}
            discovered = R.analyse(cfg, home, session, **discovery_query)
            discovery = [{"input": discovery_query, "output": discovered}]
        proposed = {"discovery": discovery, "common": common, "acquisitions_preserved": acquisitions,
                    "reader": {"input": query, "output": answer}}
        original.sort(key=lambda e: e["line"])
        reqs = [r for r in parsed["requests"] if any(start <= line <= end for line in r.get("source_lines", [r.get("first_line", 0)]))]
        old_bytes, new_bytes = len(encoded(original)), len(encoded(proposed))
        adaptive = adapt_sequence(run=run, observations=observations, original=original, discovery=discovery,
                                  base=view, fields=case["fields"], retained_lines=case.get("retain_result_lines", []),
                                  session=session, thread=thread, home=home, manual_proofs=manual_proofs)
        adapted_checks = []
        for expected in case.get("expected", []):
            hits = [f for f in adaptive["visible_facts"] if all((f.get(k) or "unspecified" if k == "role" else f.get(k)) == v
                                                             for k, v in expected.items())]
            adapted_checks.append({"expected": expected, "preserved": bool(hits), "evidence": sorted({h["evidence"] for h in hits})})
        for field in case.get("expected_missing", []):
            adapted_checks.append({"expected_missing": field, "preserved": field in adaptive["answer"]["context"]["missing_in_known_prefix"]})
        adaptive["checks"] = adapted_checks
        adaptive["all_checks_pass"] = all(c["preserved"] for c in adapted_checks)
        adaptive["rendered_utf8_bytes"]["difference_from_recorded"] = adaptive["rendered_utf8_bytes"]["adapted"] - old_bytes
        results.append({"id": case["id"], "question": case["question"], "bounds": [start, end], "run": case["run"],
                        "decision_lines_without_message": sorted(set(case.get("decision_lines", [])) - {e["source"]["line"] for e in decisions}),
                        "original": original, "proposed": proposed, "adaptive": adaptive, "checks": checks, "all_checks_pass": all(c["preserved"] for c in checks),
                        "rendered_utf8_bytes": {"original": old_bytes, "proposed": new_bytes, "difference": new_bytes - old_bytes},
                        "acquisitions_executed_in_replay": 0, "acquisitions_preserved_in_proposal": len(calls),
                        "deterministic_projection_calls": 1, "historical_artifact_version_available": False,
                        "observed_sequence_requests": [{"id": r["request_id"], "first_line": r.get("first_line"), "source_lines": r.get("source_lines"),
                                                         "usage": r["usage"]} for r in reqs],
                        "sequence_usage": T.totals({**parsed, "requests": reqs})})
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise ValueError("source modifiee pendant le rejeu")
    return {"schema": "agentwatch.run-replay.v1", "source": str(source), "source_sha256": source_hash,
            "coverage": coverage, "cases": results, "local_processing_ms": (time.perf_counter() - started) * 1000, "limits": R.LIMITS + [
                "Projection locale des sorties APRES acquisition consignée; commandes originales jamais executees.",
                "Arguments, notifications, erreurs, complements et decisions selectionnees inclus dans les volumes UTF-8 JSON compact.",
                "Requetes chevauchant chaque sequence: leurs autres actions restent incluses dans les tokens, sans attribution a cette lecture.",
                "Les cas peuvent se chevaucher; leurs tokens ne se somment pas. Aucun gain reel de session mesure."]}


def main():
    # The replay measures UTF-8. Windows pipe encodings must not silently turn
    # its printable answer into cp1252 (or fail on a character outside it).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--home", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--thread", required=True)
    p.add_argument("--cases", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--answer", help="ID d'un cas: afficher seulement sa reponse adaptee; audit complet toujours ecrit dans --out")
    args = p.parse_args()
    from agentwatch.config import load_config
    cfg = load_config(args.home)
    out = Path(args.out)
    for source in R.I.session_files(cfg, args.session):
        if source.resolve() == out.resolve() or (out.exists() and out.samefile(source)):
            raise ValueError("le fichier de sortie est un journal source")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.answer and sum(c["id"] == args.answer for c in cases) != 1:
        raise ValueError("--answer doit identifier exactement un cas")
    result = replay(cfg, args.home, args.session, args.thread, cases)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.answer:
        matches = [c for c in result["cases"] if c["id"] == args.answer]
        print(encoded(matches[0]["adaptive"]["answer"]).decode("utf-8"))
    else:
        print(json.dumps({"out": str(out), "checks": all(c["all_checks_pass"] and c["adaptive"]["all_checks_pass"] for c in result["cases"]),
                          "volumes": {c["id"]: c["adaptive"]["rendered_utf8_bytes"] for c in result["cases"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
