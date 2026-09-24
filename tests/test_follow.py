"""Suivi continu : un seul collecteur, un etat et un journal verifiables.

# * Constate le 2026-09-21 : le suivi lance le 19 avait disparu au redemarrage de la machine sans laisser de trace, et
#   rien n'empechait deux collecteurs (ou un suivi et l'import automatique d'un rapport) de reecrire le meme etat.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentwatch import cli
from agentwatch.collector import follow as F

REPO = Path(__file__).resolve().parent.parent
HOLDER = ("import sys, time; sys.path.insert(0, sys.argv[1]); from agentwatch.collector import follow as F; "
          "l = F.FollowLock(sys.argv[2]); print('held' if l.acquire() else 'busy', flush=True); time.sleep(30)")


class FollowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, sessions = root / "home", root / "sessions"
        sessions.mkdir()
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"health": {"codex_sessions_dir": str(sessions)},
                                                           "rollouts": {"background_priority": False}}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lock_is_exclusive_and_freed_with_the_process(self) -> None:
        home = str(self.home)
        self.assertFalse(F.is_running(home))
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(REPO), home], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            self.assertTrue(F.is_running(home))                       # * un autre processus tient le verrou
            self.assertFalse(F.FollowLock(home).acquire())
            # * un second collecteur, ou un import ponctuel, refuse de demarrer (code 3) et ne lit rien
            self.assertEqual(cli.main(["--home", home, "import-rollouts", "--follow"]), 3)
            self.assertEqual(cli.main(["--home", home, "import-rollouts"]), 3)
        finally:
            proc.kill()                                               # * processus tue : le systeme libere le verrou
            proc.wait(timeout=10)
            proc.stdout.close()
        deadline = time.time() + 5
        while F.is_running(home) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(F.is_running(home))

    def test_follow_writes_status_and_log_then_records_its_stop(self) -> None:
        home = str(self.home)
        ticks: list[float] = []
        real_sleep = time.sleep

        def fake_sleep(_s: float) -> None:
            ticks.append(_s)
            if sum(ticks) >= 4:              # * l'attente se fait par tranches d'une seconde : 2 cycles de 2 s
                raise KeyboardInterrupt
            real_sleep(0)

        cli.time.sleep = fake_sleep          # type: ignore[assignment]
        try:
            self.assertEqual(cli.main(["--home", home, "import-rollouts", "--follow", "--interval", "2"]), 0)
        finally:
            cli.time.sleep = real_sleep      # type: ignore[assignment]
        st = F.read_status(home)
        self.assertEqual((st["cycles"], st["interval_s"], st["stopped"]["reason"]), (2, 2.0, "interruption clavier"))
        self.assertEqual(Path(st["repo"]), REPO)
        d = F.describe(home)
        self.assertEqual((d["running"], d["pid"]), (False, None))      # * verrou rendu : le suivi est dit arrete
        log = Path(F.log_path(home)).read_text(encoding="utf-8")
        self.assertIn("suivi demarre", log)
        self.assertIn("suivi arrete : interruption clavier", log)
        lines = cli._follow_state_lines(self.home)
        self.assertTrue(lines[0].startswith("suivi continu : ARRETE"))

    def test_stop_request_ends_the_follow_cleanly_between_two_cycles(self) -> None:
        """Sans console, le suivi ne recoit pas d'interruption clavier : une demande ecrite l'arrete apres son cycle."""
        home = str(self.home)
        proc = subprocess.Popen([sys.executable, "-m", "agentwatch", "--home", home, "import-rollouts", "--follow", "--interval", "2"],
                                cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            while not (F.is_running(home) and F.read_status(home).get("cycles")) and time.time() < deadline:
                time.sleep(0.1)
            self.assertTrue(F.is_running(home))
            self.assertEqual(cli.main(["--home", home, "import-rollouts", "--stop-follow", "--reason", "mise a jour du code"]), 0)
            self.assertEqual(proc.wait(timeout=20), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        st = F.read_status(home)
        self.assertEqual(st["stopped"]["reason"], "arret demande : mise a jour du code")
        self.assertFalse(F.is_running(home))                           # * verrou rendu
        self.assertIsNone(F.stop_requested(home))                      # * demande consommee : le prochain suivi demarre
        self.assertIn("suivi arrete : arret demande : mise a jour du code", Path(F.log_path(home)).read_text(encoding="utf-8"))
        self.assertEqual(cli.main(["--home", home, "import-rollouts", "--stop-follow"]), 0)   # * deja arrete : sans effet
        self.assertIsNone(F.stop_requested(home))

    def test_status_without_recorded_stop_keeps_the_cause_unknown(self) -> None:
        F.write_status(str(self.home), {"pid": 4242, "started": "2026-09-19T21:30:00Z", "last_tick": "2026-09-19T23:24:04Z",
                                        "last_tick_epoch": 1.0, "cycles": 12, "interval_s": 30, "stopped": None})
        line = cli._follow_state_lines(self.home)[0]
        self.assertIn("aucun arret consigne : cause inconnue", line)
        self.assertNotIn("processus tue", line)
        self.assertNotIn("machine redemarree", line)


if __name__ == "__main__":
    unittest.main()
