"""Supervise le renderer Map MIT, un observateur natif par racine."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .storage import Project

VENDOR = Path(__file__).parent / "vendor" / "agenttrail"


class MapProcess:
    def __init__(self, project: Project):
        self.project = project
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.token = secrets.token_urlsafe(32)
        self.error: str | None = None
        self.lock = threading.RLock()
        self.cache: dict[str, Any] = {}
        self.cache_at = 0.0
        self.log = None
        self.next_start = 0.0
        self.attempts = 0

    def start(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self.stop()
            self.cache_at = 0.0
            self.cache = {}
            self.next_start = time.monotonic() + 30
            self.attempts += 1
            node = shutil.which("node")
            if not node:
                self.error = "Node.js 20 ou plus est necessaire pour la carte."
                return
            try:
                # Resolve Volta shim so this PID actually owns the child and its lifetime.
                check = subprocess.run([node, "-p", 'JSON.stringify({exe:process.execPath,major:Number(process.versions.node.split(".")[0])})'], capture_output=True, text=True,
                                       timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                runtime = json.loads(check.stdout) if check.returncode == 0 else {}
                if runtime.get("major", 0) < 20:
                    raise RuntimeError("Node.js 20 ou plus est necessaire pour la carte.")
                if Path(runtime.get("exe", "")).is_file():
                    node = runtime["exe"]
                if (self.project.home / "file-events.jsonl").is_symlink():
                    raise ValueError("Le journal des fichiers doit appartenir a ce projet.")
                log_path = self.project.home / "map.log"
                self.log = log_path.open("w", encoding="utf-8")
                env = {**os.environ, "AGENTWATCH_MAP_TOKEN": self.token}
                self.proc = subprocess.Popen([node, str(VENDOR / "bin" / "agenttrail.mjs"), str(self.project.root),
                                              "--port", "0", "--no-open"], stdin=subprocess.PIPE,
                                             stdout=self.log, stderr=self.log, env=env,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for _ in range(100):
                    if self.proc.poll() is not None:
                        raise RuntimeError("Le processus Map s'est arrete. Consulter .agentwatch/map.log.")
                    for line in log_path.read_text("utf-8", errors="replace").splitlines():
                        if line.startswith('{"agentwatchMap"'):
                            self.port = int(json.loads(line)["port"])
                            self.error = None
                            return
                    time.sleep(.05)
                raise RuntimeError("Le processus Map ne repond pas apres 5 secondes.")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                self.error = str(exc)
                self.stop()

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.write(b"stop\n")
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=3)
            if self.log:
                self.log.close()
                self.log = None
            if self.proc and self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            self.port = None

    def info(self) -> dict[str, Any]:
        return {"running": bool(self.proc and self.proc.poll() is None), "pid": self.proc.pid if self.proc else None,
                "error": self.error, "port": self.port, "start_attempts": self.attempts,
                "watch_mode": self.cache.get("watchMode", "starting"), "change_log_error": self.cache.get("changeLogError")}

    def model(self) -> dict[str, Any]:
        with self.lock:
            if not self.port or not self.proc or self.proc.poll() is not None:
                raise RuntimeError(self.error or "Observateur Map arrete.")
            if time.monotonic() - self.cache_at < .8:
                return json.loads(json.dumps(self.cache))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}/model",
                                             headers={"X-AgentWatch-Internal": self.token})
            with opener.open(request, timeout=3) as response:
                data = json.load(response)
            if not isinstance(data, dict):
                raise RuntimeError("Reponse Map invalide.")
            for node in data.get("plan", []):
                if not str(node.get("url") or "").startswith(("http://", "https://")):
                    node["url"] = ""
            self.cache, self.cache_at = data, time.monotonic()
            return json.loads(json.dumps(data))


    def capture_changes(self) -> int:
        """Persist the complete watcher journal, including bursts larger than Map's 12-row preview."""
        path = self.project.home / "file-events.jsonl"
        if not path.exists():
            return 0
        if not path.is_file() or path.is_symlink():
            raise ValueError("Journal des changements invalide.")
        stat = path.stat()
        with self.project.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS change_cursor(name TEXT PRIMARY KEY,offset INTEGER,signature TEXT)")
            row = db.execute("SELECT offset,signature FROM change_cursor WHERE name='map'").fetchone()
            offset = row[0] if row and row[1] == str(stat.st_ino) and stat.st_size >= row[0] else 0
            count = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                for _ in range(4000):
                    line = stream.readline(65537)
                    if not line or not line.endswith(b"\n"):
                        break
                    if len(line) <= 65536:
                        try:
                            event = json.loads(line)
                            db.execute("INSERT OR IGNORE INTO changes(path,at_ns) VALUES(?,?)",
                                       (str(event["file"]), int(event["at"]) * 1_000_000))
                            count += 1
                        except (ValueError, KeyError, TypeError):
                            pass
                    offset = stream.tell()
            db.execute("INSERT OR REPLACE INTO change_cursor VALUES('map',?,?)", (offset, str(stat.st_ino)))
        return count


def map_model(map_process: MapProcess) -> dict[str, Any]:
    data = map_process.model()
    # The original hook UI labels stopped sessions "idle" from heuristics. AgentWatch's own operation/session
    # views carry the real status/provenance; do not replay historical hooks into this live filesystem map.
    data.update(runs=[], hooksInstalled=True, boards=[], handoffs=[], cycles=[])
    data["project"] = map_process.project.root.name
    data["repoPath"] = str(map_process.project.root)
    data["watcher"] = map_process.info()
    return data
