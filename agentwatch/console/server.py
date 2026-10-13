"""Serveur loopback de la console. APIs de projet, observateurs, lecture et chat."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agentwatch.config import home_dir
from . import analysis
from .chat import ChatManager, prepare_context, providers
from .ingest import Ingestor
from .maps import MapProcess, VENDOR, map_model
from .storage import Registry, encode

STATIC = Path(__file__).parent / "static"


class Console:
    def __init__(self, state_dir: Path, source_home: Path):
        self.state_dir = state_dir.resolve()
        self.registry = Registry(self.state_dir)
        self.source_home = source_home.resolve()
        self.ingest = Ingestor(self.registry, self.source_home, self.state_dir)
        self.chat = ChatManager(self.state_dir)
        from .history import HistoryJobs
        self.history = HistoryJobs()
        self.maps: dict[str, MapProcess] = {}
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.worker: threading.Thread | None = None
        self.reader: threading.Thread | None = None
        self.error: str | None = None
        for p in self.registry.all():
            self.maps[p.id] = MapProcess(p)
            self.chat.recover(p)

    def add(self, root: str):
        p, added = self.registry.add(root)
        if added:
            with self.lock:
                self.maps[p.id] = MapProcess(p)
                self.chat.recover(p)
                if self.worker:
                    self.maps[p.id].start()
            self.ingest.reset()
        return p

    def start(self):
        self.reader = threading.Thread(target=self._read, daemon=True, name="agentwatch-project-reader")
        self.worker = threading.Thread(target=self._work, daemon=True, name="agentwatch-project-analysis")
        self.reader.start()
        self.worker.start()

    def remove(self, pid: str):
        with self.lock:
            self.registry.remove(pid)
            observer = self.maps.pop(pid, None)
            if observer:
                observer.stop()
        self.ingest.reset()

    def _read(self):
        while not self.closed.is_set():
            try:
                if self.registry.all():
                    # Live arrivals get a separate priority pass while older history is backfilled.
                    self.ingest.step(800, live=True)
                    self.ingest.step()
            except Exception as exc:
                with self.ingest.lock:
                    self.ingest.status.update(state="error", errors=[f"{type(exc).__name__}: {exc}"])
            self.closed.wait(1)

    def _work(self):
        while not self.closed.is_set():
            for p in self.registry.all():
                if self.closed.is_set():
                    break
                with self.lock:
                    observer = self.maps.get(p.id)
                    if observer is None:
                        continue
                    if (not observer.proc or observer.proc.poll() is not None) and time.monotonic() >= observer.next_start:
                        observer.start()
                try:
                    observer.capture_changes()
                    if observer.info()["running"]:
                        data = observer.model()
                        with p.db() as db:
                            db.executemany("INSERT OR IGNORE INTO changes(path,at_ns) VALUES(?,?)",
                                           [(c["file"], int(c["at"] * 1e6)) for c in data.get("recentActivity", [])])
                    for sid in analysis.pending(p):
                        try:
                            analysis.analyse(p, sid)
                        except Exception as exc:
                            with p.db() as db:
                                db.execute("UPDATE sessions SET error=? WHERE id=?", (f"{type(exc).__name__}: {exc}", sid))
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
            self.closed.wait(.5)

    def stop(self):
        self.closed.set()
        self.chat.stop()
        self.history.stop()
        for thread in (self.reader, self.worker):
            if thread:
                thread.join(timeout=12)
        with self.lock:
            for observer in self.maps.values():
                observer.stop()

    def projects(self):
        with self.lock:
            return [{**p.info(), "map": self.maps[p.id].info()} for p in self.registry.all()]

    def health(self):
        from agentwatch.collector.follow import describe
        try:
            state = describe(self.source_home)
            collector = {k: state.get(k) for k in ("running", "pid", "interval_s", "last_tick")}
        except Exception as exc:
            collector = {"error": f"{type(exc).__name__}: {exc}"}
        return {"reader": self.ingest.snapshot(), "collector": collector, "source_home": str(self.source_home),
                "projects": self.projects(), "registry_errors": self.registry.errors, "error": self.error,
                "note": "La console lit les observations disponibles. Elle n'installe pas de hooks a votre insu. "
                        "Les imports natifs suivent la configuration et l'intervalle du collecteur AgentWatch."}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, console: Console, port: int):
        self.console = console
        self.cookie = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # Queries/messages/context never enter the process log.

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _authorized(self, mutation=False):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            raise PermissionError("Hote refuse.")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in allowed}:
            raise PermissionError("Origine refusee.")
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            raise PermissionError("Session locale requise.") from None
        if not cookies.get("agentwatch") or not secrets.compare_digest(cookies["agentwatch"].value, self.server.cookie):
            raise PermissionError("Ouvrez la console pour commencer une session locale.")
        if mutation and not secrets.compare_digest(self.headers.get("X-AgentWatch-CSRF", ""), self.server.csrf):
            raise PermissionError("Jeton de session manquant. Rechargez la console.")

    def _send(self, data: bytes | str | dict | list, status=200, kind="application/json; charset=utf-8", **headers):
        if isinstance(data, (dict, list)):
            data = encode(data)
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            raise ValueError("Corps JSON manquant ou trop grand.")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Un corps application/json est requis.")
        obj = json.loads(self.rfile.read(length))
        if not isinstance(obj, dict):
            raise ValueError("Un objet JSON est requis.")
        return obj

    def _dispatch(self, mutation=False):
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        app = self.server.console
        if not mutation and url.path in ("/", "/index.html"):
            if self.headers.get("Host") not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                raise PermissionError("Hote refuse.")
            return self._send((STATIC / "index.html").read_bytes(), kind="text/html; charset=utf-8",
                              Set_Cookie=f"agentwatch={self.server.cookie}; Path=/; HttpOnly; SameSite=Strict",
                              Content_Security_Policy="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                      "frame-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'")
        if not mutation and url.path == "/whoami":
            if self.headers.get("Host") not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                raise PermissionError("Hote refuse.")
            return self._send({"app": "agentwatch-console", "pid": os.getpid(), "port": self.server.server_port,
                               "state_dir": str(app.state_dir)})
        self._authorized(mutation)
        if mutation and url.path == "/api/shutdown":
            self._body()
            self._send({"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if not mutation and url.path in ("/app.js", "/history.js", "/messages.js", "/mcp.js", "/timeline.js", "/app.css"):
            filename = url.path[1:]
            return self._send((STATIC / filename).read_bytes(), kind="text/javascript; charset=utf-8" if filename.endswith("js") else "text/css; charset=utf-8")
        if not mutation and url.path == "/api/bootstrap":
            return self._send({"csrf": self.server.csrf, "projects": app.projects(), "providers": providers(),
                               "reader": app.ingest.snapshot(), "version": "1.0", "source_home": str(app.source_home)})
        if not mutation and url.path == "/api/projects":
            return self._send(app.projects())
        if not mutation and url.path == "/api/health":
            return self._send(app.health())
        if url.path == "/api/projects" and mutation:
            data = self._body()
            if not data.get("root") or not Path(str(data["root"])).expanduser().is_absolute():
                raise ValueError("Indiquez le chemin absolu du dossier de travail.")
            return self._send(app.add(str(data["root"])).info(), status=201)
        if not mutation and url.path == "/api/folders":
            path = Path(query.get("path") or Path.cwd()).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("Ce chemin n'est pas un dossier.")
            folders = sorted((e for e in os.scandir(path) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                             key=lambda e: e.name.casefold())
            return self._send({"path": str(path), "parent": str(path.parent), "folders": [e.name for e in folders[:200]],
                               "truncated": len(folders) > 200})
        if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
            project = app.registry.get(parts[2])
            action = parts[3] if len(parts) > 3 else "summary"
            if not mutation:
                if action == "summary":
                    return self._send(analysis.summary(project))
                if action == "sessions":
                    return self._send(analysis.sessions(project))
                if action == "mcp":
                    from .mcp import overview
                    result = overview(project, query)
                    if query.get("format") == "json":
                        return self._send(result, Content_Disposition='attachment; filename="agentwatch-mcp.json"')
                    return self._send(result)
                if action == "history" and len(parts) == 5:
                    try:
                        job = app.history.get(project.id, parts[4])
                    except KeyError:
                        from .history import load_snapshot
                        job = load_snapshot(project, parts[4])
                    if query.get("format") == "json":
                        if job["status"] != "complete":
                            raise ValueError("Le calcul n'est pas termine.")
                        return self._send(job["result"], Content_Disposition=f'attachment; filename="agentwatch-{job["kind"]}-{job["id"]}.json"')
                    return self._send(job)
                if action == "history" and len(parts) == 4:
                    from .history import snapshots
                    return self._send(snapshots(project))
                if action == "calls":
                    if len(parts) == 5:
                        with project.db() as db:
                            call = db.execute("SELECT data FROM calls WHERE id=?", (parts[4],)).fetchone()
                            if not call:
                                raise KeyError("Appel inconnu dans ce projet.")
                            data = json.loads(call[0])
                            ids = data.get("event_ids") or []
                            events = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE client=? AND event_id IN ("
                                       + ",".join("?" for _ in ids) + ")", [data["client"], *ids])] if ids else []
                        from .sources import saved
                        return self._send({"call": data, "events": events, "output": saved(project, parts[4]), "output_note": "La taille et la duree sont observees. "
                                           "Les sorties brutes ne sont pas stockees dans les observations normalisees."})
                    return self._send(analysis.calls_page(project, query))
                if action == "timeline":
                    return self._send(analysis.timeline(project, query))
                if action == "report" and len(parts) >= 5:
                    result = project.report(parts[4])
                    if query.get("format") == "md":
                        return self._send(analysis.render_markdown(result["report"]), kind="text/markdown; charset=utf-8",
                                          Content_Disposition=f'attachment; filename="agentwatch-{parts[4]}.md"')
                    if query.get("format") == "json":
                        return self._send(result["report"], Content_Disposition=f'attachment; filename="agentwatch-{parts[4]}.json"')
                    return self._send(result)
                if action == "events":
                    limit = min(500, max(1, int(query.get("limit", 100))))
                    with project.db() as db:
                        rows = db.execute("SELECT id,data FROM events WHERE id>? ORDER BY id LIMIT ?",
                                          (int(query.get("cursor", 0)), limit + 1)).fetchall()
                    return self._send({"items": [json.loads(r[1]) for r in rows[:limit]],
                                       "cursor": rows[limit - 1][0] if len(rows) > limit else None})
                if action == "conversations":
                    return self._send(project.conversations())
                if action == "conversation" and len(parts) == 5:
                    return self._send(project.conversation(parts[4]))
                if action == "message-context" and len(parts) == 5:
                    with project.db() as db:
                        row = db.execute("SELECT context,digest,created_ns,provider FROM messages WHERE id=?", (parts[4],)).fetchone()
                    if not row or not row["context"]:
                        raise KeyError("Contexte de message inconnu dans ce projet.")
                    return self._send({"bundle": json.loads(row["context"]), "digest": row["digest"],
                                       "created_ns": row["created_ns"], "provider": row["provider"]})
                if action == "context" and len(parts) == 5:
                    with project.db() as db:
                        row = db.execute("SELECT data,digest FROM contexts WHERE id=?", (parts[4],)).fetchone()
                    if not row:
                        raise KeyError("Contexte inconnu dans ce projet.")
                    return self._send({"bundle": json.loads(row[0]), "digest": row[1]})
            else:
                data = self._body()
                if action == "unfollow":
                    app.remove(project.id)
                    return self._send({"stopped": True, "history_preserved": str(project.home)})
                if action == "context":
                    return self._send(prepare_context(project, data))
                if action in ("trends", "compare"):
                    return self._send(app.history.start(project, action, data), status=202)
                if action == "source":
                    from .sources import capture
                    return self._send(capture(project, str(data.get("call")), app.source_home, data.get("limit_chars", 20000)))
                if action == "chat":
                    return self._send(app.chat.start(project, data), status=202)
                if action == "cancel":
                    app.chat.cancel(project.id, str(data.get("message")))
                    return self._send({"cancelled": True})
                if action == "analyse":
                    with project.db() as db:
                        db.execute("UPDATE sessions SET analysed_revision=-1,error=NULL WHERE id=?", (data.get("session"),))
                    return self._send({"queued": True})
                if action == "feedback":
                    from agentwatch.reports.feedback import MARKS, set_feedback
                    if data.get("mark") not in MARKS:
                        raise ValueError("Avis invalide.")
                    note = str(data.get("note") or "")[:1000]
                    set_feedback(project.home, str(data.get("id")), data["mark"], note)
                    with project.db() as db:
                        if data["mark"] == "clear":
                            db.execute("DELETE FROM feedback WHERE id=?", (str(data.get("id")),))
                        else:
                            db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?)", (str(data.get("id")), data["mark"], note, time.time_ns()))
                        db.execute("UPDATE sessions SET analysed_revision=-1,error=NULL")
                    return self._send({"saved": True})
        if not mutation and len(parts) >= 3 and parts[0] == "p":
            project = app.registry.get(parts[1])
            observer = app.maps[project.id]
            prefix = f"/p/{project.id}"
            if parts[2] == "map":
                html = (VENDOR / "public" / "index.html").read_text("utf-8")
                # All original local API requests stay scoped to this one registered root.
                for route in ("events", "world", "tree-of", "fleet", "suggest", "spawn", "setup-board"):
                    html = html.replace(f"'/{route}", f"'{prefix}/{route}")
                html = html.replace("<head>", "<head><style>.topbar,.tree-add,.tree-add-form{display:none!important}"
                                    ".app{height:100vh}.workspace{height:100vh}.runs,.setup-btn{display:none!important}"
                                    ".sidebar{border-right:1px solid #e7e8eb}body{background:#fafbfc}</style>")
                html = html.replace("const es=new EventSource('" + prefix + "/events');let connectionTimer=null",
                                    "const es={};let connectionTimer=null;setInterval(async()=>{try{const r=await fetch('" + prefix
                                    + "/model');if(!r.ok)throw Error();const d=await r.json();es.onmessage?.({data:JSON.stringify(d)})}"
                                    "catch{es.onerror?.()}},1500)")
                html = html.replace("nothing mapped yet", "PLAN.md absent : arbre et appels disponibles")
                # Unknown durations remain unknown in the original session renderer.
                html = html.replace("t.ms>1200?Math.round(t.ms/1000)+'s':t.ms+'ms'",
                                    "t.ms==null?'duree inconnue':t.ms>1200?Math.round(t.ms/1000)+'s':t.ms+'ms'")
                return self._send(html, kind="text/html; charset=utf-8",
                                  Content_Security_Policy="default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                                          "img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'")
            if parts[2] == "model":
                return self._send(map_model(observer))
            if parts[2] == "world":
                model = map_model(observer)
                model.pop("tree", None)
                return self._send([model])
            if parts[2] == "tree-of":
                model = observer.model()
                return self._send({"tree": model.get("tree"), "treeTruncated": model.get("treeTruncated")})
            if parts[2] == "fleet":
                return self._send([])
            if parts[2] == "suggest":
                return self._send([])
        raise KeyError("Page ou API inconnue.")

    def do_GET(self):
        self._handle(False)

    def do_POST(self):
        self._handle(True)

    def _handle(self, mutation):
        try:
            self._dispatch(mutation)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PermissionError as exc:
            self._send({"error": str(exc)}, 403)
        except KeyError as exc:
            self._send({"error": str(exc).strip("'")}, 404)
        except (ValueError, TypeError, OSError) as exc:
            self._send({"error": str(exc)}, 400)
        except Exception as exc:
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)


def run_console(args) -> int:
    state = Path(args.state_dir).expanduser()
    from .storage import console_lock
    with console_lock(state):
        app = Console(state, home_dir(args.home))
        if args.project_dir:
            app.add(args.project_dir)
        server = Server(app, args.port)
        app.start()
        print(f"AgentWatch · http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever(poll_interval=.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            app.stop()
    return 0
