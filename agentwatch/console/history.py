"""Tendances et comparaison existantes, executees sur le stockage du projet choisi."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agentwatch.config import load_config
from agentwatch.core.correlate import build_session
from agentwatch.core.timeslice import slice_view
from agentwatch.reports.compare import compare, measure
from agentwatch.reports.trends import build_trends
from .storage import Project, encode, identity, within


class ScopedStore:
    def __init__(self, project: Project, cfg: dict, limit: int):
        self.project, self.cfg, self.limit = project, cfg, limit
        self.warnings = []

    def iter_sessions(self):
        with self.project.db() as db:
            rows = db.execute("SELECT id,client FROM sessions ORDER BY last_ns DESC LIMIT ?", (self.limit,)).fetchall()
        for row in rows:
            yield row["client"], row["id"], self.project.home

    def read_session_events(self, client, sid):
        cap = int(self.cfg.get("console_analysis_event_limit", 50000))
        events, _ = self.project.event_rows(sid, cap)
        with self.project.db() as db:
            retained = db.execute("SELECT event_count FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        warnings = []
        if retained > len(events):
            warnings = [f"Analyse limitee a {len(events)} / {retained} observations de la session {sid}."]
            self.warnings.extend(warnings)
        # The analysis represents this root's slice. Original cwd/provenance remains in SQLite.
        return [{**e, "project_dir": str(self.project.root)} for e in events], warnings


def trends(project: Project, options: dict) -> dict:
    cfg = load_config(project.home)
    cfg["auto_compact_threshold"] = 0
    limit = min(1000, max(1, int(options.get("session_limit", 100))))
    store = ScopedStore(project, cfg, limit)
    with project.db() as db:
        feedback = {r["id"]: {"mark": r["mark"], "note": r["note"]} for r in db.execute("SELECT * FROM feedback")}
    if (project.home / "feedback.json").exists():
        from agentwatch.reports.feedback import load_feedback
        feedback = load_feedback(project.home)
    result = build_trends(store, cfg, feedback, days=int(options.get("days", 7)),
                          min_sessions=max(2, int(options.get("min_sessions", 2))))
    result["console_scope"] = {"root": str(project.root), "session_limit": limit,
                               "sessions_retained": project.info()["sessions"], "warnings": store.warnings,
                               "note": "Tendances calculees uniquement dans cette racine. Les tokens d'une session "
                                       "traversant plusieurs racines ne sont pas un cout par projet."}
    return result


def comparison(project: Project, options: dict) -> dict:
    cfg = load_config(project.home)
    cap = max(100, int(cfg.get("console_analysis_event_limit", 50000)))
    sid = str(options.get("before") or "")
    events, _ = project.event_rows(sid, cap)
    before = build_session(events, cfg)
    before.project_dir = str(project.root)
    if options.get("boundary_ns"):
        boundary = int(options["boundary_ns"])
        if not before.first_ns < boundary < before.last_ns:
            raise ValueError("Le seuil doit etre compris entre le debut et la fin de cette session.")
        after = slice_view(before, boundary, None)
        before = slice_view(before, None, boundary)
    else:
        other = str(options.get("after") or "")
        if sid == other:
            raise ValueError("Choisissez deux sessions differentes, ou une date de separation.")
        events, _ = project.event_rows(other, cap)
        after = build_session(events, cfg)
        after.project_dir = str(project.root)
    result = compare(measure(before, cfg), measure(after, cfg))
    result["console_scope"] = {"root": str(project.root), "selection": options, "event_limit_per_session": cap,
                               "note": "Comparaison descriptive des observations choisies. Aucun gain causal n'est presume."}
    return result


class HistoryJobs:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.threads: list[threading.Thread] = []

    def start(self, project: Project, kind: str, options: dict) -> dict:
        if kind not in ("trends", "compare"):
            raise ValueError("Analyse inconnue.")
        with project.db() as db:
            version = tuple(db.execute("SELECT COUNT(*),SUM(revision) FROM sessions").fetchone())
        key = identity(encode([project.id, kind, options, version]))
        with self.lock:
            finished = [k for k, j in self.jobs.items() if j["status"] != "running"]
            for old in finished[:-32]:
                self.jobs.pop(old)
            self.threads = [t for t in self.threads if t.is_alive()]
            if key not in self.jobs:
                if any(j["project"] == project.id and j["status"] == "running" for j in self.jobs.values()):
                    raise ValueError("Une analyse historique est deja en cours pour ce projet.")
                job = {"id": key, "project": project.id, "kind": kind, "status": "running", "result": None, "error": None}
                self.jobs[key] = job
                thread = threading.Thread(target=self._run, args=(project, job, options), daemon=True)
                self.threads.append(thread)
                thread.start()
            return self.get(project.id, key)

    def _run(self, project: Project, job: dict, options: dict):
        try:
            result = trends(project, options) if job["kind"] == "trends" else comparison(project, options)
            path = project.home / "reports"
            path.mkdir(exist_ok=True)
            if not within(path, project.home):
                raise ValueError("Le dossier des rapports doit appartenir a ce projet.")
            dest = path / (job["kind"] + "-" + job["id"] + ".json")
            temp = dest.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temp.write_text(encode(result), "utf-8")
            temp.replace(dest)
            with self.lock:
                job.update(status="complete", result=result)
        except Exception as exc:
            with self.lock:
                job.update(status="failed", error=f"{type(exc).__name__}: {exc}")

    def get(self, pid: str, key: str) -> dict:
        with self.lock:
            job = self.jobs.get(key)
            if not job or job["project"] != pid:
                raise KeyError("Analyse inconnue dans ce projet.")
            return dict(job)

    def stop(self):
        deadline = time.monotonic() + 12
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))


def snapshots(project: Project) -> list[dict]:
    directory = project.home / "reports"
    if not directory.is_dir() or not within(directory, project.home):
        return []
    files = [p for kind in ("trends", "compare") for p in directory.glob(kind + "-*.json")
             if p.is_file() and not p.is_symlink()]
    return [{"id": p.stem.split("-", 1)[1], "kind": p.stem.split("-", 1)[0], "created_ns": p.stat().st_mtime_ns}
            for p in sorted(files, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:100]]


def load_snapshot(project: Project, key: str) -> dict:
    if len(key) != 24 or any(c not in "0123456789abcdef" for c in key):
        raise KeyError("Analyse inconnue dans ce projet.")
    for kind in ("trends", "compare"):
        path = project.home / "reports" / (kind + "-" + key + ".json")
        if path.is_file() and not path.is_symlink() and within(path, project.home):
            return {"id": key, "project": project.id, "kind": kind, "status": "complete", "error": None,
                    "result": json.loads(path.read_text("utf-8"))}
    raise KeyError("Analyse inconnue dans ce projet.")
