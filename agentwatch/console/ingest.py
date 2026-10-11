"""Lecture incrementale des observations existantes, sans compaction ni execution."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .storage import Registry, canonical, encode, identity, within


class Ingestor:
    def __init__(self, registry: Registry, source_home: Path, state_dir: Path):
        self.registry, self.source_home = registry, source_home.resolve()
        self.path = state_dir / "reader.sqlite3"
        self.lock = threading.RLock()
        self.reset_pending = False
        self.files = None
        self.status: dict[str, Any] = {"state": "starting", "read": 0, "inserted": 0, "duplicates": 0,
                                       "unassigned": 0, "invalid": 0, "pending_files": 0, "errors": []}
        with self.db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY,offset INTEGER,"
                             "signature TEXT, size INTEGER,mtime INTEGER);"
                             "CREATE TABLE IF NOT EXISTS routes(id TEXT PRIMARY KEY,project TEXT);"
                             "CREATE TABLE IF NOT EXISTS anchors(actor TEXT,at_ns INTEGER,project TEXT,PRIMARY KEY(actor,at_ns));"
                             "CREATE TABLE IF NOT EXISTS metadata(id TEXT PRIMARY KEY,value TEXT);")
            roots = encode({"roots": sorted(p.id for p in registry.all()), "routing_version": 3})
            row = db.execute("SELECT value FROM metadata WHERE id='roots'").fetchone()
            if not row or row[0] != roots:
                db.execute("DELETE FROM cursors")
            db.execute("INSERT OR REPLACE INTO metadata VALUES('roots',?)", (roots,))

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def reset(self) -> None:
        with self.lock:
            self.reset_pending = True

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(encode(self.status))

    def _route(self, ev: dict[str, Any], db: sqlite3.Connection) -> tuple[Any, str]:
        projects = self.registry.all()
        key = identity(encode([ev.get(k) for k in ("client", "session_id", "agent_id", "call_id")]))
        actor = "actor:" + identity(encode([ev.get(k) for k in ("client", "session_id", "agent_id")]))
        # A completion without cwd belongs to the exact project that recorded its start.
        if ev.get("call_id") and ev.get("phase") in ("end", "failure", "interrupt"):
            row = db.execute("SELECT project FROM routes WHERE id=?", (key,)).fetchone()
            if row:
                try:
                    return self.registry.get(row[0]), "call_start"
                except KeyError:
                    pass
        matches = []
        params = ev.get("params") or {}
        paths = [("workdir", params.get("workdir")), ("projectPath", params.get("projectPath")),
                 ("cwd", ev.get("cwd")), ("target", ev.get("target")), ("project_dir", ev.get("project_dir"))]
        for field, raw in paths:
            if not isinstance(raw, str) or not raw or raw.startswith(("mcp:", "http:", "https:")):
                continue
            value = Path(raw).expanduser()
            if not value.is_absolute():
                continue
            matches = [(p, field) for p in projects if within(value, p.root)]
            if matches:
                break
        if not matches:
            # A projectless native session can explicitly work in another root. Preserve this inference as
            # provenance, instead of silently rewriting the native cwd or attaching every session to the UI selection.
            row = db.execute("SELECT project FROM anchors WHERE actor=? AND at_ns<=? ORDER BY at_ns DESC LIMIT 1",
                             (actor, ev["received_time_ns"])).fetchone()
            if row:
                try:
                    project = self.registry.get(row[0])
                    if ev.get("call_id") and ev.get("phase") == "start":
                        db.execute("INSERT OR REPLACE INTO routes VALUES(?,?)", (key, project.id))
                    return project, "inferred_session_workdir"
                except KeyError:
                    pass
            return None, "unassigned"
        project, basis = max(matches, key=lambda pair: len(str(pair[0].root)))
        if ev.get("call_id") and ev.get("phase") == "start":
            db.execute("INSERT OR REPLACE INTO routes VALUES(?,?)", (key, project.id))
        db.execute("INSERT OR REPLACE INTO anchors VALUES(?,?,?)", (actor, ev["received_time_ns"], project.id))
        return project, basis

    def _files(self, recent: bool = False):
        homes = {canonical(self.source_home): self.source_home}
        for p in self.registry.all():
            homes.setdefault(canonical(p.home), p.home)
        candidates = []
        for home in homes.values():
            for kind, extension in (("spool", ".json"), ("segments", ".jsonl")):
                base = home / kind
                if not base.is_dir():
                    continue
                # Fixed depth, no recursive walk over the actual working tree.
                for client in sorted(os.scandir(base), key=lambda e: e.name):
                    if not client.is_dir(follow_symlinks=False):
                        continue
                    for session in os.scandir(client.path):
                        if not session.is_dir(follow_symlinks=False):
                            continue
                        candidates.append((session.stat(follow_symlinks=False).st_mtime_ns, session.path, extension, home, kind))
        # Recent sessions first, but each session's records in chronological filename order.
        ordered = sorted(candidates, reverse=True)
        if recent:
            ordered = ordered[:12]
        for _, directory, extension, home, kind in ordered:
            files = sorted(os.scandir(directory), key=lambda e: e.name)
            if recent:
                files = files[-256:] if kind == "spool" else files[-4:]
            for f in files:
                if f.name.endswith(extension) and f.is_file(follow_symlinks=False):
                    path = Path(f.path)
                    if within(path, home):
                        yield path, kind, path.stat(follow_symlinks=False)

    def step(self, budget: int = 2000, *, live: bool = False) -> int:
        counts = {k: 0 for k in ("read", "inserted", "duplicates", "unassigned", "invalid")}
        pending = None
        started = time.monotonic()
        errors = []
        working = None if live else self.files
        with self.db() as db:
            with self.lock:
                if self.reset_pending:
                    db.execute("DELETE FROM cursors")
                    db.execute("INSERT OR REPLACE INTO metadata VALUES('roots',?)",
                               (encode({"roots": sorted(p.id for p in self.registry.all()), "routing_version": 3}),))
                    self.reset_pending = False
                    self.files = None
                    working = None
            if working is None:
                working = iter(self._files(recent=live))
            while counts["read"] < budget and time.monotonic() - started < 6:
                try:
                    path, kind, stat = next(working)
                except StopIteration:
                    working = None
                    pending = 0
                    break
                name = str(path)
                row = db.execute("SELECT offset,signature,size,mtime FROM cursors WHERE path=?", (name,)).fetchone()
                sig = str(stat.st_ino)
                offset = row[0] if row and row[1] == sig and stat.st_size >= row[0] else 0
                if row and row[1] == sig and row[2] == stat.st_size and row[3] == stat.st_mtime_ns:
                    continue
                try:
                    retry_offset = None
                    with path.open("rb") as fh:
                        fh.seek(offset)
                        while counts["read"] < budget:
                            pos = fh.tell()
                            raw = fh.read(2_097_153) if kind == "spool" else fh.readline(2_097_153)
                            if not raw:
                                break
                            # Retry an incomplete tail on the next pass. Atomic spool JSON has no newline requirement.
                            if kind == "segments" and not raw.endswith(b"\n") and len(raw) <= 2_097_152:
                                break
                            counts["read"] += 1
                            try:
                                if len(raw) > 2_097_152:
                                    raise ValueError("Observation trop grande")
                                ev = json.loads(raw)
                                from agentwatch.core.schema import validate_event
                                if validate_event(ev):
                                    raise ValueError("Observation invalide")
                                project, basis = self._route(ev, db)
                                if project:
                                    counts["inserted" if project.append(ev, basis) else "duplicates"] += 1
                                else:
                                    counts["unassigned"] += 1
                                    if live and retry_offset is None:
                                        # Live reads can run ahead of a historical workdir/start anchor. Leave the
                                        # reference retryable for backfill instead of permanently consuming it.
                                        retry_offset = pos
                                offset = fh.tell()
                            except (ValueError, TypeError, OverflowError, UnicodeError):
                                counts["invalid"] += 1
                                if kind == "segments" and not raw.endswith(b"\n"):
                                    while raw and not raw.endswith(b"\n"):
                                        raw = fh.readline(2_097_153)
                                offset = fh.tell()
                            if kind == "spool":
                                break
                            if fh.tell() == pos:
                                break
                    if retry_offset is not None:
                        offset = retry_offset
                    done = offset == stat.st_size
                    db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?,?,?)",
                               (name, offset, sig, stat.st_size if done else -1, stat.st_mtime_ns if done else -1))
                    if not done and counts["read"] >= budget:
                        # A large segment must resume before moving on; exhausted batches must not skip its tail.
                        previous = working
                        def resume(first, tail):
                            yield first
                            yield from tail
                        working = resume((path, kind, path.stat()), previous)
                except OSError as exc:
                    errors.append(f"Lecture {path.name} : {type(exc).__name__}")
        with self.lock:
            if not live:
                self.files = working
            for key, value in counts.items():
                self.status[key] += value
            self.status.update(state="backfill" if self.files is not None else "watching", pending_files=pending,
                               last_scan_ns=time.time_ns(), scan_ms=round((time.monotonic() - started) * 1000),
                               errors=errors[:10])
            if live:
                self.status["live_scan_ns"] = time.time_ns()
        return counts["inserted"]
