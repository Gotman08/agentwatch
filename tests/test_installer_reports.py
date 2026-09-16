"""Tests : installation idempotente et reversible, rapports, retours locaux, CLI."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.installer import claude_code as IC
from agentwatch.installer import codex as IX
from agentwatch.installer import common as C
from agentwatch.reports.feedback import load_feedback, set_feedback
from agentwatch.selftest import Synth

FOREIGN = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}]},
           "permissions": {"allow": ["Bash(git:*)"]}, "custom": True}


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.entry = Path("C:/dir with spaces/é/agentwatch/hook_entry.py")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claude_install_twice_then_uninstall_preserves_foreign(self) -> None:
        once, s1 = IC.plan_install(FOREIGN, "C:/Program Files/Python/python.exe", self.entry, self.home)
        twice, s2 = IC.plan_install(once, "C:/Program Files/Python/python.exe", self.entry, self.home)
        self.assertEqual(once, twice, "idempotent")
        self.assertEqual(s2["replaced_groups"], len(IC.EVENTS))
        self.assertEqual(once["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "rtk hook claude")
        self.assertEqual(once["permissions"], FOREIGN["permissions"])
        self.assertTrue(IC.status(once)["complete"])
        self.assertIn(" ", once["hooks"]["PreToolUse"][1]["hooks"][0]["args"][1])
        removed, s3 = IC.plan_uninstall(twice)
        self.assertEqual(s3["removed_groups"], len(IC.EVENTS))
        self.assertEqual(removed, {**FOREIGN, "hooks": {"PreToolUse": FOREIGN["hooks"]["PreToolUse"]}})
        self.assertFalse(IC.status(removed)["installed_events"])

    def test_claude_mixed_group_only_our_entry_removed(self) -> None:
        mixed = {"hooks": {"Stop": [{"hooks": [IC.hook_spec("py", self.entry, self.home), {"type": "command", "command": "echo hi"}]}]}}
        removed, _ = IC.plan_uninstall(mixed)
        self.assertEqual(removed["hooks"]["Stop"][0]["hooks"], [{"type": "command", "command": "echo hi"}])

    def test_codex_quoting_and_roundtrip(self) -> None:
        spec = IX.hook_spec("C:\\Program Files\\Python\\python.exe", self.entry, self.home)
        self.assertTrue(spec["commandWindows"].startswith("& 'C:\\Program Files\\Python\\python.exe' -I '"))
        self.assertIn("--client codex", spec["command"])
        self.assertTrue(C.is_our_hook(spec))
        obj, _ = IX.plan_install({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}]}}, "py", self.entry, self.home)
        self.assertEqual(len(obj["hooks"]["PreToolUse"]), 2)
        back, s = IX.plan_uninstall(obj)
        self.assertEqual(s["removed_groups"], len(IX.EVENTS))
        self.assertEqual(back, {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}]}})

    def test_cli_configure_dry_run_and_apply_with_backup(self) -> None:
        proj = Path(self.tmp.name) / "proj é"
        (proj / ".claude").mkdir(parents=True)
        settings = proj / ".claude" / "settings.json"
        settings.write_text(json.dumps(FOREIGN), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--home", str(self.home), "configure", "--client", "claude-code", "--scope", "project", "--project-dir", str(proj)])
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), FOREIGN, "dry-run : rien n'est ecrit")
        self.assertIn("dry-run", buf.getvalue())
        with redirect_stdout(io.StringIO()):
            cli.main(["--home", str(self.home), "configure", "--client", "claude-code", "--scope", "project", "--project-dir", str(proj), "--apply"])
        obj = json.loads(settings.read_text(encoding="utf-8"))
        self.assertTrue(IC.status(obj)["complete"])
        self.assertTrue(list(settings.parent.glob("settings.json.bak-agentwatch-*")))
        with redirect_stdout(io.StringIO()):
            cli.main(["--home", str(self.home), "uninstall", "--client", "claude-code", "--scope", "project", "--project-dir", str(proj), "--apply"])
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {**FOREIGN, "hooks": {"PreToolUse": FOREIGN["hooks"]["PreToolUse"]}})

    def test_invalid_json_is_reported_not_overwritten(self) -> None:
        bad = Path(self.tmp.name) / "settings.json"
        bad.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            C.read_json_file(bad)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--home", str(self.home), *argv])
        self.assertEqual(code, 0, buf.getvalue())
        return buf.getvalue()

    def test_report_without_findings_says_so(self) -> None:
        s = Synth(self.home, session_id="clean")
        s.session_start(); s.user_prompt(); s.read("a.py", "1"); s.stop()
        md = self._run("report", "--session", "clean")
        self.assertIn("Aucun probleme demontre dans les donnees couvertes.", md)
        self.assertIn("non mesure", md)
        js = json.loads(self._run("report", "--session", "clean", "--format", "json"))
        self.assertEqual(js["findings"], [])
        self.assertEqual(js["session"]["model"], "synthetic-model")
        self.assertTrue(any(r["capability"] == "token_usage" and r["documented"] == "absent" for r in js["coverage"]))

    def test_report_with_findings_and_feedback(self) -> None:
        s = Synth(self.home, session_id="busy")
        s.session_start(); s.user_prompt()
        s.read("a.py", "x"); s.read("a.py", "x")
        for _ in range(3):
            s.bash("python run.py", fail="Exit code 1: No module named 'x'")
        js = json.loads(self._run("report", "--session", "busy", "--format", "json"))
        self.assertEqual(len(js["findings"]), 2)
        for f in js["findings"]:
            for key in ("rule_id", "rule_version", "confidence", "confidence_rationale", "calls", "evidence", "explanation",
                        "counter_indications", "missing_data", "observed_cost", "proposal", "validation_protocol"):
                self.assertIn(key, f)
        fid = js["top_findings"][0]
        self._run("feedback", "--finding", fid, "--mark", "false-positive", "--note", "test")
        self.assertEqual(load_feedback(self.home)[fid]["mark"], "false-positive")
        js2 = json.loads(self._run("report", "--session", "busy", "--format", "json"))
        self.assertNotIn(fid, js2["top_findings"])
        md = self._run("report", "--session", "busy")
        self.assertIn("Opportunites prioritaires", md)
        self.assertIn("false-positive", md)
        set_feedback(self.home, fid, "clear")
        self.assertNotIn(fid, load_feedback(self.home))
        self.assertIn("busy", self._run("sessions"))

    def test_replay_fixtures_and_compact(self) -> None:
        fixtures = Path(__file__).resolve().parent / "fixtures" / "codex"
        out = self._run("replay", "--client", "codex", str(fixtures))
        self.assertIn("12 evenement(s)", out)
        out = self._run("replay", "--client", "codex", str(fixtures))  # import repete
        self.assertIn("12 evenement(s)", out)
        js = json.loads(self._run("report", "--session", "thr_abc", "--format", "json"))
        self.assertEqual(js["stats"]["events"], 12, "import repete : identifiants deterministes, doublons elimines")
        self.assertEqual(js["stats"]["correlation"]["duplicate_events"], 12)
        self.assertEqual(len(js["calls"]), 7)
        self._run("compact")
        js2 = json.loads(self._run("report", "--session", "thr_abc", "--format", "json"))
        self.assertEqual(js2["stats"]["events"], 12)


if __name__ == "__main__":
    unittest.main()
