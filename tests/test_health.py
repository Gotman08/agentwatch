"""Tests de la sante de la collecte : panne silencieuse detectee sans lire aucun contenu."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX, cli
from agentwatch.collector.health import check_collection, install_meta_path
from agentwatch.config import load_config
from agentwatch.installer import claude_code as IC
from agentwatch.installer import codex as IX
from agentwatch.installer import common as C
from agentwatch.selftest import Synth


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.projects = root / "projects"
        self.codex_sessions = root / "codex-sessions"
        self.settings = root / "settings.json"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "transcripts": {"claude_projects_dir": str(self.projects)},
            "health": {"codex_sessions_dir": str(self.codex_sessions), "silence_minutes": 30},
        }), encoding="utf-8")
        self.cfg = load_config(self.home)
        self.entry = str(C.hook_entry_path())
        obj, _ = IC.plan_install({}, "py", Path(self.entry), self.home)
        self.settings.write_text(json.dumps(obj), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _meta(self, client: str = CLIENT_CLAUDE_CODE, **over: Any) -> None:
        config_path = self.settings
        if client == CLIENT_CODEX:
            config_path = self.settings.with_name("hooks.json")
            obj, _ = IX.plan_install({}, "py", Path(self.entry), self.home)
            config_path.write_text(json.dumps(obj), encoding="utf-8")
        meta: dict[str, Any] = {"client": client, "python": os.path.abspath(__file__), "hook_entry": self.entry,
                                "config_path": str(config_path), "configured_at": "2026-01-01T00:00:00Z"}
        meta.update(over)
        p = install_meta_path(self.home, client)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta), encoding="utf-8")

    def _codes(self) -> list[str]:
        return [h["code"] for h in check_collection(self.home, self.cfg)]

    def test_nothing_configured_means_nothing_to_report(self) -> None:
        self.assertEqual(self._codes(), [])

    def test_missing_interpreter_and_moved_entry(self) -> None:
        self._meta(python=str(self.home / "nope" / "pythonw.exe"), hook_entry=str(self.home / "ailleurs" / "hook_entry.py"))
        codes = self._codes()
        self.assertIn("interpreter_missing", codes)
        self.assertIn("entry_moved", codes)

    def test_hooks_removed_or_disabled(self) -> None:
        self._meta()
        self.assertEqual(self._codes(), [])
        obj, _ = C.read_json_file(self.settings)
        obj["disableAllHooks"] = True
        self.settings.write_text(json.dumps(obj), encoding="utf-8")
        self.assertIn("hooks_disabled", self._codes())
        removed, _ = IC.plan_uninstall(obj)
        self.settings.write_text(json.dumps(removed), encoding="utf-8")
        self.assertIn("hooks_missing", self._codes())

    def test_silence_after_client_activity(self) -> None:
        self._meta()
        (self.projects / "C--proj").mkdir(parents=True)
        transcript = self.projects / "C--proj" / "abc.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")   # * seule sa date compte, jamais son contenu
        self.assertIn("never_received", self._codes(), "journal du client modifie, aucun evenement jamais recu")
        s = Synth(self.home, session_id="live")
        s.session_start(); s.user_prompt(); s.read("a.py", "x"); s.stop()
        self.assertEqual(self._codes(), [], "evenement recu apres l'activite : rien a signaler")
        future = time.time() + 3 * 3600
        os.utime(transcript, (future, future))   # * le client "ecrit" trois heures apres le dernier evenement
        self.assertIn("silent_since_activity", self._codes())

    def test_codex_hint_mentions_hooks_approval(self) -> None:
        self._meta(CLIENT_CODEX)
        day = self.codex_sessions / "2026" / "09" / "18"
        day.mkdir(parents=True)
        (day / "rollout-x.jsonl").write_text("{}\n", encoding="utf-8")
        items = check_collection(self.home, self.cfg)
        self.assertEqual([i["code"] for i in items], ["never_received"])
        self.assertIn("/hooks", items[0]["message"])

    def test_resumed_codex_thread_in_an_older_day_folder_counts_as_activity(self) -> None:
        # * Fil cree il y a 6 jours, repris maintenant, alors que trois jours plus recents ont leur dossier.
        from agentwatch.collector.health import client_activity_mtime
        now = time.time()

        def day(offset: int) -> Path:
            d = self.codex_sessions.joinpath(*time.strftime("%Y %m %d", time.localtime(now - offset * 86400)).split())
            d.mkdir(parents=True, exist_ok=True)
            return d

        for offset in (1, 2, 3):
            f = day(offset) / f"rollout-{offset}.jsonl"
            f.write_text("{}\n", encoding="utf-8")
            os.utime(f, (now - offset * 86400, now - offset * 86400))
        resumed = day(6) / "rollout-repris.jsonl"
        resumed.write_text("{}\n", encoding="utf-8")
        os.utime(resumed, (now - 60, now - 60))
        old = day(45) / "rollout-ancien.jsonl"      # * hors fenetre : ignore meme s'il vient d'etre ecrit
        old.write_text("{}\n", encoding="utf-8")
        self.assertAlmostEqual(client_activity_mtime(CLIENT_CODEX, self.cfg) or 0, now - 60, delta=2)

    def test_doctor_and_report_surface_health(self) -> None:
        self._meta(python=str(self.home / "nope.exe"))
        s = Synth(self.home, session_id="r1")
        s.session_start(); s.user_prompt(); s.read("a.py", "x"); s.stop()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "doctor"]), 0)
        self.assertIn("Sante de la collecte (panne silencieuse)", buf.getvalue())
        self.assertIn("interpreteur fige", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "r1", "--format", "json"]), 0)
        js = json.loads(buf.getvalue())
        self.assertEqual([h["code"] for h in js["collection_health"]], ["interpreter_missing"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "r1", "--format", "markdown"]), 0)
        self.assertIn("Sante de la collecte :", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
