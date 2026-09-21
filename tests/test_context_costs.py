"""Ce que le modele recoit vraiment, ce que les sorties ajoutent au contexte, ce qui est relu apres une compaction,
et les tours coupes par le client.

# * Rollouts SYNTHETIQUES, ecrits d'apres les formes relevees le 2026-09-21 sur la session Codex 01a0bf95 (noms de
#   champs, avertissements de coupe et categories d'erreur seulement ; aucun contenu reel). Constats d'origine :
#   une sortie brute de 828 Ko livree en 40 000 caracteres ; une image de 1 Mo comptee au poids de son encodage ;
#   le tour coupe par `usage_limit_exceeded` compte comme « termine » ; 47 % de l'entree de la session etait la
#   relecture de sorties encore en contexte.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.reports import compare as C
from agentwatch.reports.context import context_costs
from tests.test_rollouts import ROOT, RolloutBuilder

CUT_WARNING = "Warning: truncated output (original token count: 195820)\nTotal output lines: 12691\n\n"


def cut_cmd(item_id: str, script: str, raw_chars: int, delivered_chars: int) -> dict[str, Any]:
    it = RolloutBuilder.cmd(item_id, script, "x" * raw_chars)
    it["formatted_output"] = CUT_WARNING + "x" * (delivered_chars - len(CUT_WARNING))
    return it


class ContextCostsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, sessions = root / "home", root / "sessions"
        self.day = sessions / "2026" / "09" / "21"
        self.day.mkdir(parents=True)
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"health": {"codex_sessions_dir": str(sessions)},
                                                           "rollouts": {"background_priority": False}}), encoding="utf-8")
        self.cfg = load_config(self.home)
        self.store = EventStore(self.home, self.cfg)
        self.path = self.day / f"rollout-2026-09-21T10-00-00-{ROOT}.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _view(self, b: RolloutBuilder) -> Any:
        self.path.write_text(b.text(), encoding="utf-8", newline="\n")
        out = R.import_rollouts(self.store, self.cfg, [str(self.path)])
        self.assertEqual(out["errors"], [])
        return load_session(self.store, "codex", ROOT, self.cfg)

    # ------------------------------------------------------------------ ce que le modele recoit
    def test_truncated_command_is_recorded_and_weighted_by_what_was_delivered(self) -> None:
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        b.exec_("call_big", [cut_cmd("exec-big", "Get-Content -LiteralPath 'Saved/result.json' -Raw", 800_000, 20_000),
                             RolloutBuilder.cmd("exec-small", "Get-Content -LiteralPath 'Docs/a.md' -Raw", "y" * 20_000)],
                usage=(50_000, 49_000, 300),
                output="Script completed\n" + "x" * 12_000 + "\u20263554 tokens truncated\u2026" + "y" * 12_000)
        b.usage(62_300, 50_000, 100)          # * reponse qui consomme la sortie : 12 000 tokens de plus que 50 000 + 300
        v = self._view(b)
        calls = {c.call_id: c for c in v.calls}
        big, small = calls["exec-big"], calls["exec-small"]
        self.assertEqual((big.output_truncated, big.evidence.get("original_token_count"), big.evidence.get("delivered_chars")),
                         (True, 195820, 20_000))
        self.assertEqual((small.output_truncated, small.evidence.get("delivered_chars")), (False, 20_000))
        self.assertGreater(big.output_size_bytes, 800_000)                    # * la taille brute reste relevee
        # * exec coupe au milieu : fait de l'exec entier, porte par ses actions
        self.assertEqual((big.evidence.get("exec_truncated_tokens"), small.evidence.get("exec_truncated_tokens")), (3554, 3554))
        self.assertGreater(big.evidence.get("exec_delivered_chars"), 24_000)
        # * partage au prorata de ce qui est LIVRE : deux sorties livrees a 20 000 caracteres pesent autant
        #   (au prorata du brut, la premiere aurait pris 97,6 % des tokens)
        self.assertEqual(big.usage["uncached_input_tokens"], small.usage["uncached_input_tokens"])
        ctx = context_costs(v, self.cfg)
        self.assertEqual(ctx["added_tokens"], 12_000)                          # * 62 300 - 50 000 - 300, mesure
        self.assertEqual((ctx["truncation"]["known"], ctx["truncation"]["truncated_calls"], ctx["truncation"]["execs_cut_in_the_middle"],
                          ctx["truncation"]["tokens_cut_from_execs"]), (True, 1, 1, 3554))

    def test_encoded_image_does_not_weigh_its_base64(self) -> None:
        image_json = json.dumps({"content": [{"type": "text", "text": "Successfully fetched 1 image."},
                                             {"type": "image", "data": "A" * 1_000_000, "mimeType": "image/png"}]})
        self.assertEqual(R.mcp_delivery([{"type": "text", "text": image_json}]), (len("Successfully fetched 1 image."), 1))
        self.assertEqual(R.mcp_delivery([{"type": "image", "data": "A" * 10}, {"type": "text", "text": "abc"}]), (3, 1))
        self.assertEqual(R.mcp_delivery([{"type": "text", "text": '{"pas": "une image"}'}]), (20, 0))
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        b.exec_("call_img", [RolloutBuilder.mcp("exec-img", "linear", "extract_images", {"markdown": "SECRET"}, image_json),
                             RolloutBuilder.mcp("exec-txt", "linear", "get_issue", {"id": "NYK-1"}, "t" * 24_000)],
                usage=(60_000, 59_000, 200))
        b.usage(70_200, 60_000, 100)
        v = self._view(b)
        calls = {c.call_id: c for c in v.calls}
        self.assertEqual((calls["exec-img"].evidence.get("image_parts"), calls["exec-img"].evidence.get("text_chars")), (1, 29))
        # * l'image (1 Mo encode) ne prend plus l'essentiel des tokens face a 24 000 caracteres de texte
        self.assertLess(calls["exec-img"].usage["uncached_input_tokens"], calls["exec-txt"].usage["uncached_input_tokens"])

    # ------------------------------------------------------------------ residence et relectures apres compaction
    def _two_windows(self) -> RolloutBuilder:
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        brief = RolloutBuilder.cmd("exec-brief-1", "Get-Content -LiteralPath 'Docs/BRIEF.md' -Raw", "b" * 28_000)
        b.exec_("call_1", [brief], usage=(40_000, 39_000, 200))
        b.exec_("call_2", [RolloutBuilder.cmd("exec-code-1", "Get-Content -LiteralPath 'Source/a.cpp' -Raw", "c" * 9_000)],
                usage=(48_200, 40_000, 100))                                  # * le brief a ajoute 8 000 tokens
        b.usage(51_300, 48_000, 100)                                          # * a.cpp a ajoute 3 000 tokens
        b.usage(51_500, 51_000, 100)
        b.usage(200_000, 51_000, 900)                                         # * demande de compaction
        b.add("compacted", {"message": "SECRET SUMMARY", "window_number": 1, "replacement_history": []})
        brief2 = RolloutBuilder.cmd("exec-brief-2", "Get-Content -LiteralPath 'Docs/BRIEF.md' -Raw", "b" * 28_000)
        b.exec_("call_3", [brief2], usage=(43_000, 30_000, 150))
        b.exec_("call_4", [{"type": "FileChange", "id": "patch-1", "status": "completed",
                            "changes": {"C:\\proj\\Source\\a.cpp": {"type": "update", "unified_diff": "@@ SECRET"}}}],
                usage=(51_150, 43_000, 80))                                   # * le brief relu a ajoute 8 000 tokens
        b.exec_("call_5", [RolloutBuilder.cmd("exec-code-2", "Get-Content -LiteralPath 'Source/a.cpp' -Raw", "d" * 9_000)],
                usage=(51_300, 51_000, 90))
        b.usage(54_500, 51_000, 60)
        return b

    def test_added_tokens_are_measured_and_residence_stops_at_the_compaction(self) -> None:
        v = self._view(self._two_windows())
        ctx = context_costs(v, self.cfg)
        by_seq = {o["label"]: o for o in ctx["top_outputs"]}
        first = next(o for o in ctx["top_outputs"] if o["window"] == 0 and "BRIEF" in (o["label"] or ""))
        # * 8 000 tokens ajoutes, relus par les 3 requetes suivantes de la fenetre (dont la demande de compaction)
        self.assertEqual((first["added_tokens"], first["later_requests_in_window"], first["reread_tokens"]), (8_000, 3, 24_000))
        self.assertTrue(by_seq)
        self.assertEqual(ctx["windows_after_compaction"], 1)
        self.assertEqual(ctx["session_input_tokens"], 40_000 + 48_200 + 51_300 + 51_500 + 200_000 + 43_000 + 51_150 + 51_300 + 54_500)
        # * socle : premiere requete du fil (40 000), relue par les 9 requetes ; premiere requete apres compaction : 43 000
        self.assertEqual((ctx["floor"]["thread_first_input"], ctx["floor"]["window_first_input_median"]), ({"main": 40_000}, 43_000))
        self.assertEqual(ctx["floor"]["thread_first_share_of_session_input"], round(9 * 40_000 / ctx["session_input_tokens"], 4))
        fam = {f["family"]: f for f in ctx["families"]}
        self.assertEqual(fam["shell : get-content"]["calls"], 4)

    def test_rereads_after_compaction_say_identical_or_modified(self) -> None:
        v = self._view(self._two_windows())
        ac = context_costs(v, self.cfg)["after_compaction"]
        res = {r["resource"]: r for r in ac["resources"]}
        self.assertEqual(res["docs/brief.md"]["status"], {"identical": 1})                 # * meme empreinte de contenu
        self.assertEqual(res["docs/brief.md"]["added_tokens"], 8_000)
        self.assertEqual(res["source/a.cpp"]["status"], {"modified_between": 1})           # * patch observe entre les deux lectures
        self.assertEqual((ac["rereads"], ac["early"]["rereads"], ac["window_start"]["windows"]), (2, 2, 1))
        self.assertEqual(res["docs/brief.md"]["refs"][0]["source"]["file"], self.path.name)  # * preuve : ligne du rollout

    def test_no_response_records_no_section(self) -> None:
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        self.assertIsNone(context_costs(self._view(b), self.cfg))

    # ------------------------------------------------------------------ quota et tours coupes
    def test_turn_cut_by_the_client_is_not_a_finished_task(self) -> None:
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        for used in (19.0, 19.0, 19.4, 60.0, 100.0):
            b.add("event_msg", {"type": "token_count", "info": None,
                                "rate_limits": {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": 10080,
                                                                                 "resets_at": 1790416205}}})
        b.usage(1000, 900, 50)
        b.add("event_msg", {"type": "task_complete", "turn_id": "turn-1", "duration_ms": 22_187_805, "last_agent_message": None,
                            "error": {"message": "SECRET You've hit your usage limit.", "codex_error_info": "usage_limit_exceeded"}})
        b.turn("turn-2", "SECRET suite")
        b.end_turn("turn-2")
        v = self._view(b)
        lim = context_costs(v, self.cfg)["limits"]
        self.assertEqual((lim["turns_cut"]["total"], lim["turns_cut"]["by_kind"]), (1, {"usage_limit_exceeded": 1}))
        self.assertEqual((lim["quota"]["first_percent"], lim["quota"]["last_percent"], lim["quota"]["readings"],
                          lim["quota"]["window_minutes"]), (19.0, 100.0, 3, 10080))   # * 19,4 : moins d'un point, pas de releve
        tasks = C.measure(v, self.cfg)["tasks"]
        self.assertEqual((tasks["main_done"], tasks["main_cut"], tasks["main_aborted"]), (1, 1, 0))
        self.assertEqual(tasks["main_durations_ms"], [5000])                   # * la duree du tour coupe n'entre pas dans la mediane
        raw = json.dumps([e for e in self.store.read_session_events("codex", ROOT)[0]], ensure_ascii=False)
        self.assertNotIn("SECRET", raw)                                        # * jamais le message d'erreur, seulement sa categorie

    # ------------------------------------------------------------------ compare : trois natures, interruptions visibles
    def test_compare_keeps_exact_attributed_and_scenarios_apart(self) -> None:
        v = self._view(self._two_windows())
        m = C.measure(v, self.cfg)
        exact, attributed = m["context"]["exact"], m["context"]["attributed"]
        # * releves exacts : sommes des releves du client, independantes du partage entre appels
        # * ajoutes : brief 8 000, a.cpp 3 000, brief relu 8 000, patch 70, a.cpp relu 3 110 (differences d'entree)
        self.assertEqual((exact["added_tokens"], exact["windows_after_compaction"], exact["window_first_inputs"]), (22_180, 1, [43_000]))
        self.assertEqual(exact["reread_tokens"], 8_000 * 3 + 3_000 * 2 + 8_000 * 2 + 70 * 1 + 3_110 * 0)
        self.assertNotIn("rereads_after_compaction", exact)
        # * attributions reconstruites : ressource reconnue d'une fenetre a l'autre, etat de la relecture
        self.assertEqual((attributed["rereads_after_compaction"], attributed["windows_with_rereads"], attributed["rereads_unchanged"]), (2, 1, 1))
        self.assertNotIn("added_tokens", attributed)
        merged = C.merge([m, m])                                           # * deux sessions : tout s'additionne
        self.assertEqual((merged["context"]["sessions"], merged["context"]["exact"]["added_tokens"],
                          merged["context"]["exact"]["window_first_inputs"],
                          merged["context"]["attributed"]["families"]["shell : get-content"]["calls"]), (2, 44_360, [43_000, 43_000], 8))
        res = C.compare(merged, merged)
        ctx = res["context"]
        self.assertEqual({r["key"] for r in ctx["exact"]} & {r["key"] for r in ctx["attributed"]}, set())
        by = {r["key"]: r for r in ctx["exact"]}
        self.assertEqual((by["window_first_input"]["before"], by["window_start"]["before"]), (43_000, 11_180))
        self.assertEqual(by["quota_points"]["before"], None)               # * non releve : jamais un faux zero
        sc = ctx["scenarios"][0]
        self.assertEqual((sc[0]["key"], sc[0]["added_tokens"]), ("unchanged_rereads", 16_000))  # * hypothese, a part (2 sessions)
        self.assertIn("borne haute", sc[0]["caveat"])
        md = C.render_markdown({"before": merged, "after": merged, "comparison": res, "at_label": "test", "client": "codex"})
        for title in ("## Contexte : releves exacts", "## Contexte : attributions reconstruites", "## Scenarios d'economie"):
            self.assertIn(title, md)
        self.assertIn("relectures d'une ressource deja lue", md)           # * ligne testee, grappes = fenetres

    def test_cut_turn_stays_in_the_task_balance_as_an_interruption(self) -> None:
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        b.usage(1000, 900, 50)
        b.add("event_msg", {"type": "task_complete", "turn_id": "turn-1", "duration_ms": 1000, "last_agent_message": None,
                            "error": {"message": "SECRET", "codex_error_info": "usage_limit_exceeded"}})
        b.turn("turn-2", "SECRET suite")
        b.end_turn("turn-2")
        m = C.measure(self._view(b), self.cfg)
        row = next(r for r in C.compare(m, m)["rows"] if r["key"] == "taches.main")
        # * 1 terminee sur 2 : le tour coupe reste au denominateur, nomme, ni termine ni disparu
        self.assertEqual((row["before"]["count"], row["before"]["activity"], row["before"]["cut"], row["before"]["rate"]), (1, 2, 1, 50.0))
        self.assertEqual(m["context"]["exact"]["turns_cut_by_kind"], {"usage_limit_exceeded": 1})

    def test_time_slice_does_not_take_a_mid_thread_request_for_the_floor(self) -> None:
        from agentwatch.core.correlate import build_session
        self._view(self._two_windows())
        events, _ = self.store.read_session_events("codex", ROOT)
        # * tranche qui commence apres la premiere requete du fil : le socle n'y est pas releve
        late = [e for e in events if not ((e.get("usage") or {}).get("scope") == "response" and (e["usage"].get("index") or 0) < 2)]
        ctx = context_costs(build_session(late, self.cfg), self.cfg)
        self.assertEqual((ctx["floor"]["thread_first_input"], ctx["floor"]["threads_started_before_view"]), ({}, 1))
        self.assertEqual(ctx["floor"]["window_first_inputs"], [43_000])     # * la fenetre 1 commence dans la tranche : gardee


if __name__ == "__main__":
    unittest.main()
