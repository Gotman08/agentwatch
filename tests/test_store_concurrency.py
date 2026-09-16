"""Tests du stockage : ecritures concurrentes, quota, disque indisponible, compaction, retention."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentwatch.collector.ingest import ingest_payload
from agentwatch.collector.store import EventStore, StoreError, session_key
from agentwatch.config import load_config
from agentwatch.core import schema as S


def _writer(home: str, idx: int, count: int) -> int:
    cfg = load_config(home)
    ok = 0
    for i in range(count):
        payload = {"session_id": "concurrent", "cwd": home, "hook_event_name": "PreToolUse", "tool_name": "Read",
                   "tool_input": {"file_path": os.path.join(home, f"f{idx}_{i}.py")}, "tool_use_id": f"t{idx}_{i}"}
        path, incident = ingest_payload(payload, "claude-code", home, cfg)
        ok += 1 if path and not incident else 0
    return ok


def _key_hex(home: str) -> str:
    from agentwatch.collector import privacy as P
    return P.ensure_key(home).hex()


class KeyCreationTests(unittest.TestCase):
    def test_concurrent_first_key_creation_agrees(self) -> None:
        """Huit processus creent la cle en meme temps : tous doivent obtenir la cle persistee."""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(8) as pool:
                keys = pool.map(_key_hex, [tmp] * 8)
            on_disk = (Path(tmp) / "keys" / "hmac.key").read_bytes().hex()
            self.assertEqual(set(keys), {on_disk}, "aucun hook ne doit signer avec une cle non persistee")
            self.assertFalse(list((Path(tmp) / "keys").glob("*.tmp-*")))

    def test_key_dir_is_a_file_raises(self) -> None:
        from agentwatch.collector import privacy as P
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "keys").write_text("x", encoding="utf-8")
            with self.assertRaises(OSError):
                P.ensure_key(Path(tmp))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_writers_lose_nothing(self) -> None:
        procs, per = 6, 25
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(procs) as pool:
            results = pool.starmap(_writer, [(str(self.home), i, per) for i in range(procs)])
        self.assertEqual(sum(results), procs * per)
        store = EventStore(self.home, load_config(self.home))
        events, warnings = store.read_session_events("claude-code", "concurrent")
        self.assertEqual(warnings, [])
        self.assertEqual(len(events), procs * per)
        self.assertEqual(len({e["event_id"] for e in events}), procs * per)
        self.assertFalse(list((self.home / "keys").glob("*.tmp-*")), "aucun fichier temporaire de cle orphelin")

    def test_session_key_is_safe(self) -> None:
        self.assertEqual(session_key("abc-123"), "abc-123")
        weird = session_key("a/b\\c:d e" + "x" * 100)
        self.assertNotIn("/", weird)
        self.assertLessEqual(len(weird), 64)
        self.assertEqual(session_key(None), "_no-session")

    def test_quota_drops_with_diagnostic(self) -> None:
        cfg = load_config(self.home)
        cfg["max_session_events"] = 1
        store = EventStore(self.home, cfg)
        ev = S.empty_event(); ev.update({"client": "codex", "session_id": "q", "phase": "start"})
        store.write_event(ev)
        dropped = False
        for _ in range(200):  # echantillonnage 1/16 : quelques essais suffisent
            ev2 = S.empty_event(); ev2.update({"client": "codex", "session_id": "q", "phase": "start"})
            try:
                store.write_event(ev2)
            except StoreError:
                dropped = True
                break
        self.assertTrue(dropped)
        path, incident = ingest_payload({"session_id": "q2", "hook_event_name": "Stop"}, "codex", self.home, dict(cfg, max_event_bytes=10))
        self.assertIsNone(path)
        self.assertEqual(incident, "drop")
        self.assertTrue(any(d["kind"] == "drop" for d in store.read_diagnostics()))

    def test_disk_unavailable_never_raises(self) -> None:
        bad_home = self.home / "file_not_dir"
        bad_home.write_text("x", encoding="utf-8")
        payload = {"session_id": "d", "hook_event_name": "Stop"}
        path, incident = ingest_payload(payload, "claude-code", bad_home, load_config(bad_home))
        self.assertIsNone(path)
        self.assertIn(incident, ("key_error", "write_error"))

    def test_compact_and_prune(self) -> None:
        cfg = load_config(self.home)
        for i in range(3):
            ingest_payload({"session_id": "c1", "hook_event_name": "Stop"}, "codex", self.home, cfg)
        store = EventStore(self.home, cfg)
        self.assertEqual(store.compact_session("codex", "c1"), 3)
        events, _ = store.read_session_events("codex", "c1")
        self.assertEqual(len(events), 3)
        self.assertEqual(store.compact_session("codex", "c1"), 0)
        removed = store.prune(retention_days=0, now=time.time() + 10 * 86400)
        self.assertEqual(removed, ["codex/c1"])
        self.assertEqual(list(store.iter_sessions()), [])

    def test_malformed_and_oversized_stdin_via_subprocess(self) -> None:
        entry = Path(__file__).resolve().parent.parent / "agentwatch" / "hook_entry.py"
        cmd = [sys.executable, "-I", str(entry), "ingest", "--client", "codex", "--home", str(self.home)]
        import subprocess
        r = subprocess.run(cmd, input=b"{not json", capture_output=True)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        cfg_path = self.home / "config.json"
        cfg_path.write_text(json.dumps({"max_stdin_bytes": 200}), encoding="utf-8")
        big = json.dumps({"session_id": "big", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "x"},
                          "tool_use_id": "b", "tool_response": {"output": "y" * 5000, "exit_code": 0}}).encode()
        r = subprocess.run(cmd, input=big, capture_output=True)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        store = EventStore(self.home, load_config(self.home))
        kinds = [d["kind"] for d in store.read_diagnostics()]
        self.assertIn("malformed", kinds)
        self.assertEqual(kinds.count("malformed"), 2, "un payload tronque par max_stdin_bytes est un incident, pas un evenement invente")
        r = subprocess.run(cmd, input=b"", capture_output=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("empty_stdin", [d["kind"] for d in store.read_diagnostics()])


if __name__ == "__main__":
    unittest.main()
