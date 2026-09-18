"""Tests du detecteur E : service externe manipule a la main alors qu'un outil MCP ferait le travail."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentwatch.detectors.tool_gap import family_of, related_servers, target_of
from agentwatch.reports.trends import pattern_key
from agentwatch.selftest import Synth


class ToolGapUnitTests(unittest.TestCase):
    def test_family_and_target(self) -> None:
        self.assertEqual(family_of(["rtk", "ssh"]), ("ssh", "ssh"))
        self.assertEqual(family_of(["cd", "sbatch"]), ("slurm", "sbatch"))
        self.assertEqual(family_of(["python"]), (None, "python"))
        self.assertEqual(target_of("ssh", "ssh", "ssh -p 2222 -i ~/.ssh/k romeo 'squeue -u me'"), "romeo")
        self.assertEqual(target_of("ssh", "scp", "scp ./job.sh nicol@romeo.univ:/home/x/"), "romeo.univ")
        self.assertEqual(target_of("ssh", "rsync", "rsync -av ./data romeo:/scratch/"), "romeo")
        self.assertEqual(target_of("http", "curl", "curl -s https://api.github.com/repos/x/y"), "api.github.com")
        self.assertEqual(target_of("slurm", "squeue", "squeue -u me"), "slurm")
        self.assertEqual(target_of("github", "gh", "gh pr view 12"), "github")

    def test_related_servers(self) -> None:
        servers = {"romeo": {"submit_job", "job_status"}, "unreal": {"unreal_editor_py"}, "github": {"create_pull_request"}}
        by_name = related_servers("ssh", "romeo", servers)
        self.assertEqual([r["server"] for r in by_name], ["romeo"])
        self.assertIn("cible", by_name[0]["basis"])
        by_tools = related_servers("slurm", "slurm", servers)
        self.assertEqual([r["server"] for r in by_tools], ["romeo"], "outils submit_job/job_status : famille slurm")
        self.assertEqual(related_servers("container", "docker", servers), [])
        self.assertEqual([r["server"] for r in related_servers("github", "github", servers)], ["github"])


class ToolGapScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_manual_ssh_with_mcp_available_is_high(self) -> None:
        s = Synth(self.home, session_id="e1")
        s.session_start(); s.user_prompt()
        s.mcp("romeo", "romeo_status", {}, "ok")
        s.bash("ls ~/.ssh", "id_ed25519\n")
        s.bash("ssh romeo 'squeue -u me'", "JOBID\n"); s.bash("scp job.sh romeo:/home/me/", ""); s.bash("ssh romeo 'sbatch job.sh'", "Submitted\n")
        f = s.findings(["tool_gap"])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].confidence, "high")
        self.assertEqual((f[0].evidence["family"], f[0].evidence["target"]), ("ssh", "romeo"))
        self.assertEqual(f[0].proposal["tool_candidates"], ["romeo"])
        self.assertEqual(f[0].proposal["steps_replaced"], 3)
        self.assertIn("romeo", f[0].title)
        key, label = pattern_key(f[0])
        self.assertEqual(key, "E|ssh|romeo")
        self.assertIn("romeo", label)

    def test_without_mcp_confidence_depends_on_volume_and_failures(self) -> None:
        s = Synth(self.home, session_id="e2")
        s.session_start(); s.user_prompt()
        for _ in range(3):
            s.bash("curl -s https://svc.example/health", "ok")
        low = s.findings(["tool_gap"])
        self.assertEqual([x.confidence for x in low], ["low"])
        s2 = Synth(self.home, session_id="e3")
        s2.session_start(); s2.user_prompt()
        for i in range(3):
            s2.bash("gh pr view 1", "x", fail="Exit code 1: not found" if i == 1 else None)
        self.assertEqual([x.confidence for x in s2.findings(["tool_gap"])], ["medium"])
        s3 = Synth(self.home, session_id="e4")
        s3.session_start(); s3.user_prompt()
        for _ in range(6):
            s3.bash("docker ps", "x")
        self.assertEqual([x.confidence for x in s3.findings(["tool_gap"])], ["medium"])

    def test_different_targets_or_few_calls_not_reported(self) -> None:
        s = Synth(self.home, session_id="e5")
        s.session_start(); s.user_prompt()
        s.bash("ssh a 'ls'", "x"); s.bash("ssh a 'ls'", "x")
        s.bash("ssh b 'ls'", "x"); s.bash("ssh b 'ls'", "x")
        s.bash("python run.py", "x"); s.bash("python run.py", "x"); s.bash("python run.py", "x")
        self.assertEqual(s.findings(["tool_gap"]), [])


if __name__ == "__main__":
    unittest.main()
