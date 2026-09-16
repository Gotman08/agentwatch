"""Tests du rendu Rich (optionnel) : ignores si Rich n'est pas installe ; le hook n'en depend jamais."""

from __future__ import annotations

import importlib.util
import io
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

    def test_hook_never_imports_rich(self) -> None:
        r = subprocess.run([sys.executable, "-X", "importtime", "-I", str(ENTRY), "ingest", "--client", "codex", "--home", str(self.home)],
                           input=b'{"session_id":"h","hook_event_name":"Stop"}', capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(b" rich", r.stderr, "le chemin chaud ne doit jamais charger Rich")


if __name__ == "__main__":
    unittest.main()
