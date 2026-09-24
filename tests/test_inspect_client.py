"""La lecture explicite Claude ne doit pas lancer la collecte Codex."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agentwatch import cli


class InspectClientTests(unittest.TestCase):
    def test_claude_cli_without_hooks_does_not_import_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            projects = Path(tmp) / "projects"
            project = projects / "synthetic-project"
            project.mkdir(parents=True)
            home.mkdir()
            (home / "config.json").write_text(json.dumps({
                "transcripts": {"claude_projects_dir": str(projects)},
                "rollouts": {"auto_import": True},
            }), encoding="utf-8")
            source = project / "synthetic-session.jsonl"
            source.write_text(json.dumps({
                "type": "assistant", "sessionId": "synthetic-session",
                "requestId": "r1", "timestamp": "2026-09-24T00:00:00Z",
                "message": {"id": "m1", "model": "synthetic", "content": [
                    {"type": "text", "text": "Resultat synthetique"}],
                    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 3,
                              "cache_read_input_tokens": 4, "output_tokens": 5}},
            }) + "\n", encoding="utf-8")
            before = source.read_bytes()
            out = Path(tmp) / "inspection.jsonl"
            with patch.object(cli, "_auto_import_rollouts") as importer, redirect_stdout(StringIO()):
                result = cli.main(["--home", str(home), "inspect", "--client", "claude-code",
                                   "--session", "synthetic-session", "--format", "jsonl", "--out", str(out)])
            self.assertEqual(result, 0)
            importer.assert_not_called()
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((home / "spool").exists())
            rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(rows)
            self.assertIn("Resultat synthetique", out.read_text(encoding="utf-8"))
            with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, "journal source"):
                cli.main(["--home", str(home), "inspect", "--client", "claude-code",
                          "--session", "synthetic-session", "--out", str(source)])
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
