"""Tranches de temps, reference enregistree et comparaison avant / apres (verification d'une correction).

Principe (consigne du 2026-09-19) : identifier des pertes evitables avec des preuves, puis verifier que les
corrections ameliorent reellement le travail. Aucun gain n'est annonce avant mesure ; une difference n'est dite
demontree que si l'intervalle de confiance exclut l'absence d'effet.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.core.timeslice import parse_when, slice_view
from agentwatch.reports import compare as CMP
from agentwatch.reports.stats import session_tokens
from tests.test_rollouts import ROOT, RolloutBuilder


class RateTests(unittest.TestCase):
    def test_rate_ratio_and_conclusions(self) -> None:
        rr = CMP.rate_ratio(100, 1000, 50, 1000)
        assert rr is not None
        self.assertAlmostEqual(rr["ratio"], 0.5, places=6)
        self.assertLess(rr["high"], 1)
        self.assertEqual(CMP._conclusion(100, 1000, 50, 1000, rr, loss=True), "baisse demontree : amelioration")
        small = CMP.rate_ratio(3, 1000, 1, 1000)
        self.assertEqual(CMP._conclusion(3, 1000, 1, 1000, small, loss=True), "trop peu de donnees pour conclure")
        same = CMP.rate_ratio(40, 1000, 38, 1000)
        self.assertEqual(CMP._conclusion(40, 1000, 38, 1000, same, loss=True), "pas de difference demontree")
        zero = CMP.rate_ratio(20, 1000, 0, 1000)                     # * compte nul : correction de 0,5, pas de division par zero
        assert zero is not None
        self.assertLess(zero["high"], 1)

    def test_parse_when_accepts_utc_and_local(self) -> None:
        self.assertEqual(parse_when("2026-09-19T12:00:00Z"), 1_789_819_200 * 10**9)
        self.assertEqual(parse_when("2026-09-19T14:00:00+02:00"), 1_789_819_200 * 10**9)
        with self.assertRaises(ValueError):
            parse_when("hier")


class SliceAndReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, sessions = root / "home", root / "sessions"
        day = sessions / "2026" / "09" / "19"
        day.mkdir(parents=True)
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"health": {"codex_sessions_dir": str(sessions)},
                                                           "rollouts": {"background_priority": False}}), encoding="utf-8")
        self.cfg = load_config(self.home)
        self.store = EventStore(self.home, self.cfg)
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "Lance le build et attends-le.")
        for i in range(6):                     # * 6 attentes identiques, relancees : 10 h 00 a 10 h 01
            b.fn(f"w{i}", "collaboration", "wait_agent", {"targets": ["w"], "timeout_ms": 30000}, '{"timed_out": true}')
        b.end_turn("turn-1")
        b.t = b.t.replace(hour=15)             # * l'apres-midi : un second tour, sans attente
        b.turn("turn-2", "Verifie le resultat.")
        b.exec_("call_x", [b.cmd("exec-x", "git status --short", " M a.cpp")])
        b.end_turn("turn-2")
        p = day / f"rollout-2026-09-19T10-00-00-{ROOT}.jsonl"
        p.write_text(b.text(), encoding="utf-8", newline="\n")
        R.import_rollouts(self.store, self.cfg, [str(p)])
        self.view = load_session(self.store, "codex", ROOT, self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_slice_keeps_the_period_and_recomputes_tokens(self) -> None:
        morning = slice_view(self.view, None, parse_when("2026-09-19T12:00:00Z"))
        afternoon = slice_view(self.view, parse_when("2026-09-19T12:00:00Z"), None)
        self.assertEqual(len(morning.calls) + len(afternoon.calls), len(self.view.calls))
        self.assertTrue(all(c.tool_name == "collaboration.wait_agent" for c in morning.calls))
        tm, ta, tt = session_tokens(morning), session_tokens(afternoon), session_tokens(self.view)
        assert tm and ta and tt
        self.assertEqual(tm["requests"] + ta["requests"], tt["requests"])      # * chaque reponse dans une seule tranche
        self.assertEqual(tm["input_tokens"] + ta["input_tokens"], tt["input_tokens"])
        self.assertTrue(any("tranche" in w for w in morning.warnings))

    def test_measure_counts_waits_without_gain_and_tasks(self) -> None:
        m = CMP.measure(self.view, self.cfg)
        self.assertEqual(m["counts"].get("G.sans_apport_attente"), 5)          # * 6 attentes : 5 relances sans apport
        self.assertEqual(m["nogain_cost"]["G.sans_apport_attente"]["responses"], 5)
        self.assertEqual(m["tasks"]["main_done"], 2)
        self.assertEqual(m["tasks"]["main_durations_ms"], [5000, 5000])

    def _cli(self, *args: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), *args]), 0)
        return buf.getvalue()

    def test_reference_is_saved_with_an_explicit_period_and_compared(self) -> None:
        ref_path = Path(self.tmp.name) / "ref.json"
        with self.assertRaises(SystemExit):                                     # * pas de reference sans periode explicite
            self._cli("compare", "--client", "codex", "--save-reference", str(ref_path), "--since", "2026-09-19T00:00:00Z")
        out = self._cli("compare", "--client", "codex", "--save-reference", str(ref_path),
                        "--since", "2026-09-19T00:00:00Z", "--until", "2026-09-19T12:00:00Z")
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        self.assertEqual(ref["period"]["until"], "2026-09-19T12:00:00.000Z")
        self.assertEqual(ref["measure"]["counts"].get("G.sans_apport_attente"), 5)
        self.assertIn("## 1. Mesures (constatees sur la periode)", out)
        self.assertIn("## 2. Economies estimees (hypotheses, pas des gains)", out)
        self.assertIn("## 3. Gains constates", out)
        self.assertIn("Aucun a ce jour", out)
        cmp_out = self._cli("compare", "--client", "codex", "--reference", str(ref_path), "--since", "2026-09-19T12:00:00Z")
        self.assertIn("Ecarts testes", cmp_out)
        self.assertIn("trop peu de donnees pour conclure", cmp_out)             # * 1 journee synthetique : rien de demontre
        with self.assertRaises(SystemExit):                                     # * la periode comparee suit la reference
            self._cli("compare", "--client", "codex", "--reference", str(ref_path), "--since", "2026-09-19T06:00:00Z")

    def test_report_can_be_restricted_to_a_day_or_a_period(self) -> None:
        out = self._cli("report", "--client", "codex", "--session", ROOT, "--format", "json",
                        "--since", "2026-09-19T12:00:00Z")
        data = json.loads(out)
        self.assertEqual(data["stats"]["calls"], 1)
        self.assertTrue(any("tranche" in w for w in data["session"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
