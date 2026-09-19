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
RC = (9000, 8800, 400)       # * demande de compaction : Codex resume l'historique (des tokens reels, pas une reponse)
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
        b.usage(*RC)
        b.add("compacted", {"message": "resume", "window_number": 1, "replacement_history": [],
                            "compaction_response_id": f"resp_{ROOT[-2:]}_{b.responses}"})
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
        self.assertEqual(tot["requests"], 4)                          # * toutes les requetes au modele, compaction comprise
        self.assertEqual(tot["input_tokens"], R1[0] + R2[0] + RC[0] + R3[0])
        self.assertEqual(tot["cached_input_tokens"], R1[1] + R2[1] + RC[1] + R3[1])
        self.assertEqual(tot["output_tokens"], R1[2] + R2[2] + RC[2] + R3[2])

    def test_per_response_records_match_the_rollout(self) -> None:
        per = [m.meta["usage"] for m in self.view.markers if m.phase == "usage" and m.meta["usage"].get("scope") == "response"]
        self.assertEqual([u["input_tokens"] for u in per], [R1[0], R2[0], RC[0], R3[0]])
        self.assertEqual(per[0]["emitted_calls"], 2)                  # * une reponse, deux appels emis
        m = CMP.measure(self.view, self.cfg)
        self.assertEqual((m["responses"], m["input_tokens"]), (3, R1[0] + R2[0] + R3[0]))   # * reponses : hors compaction
        self.assertEqual(m["compaction"], {"requests": 1, "input_tokens": RC[0], "cached_input_tokens": RC[1], "output_tokens": RC[2]})

    def test_compaction_request_is_recognised_by_id_and_by_position(self) -> None:
        from agentwatch.reports.stats import compaction_request_ids, compaction_requests
        rid = f"resp_{ROOT[-2:]}_3"
        self.assertEqual(compaction_requests(self.view), {rid: "identifiant"})
        comp = next(m for m in self.view.markers if m.phase == "compact_end")
        self.assertEqual((comp.meta.get("compaction_response_id"), comp.meta.get("replacement_items")), (rid, 0))
        comp.meta.pop("compaction_response_id")                        # * import anterieur, sans identifiant
        self.assertEqual(compaction_requests(self.view), {rid: "position"})   # * releve du meme fil juste avant
        self.assertEqual(compaction_request_ids(self.view), {rid})

    def test_status_names_compaction_requests_and_their_basis(self) -> None:
        from agentwatch.reports.stats import _usage_summary
        status = _usage_summary(self.view)["status"]
        self.assertIn("dont 1 demande(s) de compaction (1 reliee(s) a leur ligne compacted par l'identifiant ecrit par Codex, "
                      "0 par position)", status)

    def test_export_labels_the_compaction_request(self) -> None:
        import io
        from agentwatch.reports import inspect as INS
        buf = io.StringIO()
        INS.export_session(self.cfg, str(self.home), ROOT, buf)
        out = buf.getvalue()
        self.assertIn("Demande de compaction `resp_", out)
        self.assertIn("1 demande(s) de compaction ; 1 ligne(s) compacted (historique remplace par Codex)", out)
        self.assertIn("Dont demandes de compaction (MESURE par Codex, comptees a part)", out)
        self.assertRegex(out, r"Ligne compacted : Codex a remplace l'historique \(fenetre 1\) ; demande de compaction "
                              r"`resp_[^`]+`, tokens au releve L\d+")
        self.assertNotIn("Requete de compaction", out)
        self.assertNotIn("compaction reussie", out.lower())

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
