"""Les tokens d'une reponse du modele ne sont comptes qu'une fois, meme quand elle emet plusieurs appels.

Cas vise : une reponse emet un `exec` (trois actions imbriquees) ET un appel de fonction ; la reponse suivante
consomme leurs sorties. Chaque somme d'AgentWatch doit retrouver exactement les releves de Codex : totaux de
session, releves par reponse, parts attribuees aux appels, contexte relu (une fois par reponse emettrice).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.detectors import base as B
from agentwatch.detectors import repeated_calls as G
from agentwatch.reports import compare as CMP
from agentwatch.reports.stats import session_tokens
from tests.test_rollouts import ROOT, RolloutBuilder

R1 = (5000, 4000, 100)       # * reponse 1 : emet l'exec (3 actions) et la fonction
R2 = (7000, 5000, 60)        # * reponse 2 : consomme les 4 sorties, repond
R3 = (1200, 1100, 30)        # * reponse 3 : fin du tour


class TokenAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, sessions = root / "home", root / "sessions"
        day = sessions / "2026" / "09" / "19"
        day.mkdir(parents=True)
        self.home.mkdir()
        (self.home / "config.json").write_text(
            '{"health": {"codex_sessions_dir": "%s"}, "rollouts": {"background_priority": false}}' % str(sessions).replace("\\", "/"),
            encoding="utf-8")
        self.cfg = load_config(self.home)
        self.store = EventStore(self.home, self.cfg)
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "Construis et verifie.")
        b.add("response_item", {"type": "custom_tool_call", "id": "ctc_x", "status": "completed", "call_id": "call_x",
                                "name": "exec", "input": "code"})
        b.add("response_item", {"type": "function_call", "id": "fc_f", "name": "wait_agent", "namespace": "collaboration",
                                "arguments": '{"targets": ["w"], "timeout_ms": 30000}', "call_id": "call_f"})
        b.usage(*R1)
        for i, out in enumerate(("aaaa", "bbbbbbbb", "cc")):
            start = b.ms()
            b.add("event_msg", {"type": "item_completed", "thread_id": ROOT, "turn_id": "t", "item": b.cmd(f"exec-{i}", f"echo {i}", out),
                                "started_at_ms": start, "completed_at_ms": start + 50}, ms=60)
        b.add("response_item", {"type": "custom_tool_call_output", "id": "o_x", "call_id": "call_x",
                                "output": [{"type": "input_text", "text": "sortie de l'exec"}]})
        b.add("response_item", {"type": "function_call_output", "id": "fo_f", "call_id": "call_f", "output": '{"timed_out": true}'})
        b.usage(*R2)
        b.end_turn("turn-1")
        p = day / f"rollout-2026-09-19T10-00-00-{ROOT}.jsonl"
        p.write_text(b.text(), encoding="utf-8", newline="\n")
        R.import_rollouts(self.store, self.cfg, [str(p)])
        self.view = load_session(self.store, "codex", ROOT, self.cfg)
        self.calls = [c for c in self.view.calls if c.call_id in ("exec-0", "exec-1", "exec-2", "call_f")]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_session_totals_count_each_response_once(self) -> None:
        tot = session_tokens(self.view)
        self.assertEqual(tot["requests"], 3)
        self.assertEqual(tot["input_tokens"], R1[0] + R2[0] + R3[0])
        self.assertEqual(tot["cached_input_tokens"], R1[1] + R2[1] + R3[1])
        self.assertEqual(tot["output_tokens"], R1[2] + R2[2] + R3[2])

    def test_per_response_records_match_the_rollout(self) -> None:
        per = [m.meta["usage"] for m in self.view.markers if m.phase == "usage" and m.meta["usage"].get("scope") == "response"]
        self.assertEqual([u["input_tokens"] for u in per], [R1[0], R2[0], R3[0]])
        self.assertEqual(per[0]["emitted_calls"], 2)                  # * une reponse, deux appels emis
        self.assertEqual(CMP.measure(self.view, self.cfg)["input_tokens"], R1[0] + R2[0] + R3[0])

    def test_call_shares_add_up_to_the_responses(self) -> None:
        self.assertEqual(len(self.calls), 4)
        # * sortie de la reponse emettrice : partagee entre l'exec (ses 3 actions) et la fonction, jamais recopiee
        self.assertEqual(sum(c.usage["output_tokens"] for c in self.calls), R1[2])
        # * entree non mise en cache de la reponse consommatrice : repartie au prorata des sorties, somme exacte
        self.assertEqual(sum(c.usage["uncached_input_tokens"] for c in self.calls), R2[0] - R2[1])
        cost = B.observed_cost(self.calls)
        self.assertEqual(cost["tokens"]["total"], R1[2] + (R2[0] - R2[1]))

    def test_context_reread_is_counted_once_per_emitting_response(self) -> None:
        # * chaque appel porte l'entree totale de sa reponse emettrice : sommee une seule fois par reponse
        self.assertTrue(all(c.usage.get("emitter_input_tokens") == R1[0] for c in self.calls))
        self.assertEqual(G._context_reread(self.calls), R1[0])


if __name__ == "__main__":
    unittest.main()
