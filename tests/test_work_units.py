"""Tests des unites de travail : meme tache par des outils differents, relances sans changement,
traduction conservatrice des commandes shell, section du rapport, compatibilite du schema 1.0."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.core import intent as I
from agentwatch.core.correlate import build_session
from agentwatch.selftest import Synth


class IntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Synth(Path(self.tmp.name), session_id="it")
        self.s.session_start(); self.s.user_prompt()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ops(self) -> list:
        return [(c.tool_name, c.op, c.op_target, c.op_params, c.op_key) for c in self.s.view().calls]

    def test_read_tool_and_shell_forms_share_a_work_unit(self) -> None:
        self.s.read("src/a.py", "X")
        self.s.bash("cat src/a.py", "X")
        self.s.bash("Get-Content src/a.py", "X")
        self.s.bash("sed -n '1,5p' src/a.py", "X")
        self.s.bash("cat src/a.py | mystery-filter", "X")
        ops = self._ops()
        self.assertEqual(ops[0][1], I.OP_READ)
        self.assertEqual(ops[0][4], ops[1][4], "Read et cat : meme unite de travail")
        self.assertEqual(ops[1][4], ops[2][4], "cat et Get-Content : meme unite de travail")
        self.assertEqual((ops[3][1], ops[3][3].get("range")), (I.OP_READ, [1, 5]))
        self.assertNotEqual(ops[3][4], ops[0][4], "une plage est un parametre different")
        self.assertEqual(ops[4][1], I.OP_UNKNOWN, "un filtre inconnu dans le tube n'est pas traduit")

    def test_cd_prefix_sets_effective_cwd(self) -> None:
        self.s.read("src/a.py", "X")
        self.s.bash('cd src && cat a.py', "X")
        self.s.bash('cd "src" && python -m pytest -q', "ok")
        self.s.bash("cd src && cd .. && cat src/a.py", "X")
        ops = self._ops()
        self.assertEqual(ops[0][4], ops[1][4], "cd src && cat a.py == Read src/a.py")
        self.assertEqual((ops[2][1], ops[2][3]["cwd"].replace("\\", "/").endswith("/src")), (I.OP_RUN_TESTS, True))
        self.assertEqual(ops[3][4], ops[0][4], "cd src && cd .. remet le dossier initial")

    def test_search_list_tests_forms(self) -> None:
        self.s.grep("TODO", "src", ["src/a.py"])
        self.s.bash("rg TODO src", "src/a.py")
        self.s.bash("pytest tests/test_x.py -q", "ok")
        self.s.bash("python -m pytest tests/test_x.py -q", "ok")
        self.s.bash("git status", "clean")
        self.s.bash("python build.py --release", "ok")
        self.s.bash("weird-tool --do things", "?")
        ops = self._ops()
        self.assertEqual((ops[0][1], ops[1][1]), (I.OP_SEARCH, I.OP_SEARCH))
        self.assertEqual(ops[0][2], ops[1][2], "meme dossier cible")
        self.assertEqual(ops[2][4], ops[3][4], "pytest et python -m pytest : meme execution")
        self.assertEqual(ops[4][1], I.OP_VCS_READ)
        self.assertEqual((ops[5][1], ops[5][3]["args"]), (I.OP_RUN_SCRIPT, ["--release"]))
        self.assertEqual(ops[6][1], I.OP_UNKNOWN)


class CrossToolRedundancyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_file_read_by_two_tools_is_flagged_with_content_proof(self) -> None:
        s = Synth(self.home, session_id="ct1")
        s.session_start(); s.user_prompt()
        s.read("cfg.toml", "a = 1\nb = 2\n")
        s.bash("git status", "clean")
        s.bash("cat cfg.toml", "a = 1\r\nb = 2\r\n")   # CRLF : meme contenu normalise
        f = s.findings(["redundant_reads"])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].kind, "repeated_read_cross_tool")
        self.assertEqual(f[0].confidence, "high")
        self.assertEqual(f[0].evidence["tools_used"], ["Bash", "Read"])
        self.assertIn("contenu", f[0].evidence["same_result_basis"][0])

    def test_different_content_is_not_flagged(self) -> None:
        s = Synth(self.home, session_id="ct2")
        s.session_start(); s.user_prompt()
        s.read("cfg.toml", "a = 1\n")
        s.bash("cat cfg.toml", "a = 2\n")   # modification externe non observee, mais contenu different
        self.assertEqual(s.findings(["redundant_reads"]), [])

    def test_repeated_run_without_change(self) -> None:
        s = Synth(self.home, session_id="rr1")
        s.session_start(); s.user_prompt()
        for _ in range(3):
            s.bash("python -m pytest -q", "3 passed\n")
        f = s.findings(["redundant_reads"])
        self.assertEqual([x.kind for x in f], ["repeated_run"])
        self.assertEqual(f[0].confidence, "medium")
        self.assertEqual(len(f[0].calls), 3)
        # relance apres une edition : pas de signalement ; echec puis succes : progres, pas de signalement
        s2 = Synth(self.home, session_id="rr2")
        s2.session_start(); s2.user_prompt()
        s2.bash("pytest -q", "1 failed\n", fail="Exit code 1: 1 failed")
        s2.edit("src/a.py", "x", "y")
        s2.bash("pytest -q", "3 passed\n")
        s2.bash("pytest -q", "3 passed\n")
        f2 = s2.findings(["redundant_reads"])
        self.assertEqual([x.kind for x in f2], ["repeated_run"], "seules les deux relances identiques apres l'edition")
        self.assertEqual(len(f2[0].calls), 2)

    def test_work_units_section_in_report(self) -> None:
        s = Synth(self.home, session_id="wu")
        s.session_start(); s.user_prompt()
        s.read("a.py", "A"); s.bash("cat a.py", "A"); s.bash("type a.py", "A")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "wu", "--format", "json"]), 0)
        js = json.loads(buf.getvalue())
        wu = js["stats"]["work_units"][0]
        self.assertEqual((wu["op"], wu["calls"], wu["tools"], wu["distinct_contents"]), ("read", 3, ["Bash", "Read"], 1))
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--home", str(self.home), "report", "--session", "wu"])
        self.assertIn("Unites de travail refaites", buf.getvalue())
        self.assertIn("| read | a.py | 3 | Bash, Read | 1 | 1 |", buf.getvalue())

    def test_schema_1_0_events_still_accepted(self) -> None:
        ev = {"schema_version": "1.0", "event_id": "e1", "client": "codex", "phase": "turn_end", "received_time": "2026-01-01T00:00:00Z",
              "received_time_ns": 1, "params": {}, "warnings": [], "evidence": {}}
        v = build_session([ev], {})
        self.assertEqual(v.counts["invalid_events"], 0)
        self.assertEqual(v.markers[0].phase, "turn_end")


if __name__ == "__main__":
    unittest.main()
