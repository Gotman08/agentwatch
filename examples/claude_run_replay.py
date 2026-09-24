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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentwatch.collector import transcripts as T
from agentwatch.reports import inspect_runs as R


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def replay(cfg: dict, home: str, session: str, thread: str, cases: list[dict]) -> dict:
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
        if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end):
            raise ValueError("bornes physiques positives et ordonnees requises")
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
        for call in calls:
            entry = {"kind": "appel", "line": call["source"]["line"], "call_id": call["data"]["call_id"], "input": call["data"]["input"]}
            original.append(entry)
            acquisitions.append(entry)
        for obs in observations:
            entry = {"kind": obs["kind"], "line": obs["source"]["line"], "content": obs["text"], "is_error": obs["is_error"]}
            original.append(entry)
            # Notifications and failed/transport-only results cannot be hidden
            # as though successful measurements replaced them.
            if obs["kind"] == "notification" or obs["is_error"] or obs["transport_receipt"] or not obs["facts"] or obs["source"]["line"] in case.get("retain_result_lines", []):
                common.append(entry)
        for event in available:
            if event["source"]["line"] in case.get("common_lines", []) and event["kind"] in {"appel", "resultat", "message"}:
                entry = {"kind": event["kind"], "line": event["source"]["line"], "data": event["data"]}
                original.append(entry)
                common.append(entry)
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
        results.append({"id": case["id"], "question": case["question"], "bounds": [start, end], "run": case["run"],
                        "decision_lines_without_message": sorted(set(case.get("decision_lines", [])) - {e["source"]["line"] for e in decisions}),
                        "original": original, "proposed": proposed, "checks": checks, "all_checks_pass": all(c["preserved"] for c in checks),
                        "rendered_utf8_bytes": {"original": old_bytes, "proposed": new_bytes, "difference": new_bytes - old_bytes},
                        "acquisitions_executed_in_replay": 0, "acquisitions_preserved_in_proposal": len(calls),
                        "deterministic_projection_calls": 1, "historical_artifact_version_available": False,
                        "observed_sequence_requests": [{"id": r["request_id"], "first_line": r.get("first_line"), "source_lines": r.get("source_lines"),
                                                         "usage": r["usage"]} for r in reqs],
                        "sequence_usage": T.totals({**parsed, "requests": reqs})})
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise ValueError("source modifiee pendant le rejeu")
    return {"schema": "agentwatch.run-replay.v1", "source": str(source), "source_sha256": source_hash,
            "coverage": coverage, "cases": results, "limits": R.LIMITS + [
                "Projection locale des sorties APRES acquisition consignée; commandes originales jamais executees.",
                "Arguments, notifications, erreurs, complements et decisions selectionnees inclus dans les volumes UTF-8 JSON compact.",
                "Requetes chevauchant chaque sequence: leurs autres actions restent incluses dans les tokens, sans attribution a cette lecture.",
                "Les cas peuvent se chevaucher; leurs tokens ne se somment pas. Aucun gain reel de session mesure."]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--home", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--thread", required=True)
    p.add_argument("--cases", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    from agentwatch.config import load_config
    cfg = load_config(args.home)
    out = Path(args.out)
    for source in R.I.session_files(cfg, args.session):
        if source.resolve() == out.resolve() or (out.exists() and out.samefile(source)):
            raise ValueError("le fichier de sortie est un journal source")
    result = replay(cfg, args.home, args.session, args.thread, json.loads(Path(args.cases).read_text(encoding="utf-8")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "checks": all(c["all_checks_pass"] for c in result["cases"]),
                      "volumes": {c["id"]: c["rendered_utf8_bytes"] for c in result["cases"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
