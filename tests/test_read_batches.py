"""A.redundant_reads : un lot de lectures refait ensemble est UN signalement (constate le 2026-09-19).

Un sous-agent Codex a relu d'un bloc 26 tickets Linear 24 s apres les avoir lus, dans un nouvel exec, contenu
identique : 26 signalements pour un seul geste. Le regroupement exige la preuve exacte (requetes emettrices).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentwatch.detectors import redundant_reads
from agentwatch.selftest import Synth


class ReadBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _batch(self, sid: str, items: int, emitters: bool = True):
        s = Synth(self.home / sid, session_id=sid)
        s.session_start(); s.user_prompt()
        s.response_gap_ms = 50
        for rnd in range(2):
            for i in range(items):
                s.mcp("linear", "get_issue", {"id": f"X-{i}"}, json.dumps({"id": f"X-{i}", "title": f"t{i}"}))
            s.tick(24_000)
        view = s.view()
        if emitters:
            reads = [c for c in view.calls if c.tool_name == "mcp__linear__get_issue"]
            for k, c in enumerate(reads):
                c.usage = {"source": "codex:rollout", "emitter_request_id": "resp_1" if k < items else "resp_2"}
        return view, s.cfg

    def test_batch_reread_is_one_finding(self) -> None:
        view, cfg = self._batch("batch", 5)
        f = redundant_reads.detect(view, cfg)
        self.assertEqual([x.kind for x in f], ["repeated_read_batch"])
        ev = f[0].evidence
        self.assertEqual(ev["items"], 5)
        self.assertEqual(len(f[0].calls), 10)
        self.assertEqual((ev["first_request"], ev["second_request"]), ("resp_1", "resp_2"))
        self.assertEqual(len(ev["member_findings"]), 5)
        self.assertIn("Lot de 5 lectures", f[0].title)

    def test_small_batch_stays_as_pairs(self) -> None:
        view, cfg = self._batch("small", 2)
        f = redundant_reads.detect(view, cfg)
        self.assertEqual(sorted(x.kind for x in f), ["repeated_read", "repeated_read"])

    def test_no_merge_without_exact_emitters(self) -> None:
        view, cfg = self._batch("noemit", 5, emitters=False)
        f = redundant_reads.detect(view, cfg)
        self.assertEqual(len(f), 5)
        self.assertTrue(all(x.kind == "repeated_read" for x in f))


if __name__ == "__main__":
    unittest.main()
