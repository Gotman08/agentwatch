"""Confiance des hooks Codex lue aupres de `codex app-server` (hooks/list), avec un faux serveur."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agentwatch import CLIENT_CODEX, cli
from agentwatch.collector.health import install_meta_path
from agentwatch.installer import codex as IX
from agentwatch.installer import codex_trust as XT
from agentwatch.installer import common as C

_OURS = r"& 'C:\Py\pythonw.exe' -I 'C:\Outils\agentwatch\agentwatch\hook_entry.py' ingest --client codex --home 'C:\u\.agentwatch'"

_FAKE_SERVER = textwrap.dedent('''
    import json, sys
    mode = sys.argv[1]
    hooks = json.loads(sys.argv[2])
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            print("pas du json", flush=True)
            print(json.dumps({"method": "notification/quelconque", "params": {}}), flush=True)
            print(json.dumps({"id": msg["id"], "result": {"userAgent": "fake"}}), flush=True)
        elif msg.get("method") == "hooks/list":
            if mode == "silent":
                continue
            if mode == "error":
                print(json.dumps({"id": msg["id"], "error": {"code": -32601, "message": "method not found"}}), flush=True)
                continue
            print(json.dumps({"id": msg["id"], "result": {"data": [{"cwd": msg["params"]["cwds"][0], "errors": [],
                  "warnings": ["clamping SessionEnd hook timeout to 3s"], "hooks": hooks}]}}), flush=True)
''')


def _hook(event: str, trust: str, command: str = _OURS, enabled: bool = True) -> dict[str, Any]:
    return {"eventName": event, "source": "user", "enabled": enabled, "trustStatus": trust, "command": command,
            "sourcePath": "C:\\u\\.codex\\hooks.json"}


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / "fake_codex.py"
        self.script.write_text(_FAKE_SERVER, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _argv(self, mode: str, hooks: list[dict[str, Any]]) -> list[str]:
        # * Le faux serveur recoit "app-server" en dernier argument, ignore ici.
        return [sys.executable, str(self.script), mode, json.dumps(hooks)]

    def test_untrusted_hooks_are_reported_as_not_runnable(self) -> None:
        hooks = [_hook("preToolUse", "untrusted"), _hook("postToolUse", "trusted"),
                 _hook("stop", "modified"), _hook("preToolUse", "untrusted", command="node autre-outil.js")]
        res = XT.probe(self._argv("ok", hooks), ["C:\\projet"], timeout=20)
        self.assertTrue(res["ok"], res)
        s = XT.summarize(res["entries"])
        self.assertEqual(s["hooks"], 3)                      # * le hook etranger n'est pas compte
        self.assertEqual(s["trust"], {"untrusted": 1, "trusted": 1, "modified": 1})
        self.assertEqual(s["runnable"], 1)
        self.assertEqual(s["not_runnable_events"], ["preToolUse", "stop"])
        lines = XT.format_lines(res)
        self.assertTrue(any(line.startswith("! Codex n'executera que 1 hook(s) AgentWatch sur 3") for line in lines), lines)
        self.assertTrue(any("clamping SessionEnd" in line for line in lines))

    def test_all_trusted_gives_no_alert(self) -> None:
        res = XT.probe(self._argv("ok", [_hook("preToolUse", "trusted"), _hook("postToolUse", "managed")]), ["C:\\p"], timeout=20)
        lines = XT.format_lines(res)
        self.assertFalse(any(line.startswith("!") for line in lines), lines)
        self.assertIn("2 hooks AgentWatch vus", lines[0])

    def test_disabled_hook_is_not_runnable(self) -> None:
        s = XT.summarize([{"hooks": [_hook("preToolUse", "trusted", enabled=False)]}])
        self.assertEqual((s["runnable"], s["disabled"]), (0, 1))

    def test_no_agentwatch_hook_seen(self) -> None:
        res = XT.probe(self._argv("ok", []), ["C:\\p"], timeout=20)
        self.assertTrue(XT.format_lines(res)[0].startswith("! confiance des hooks"))

    def test_old_codex_without_method(self) -> None:
        res = XT.probe(self._argv("error", []), ["C:\\p"], timeout=20)
        self.assertFalse(res["ok"])
        self.assertIn("hooks/list refuse", res["error"])
        self.assertIn("controle impossible", XT.format_lines(res)[0])

    def test_silent_server_times_out_and_is_killed(self) -> None:
        res = XT.probe(self._argv("silent", []), ["C:\\p"], timeout=2)
        self.assertFalse(res["ok"])
        self.assertIn("pas de reponse", res["error"])

    def test_missing_executable(self) -> None:
        res = XT.probe([str(Path(self.tmp.name) / "absent.exe")], ["C:\\p"], timeout=2)
        self.assertFalse(res["ok"])
        self.assertIn("impossible", res["error"])


class DoctorTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        # * Dossier utilisateur factice : doctor lit ~/.codex/hooks.json ; le test ne doit jamais dependre de la
        #   configuration reelle de la machine (constate le 2026-09-19 : il echouait apres le retrait des hooks).
        self.user_home = Path(self.tmp.name) / "userhome"
        (self.user_home / ".codex").mkdir(parents=True)
        hooks_json = self.user_home / ".codex" / "hooks.json"
        obj, _ = IX.plan_install({}, "py", C.hook_entry_path(), self.home)
        hooks_json.write_text(json.dumps(obj), encoding="utf-8")
        p = install_meta_path(self.home, CLIENT_CODEX)
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"client": CLIENT_CODEX, "config_path": str(hooks_json), "scope": "user",
                                 "hook_entry": str(C.hook_entry_path()), "configured_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _doctor(self, *extra: str) -> str:
        untrusted = {"ok": True, "error": None, "elapsed_ms": 5,
                     "entries": [{"errors": [], "warnings": [], "hooks": [_hook("preToolUse", "untrusted")]}]}

        def fake_probe(argv: list[str], cwds: list[str], timeout: float = 30.0, cwd: str | None = None) -> dict[str, Any]:
            self.calls.append(argv)
            return untrusted

        def fake_version(client: str) -> tuple[str | None, str | None]:
            return ("codex-cli 0.0.0", "codex.exe") if client == CLIENT_CODEX else (None, None)

        buf = io.StringIO()
        with mock.patch.object(XT, "probe", fake_probe), mock.patch.object(cli, "_client_version", fake_version), \
                mock.patch("pathlib.Path.home", return_value=self.user_home), redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "doctor", *extra]), 0)
        return buf.getvalue()

    def test_doctor_surfaces_untrusted_hooks_in_health(self) -> None:
        out = self._doctor()
        self.assertEqual(self.calls, [["codex.exe"]])
        self.assertIn("complets dans le fichier", out)
        self.assertIn("1 untrusted (jamais approuves : Codex les ignore)", out)
        health = out.split("- Sante de la collecte", 1)[1]
        self.assertIn("! codex : Codex n'executera que 0 hook(s) AgentWatch sur 1", health)

    def test_doctor_can_skip_the_probe(self) -> None:
        out = self._doctor("--no-codex-trust")
        self.assertEqual(self.calls, [])
        self.assertIn("etat de confiance non verifie (--no-codex-trust)", out)


if __name__ == "__main__":
    unittest.main()
