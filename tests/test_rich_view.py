"""Tests du rendu Rich (optionnel) : ignores si Rich n'est pas installe ; le hook n'en depend jamais."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.selftest import Synth

HAS_RICH = importlib.util.find_spec("rich") is not None
ENTRY = Path(__file__).resolve().parent.parent / "agentwatch" / "hook_entry.py"


class RichViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        s = Synth(self.home, session_id="rich")
        s.session_start(); s.user_prompt()
        s.read("a.py", "x"); s.read("a.py", "x")
        for _ in range(3):
            s.bash("python run.py", fail="Exit code 1: ModuleNotFoundError: No module named 'x'")
        s.agent_run("Explore", lambda: s.read("b.py", "B"), agent_id="agent-rich",
                    usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), *argv]), 0, buf.getvalue())
        return buf.getvalue()

    @unittest.skipUnless(HAS_RICH, "rich non installe")
    def test_html_svg_and_text_exports(self) -> None:
        html_path = self.home / "r.html"
        self._run("report", "--session", "rich", "--format", "html", "--out", str(html_path))
        html = html_path.read_text(encoding="utf-8")
        self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"))
        self.assertIn("AgentWatch", html)
        self.assertIn("repete 2 fois", html)
        svg_path = self.home / "r.svg"
        self._run("report", "--session", "rich", "--format", "svg", "--out", str(svg_path))
        self.assertIn("<svg", svg_path.read_text(encoding="utf-8")[:300])
        txt_path = self.home / "r.txt"
        self._run("report", "--session", "rich", "--format", "rich", "--out", str(txt_path))
        txt = txt_path.read_text(encoding="utf-8")
        self.assertIn("Opportunites prioritaires", txt)
        self.assertIn("agent-rich", txt)
        self.assertNotIn("\x1b[", txt, "export texte sans sequences ANSI")

    def test_auto_format_is_markdown_when_not_a_tty(self) -> None:
        out = self._run("report", "--session", "rich")
        self.assertTrue(out.startswith("# AgentWatch - rapport de session"))

    def test_missing_rich_names_the_running_interpreter(self) -> None:
        # * Cas reel : deux Python sur la machine, Rich installe dans l'un seulement.
        from unittest import mock
        from agentwatch.reports import rich_view
        with mock.patch.object(rich_view, "rich_available", return_value=False):
            for fmt in ("rich", "html", "svg"):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["--home", str(self.home), "report", "--session", "rich", "--format", fmt])
                message = str(ctx.exception.code)
                self.assertIn(sys.executable, message)
                self.assertIn("-m pip install rich", message)
            out = self._run("report", "--session", "rich")  # auto : repli Markdown silencieux
            self.assertTrue(out.startswith("# AgentWatch - rapport de session"))
            self.assertIn("-m pip install rich", self._run("doctor"))

    def test_doctor_warns_when_home_is_under_appdata(self) -> None:
        # * Piege constate : une application MSIX (Claude de bureau) redirige ses ecritures AppData.
        from unittest import mock
        with mock.patch.dict("os.environ", {"APPDATA": str(self.home.parent)}):
            self.assertIn("sous AppData", self._run("doctor"))
        with mock.patch.dict("os.environ", {"APPDATA": str(self.home / "ailleurs"), "LOCALAPPDATA": str(self.home / "ailleurs2")}):
            self.assertNotIn("sous AppData", self._run("doctor"))

    def test_piped_report_is_utf8(self) -> None:
        # * Sous Windows un tube herite de la page de code locale (cp1252) : le JSON redirige doit rester de l'UTF-8.
        for fmt in ("json", "markdown"):
            r = subprocess.run([sys.executable, "-m", "agentwatch", "--home", str(self.home), "report", "--session", "rich", "--format", fmt],
                               capture_output=True, timeout=120, cwd=str(ENTRY.parent.parent))
            self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
            text = r.stdout.decode("utf-8")  # leve UnicodeDecodeError si la sortie n'est pas UTF-8
            if fmt == "json":
                self.assertEqual(json.loads(text)["session"]["session_id"], "rich")
            else:
                self.assertTrue(text.startswith("# AgentWatch - rapport de session"))

    def test_hook_never_imports_rich(self) -> None:
        r = subprocess.run([sys.executable, "-X", "importtime", "-I", str(ENTRY), "ingest", "--client", "codex", "--home", str(self.home)],
                           input=b'{"session_id":"h","hook_event_name":"Stop"}', capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(b" rich", r.stderr, "le chemin chaud ne doit jamais charger Rich")


if __name__ == "__main__":
    unittest.main()
