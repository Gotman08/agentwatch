"""Indice d'erreur MCP : une reponse JSON est lue structurellement, pas au mot pres.

Faux positif reel (2026-09-19) : le serveur romeo repond `{"erreur": null, ...}` quand tout va bien ;
188 attentes d'un job SLURM en file passaient pour 188 pannes (B : "94 echecs identiques", deux fois).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentwatch import CLIENT_CODEX
from agentwatch.adapters.base import mcp_error_hint
from agentwatch.selftest import Synth


def _resp(obj: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj) if not isinstance(obj, str) else obj}], "isError": False}


class McpErrorHintTests(unittest.TestCase):
    def test_empty_error_field_is_not_an_error(self) -> None:
        self.assertIsNone(mcp_error_hint(_resp({"job_id": 42, "etat": "PENDING", "raison": "Priority", "erreur": None})))
        self.assertIsNone(mcp_error_hint(_resp({"results": [{"ok": True, "errors": []}]})))

    def test_filled_error_field_or_failed_status_is_an_error(self) -> None:
        self.assertIn("session SSH interrompue", mcp_error_hint(_resp({"erreur": "session SSH interrompue"})) or "")
        self.assertIsNotNone(mcp_error_hint(_resp({"job": {"status": "FAILED", "exit_code": 1}})))

    def test_plain_text_keeps_the_textual_rule(self) -> None:
        self.assertIsNotNone(mcp_error_hint(_resp("Erreur : session SSH interrompue, reconnexion necessaire")))
        self.assertIsNone(mcp_error_hint(_resp("Job 42 en file (Priority), rien a signaler")))

    def test_polling_a_pending_job_is_not_an_error_loop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            s = Synth(Path(d), client=CLIENT_CODEX, session_id="w")
            s.session_start(); s.user_prompt()
            for _ in range(5):
                s.call("mcp__romeo__wait_for_job", {"job_id": "42"}, _resp({"job_id": 42, "etat": "PENDING", "erreur": None}))
            self.assertEqual(s.findings(["error_loops"]), [])
            self.assertTrue(all(c.status == "success" and not c.evidence.get("error_hint") for c in s.view().calls))


if __name__ == "__main__":
    unittest.main()
