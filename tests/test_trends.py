"""Tests de la vue multi-sessions (`agentwatch trends`) : motifs recurrents par projet, client et session."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import CLIENT_CODEX, cli
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.reports.feedback import load_feedback, set_feedback
from agentwatch.reports.trends import build_trends, pattern_key, render_trends_markdown
from agentwatch.selftest import Synth

T0 = 1_800_000_000_000_000_000          # horloge de depart de Synth (ns)
NOW = T0 + 86_400 * 1_000_000_000       # "maintenant" = un jour apres les sessions synthetiques
OLD = 1_700_000_000_000_000_000         # ~3 ans plus tot : hors de toute fenetre raisonnable


def _read_twice_and_fail(s: Synth) -> None:
    s.session_start(); s.user_prompt()
    s.read("a.py", "x"); s.read("a.py", "x")
    for _ in range(3):
        s.bash("python run.py", fail="Exit code 1: ModuleNotFoundError: No module named 'x'")
    s.stop()


class TrendsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = load_config(self.home)
        # * Projet 1 : trois sessions Claude Code avec la meme relecture et la meme boucle d'erreur,
        #   plus une session Codex qui relit un autre fichier (motif A different, une seule fois).
        # ? Les identifiants d'appel de Synth repartent de 1 a chaque session ; en decalant le
        #   compteur, chaque signalement garde un finding_id distinct, comme avec de vrais tool_use_id.
        for i, sid in enumerate(("p1s1", "p1s2", "p1s3"), 1):
            s = Synth(self.home, session_id=sid, cwd=r"C:\proj1")
            s.n = i * 100
            _read_twice_and_fail(s)
        cx = Synth(self.home, client=CLIENT_CODEX, session_id="thr_p1", cwd=r"C:\proj1")
        cx.session_start(); cx.user_prompt()
        cx.bash("cat notes.txt", "N", exit_code=0); cx.bash("cat notes.txt", "N", exit_code=0)
        cx.stop()
        # * Projet 2 : une session avec la meme relecture de a.py (motif partage entre projets)
        #   et une serie regroupable (motif C vu une seule fois).
        s = Synth(self.home, session_id="p2s1", cwd=r"C:\proj2")
        s.n = 500
        s.session_start(); s.user_prompt()
        s.read("a.py", "x"); s.read("a.py", "x")
        s.glob("src/*.py", filenames=["src/a.py", "src/b.py", "src/c.py"])
        s.read("src/a.py", "A"); s.read("src/b.py", "B"); s.read("src/c.py", "C")
        s.stop()
        # * Session ancienne : hors fenetre, jamais comptee.
        old = Synth(self.home, session_id="p1old", cwd=r"C:\proj1")
        old.t, old.n = OLD, 900
        _read_twice_and_fail(old)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _trends(self, **kw: object) -> dict:
        store = EventStore(self.home, self.cfg)
        return build_trends(store, self.cfg, load_feedback(self.home), now_ns=NOW, **kw)  # type: ignore[arg-type]

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--home", str(self.home), *argv])
        self.assertEqual(code, 0, buf.getvalue())
        return buf.getvalue()

    def test_pattern_key_is_stable_across_sessions(self) -> None:
        keys_a = {pattern_key(f) for sid in ("p1s1", "p2s1")
                  for f in Synth(self.home, session_id=sid).findings(["redundant_reads"])}
        self.assertEqual(len(keys_a), 1, "meme lecture refaite dans deux sessions et deux projets = un seul motif")
        key, label = keys_a.pop()
        self.assertTrue(key.startswith("A|repeated_read|read|"))
        self.assertIn("a.py", label)
        fb = Synth(self.home, session_id="p1s1").findings(["error_loops"])
        key_b, _ = pattern_key(fb[0])
        self.assertTrue(key_b.startswith("B|persistent|Bash|"))
        self.assertNotIn("run.py", key_b, "la cle B porte la signature d'erreur, pas la cible")

    def test_recurring_patterns_counted_by_session_and_project(self) -> None:
        r = self._trends(days=7)
        self.assertEqual(r["sessions_analysed"], 5)
        self.assertEqual(r["sessions_skipped"]["out_of_window"], 1)
        self.assertEqual(r["projects"], 2)
        self.assertEqual(r["sessions_by_client"], {"claude-code": 4, "codex": 1})
        labels = {row["pattern_key"].split("|")[0]: row for row in r["recurring"]}
        self.assertEqual(sorted(labels), ["A", "B"], "seuls les motifs vus dans >= 2 sessions sont recurrents")
        top = r["recurring"][0]
        self.assertTrue(top["pattern_key"].startswith("A|repeated_read"))
        self.assertEqual((top["sessions"], top["projects"], top["clients"]), (4, 2, ["claude-code"]))
        self.assertEqual(top["occurrences"], 4)
        self.assertEqual(top["confidence_max"], "high")
        b = labels["B"]
        self.assertEqual((b["sessions"], b["projects"]), (3, 1))
        self.assertTrue(b["proposal"])
        # * Le motif C (une session) et la relecture Codex de notes.txt (une session) ne sont pas listes.
        self.assertEqual(r["single_session_patterns"], 2)
        self.assertEqual(r["findings_by_rule"], {"A": 5, "B": 3, "C": 1, "D": 0, "E": 0, "F": 0, "G": 0})

    def test_breakdown_by_project_client_and_session(self) -> None:
        r = self._trends(days=7)
        projects = {g["project"]: g for g in r["by_project"]}
        self.assertEqual(set(projects), {r"C:\proj1", r"C:\proj2"})
        p1, p2 = projects[r"C:\proj1"], projects[r"C:\proj2"]
        self.assertEqual((p1["sessions"], p1["clients"]), (4, ["claude-code", "codex"]))
        self.assertEqual([row["pattern_key"].split("|")[0] for row in p1["recurring"]], ["A", "B"])
        self.assertEqual(p1["recurring"][0]["sessions"], 3, "compte des sessions limite au projet")
        self.assertEqual(p1["single_session_patterns"], 1, "notes.txt relu une seule fois dans le projet")
        self.assertEqual(p2["sessions"], 1)
        self.assertEqual(p2["recurring"], [], "une seule session : aucune recurrence mesurable dans le projet")
        clients = {g["client"]: g for g in r["by_client"]}
        self.assertEqual(clients["claude-code"]["sessions"], 4)
        self.assertEqual(clients["claude-code"]["projects"], 2)
        self.assertEqual(clients["codex"]["recurring"], [])
        self.assertEqual(len(r["by_session"]), 5)
        by_id = {s["session_id"]: s for s in r["by_session"]}
        self.assertEqual(by_id["p2s1"]["findings_by_rule"], {"A": 1, "B": 0, "C": 1, "D": 0, "E": 0, "F": 0, "G": 0})
        self.assertEqual(by_id["p2s1"]["recurring_patterns"], 1, "seule la relecture de a.py revient ailleurs")
        self.assertEqual(by_id["p1s1"]["recurring_patterns"], 2)
        self.assertEqual(by_id["thr_p1"]["recurring_patterns"], 0)
        self.assertNotIn("p1old", by_id)

    def test_filters_window_and_min_sessions(self) -> None:
        self.assertEqual(self._trends(days=7, project="proj2")["sessions_analysed"], 1)
        self.assertEqual(self._trends(days=7, project="proj2")["recurring"], [])
        self.assertEqual(self._trends(days=7, client=CLIENT_CODEX)["sessions_analysed"], 1)
        self.assertEqual(self._trends(days=0)["sessions_analysed"], 6, "days=0 : toutes les sessions, l'ancienne comprise")
        strict = self._trends(days=7, min_sessions=4)
        self.assertEqual([row["pattern_key"].split("|")[0] for row in strict["recurring"]], ["A"])
        far = build_trends(EventStore(self.home, self.cfg), self.cfg, {}, days=7, now_ns=NOW + 100 * 86_400 * 1_000_000_000)
        self.assertEqual(far["sessions_analysed"], 0)
        self.assertIn("Aucune session dans la fenetre", render_trends_markdown(far))

    def test_false_positive_feedback_removes_pattern_when_marked_everywhere(self) -> None:
        r = self._trends(days=7)
        b = next(row for row in r["recurring"] if row["pattern_key"].startswith("B|"))
        ids = [e["finding_id"] for e in b["examples"]]
        set_feedback(self.home, ids[0], "false-positive", "test")
        r2 = self._trends(days=7)
        b2 = next(row for row in r2["recurring"] if row["pattern_key"].startswith("B|"))
        self.assertEqual(b2["sessions"], 2, "une session marquee faux positif ne compte plus")
        self.assertEqual(b2["feedback"], {"false-positive": 1})
        for fid in ids[1:]:
            set_feedback(self.home, fid, "false-positive")
        r3 = self._trends(days=7)
        self.assertFalse(any(row["pattern_key"].startswith("B|") for row in r3["recurring"]))
        self.assertEqual(r3["false_positive_only_patterns"], 1)

    def test_cli_markdown_json_and_out(self) -> None:
        md = self._run("trends", "--days", "0")
        for section in ("# AgentWatch - gaspillages recurrents", "## Top des gaspillages recurrents", "## Par projet",
                        "## Par client", "## Par session", "C:\\proj1", "a.py"):
            self.assertIn(section, md)
        js = json.loads(self._run("trends", "--days", "0", "--format", "json"))
        self.assertEqual(js["sessions_analysed"], 6)
        self.assertEqual(js["window"]["days"], 0)
        out = self.home / "trends.md"
        self._run("trends", "--days", "0", "--min-sessions", "3", "--out", str(out))
        self.assertIn("## Par session", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
