"""Un depot casse (erreur d'import) ne doit plus couper la collecte en silence : incident hook_crash."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import agentwatch

PAYLOAD = json.dumps({"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "echo secret-token-123"}, "tool_use_id": "exec-1"}).encode("utf-8")


class HookCrashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pkg = root / "copie" / "agentwatch"
        shutil.copytree(Path(agentwatch.__file__).parent, self.pkg, ignore=shutil.ignore_patterns("__pycache__"))
        self.home = root / "home"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([sys.executable, "-I", str(self.pkg / "hook_entry.py"), "ingest", "--client", "codex",
                               "--home", str(self.home)], input=PAYLOAD, capture_output=True, timeout=60)

    def _diags(self) -> list[dict]:
        d = self.home / "diagnostics"
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    def test_broken_import_leaves_an_incident_and_exits_zero(self) -> None:
        (self.pkg / "collector" / "ingest.py").write_text("def casse(:\n", encoding="utf-8")
        r = self._run()
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        diags = self._diags()
        self.assertEqual([d["kind"] for d in diags], ["hook_crash"])
        self.assertEqual(diags[0]["client"], "codex")
        self.assertIn("SyntaxError", diags[0]["error"])
        self.assertNotIn("secret-token", json.dumps(diags))   # * aucun contenu du payload
        self.assertFalse((self.home / "spool").exists())

    def test_healthy_copy_writes_the_event_and_no_crash(self) -> None:
        r = self._run()
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual(self._diags(), [])
        self.assertEqual(len(list((self.home / "spool").rglob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
