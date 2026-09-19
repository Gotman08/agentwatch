"""Codes de sortie qui ne sont pas des echecs ; B : meme panne sur des entrees differentes.

Constate sur une session Codex reelle (2026-09-19) : 25 des 179 "echecs" shell etaient des recherches vides
(`rg`, code 1) ou des `git diff --no-index` (code 1 = differences) ; `Remote Python execution failed ...
connection aborted` revenait 5 fois avec 5 scripts differents sans aucun signalement.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX
from agentwatch.selftest import Synth


class ExitStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _status(self, command: str, output: str, client: str = CLIENT_CODEX) -> tuple[str, dict]:
        s = Synth(self.home / str(abs(hash((command, output, client)))), client=client, session_id="x")
        s.session_start(); s.user_prompt()
        if client == CLIENT_CODEX:
            s.bash(command, fail=output, exit_code=1)
        else:
            s.bash(command, fail=f"Exit code 1\n{output}".rstrip())
        c = s.view().calls[0]
        return c.status, c.evidence

    def test_empty_search_is_not_a_failure(self) -> None:
        status, ev = self._status("rg OnCraftResult Source", "")
        self.assertEqual(status, "success")
        self.assertEqual(ev.get("result_count"), 0)
        self.assertEqual(self._status("rg OnCraftResult Source", "", CLIENT_CLAUDE_CODE)[0], "success")

    def test_search_on_a_missing_path_stays_a_failure(self) -> None:
        self.assertEqual(self._status("rg foo Nope", "rg: Nope: No such file or directory (os error 2)")[0], "error")

    def test_git_diff_no_index_differences_are_not_a_failure(self) -> None:
        out = "warning: in the working copy of 'Source/a.cpp', LF will be replaced by CRLF the next time Git touches it"
        self.assertEqual(self._status("git diff --no-index --check -- /dev/null Source/a.cpp", out)[0], "success")
        self.assertEqual(self._status("git diff --no-index --check -- /dev/null Source/a.cpp", "Source/a.cpp:3: trailing whitespace.")[0],
                         "error")

    def test_other_failures_are_kept(self) -> None:
        self.assertEqual(self._status("Get-Content Source/Absent.cpp", "Get-Content: Cannot find path because it does not exist.")[0],
                         "error")
        self.assertEqual(self._status("git commit -m x", "nothing to commit")[0], "error")


class SameFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_failure_on_different_inputs_is_reported(self) -> None:
        s = Synth(self.home, client=CLIENT_CODEX, session_id="u")
        s.session_start(); s.user_prompt()
        err = {"content": [{"type": "text", "text": "Remote Python execution failed after 3.2s: [WinError 10053] connection aborted"}],
               "isError": True}
        for i in range(4):
            s.call("mcp__unreal__unreal_editor_py", {"script": f"import unreal\nstep({i})"}, err)
        f = s.findings(["error_loops"])
        self.assertEqual([x.kind for x in f], ["same_failure_different_inputs"])
        self.assertEqual(f[0].confidence, "medium")
        self.assertEqual(f[0].evidence["distinct_inputs"], 4)

    def test_identical_operation_keeps_the_original_rule_only(self) -> None:
        s = Synth(self.home, client=CLIENT_CODEX, session_id="p")
        s.session_start(); s.user_prompt()
        for _ in range(3):
            s.bash("python run.py", fail="ModuleNotFoundError: No module named 'x'", exit_code=1)
        kinds = [x.kind for x in s.findings(["error_loops"])]
        self.assertEqual(kinds, ["persistent"])


if __name__ == "__main__":
    unittest.main()
