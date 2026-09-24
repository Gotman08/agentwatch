"""Synthetic tests for the bounded development usage example."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from examples import development_usage as U


ROOT = "01a0aaaa-0000-7000-8000-000000000001"
CHILD = "01a0aaaa-0000-7000-8000-000000000002"
OTHER = "01a0aaaa-0000-7000-8000-000000000003"


def _meta(thread: str, *, root: str = ROOT, parent: str | None = None, agent_path: str | None = None) -> dict:
    if parent:
        source = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1,
                                                               "agent_path": agent_path, "agent_role": "worker"}}})
        session_id = root
        thread_source = "subagent"
    else:
        source = "vscode"
        session_id = thread
        thread_source = "user"
    return {"type": "session_meta", "payload": {"id": thread, "session_id": session_id,
            "cwd": "C:/synthetic", "source": source, "thread_source": thread_source}}


def _usage(thread: str, response: str, *, root: str = ROOT, inp: int | None = 10,
           cached: int | None = 4, out: int | None = 6, reasoning: int | None = 2,
           total: int | None = 16, cache_write: int | None = 0,
           cumulative: int | dict[str, int] | None = None, turn: str = "turn") -> dict:
    usage = {}
    for key, value in (("input_tokens", inp), ("cached_input_tokens", cached),
                       ("cache_write_input_tokens", cache_write), ("output_tokens", out),
                       ("reasoning_output_tokens", reasoning), ("total_tokens", total)):
        if value is not None:
            usage[key] = value
    cumulative_usage = None
    if isinstance(cumulative, dict):
        cumulative_usage = dict(cumulative)
        cumulative_usage.setdefault("cache_write_input_tokens", 0)
    elif cumulative is not None:
        cumulative_usage = {"input_tokens": cumulative, "cached_input_tokens": cumulative // 2,
                            "cache_write_input_tokens": 0,
                            "output_tokens": cumulative, "reasoning_output_tokens": cumulative // 3,
                            "total_tokens": cumulative * 2}
    payload = {"thread_id": thread, "session_id": root, "turn_id": turn, "root_turn_id": "root-" + turn,
               "response_id": response, "usage": usage}
    if cumulative_usage is not None:
        payload["thread_token_usage"] = cumulative_usage
    return {"type": "token_usage_record", "payload": payload}


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps({"type": row["type"], "payload": row["payload"]}) + "\n" for row in rows),
                    encoding="utf-8", newline="\n")


class DevelopmentUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rollouts = self.root / "rollouts"
        self.rollouts.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_selects_bounded_root_and_prefixed_children_and_checks_deltas(self) -> None:
        root_path = self.rollouts / f"rollout-2026-09-24T00-00-00-{ROOT}.jsonl"
        root_rows = [_meta(ROOT), _usage(ROOT, "baseline", inp=10, cached=5, out=10, reasoning=3, total=20,
                                         cumulative={"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 10,
                                                      "reasoning_output_tokens": 3, "total_tokens": 20}),
                     _usage(ROOT, "r1", inp=5, cached=2, out=3, reasoning=1, total=8,
                            cumulative={"input_tokens": 15, "cached_input_tokens": 7, "output_tokens": 13,
                                        "reasoning_output_tokens": 4, "total_tokens": 28}),
                     {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 99}}}},
                     _usage(ROOT, "r2", inp=2, cached=1, out=1, reasoning=0, total=3,
                            cumulative={"input_tokens": 17, "cached_input_tokens": 8, "output_tokens": 14,
                                        "reasoning_output_tokens": 4, "total_tokens": 31})]
        _write(root_path, root_rows)

        child_path = self.rollouts / f"rollout-2026-09-24T00-01-00-{CHILD}.jsonl"
        child_rows = [_meta(CHILD, parent=ROOT, agent_path="/root/cycle3_worker"),
                      _meta(ROOT),  # copied parent metadata, ignored by read_thread_meta
                      _usage(ROOT, "parent-copy", inp=999, cached=1, out=1, reasoning=0, total=1000, cumulative=999),
                      _usage(CHILD, "c1", inp=7, cached=3, out=4, reasoning=2, total=11,
                             cumulative={"input_tokens": 7, "cached_input_tokens": 3, "output_tokens": 4,
                                         "reasoning_output_tokens": 2, "total_tokens": 11})]
        _write(child_path, child_rows)

        other_path = self.rollouts / f"rollout-2026-09-24T00-02-00-{OTHER}.jsonl"
        _write(other_path, [_meta(OTHER, parent=ROOT, agent_path="/root/other"),
                            _usage(OTHER, "other", inp=100, cached=10, out=10, reasoning=2, total=110, cumulative=100)])

        report = U.build_report(self.rollouts, ROOT, 3, "/root/cycle3_")
        self.assertEqual({Path(row["path"]).name for row in report["selected_files"]}, {root_path.name, child_path.name})
        self.assertEqual(report["response_count"], 3)
        self.assertEqual(report["totals"]["input_tokens"], 14)
        self.assertEqual(report["totals"]["cached_input_tokens"], 6)
        self.assertEqual(report["totals"]["output_tokens"], 8)
        self.assertEqual(report["verification"]["thread_delta"]["input_tokens"], 14)
        self.assertTrue(report["verification"]["matches"])
        self.assertEqual(report["root_turn_ids"], ["root-turn"])
        self.assertEqual(report["counters"]["excluded_before_root_start"], 1)
        self.assertEqual(report["counters"]["excluded_parent_copy"], 1)
        self.assertEqual(report["counters"]["token_count_excluded"], 1)
        self.assertEqual(report["first_source"], {"file": str(root_path), "line": 3})
        self.assertEqual(report["last_source"], {"file": str(child_path), "line": 4})

    def test_missing_fields_and_conflicting_duplicate_id_are_unknown(self) -> None:
        path = self.rollouts / f"rollout-2026-09-24T00-00-00-{ROOT}.jsonl"
        rows = [_meta(ROOT),
                _usage(ROOT, "same", inp=4, cached=1, out=2, reasoning=1, total=6, cumulative=4),
                _usage(ROOT, "same", inp=4, cached=1, out=2, reasoning=1, total=6, cumulative=4),
                _usage(ROOT, "same", inp=9, cached=1, out=2, reasoning=1, total=11, cumulative=9),
                _usage(ROOT, "missing", inp=None, cached=1, out=2, reasoning=1, total=3, cumulative=None)]
        _write(path, rows)
        report = U.build_report(self.rollouts, ROOT, 2, "/root/cycle3_")
        self.assertEqual(report["response_count"], 2)
        self.assertEqual(report["counters"]["duplicate_response_id"], 2)
        self.assertEqual(report["counters"]["conflicting_response_id"], 1)
        self.assertEqual(report["counters"]["incomplete_response"], 2)
        self.assertIsNone(report["totals"]["input_tokens"])
        self.assertEqual(report["totals"]["output_tokens"], 4)
        self.assertTrue(any(row["response_id"] is None and row.get("conflict") for row in report["responses"]))

    def test_invariants_do_not_double_sum_cached_or_reasoning_tokens(self) -> None:
        path = self.rollouts / f"rollout-2026-09-24T00-00-00-{ROOT}.jsonl"
        row = _meta(ROOT)
        usage = _usage(ROOT, "r1", inp=10, cached=6, out=8, reasoning=3, total=18, cumulative=10)
        _write(path, [row, usage])
        report = U.build_report(self.rollouts, ROOT, 2, "/root/cycle3_")
        self.assertEqual(report["totals"], {"input_tokens": 10, "cached_input_tokens": 6,
                                             "cache_write_input_tokens": 0, "output_tokens": 8,
                                             "reasoning_output_tokens": 3, "total_tokens": 18})
        self.assertEqual(report["counters"]["invariant_violation"], 0)
        verification = report["verification"]["per_file"][0]
        self.assertEqual(verification["baseline_basis"], "unobserved_before_root_start")
        self.assertIsNone(verification["baseline"])
        self.assertIsNone(verification["thread_delta"]["input_tokens"])
        self.assertFalse(report["completeness"]["all_thread_deltas_known"])

    def test_invalid_numeric_fields_stay_unknown_without_zero_fallback(self) -> None:
        numbers, missing = U._numeric_snapshot({
            "input_tokens": 100,
            "cached_input_tokens": 7,
            "output_tokens": "bad",
            "cache_write_input_tokens": 1.5,
            "reasoning_output_tokens": -2,
            "total_tokens": "bad",
        })
        self.assertEqual(numbers["input_tokens"], 100)
        self.assertEqual(numbers["cached_input_tokens"], 7)
        self.assertIsNone(numbers["output_tokens"])
        self.assertIsNone(numbers["cache_write_input_tokens"])
        self.assertIsNone(numbers["reasoning_output_tokens"])
        self.assertIsNone(numbers["total_tokens"])
        self.assertEqual(set(missing), {"output_tokens", "cache_write_input_tokens",
                                        "reasoning_output_tokens", "total_tokens"})
        self.assertNotEqual(numbers["output_tokens"], 0)

    def test_metadata_conflict_clears_response_identity_and_keeps_only_agreed_numbers(self) -> None:
        path = self.rollouts / f"rollout-2026-09-24T00-00-00-{ROOT}.jsonl"
        _write(path, [_meta(ROOT),
                      _usage(ROOT, "same", inp=4, cached=1, out=2, reasoning=1, total=6,
                             cumulative=4, turn="one"),
                      _usage(ROOT, "same", inp=4, cached=1, out=2, reasoning=1, total=6,
                             cumulative=4, turn="two")])
        report = U.build_report(self.rollouts, ROOT, 2, "/root/cycle3_")
        self.assertEqual(report["counters"]["conflicting_metadata"], 1)
        self.assertEqual(report["totals"]["input_tokens"], 4)
        conflict = report["responses"][0]
        self.assertIsNone(conflict["response_id"])
        self.assertIn("turn_id", conflict["conflicting_metadata_fields"])
        self.assertTrue(conflict["conflict"])

    def test_cli_writes_report_and_never_overwrites_source(self) -> None:
        path = self.rollouts / f"rollout-2026-09-24T00-00-00-{ROOT}.jsonl"
        _write(path, [_meta(ROOT), _usage(ROOT, "r1", cumulative=1)])
        out = self.root / "report.json"
        self.assertEqual(U.main(["--rollouts-dir", str(self.rollouts), "--root-thread", ROOT,
                                 "--root-start-line", "2", "--agent-prefix", "/root/cycle3_", "--out", str(out)]), 0)
        loaded = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["root_thread"], ROOT)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            U.write_report(loaded, path, [str(path)])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(loaded["fingerprint"]["bytes"], len(before))
        self.assertEqual(hashlib.sha256(before).hexdigest(), loaded["fingerprint"]["sha256"])
        hardlink = self.root / "rollout-hardlink.jsonl"
        try:
            os.link(path, hardlink)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaises(ValueError):
            U.write_report(loaded, hardlink, [str(path)])
        self.assertEqual(hardlink.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
