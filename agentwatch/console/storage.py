"""Stockage par racine de travail ; la registry ne contient aucune trace de projet."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def canonical(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def within(value: str | Path, root: str | Path) -> bool:
    try:
        return os.path.commonpath([canonical(value), canonical(root)]) == canonical(root)
    except (ValueError, OSError):
        return False


def session_key(client: str, session: str) -> str:
    return identity(client + "\0" + session)


@contextmanager
def console_lock(state: Path):
    """One owner of the shared reader cursors; automatically released even after a crash."""
    state.mkdir(parents=True, exist_ok=True)
    with (state / "console.lock").open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Une console utilise deja ce dossier d'etat.") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Project:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir() or self.root.name == ".agentwatch":
            raise ValueError("Choisissez un dossier de travail, pas le dossier de donnees .agentwatch.")
        self.id = identity(canonical(self.root))
        self.home = self.root / ".agentwatch"
        self.home.mkdir(exist_ok=True)
        # Never follow a substituted data directory into another project's storage.
        if self.home.is_symlink() or self.home.resolve().parent != self.root:
            raise ValueError(".agentwatch doit etre un dossier local a cette racine.")
        self.path = self.home / "history.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Le fichier de donnees ne doit pas etre un lien symbolique.")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, client TEXT NOT NULL, event_id TEXT NOT NULL,
                    session TEXT NOT NULL, at_ns INTEGER NOT NULL, phase TEXT, route TEXT,
                    data TEXT NOT NULL, UNIQUE(client,event_id));
                CREATE INDEX IF NOT EXISTS events_session ON events(session,at_ns,id);
                CREATE INDEX IF NOT EXISTS events_time ON events(at_ns,id);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, client TEXT, native_id TEXT, revision INTEGER DEFAULT 0,
                    analysed_revision INTEGER DEFAULT -1, first_ns INTEGER, last_ns INTEGER,
                    event_count INTEGER DEFAULT 0, report TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS sessions_time ON sessions(last_ns DESC);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, at_ns INTEGER NOT NULL,
                    end_ns INTEGER, tool TEXT, client TEXT, status TEXT, mcp TEXT, data TEXT);
                CREATE INDEX IF NOT EXISTS calls_time ON calls(at_ns DESC,id);
                CREATE INDEX IF NOT EXISTS calls_session ON calls(session,at_ns);
                CREATE INDEX IF NOT EXISTS calls_mcp ON calls(mcp,at_ns);
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY, mark TEXT, note TEXT, updated_ns INTEGER);
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY, path TEXT, at_ns INTEGER, UNIQUE(path,at_ns));
                CREATE INDEX IF NOT EXISTS changes_time ON changes(at_ns DESC);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT, created_ns INTEGER);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation TEXT, role TEXT, provider TEXT,
                    model TEXT, content TEXT DEFAULT '', status TEXT, created_ns INTEGER,
                    context TEXT, digest TEXT, error TEXT, usage TEXT);
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation,created_ns);
            """)
        self.ignore_note = self._exclude()

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=15000")
            with db:
                yield db
        finally:
            db.close()

    def _exclude(self) -> str:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--git-path", "info/exclude"],
                                    capture_output=True, text=True, timeout=3, creationflags=flags)
            if result.returncode:
                return "Dossier sans depot Git."
            path = Path(result.stdout.strip())
            if not path.is_absolute():
                path = self.root / path
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if ".agentwatch/" not in text.splitlines():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(("\n" if text and not text.endswith("\n") else "") + ".agentwatch/\n")
            return "Exclu de Git via info/exclude."
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            return f"Exclusion Git a verifier : {type(exc).__name__}"

    def info(self) -> dict[str, Any]:
        with self.db() as db:
            s = db.execute("SELECT COUNT(*) n,COALESCE(SUM(event_count),0) events,MAX(last_ns) last_ns "
                           "FROM sessions").fetchone()
            c = db.execute("SELECT COUNT(*) n, SUM(status IN ('error','timeout','denied','failure')) failures,COUNT(DISTINCT mcp) mcp "
                           "FROM calls").fetchone()
            pending = db.execute("SELECT COUNT(*) FROM sessions WHERE revision!=analysed_revision").fetchone()[0]
        return {"id": self.id, "name": self.root.name, "root": str(self.root), "storage": str(self.home),
                "sessions": s["n"], "events": s["events"], "last_ns": s["last_ns"], "calls": c["n"],
                "failures": c["failures"] or 0, "mcp_servers": c["mcp"], "pending_analysis": pending,
                "git": self.ignore_note}

    def append(self, event: dict[str, Any], route: str) -> bool:
        client = str(event.get("client") or "unknown")
        native = str(event.get("session_id") or "unknown")
        sid = session_key(client, native)
        eid = str(event.get("event_id") or identity(encode(event)))
        ns = int(event.get("received_time_ns") or 0)
        with self.db() as db:
            cur = db.execute("INSERT OR IGNORE INTO events(client,event_id,session,at_ns,phase,route,data) "
                             "VALUES(?,?,?,?,?,?,?)", (client, eid, sid, ns, event.get("phase"), route, encode(event)))
            if not cur.rowcount:
                return False
            db.execute("INSERT INTO sessions(id,client,native_id,revision,first_ns,last_ns,event_count) "
                       "VALUES(?,?,?,1,?,?,1) ON CONFLICT(id) DO UPDATE SET revision=revision+1, "
                       "first_ns=MIN(first_ns,excluded.first_ns),last_ns=MAX(last_ns,excluded.last_ns), "
                       "event_count=event_count+1", (sid, client, native, ns, ns))
        return True

    def event_rows(self, sid: str, limit: int | None = None) -> tuple[list[dict[str, Any]], int]:
        with self.db() as db:
            meta = db.execute("SELECT revision FROM sessions WHERE id=?", (sid,)).fetchone()
            if not meta:
                raise KeyError("Session inconnue.")
            if limit:
                rows = list(reversed(db.execute("SELECT data FROM events WHERE session=? ORDER BY at_ns DESC,id DESC LIMIT ?",
                                               (sid, limit)).fetchall()))
            else:
                rows = db.execute("SELECT data FROM events WHERE session=? ORDER BY at_ns,id", (sid,)).fetchall()
        return [json.loads(row[0]) for row in rows], meta[0]

    def save_analysis(self, sid: str, revision: int, report: dict[str, Any], calls: list[dict[str, Any]]) -> bool:
        with self.db() as db:
            current = db.execute("SELECT revision FROM sessions WHERE id=?", (sid,)).fetchone()
            if not current or current[0] < revision:
                return False
            # Publish this revision even when newer observations arrived. The UI explicitly marks it pending,
            # and the next pass analyses the new revision; a busy session must not starve indefinitely.
            db.execute("DELETE FROM calls WHERE session=?", (sid,))
            db.executemany("INSERT INTO calls(id,session,at_ns,end_ns,tool,client,status,mcp,data) "
                           "VALUES(?,?,?,?,?,?,?,?,?)",
                           [(c["id"], sid, c.get("start_ns") or c.get("end_ns") or 0, c.get("end_ns"),
                             c.get("tool_name"), c.get("client"), c.get("status"), c.get("mcp_server"), encode(c))
                            for c in calls])
            db.execute("UPDATE sessions SET report=?,analysed_revision=?,error=NULL WHERE id=?",
                       (encode(report), revision, sid))
        return True

    def report(self, sid: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute("SELECT report,error,revision,analysed_revision FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise KeyError("Session inconnue.")
        report = json.loads(row[0]) if row[0] else {}
        return {"report": report, "error": row[1], "revision": row[2], "analysed_revision": row[3],
                "pending": row[2] != row[3]}

    def conversations(self) -> list[dict[str, Any]]:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM conversations ORDER BY created_ns DESC LIMIT 100")]

    def conversation(self, cid: str) -> list[dict[str, Any]]:
        with self.db() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone():
                raise KeyError("Conversation inconnue dans ce projet.")
            return [dict(r) for r in db.execute("SELECT id,role,provider,model,content,status,created_ns,digest,error,usage "
                                               "FROM messages WHERE conversation=? ORDER BY created_ns,id", (cid,))]

    def new_conversation(self, title: str) -> str:
        cid = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO conversations VALUES(?,?,?)", (cid, title[:120], time.time_ns()))
        return cid


class Registry:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "projects.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.projects: dict[str, Project] = {}
        self.unavailable: list[dict[str, str]] = []
        self.errors: list[str] = []
        if self.path.exists():
            try:
                for entry in json.loads(self.path.read_text("utf-8")):
                    try:
                        project = Project(entry["root"])
                        self.projects[project.id] = project
                    except (OSError, ValueError) as exc:
                        self.unavailable.append({"root": entry["root"]})
                        self.errors.append(f"{entry.get('root')} : {exc}")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.errors.append(f"Registry illisible : {exc}")
                raise ValueError("Registry de projets illisible ; conserver le fichier pour diagnostic.") from exc

    def add(self, root: str) -> tuple[Project, bool]:
        with self.lock:
            pid = identity(canonical(root))
            if pid in self.projects:
                return self.projects[pid], False
            project = Project(root)
            remaining = {**self.projects, project.id: project}
            unavailable = [entry for entry in self.unavailable if canonical(entry["root"]) != canonical(project.root)]
            temp = self.path.with_suffix(".tmp")
            temp.write_text(encode([{"root": str(p.root)} for p in remaining.values()] + unavailable), "utf-8")
            os.replace(temp, self.path)
            self.projects = remaining
            self.unavailable = unavailable
            self.errors = [error for error in self.errors if not error.startswith(str(project.root) + " : ")]
            return project, True

    def all(self) -> list[Project]:
        with self.lock:
            return list(self.projects.values())

    def remove(self, pid: str) -> None:
        with self.lock:
            self.get(pid)
            remaining = {key: p for key, p in self.projects.items() if key != pid}
            temp = self.path.with_suffix(".tmp")
            temp.write_text(encode([{"root": str(p.root)} for p in remaining.values()] + self.unavailable), "utf-8")
            os.replace(temp, self.path)
            self.projects = remaining

    def get(self, pid: str) -> Project:
        with self.lock:
            if pid not in self.projects:
                raise KeyError("Projet inconnu.")
            return self.projects[pid]
