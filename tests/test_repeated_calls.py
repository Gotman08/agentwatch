"""G.repeated_calls : pourquoi un appel est refait, a quel rythme, et ce qu'on peut y gagner (tous les outils).

Cas d'origine (2026-09-19, sessions Codex 01a0a5ac et 01a08ca6) : des centaines d'appels d'etat identiques. Certains
sondaient un job en cours, d'autres reessayaient un service pas encore actif (legitime), d'autres redemandaient
ce que l'agent savait deja. Le detecteur doit dire lequel, et chiffrer une cadence, sans rien inventer.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentwatch import CLIENT_CODEX
from agentwatch.adapters import base as AB
from agentwatch.core.correlate import Marker
from agentwatch.detectors import repeated_calls as G
from agentwatch.reports.markdown import render_markdown
from agentwatch.selftest import Synth


def _fp(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class ResultFactsTests(unittest.TestCase):
    def test_timestamps_and_durations_do_not_change_the_state(self) -> None:
        a = AB.result_facts('{"status": "RUNNING", "elapsed": 12, "updated_at": "2026-09-19T10:00:00Z", "job": 7}', "success", _fp,
                            "mcp__romeo__job_status")
        b = AB.result_facts('{"status": "RUNNING", "elapsed": 95, "updated_at": "2026-09-19T10:01:30Z", "job": 7}', "success", _fp,
                            "mcp__romeo__job_status")
        c = AB.result_facts('{"status": "COMPLETED", "elapsed": 99, "job": 7}', "success", _fp, "mcp__romeo__job_status")
        self.assertEqual(a["state_fp"], b["state_fp"])
        self.assertEqual((a["result_phase"], c["result_phase"]), ("in_progress", "done"))
        self.assertNotEqual(a["state_fp"], c["state_fp"])
        t1 = AB.result_facts("Job 7 still running (for 3 min) at 10:00:01", "success", _fp, "Bash", "status")
        t2 = AB.result_facts("Job 7 still running (for 5 min) at 10:02:01", "success", _fp, "Bash", "status")
        self.assertEqual(t1["state_fp"], t2["state_fp"])
        self.assertEqual(t1["result_phase"], "in_progress")

    def test_unavailability_is_read_in_a_field_or_the_head_never_in_content(self) -> None:
        self.assertEqual(AB.result_facts('{"error": "Romeo n\'est pas actif (connection refused)"}', "success", _fp)["result_phase"],
                         "unavailable")
        self.assertEqual(AB.result_facts('{"connected": false, "hint": "start the tunnel"}', "success", _fp)["result_phase"],
                         "unavailable")
        # * un mot "unavailable" dans le CONTENU d'une reponse n'est pas une indisponibilite du service
        self.assertEqual(AB.result_facts('{"assets": [{"name": "x", "note": "texture unavailable in LOD2"}]}', "success", _fp)
                         ["result_phase"], "done")
        # * un fichier lu (contenu) n'a jamais de phase textuelle ni d'empreinte d'etat
        r = AB.result_facts("service offline, still running, pending", "success", _fp, "Read", "content")
        self.assertEqual(r, {"state_fp": None, "result_phase": "done"})
        self.assertEqual(AB.result_facts("Connection refused", "error", _fp, "Bash", "run")["result_phase"], "unavailable")
        self.assertEqual(AB.result_facts("Traceback: boom", "error", _fp, "Bash", "run")["result_phase"], "failed")

    def test_facts_kind(self) -> None:
        self.assertEqual(AB.facts_kind("edit", {}), None)
        self.assertEqual(AB.facts_kind("read", {}), "content")
        self.assertEqual(AB.facts_kind("shell", {"shell_heads": ["squeue"]}), "status")
        self.assertEqual(AB.facts_kind("shell", {"shell_heads": ["cat"], "shell_kind": "read"}), "content")
        self.assertEqual(AB.facts_kind("shell", {"shell_heads": ["python"], "shell_kind": "run"}), "run")
        self.assertEqual(AB.facts_kind("mcp", {}, "mcp__romeo__job_status"), "status")
        self.assertEqual(AB.facts_kind("other", {}, "collaboration.wait_agent"), "status")
        # * un outil dont le nom n'annonce pas un etat : seule l'indisponibilite est lue
        self.assertEqual(AB.facts_kind("mcp", {}, "mcp__linear__get_issue"), "run")

    def test_real_cases(self) -> None:
        # * Constate le 2026-09-19 : un ticket Linear "In Progress" relu n'est pas un traitement en cours.
        kind = AB.facts_kind("mcp", {}, "mcp__linear__get_issue")
        self.assertEqual(AB.result_facts('{"id": "X-1", "state": "In Progress", "title": "t"}', "success", _fp,
                                         "mcp__linear__get_issue", kind)["result_phase"], "done")
        # * Gabarit de la fonction `wait` de Codex (44 sorties sur 67) : la cellule tourne encore.
        r = AB.result_facts("Script running with cell ID 3\nWall time 30 seconds\nOutput:\n...", "success", _fp, "wait", "status")
        self.assertEqual(r["result_phase"], "in_progress")
        # * sortie vide : un etat comparable, pas une inconnue
        self.assertEqual(AB.result_facts("", "success", _fp, "Bash", "run")["state_fp"], "empty")


class RepeatedCallsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _synth(self, sid: str, client: str | None = None) -> Synth:
        s = Synth(self.home / sid, session_id=sid, **({"client": client} if client else {}))
        s.session_start(); s.user_prompt()
        return s

    def _group(self, view, tool: str) -> dict:
        res = G.analyse(view, view_cfg(self))
        return next(g for g in res["groups"] if g["tool"] == tool)

    # ------------------------------------------------------------------ sondage
    def test_polling_a_running_job_suggests_a_cadence(self) -> None:
        s = self._synth("poll")
        s.response_gap_ms = 20_000
        for i in range(14):
            state = "RUNNING" if i < 13 else "COMPLETED"
            s.mcp("romeo", "job_status", {"job_id": 42}, json.dumps({"status": state, "elapsed_s": 20 * i}))
        view = s.view()
        g = self._group(view, "mcp__romeo__job_status")
        self.assertEqual(g["calls"], 14)
        self.assertEqual(g["reasons"].get("waiting"), 13)
        self.assertEqual(g["outcomes"].get("same"), 12)        # * seul le dernier resultat change
        self.assertEqual(g["phase_changes"], 1)
        self.assertEqual((g["verdict"], g["kind"]), ("outil", "polling_cadence"))
        rec = g["cadence"]["recommended"]
        self.assertIsNotNone(rec)
        self.assertGreater(rec["avoided"], 0)
        self.assertLessEqual(rec["max_delay_s"], g["cadence"]["tolerated_delay_s"])
        self.assertEqual(g["interval_s"]["pattern"], "cadence fixe")
        f = [x for x in G.detect(view, view_cfg(self))]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].kind, "polling_cadence")
        self.assertIn("Sondage", f[0].title)
        self.assertEqual(f[0].proposal["verdict"], "outil")

    def test_polling_while_a_wait_tool_exists_is_an_agent_issue(self) -> None:
        s = self._synth("poll-wait")
        s.mcp("romeo", "wait_for_job", {"job_id": 1}, json.dumps({"status": "COMPLETED"}))
        s.response_gap_ms = 15_000
        for _ in range(6):
            s.mcp("romeo", "job_status", {"job_id": 42}, json.dumps({"status": "PENDING"}))
        g = self._group(s.view(), "mcp__romeo__job_status")
        self.assertEqual((g["verdict"], g["kind"]), ("agent", "polling_instead_of_wait"))
        self.assertEqual(g["wait_alternative"]["tool"], "mcp__romeo__wait_for_job")

    def test_a_single_in_progress_repeat_is_not_polling(self) -> None:
        # * Faux positif du 2026-09-19 : job_status repris une fois "en cours", puis apres une action ; l'etat a change.
        s = self._synth("thin")
        s.mcp("romeo", "wait_for_job", {"job_id": 1}, json.dumps({"status": "COMPLETED"}))
        s.response_gap_ms = 15_000
        s.mcp("romeo", "job_status", {"job_id": 42}, json.dumps({"status": "PENDING"}))
        s.mcp("romeo", "job_status", {"job_id": 42}, json.dumps({"status": "RUNNING"}))
        s.mcp("romeo", "submit_job", {"script": "x"}, json.dumps({"job_id": 43}))
        s.mcp("romeo", "job_status", {"job_id": 42}, json.dumps({"status": "COMPLETED"}))
        g = self._group(s.view(), "mcp__romeo__job_status")
        self.assertNotEqual(g["kind"], "polling_instead_of_wait")
        self.assertEqual(g["verdict"], "justifie")

    def test_retrying_an_unavailable_service_is_legitimate(self) -> None:
        # * Le cas decrit par l'utilisateur : Romeo pas encore actif, l'agent retente ; les reprises sont justifiees.
        s = self._synth("unavail")
        s.response_gap_ms = 5_000
        for _ in range(8):
            s.mcp("romeo", "romeo_status", {}, json.dumps({"error": "Romeo n'est pas actif : connection refused"}))
        s.mcp("romeo", "romeo_status", {}, json.dumps({"status": "ok"}))
        view = s.view()
        g = self._group(view, "mcp__romeo__romeo_status")
        self.assertEqual(g["reasons"].get("unavailable"), 8)
        self.assertEqual(g["verdict"], "environnement")
        self.assertIn("legitime", g["why"])
        self.assertIn("retry_after", g["suggestion"])

    def test_timed_out_waits_ask_for_a_longer_timeout(self) -> None:
        s = self._synth("wait-timeout", CLIENT_CODEX)
        s.response_gap_ms = 2_500
        for _ in range(5):
            s.call("collaboration.wait_agent", {"targets": ["worker"], "timeout_ms": 30000},
                   {"output": json.dumps({"timed_out": True, "status": {}})}, duration_ms=30_000)
        g = self._group(s.view(), "collaboration.wait_agent")
        # * outil du client : c'est l'agent qui choisit le delai, pas un serveur a modifier. Toutes les reprises sont
        #   des attentes arrivees a echeance, sans rien d'autre d'observe : l'attribution tient.
        self.assertEqual((g["verdict"], g["kind"]), ("agent", "wait_timeout"))
        self.assertIn("timeout_ms=['30000']", g["why"])
        # * La suggestion enonce un fait et une piste, jamais une instruction de modifier l'agent observe.
        self.assertIn("fait observe", g["suggestion"])
        self.assertIn("A examiner", g["suggestion"])
        self.assertNotIn("consigne", g["suggestion"])

    def test_wait_to_deadline_cites_the_longest_delay_honored(self) -> None:
        # * Constate le 2026-09-19 (117 sessions) : wait_agent respecte 300 s et 600 s, mais l'agent demande 30 s
        #   (818 fois, 627 encore en cours au retour). La preuve qu'un delai plus long passe est dans la session.
        s = self._synth("deadline", CLIENT_CODEX)
        s.response_gap_ms = 2_000
        s.call("collaboration.wait_agent", {"targets": ["other"], "timeout_ms": 300000},
               {"output": json.dumps({"timed_out": True})}, duration_ms=300_000)
        for _ in range(4):
            s.call("collaboration.wait_agent", {"targets": ["worker"], "timeout_ms": 30000},
                   {"output": json.dumps({"timed_out": True})}, duration_ms=30_000)
        g = self._group(s.view(), "collaboration.wait_agent")
        self.assertIn("4 attente(s) sur 4 vont jusqu'au delai demande", g["why"])
        self.assertIn("5 min", g["why"])
        self.assertIn("delai de 5 min a deja ete respecte", g["suggestion"])

    def test_wait_returning_early_points_to_intermediate_output(self) -> None:
        # * `wait` de Codex rend la main des qu'une sortie arrive : un script bavard la reveille sans cesse.
        s = self._synth("early", CLIENT_CODEX)
        s.response_gap_ms = 2_000
        for _ in range(5):
            s.call("wait", {"cell_id": "7", "yield_time_ms": 30000}, {"output": "Script running with cell ID 7\nOutput:\nstep"},
                   duration_ms=200)
        g = self._group(s.view(), "wait")
        self.assertEqual(g["kind"], "wait_timeout")
        self.assertIn("5 rendent la main avant", g["why"])
        self.assertIn("progression va dans un journal", g["suggestion"])

    def test_mcp_wait_tool_timing_out_is_a_tool_issue(self) -> None:
        s = self._synth("mcp-wait-timeout")
        s.response_gap_ms = 3_000
        for _ in range(5):
            s.mcp("romeo", "wait_for_job", {"job_id": 7, "timeout_s": 60}, json.dumps({"status": "RUNNING", "timed_out": True}),
                  duration_ms=60_000)
        g = self._group(s.view(), "mcp__romeo__wait_for_job")
        self.assertEqual((g["verdict"], g["kind"]), ("outil", "wait_timeout"))
        self.assertIn("timeout_s=['60']", g["why"])
        self.assertIn("serveur", g["suggestion"])

    def test_periodic_monitoring_script_is_polling_even_if_progress_changes(self) -> None:
        # * Constate le 2026-09-19 : un script de suivi relance 98 fois toutes les ~55 s ; sa sortie change
        #   (compteurs de progression) mais aucune decision n'en decoule. Ce n'est pas "justifie".
        s = self._synth("periodic")
        s.response_gap_ms = 55_000
        for i in range(10):
            s.bash("python monitor.py --state run.json", json.dumps({"frames": 100 * i, "phase": "measure"}))
        view = s.view()
        g = self._group(view, "Bash")
        self.assertTrue(g["periodic"])
        self.assertEqual((g["verdict"], g["kind"]), ("agent", "polling_cadence"))
        self.assertIn("suivi periodique", g["why"])
        self.assertIn("boucle dans le script", g["suggestion"])

    def test_repeated_sleep_is_waiting_not_redundancy(self) -> None:
        s = self._synth("sleep", CLIENT_CODEX)
        s.response_gap_ms = 3_000
        for _ in range(5):
            s.call("clock.sleep", {"seconds": 30}, {"output": ""}, duration_ms=30_000)
        g = self._group(s.view(), "clock.sleep")
        self.assertEqual(g["reasons"], {"waiting": 4})
        self.assertNotEqual(g["kind"], "unexplained_repeats")

    # ------------------------------------------------------------------ redondance et justification
    def test_asking_again_for_what_is_known_is_an_agent_issue(self) -> None:
        s = self._synth("redundant")
        s.response_gap_ms = 4_000
        for _ in range(4):
            s.mcp("unreal", "unreal_asset_info", {"path": "/Game/Hero"}, json.dumps({"class": "Blueprint", "size": 12}))
        view = s.view()
        g = self._group(view, "mcp__unreal__unreal_asset_info")
        self.assertEqual(g["reasons"], {"none": 3})
        self.assertEqual((g["verdict"], g["kind"]), ("agent", "unexplained_repeats"))
        self.assertEqual([f.kind for f in G.detect(view, view_cfg(self))], ["unexplained_repeats"])

    def test_rechecking_after_each_edit_is_justified(self) -> None:
        s = self._synth("after-edit")
        for i in range(4):
            s.bash("git status --short", f" M src/f{i}.py")
            s.edit(f"src/f{i}.py", "a", "b")
        view = s.view()
        g = self._group(view, "Bash")
        self.assertEqual(g["reasons"].get("after_change"), 3)
        self.assertEqual(g["verdict"], "justifie")
        self.assertEqual(G.detect(view, view_cfg(self)), [])

    def test_repeats_inside_one_response_cost_no_round_trip(self) -> None:
        s = self._synth("script-loop")
        s.tick(5_000)        # * le modele a mis le temps de repondre : un fil reel ne tient pas en 2 s
        s.response_gap_ms = 50
        for _ in range(5):
            s.bash("squeue -u me", "JOBID 1 R")
        view = s.view()
        g = self._group(view, "Bash")
        self.assertEqual((g["round_trips"], g["verdict"]), (0, "gratuit"))
        self.assertEqual(G.detect(view, view_cfg(self)), [])

    def test_new_user_message_and_compaction_explain_a_repeat(self) -> None:
        s = self._synth("inputs")
        s.response_gap_ms = 4_000
        s.mcp("romeo", "romeo_quota", {}, json.dumps({"used": 1}))
        s.user_prompt("et le quota ?")
        s.mcp("romeo", "romeo_quota", {}, json.dumps({"used": 1}))
        s.compact()
        s.mcp("romeo", "romeo_quota", {}, json.dumps({"used": 1}))
        g = self._group(s.view(), "mcp__romeo__romeo_quota")
        self.assertEqual(g["reasons"], {"new_input": 1, "context_loss": 1})
        self.assertEqual(g["verdict"], "justifie")

    def test_declared_reasons_come_from_the_agent_commentary(self) -> None:
        s = self._synth("declared")
        s.response_gap_ms = 10_000
        for _ in range(4):
            s.mcp("romeo", "romeo_status", {}, json.dumps({"error": "not active"}))
        view = s.view()
        calls = [c for c in view.calls if c.tool_name == "mcp__romeo__romeo_status"]
        for i, c in enumerate(calls[1:]):
            view.markers.append(Marker(phase="message", ns=c.order_ns - 1_000_000, time=None, agent_id=None,
                                       meta={"role": "assistant", "declared": ["retry", "unavailable"], "message_id": f"m{i}"}))
        g = self._group(view, "mcp__romeo__romeo_status")
        self.assertEqual(g["declared"], {"retry": 3, "unavailable": 3})

    def test_declared_reason_is_used_when_nothing_is_observed(self) -> None:
        s = self._synth("declared-only")
        s.response_gap_ms = 30_000
        for _ in range(4):
            s.mcp("unreal", "unreal_editor_py", {"code_fp": "x"}, "frames=10")
        view = s.view()
        calls = [c for c in view.calls if c.tool_name == "mcp__unreal__unreal_editor_py"]
        for i, c in enumerate(calls[1:]):
            view.markers.append(Marker(phase="message", ns=c.order_ns - 1_000_000, time=None, agent_id=None,
                                       meta={"role": "assistant", "declared": ["in_progress"], "message_id": f"d{i}"}))
        g = self._group(view, "mcp__unreal__unreal_editor_py")
        self.assertEqual(g["reasons"], {"waiting": 3})
        self.assertTrue(all(r["reason_basis"] == "annoncee par l'agent" for r in g["repeats"]))

    # ------------------------------------------------------------------ rythme, epoques, rapport
    def test_thread_rewritten_in_one_block_is_left_out(self) -> None:
        # * Constate le 2026-09-19 : 190 rollouts de juin a aout ont toutes leurs lignes a la meme milliseconde. Leurs
        #   intervalles seraient nuls : tout passerait pour des repetitions "dans une meme reponse".
        s = self._synth("bulk")
        s.response_gap_ms = 0
        for _ in range(4):
            s.mcp("romeo", "job_status", {"job_id": 1}, json.dumps({"status": "RUNNING"}), duration_ms=0)
        view = s.view()
        self.assertEqual(view.timing_unreliable_agents, ["main"])
        self.assertTrue(any("horodatages non fiables" in w for w in view.warnings))
        res = G.analyse(view, view_cfg(self))
        self.assertEqual(res["groups"], [])
        self.assertEqual(res["totals"]["excluded_unreliable_calls"], 4)

    def test_rhythm_table_covers_every_tool(self) -> None:
        s = self._synth("rhythm")
        s.response_gap_ms = 5_000
        for i in range(12):
            s.mcp("romeo", "job_status", {"job_id": i}, json.dumps({"status": "RUNNING"}))
        s.read("src/a.py", "A")
        res = G.analyse(s.view(), view_cfg(self))
        row = next(r for r in res["rhythm"] if r["tool"] == "mcp__romeo__job_status")
        self.assertEqual(row["calls"], 12)
        self.assertEqual(row["identical_repeats"], 0)            # * jobs differents : pas des repetitions
        self.assertGreaterEqual(row["max_per_min"], 11)
        self.assertTrue(any(r["tool"] == "Read" for r in res["rhythm"]))

    def test_subagent_compaction_does_not_reset_the_main_context(self) -> None:
        s = self._synth("epochs")
        s.bash("ls", "a")
        s.set_agent("sub-1")
        s.compact()
        s.bash("ls", "a")
        s.set_agent(None)
        s.bash("ls", "a")
        view = s.view()
        main = [c for c in view.calls if c.agent_id is None]
        sub = [c for c in view.calls if c.agent_id == "sub-1"]
        self.assertEqual({c.context_epoch for c in main}, {0})
        self.assertEqual({c.context_epoch for c in sub}, {1})
        self.assertEqual(view.epochs, 1)

    def test_markdown_explains_why(self) -> None:
        s = self._synth("render")
        s.response_gap_ms = 20_000
        for i in range(8):
            s.mcp("romeo", "job_status", {"job_id": 42}, json.dumps({"status": "RUNNING" if i < 7 else "COMPLETED"}))
        from agentwatch.detectors import run_detectors
        from agentwatch.reports.json_report import build_report
        from agentwatch.reports.stats import compute_stats, coverage_matrix
        view = s.view()
        cfg = view_cfg(self)
        report = build_report(view, compute_stats(view, cfg), coverage_matrix(view), run_detectors(view, cfg), cfg, {}, None)
        md = render_markdown(report)
        self.assertIn("## Appels repetes : pourquoi, a quel rythme", md)
        self.assertIn("attente en cours 7", md)
        self.assertIn("Cadence simulee", md)
        self.assertIn("Rythme des outils", md)
        self.assertIn("G.repeated_calls", json.dumps(report["findings"]))


def view_cfg(tc: RepeatedCallsTests) -> dict:
    from agentwatch.config import load_config
    return load_config(tc.home)


if __name__ == "__main__":
    unittest.main()
