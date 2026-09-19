"""C.batchable : des appels emis dans une meme reponse du modele ne sont pas reproches au modele.

Faux positif reel (2026-09-19, session 38f05d3c) : 3 Read emis ensemble, executes en serie par
Claude Code (ecarts de ~610 ms entre la fin d'un appel et le debut du suivant), signales comme
trois allers-retours.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentwatch.detectors import batchable
from agentwatch.selftest import Synth


class SameResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _reads(self, sid: str, gap_ms: int) -> Synth:
        s = Synth(self.home / sid, session_id=sid)
        s.session_start(); s.user_prompt()
        s.response_gap_ms = gap_ms
        s.read("src/a.py", "A"); s.read("src/b.py", "B"); s.read("src/c.py", "C")
        s.response_gap_ms = 3000
        s.bash("python -m pytest", "ok")
        return s

    def test_serialized_calls_of_one_response_are_not_flagged(self) -> None:
        # * Ecart mesure dans le faux positif reel : ~610 ms (surcout des hooks, pas un aller-retour du modele).
        s = self._reads("fp", 610)
        self.assertEqual(s.findings(["batchable"]), [])

    def test_distinct_responses_are_flagged_with_gaps_as_evidence(self) -> None:
        s = self._reads("tp", 3000)
        f = s.findings(["batchable"])
        self.assertEqual(len(f), 1)
        ev = f[0].evidence
        self.assertEqual(ev["round_trips"], 3)
        self.assertIn("heuristique", ev["separation_basis"])
        self.assertTrue(all(g >= 2000 for g in ev["gaps_ms"]), ev["gaps_ms"])
        self.assertTrue(any("requete emettrice inconnue" in m for m in f[0].missing_data))
        self.assertIn("3 reponses successives", f[0].explanation)

    def test_threshold_is_configurable(self) -> None:
        s = self._reads("cfg", 3000)
        cfg = dict(s.cfg)
        cfg["detectors"] = {**cfg["detectors"], "batchable": {**cfg["detectors"]["batchable"], "same_response_gap_ms": 5000}}
        self.assertEqual(batchable.detect(s.view(), cfg), [])

    def test_transcript_emitter_overrides_the_gap(self) -> None:
        # * Meme requete emettrice malgre un grand ecart : emis ensemble, pas de signalement.
        s = self._reads("tx-same", 3000)
        view = s.view()
        reads = [c for c in view.calls if c.tool_name == "Read"]
        for c in reads:
            c.usage = {"source": "claude-code:transcript", "emitter_request_id": "req_A"}
        self.assertEqual(batchable.detect(view, s.cfg), [])
        # * Requetes distinctes malgre de petits ecarts : trois allers-retours, preuve exacte.
        s2 = self._reads("tx-diff", 50)
        view2 = s2.view()
        for i, c in enumerate(c for c in view2.calls if c.tool_name == "Read"):
            c.usage = {"source": "claude-code:transcript", "emitter_request_id": f"req_{i}"}
        f = batchable.detect(view2, s2.cfg)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].evidence["separation_basis"], "requetes emettrices distinctes (transcript ou rollout)")
        self.assertFalse(any("requete emettrice inconnue" in m for m in f[0].missing_data))

    def test_partial_batching_counts_round_trips(self) -> None:
        # * a et b dans une reponse, puis c et d chacun dans la sienne : 3 allers-retours (b, c, d).
        s = Synth(self.home / "partial", session_id="partial")
        s.session_start(); s.user_prompt()
        s.response_gap_ms = 50
        s.read("src/a.py", "A")
        s.response_gap_ms = 3000
        s.read("src/b.py", "B"); s.read("src/c.py", "C"); s.read("src/d.py", "D")
        f = s.findings(["batchable"])
        self.assertEqual(len(f), 1)
        self.assertEqual([r["target"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for r in f[0].call_refs], ["b.py", "c.py", "d.py"])
        # * le modele a deja emis deux Read ensemble dans cette session : regroupement verifie
        self.assertEqual(f[0].evidence["grouped_tool"]["status"], "verified_in_session")


if __name__ == "__main__":
    unittest.main()
