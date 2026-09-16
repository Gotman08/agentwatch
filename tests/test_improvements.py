"""Tests des ameliorations : compaction automatique, report --latest, pythonw, listing groupe,
robustesse du hook face a des entrees hostiles."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.installer import common as C
from agentwatch.selftest import Synth

ENTRY = Path(__file__).resolve().parent.parent / "agentwatch" / "hook_entry.py"


class AutoCompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_load_session_compacts_above_threshold(self) -> None:
        (self.home / "config.json").write_text(json.dumps({"auto_compact_threshold": 10}), encoding="utf-8")
        s = Synth(self.home, session_id="big")
        s.session_start(); s.user_prompt()
        for i in range(12):
            s.read(f"f{i}.py", "x")
        cfg = load_config(self.home)
        store = EventStore(self.home, cfg)
        spool = store.session_dir("claude-code", "big")
        self.assertGreaterEqual(len(list(spool.glob("*.json"))), 10)
        view = load_session(store, "claude-code", "big", cfg)
        self.assertEqual(len(view.calls), 12)
        self.assertEqual(list(spool.glob("*.json")), [], "spool fusionne en segment")
        self.assertTrue(list((store.segments / "claude-code" / "big").glob("segment-*.jsonl")))
        # * De nouveaux evenements apres compaction sont lus avec le segment.
        s.read("late.py", "y")
        view2 = load_session(store, "claude-code", "big", cfg)
        self.assertEqual(len(view2.calls), 13)

    def test_threshold_zero_disables(self) -> None:
        (self.home / "config.json").write_text(json.dumps({"auto_compact_threshold": 0}), encoding="utf-8")
        s = Synth(self.home, session_id="nc")
        s.session_start(); s.read("a.py", "x")
        cfg = load_config(self.home)
        store = EventStore(self.home, cfg)
        load_session(store, "claude-code", "nc", cfg)
        self.assertTrue(list(store.session_dir("claude-code", "nc").glob("*.json")))


def _compactor(home: str, rounds: int) -> int:
    store = EventStore(home, load_config(home))
    total = 0
    for _ in range(rounds):
        total += store.compact_session("claude-code", "race")
    return total


def _writer(home: str, count: int) -> int:
    from agentwatch.collector.ingest import ingest_payload
    cfg = load_config(home)
    ok = 0
    for i in range(count):
        path, incident = ingest_payload({"session_id": "race", "hook_event_name": "Stop", "cwd": home}, "claude-code", home, cfg)
        ok += 1 if path and not incident else 0
    return ok


class ConcurrentCompactionTests(unittest.TestCase):
    def test_two_compactions_and_a_writer_lose_nothing(self) -> None:
        import multiprocessing
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.json").write_text(json.dumps({"auto_compact_threshold": 0}), encoding="utf-8")
            s = Synth(home, session_id="race")
            for i in range(75):
                s.read(f"f{i}.py", "x")   # 150 evenements distincts (les payloads identiques en rejeu partagent un id)
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(3) as pool:
                r_w = pool.apply_async(_writer, (str(home), 60))
                r_c1 = pool.apply_async(_compactor, (str(home), 4))
                r_c2 = pool.apply_async(_compactor, (str(home), 4))
                written, c1, c2 = r_w.get(120), r_c1.get(120), r_c2.get(120)
            self.assertEqual(written, 60)
            store = EventStore(home, load_config(home))
            store.compact_session("claude-code", "race")
            view = load_session(store, "claude-code", "race", load_config(home))
            # * Dedoublonnage par event_id : les evenements presents dans deux segments ne comptent qu'une fois.
            self.assertEqual(view.counts["events"], 150 + 60, (c1, c2, view.counts))
            self.assertGreater(view.counts["duplicate_events"], 0, "les segments concurrents se chevauchent, sans perte")


class CliImprovementTests(unittest.TestCase):
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

    def test_report_latest_and_grouped_listing(self) -> None:
        s = Synth(self.home, session_id="older")
        s.session_start(); s.read("a.py", "1")
        s2 = Synth(self.home, session_id="newer")
        s2.t += 60_000_000_000
        s2.session_start(); s2.user_prompt()
        for i in range(20):
            s2.read(f"dup{i}.py", "same"); s2.read(f"dup{i}.py", "same")   # 20 groupes distincts de relecture
        js = json.loads(self._run("report", "--latest", "--format", "json"))
        self.assertEqual(js["session"]["session_id"], "newer")
        self.assertGreaterEqual(sum(1 for f in js["findings"] if f["rule_id"] == "A.redundant_reads"), 20)
        md = self._run("report", "--latest")
        self.assertIn("## Autres signalements", md)
        self.assertIn("### A.redundant_reads", md)
        self.assertIn("autre(s) (tous presents dans l'export JSON)", md)
        with self.assertRaises(SystemExit):
            cli.main(["--home", str(self.home), "report"])

    def test_hook_python_executable(self) -> None:
        exe = C.hook_python_executable()
        self.assertTrue(Path(exe).is_file())
        if sys.platform == "win32" and Path(sys.executable).with_name("pythonw.exe").is_file():
            self.assertTrue(exe.lower().endswith("pythonw.exe"))
        r = subprocess.run([exe, "-I", str(ENTRY), "ingest", "--client", "codex", "--home", str(self.home)],
                           input=json.dumps({"session_id": "pyw", "hook_event_name": "Stop"}).encode(), capture_output=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertTrue(list((self.home / "spool").rglob("*.json")))


class EditEchoTests(unittest.TestCase):
    def test_edit_and_write_output_size_excludes_echoed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = Synth(Path(tmp), session_id="echo")
            s.session_start(); s.user_prompt()
            big = "x" * 20000
            s.call("Write", {"file_path": s._abs("big.py"), "content": big},
                   {"type": "create", "filePath": s._abs("big.py"), "content": big, "structuredPatch": [], "originalFile": ""})
            s.call("Edit", {"file_path": s._abs("big.py"), "old_string": "x", "new_string": "y"},
                   {"filePath": s._abs("big.py"), "oldString": "x", "newString": "y", "originalFile": big, "structuredPatch": [{"lines": ["-x", "+y"]}]})
            s.read("big.py", big)
            v = s.view()
            write, edit, read = v.calls
            self.assertLess(write.output_size_bytes, 500)
            self.assertLess(edit.output_size_bytes, 500)
            self.assertGreater(read.output_size_bytes, 20000, "une lecture reste une vraie sortie")
            store = EventStore(Path(tmp), load_config(Path(tmp)))
            events, _ = store.read_session_events("claude-code", "echo")
            ends = {e["tool_name"]: e for e in events if e["phase"] == "end"}
            self.assertEqual(ends["Edit"]["output_size_source"], "serialized_tool_response_minus_echoed_file")
            self.assertGreater(ends["Edit"]["evidence"]["echoed_file_bytes"], 20000)
            self.assertEqual(ends["Read"]["output_size_source"], "serialized_tool_response")


class HostileInputTests(unittest.TestCase):
    """Le hook doit toujours sortir en 0, sans rien ecrire sur stdout, quel que soit stdin."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _hook(self, data: bytes) -> None:
        r = subprocess.run([sys.executable, "-I", str(ENTRY), "ingest", "--client", "claude-code", "--home", str(self.home)],
                           input=data, capture_output=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr[-300:])

    def test_hostile_inputs(self) -> None:
        deep: dict = {}
        cur = deep
        for _ in range(3000):
            cur["k"] = {}
            cur = cur["k"]
        self._hook(json.dumps({"session_id": "h", "hook_event_name": "PostToolUse", "tool_name": "mcp__x__y", "tool_input": {"a": 1},
                               "tool_use_id": "b", "tool_response": deep}).encode())
        self._hook(("{\"session_id\":\"h\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls\",\"x\":"
                    + "[" * 3000 + "]" * 3000 + "},\"tool_use_id\":\"a\"}").encode())
        self._hook(b'{"session_id":"h","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo \xff\xfe"},"tool_use_id":"c"}')
        self._hook(json.dumps({"session_id": 123, "hook_event_name": ["x"], "tool_name": {"a": 1}, "tool_input": "notadict", "tool_use_id": 5}).encode())
        cwd = "C:\\Users\\nicol\\Desktop\\proj\u00e9t \u00e9t\u00e9"
        self._hook(json.dumps({"session_id": "h", "cwd": cwd, "hook_event_name": "PreToolUse", "tool_name": "Read",
                               "tool_input": {"file_path": cwd + "\\d\u00e9j\u00e0 vu.py"}, "tool_use_id": "d"}, ensure_ascii=False).encode("utf-8"))
        store = EventStore(self.home, load_config(self.home))
        events, warnings = store.read_session_events("claude-code", "h")
        self.assertEqual(warnings, [])
        self.assertEqual(len(events), 4)
        accents = next(e for e in events if e["call_id"] == "d")
        self.assertEqual(accents["target"], "d\u00e9j\u00e0 vu.py")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "h", "--format", "json"]), 0)
        js = json.loads(buf.getvalue())
        self.assertIn("d\u00e9j\u00e0 vu.py", [c["target"] for c in js["calls"]])
        self.assertIn("proj\u00e9t \u00e9t\u00e9", js["session"]["project_dir"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "report", "--session", "h"]), 0)
        self.assertIn("proj\u00e9t \u00e9t\u00e9", buf.getvalue(), "le Markdown conserve les accents")


if __name__ == "__main__":
    unittest.main()
