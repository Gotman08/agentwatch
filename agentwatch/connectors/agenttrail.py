"""Relais local AgentWatch -> AgentTrail Map, sans nouvelle collecte.

Map date les hooks a leur reception : seules les observations recentes sont
transmises. Les commandes, prompts, resultats et tokens restent dans AgentWatch.
Les cibles sont des details d'outil, jamais des modifications supposees.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib import error, request

from agentwatch.core import schema as S

MAX_AGE_SECONDS = 120
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_PENDING = 4000
_PROVIDERS = {"codex": "codex", "claude-code": "claude"}
_KINDS = {"start": "PreToolUse", "end": "PostToolUse", "failure": "PostToolUse",
          "interrupt": "Stop", "session_start": "SessionStart",
          "session_end": "SessionEnd", "turn_start": "SessionStart",
          "turn_end": "Stop", "subagent_start": "SessionStart",
          "subagent_stop": "Stop"}


def _clean(value: Any, limit: int) -> str:
    return "".join(" " if ord(c) < 32 else c for c in value)[:limit] if isinstance(value, str) else ""


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _local_path(value: Any, relative_to: Path | None = None) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # La collecte masque le dossier utilisateur par ~. Les cibles relatives
        # sont ancrees au projet source par core.normalize, pas au cwd de l'outil.
        path = Path.home() / value[2:] if value.startswith(("~/", "~\\")) else Path.home() if value == "~" else Path(value)
        if not path.is_absolute():
            if relative_to is None:
                return None
            path = relative_to / path
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _observed_ns(event: dict[str, Any]) -> int | None:
    ns = event.get("received_time_ns")
    return ns if isinstance(ns, int) and not isinstance(ns, bool) and ns > 0 else None


def map_events(event: dict[str, Any], project: Path) -> list[dict[str, Any]]:
    """Projection par liste blanche, sans todo ni activite de fichier inventee."""
    if S.validate_event(event):
        return []
    if not all(isinstance(event.get(key), str) for key in ("client", "phase", "event_id")):
        return []
    provider = _PROVIDERS.get(event.get("client"))
    kind = _KINDS.get(event.get("phase"))
    parent = event.get("session_id") if isinstance(event.get("session_id"), str) else ""
    child = event.get("agent_id") if isinstance(event.get("agent_id"), str) else ""
    if child in ("main", parent):
        child = ""
    if not provider or not kind or not parent:
        return []
    if event.get("phase", "").startswith("subagent_") and not child:
        return []  # Une identite manquante ne doit pas terminer le parent.
    cwd = event.get("cwd") or event.get("project_dir")
    cwd_path = _local_path(cwd)
    file = None
    if event.get("target_kind") == "path" and isinstance(event.get("target"), str):
        source_project = _local_path(event.get("project_dir")) or cwd_path
        target = _local_path(event["target"], source_project)
        if target is not None and _within(target, project):
            file = target
    if cwd_path is not None and _within(cwd_path, project):
        # Le daemon compare des chaines sensibles a la casse, meme sous Windows.
        cwd_path = project / cwd_path.relative_to(project)
    elif file is not None:
        # Un outil peut agir dans le depot depuis une session ouverte ailleurs.
        cwd_path = project
    else:
        return []
    identity = json.dumps([provider, parent, child], ensure_ascii=False)
    base: dict[str, Any] = {"agent": provider, "cwd": str(cwd_path),
                           "session_id": "agentwatch-" + provider + "-" + hashlib.sha256(identity.encode()).hexdigest(),
                           "hook_event_name": kind}
    if kind in ("PreToolUse", "PostToolUse"):
        tool = _clean(event.get("tool_name"), 80)
        if not tool:
            return []
        base["tool_name"] = tool
        detail = "AgentWatch: tool-start"
        if file is not None:
            detail = "target=" + file.relative_to(project).as_posix()
        if kind == "PostToolUse":
            status = event.get("status")
            status = "error" if event.get("phase") == "failure" else status
            if status not in ("success", "error", "denied", "timeout", "interrupted"):
                status = "unknown"
            detail = "result=" + status + ("; " + detail if file is not None else "")
        # Map chauffe les fichiers de TOUT PostToolUse contenant file_path,
        # y compris Read. Ne pas fournir ce champ : fs.watch reste la preuve
        # des modifications. pattern est uniquement une ligne de metadonnees.
        base["tool_input"] = {"pattern": _clean(detail, 240)}
    return [base]


class EventTail:
    """Lecture du spool et des segments modifies, sans compaction ni ecriture.

    Le nom des fichiers spool permet d'eviter d'ouvrir les anciens evenements.
    Une compaction concurrente est toleree; ses copies sont dedoublonnees.
    """

    def __init__(self, home: Path, since_ns: int) -> None:
        self.home, self.since_ns = home, since_ns
        self.files: dict[str, tuple[int, int]] = {}
        self.seen: set[str] = set()
        self.order: deque[str] = deque()
        self.warnings: list[str] = []

    def _rows(self, file: Path, segmented: bool, cutoff: int, now_ns: int) -> list[dict[str, Any]]:
        rows = []
        with file.open("rb") as stream:
            while True:
                raw = stream.readline(MAX_EVENT_BYTES + 1) if segmented else stream.read(MAX_EVENT_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_EVENT_BYTES:
                    self.warnings.append("evenement trop volumineux ignore")
                    break
                try:
                    row = json.loads(raw)
                    ns = _observed_ns(row) if isinstance(row, dict) else None
                    if ns is not None and cutoff <= ns <= now_ns + 60_000_000_000:
                        rows.append(row)
                except (ValueError, UnicodeError):
                    self.warnings.append("evenement JSON invalide ignore")
                if not segmented:
                    break
        return rows

    def poll(self, now_ns: int | None = None) -> list[dict[str, Any]]:
        now_ns = time.time_ns() if now_ns is None else now_ns
        cutoff = max(self.since_ns, now_ns - MAX_AGE_SECONDS * 1_000_000_000)
        rows: list[dict[str, Any]] = []
        alive: set[str] = set()
        self.warnings = []
        for folder, pattern in (("spool", "*.json"), ("segments", "segment-*.jsonl")):
            for client in _PROVIDERS:
                root = self.home / folder / client
                if not root.is_dir():
                    continue
                for file in root.glob("*/" + pattern):
                    segmented = folder == "segments"
                    if not segmented:
                        prefix = file.name.split("-", 1)[0]
                        if not prefix.isdecimal() or int(prefix) < cutoff:
                            continue
                    if file.is_symlink() or not _within(file.resolve(), root.resolve()):
                        continue
                    key = str(file)
                    alive.add(key)
                    try:
                        stat = file.stat()
                        stamp = (stat.st_mtime_ns, stat.st_size)
                        if self.files.get(key) == stamp:
                            continue
                        # Un vieux segment ne peut pas contenir d'observations nouvelles.
                        if segmented and stat.st_mtime_ns < self.since_ns:
                            self.files[key] = stamp
                            continue
                        events = self._rows(file, segmented, cutoff, now_ns)
                        self.files[key] = stamp
                    except FileNotFoundError:
                        continue  # Un autre lecteur vient de compacter le spool.
                    except OSError:
                        self.warnings.append("fichier de collecte illisible; nouvel essai au prochain passage")
                        continue
                    for row in events:
                        ns, identity = _observed_ns(row), row.get("event_id")
                        if ns is None or ns < cutoff or ns > now_ns + 60_000_000_000:
                            continue
                        if not isinstance(identity, str) or not identity:
                            continue
                        identity = str(row.get("client")) + ":" + identity
                        if identity in self.seen:
                            continue
                        self.seen.add(identity)
                        self.order.append(identity)
                        while len(self.order) > 50_000:
                            self.seen.discard(self.order.popleft())
                        rows.append(row)
        self.files = {key: value for key, value in self.files.items() if key in alive}
        return sorted(rows, key=lambda row: (_observed_ns(row) or 0, str(row.get("event_id"))))


class ConnectorError(RuntimeError):
    """Erreur locale expurgee : ne jamais exposer le corps HTTP."""


class DeliveryUncertain(ConnectorError):
    """Un POST a pu etre traite : Map n'a pas de cle de dedoublonnage."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class MapClient:
    def __init__(self, port: int, project: Path) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            raise ValueError("--port doit etre compris entre 1024 et 65535")
        self.port, self.project = port, project
        self.connected = False
        self.server_project: Path | None = None
        self.opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())

    def _http(self, route: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        req = request.Request(f"http://127.0.0.1:{self.port}{route}", data=body, headers=headers)
        try:
            with self.opener.open(req, timeout=2) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError
                result = json.loads(raw) if raw else {}
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except error.HTTPError as exc:
            cls = DeliveryUncertain if data is not None else ConnectorError
            raise cls(f"Map a refuse la requete (HTTP {exc.code})") from None
        except (error.URLError, OSError, ValueError, HTTPException):
            cls = DeliveryUncertain if data is not None else ConnectorError
            raise cls("Map est indisponible ou sa reponse est invalide") from None

    def connect(self) -> str:
        self.connected = False
        identity = self._http("/whoami")
        if _local_path(identity.get("repoPath")) != self.project or identity.get("port") != self.port:
            raise ConnectorError("ce port Map ne correspond pas au --project-dir demande")
        model = self._http("/model")
        if not isinstance(model.get("runs"), list) or not isinstance(model.get("hasPlan"), bool):
            raise ConnectorError("ce service ne fournit pas le modele AgentTrail Map")
        self.server_project = Path(identity["repoPath"])
        self.connected = True
        return f"http://127.0.0.1:{self.port}"

    def send(self, event: dict[str, Any]) -> bool:
        if not self.connected or self.server_project is None:
            raise ConnectorError("Map n'est pas connecte")
        cwd = _local_path(event.get("cwd"))
        if cwd is None or not _within(cwd, self.project):
            raise ConnectorError("evenement hors du projet Map")
        # Conserver l'orthographe de /whoami : le daemon compare des chaines,
        # meme lorsque Windows accepte une autre casse ou un alias du dossier.
        payload = dict(event, cwd=str(self.server_project / cwd.relative_to(self.project)))
        self._http("/hook", payload)
        return True  # HTTP 200 est une transmission, pas un accusé d'acceptation.


def run_bridge(args: Any, home: Path, out: Any = print, err: Any = print) -> int:
    project = Path(args.project_dir).resolve()
    if not project.is_dir():
        raise ValueError("--project-dir doit designer un dossier existant")
    if (not math.isfinite(args.interval) or args.interval < 0.5
            or not math.isfinite(args.recent_seconds) or not 0 <= args.recent_seconds <= MAX_AGE_SECONDS):
        raise ValueError("--interval >= 0.5 s et --recent-seconds entre 0 et 120 s")
    tail = EventTail(home, time.time_ns() - int(args.recent_seconds * 1_000_000_000))
    client = MapClient(args.port, project)
    pending: deque[tuple[int, dict[str, Any]]] = deque()
    sent = uncertain = expired = 0
    previous_error = None
    connected_url = None
    if not args.dry_run:
        out("AgentWatch -> AgentTrail Map : lecture locale; seuls les evenements nouveaux sont relayes"
            if not args.recent_seconds else f"AgentWatch -> Map : observations des {args.recent_seconds:g} dernieres secondes")
    try:
        while True:
            for row in tail.poll():
                for event in map_events(row, project):
                    pending.append((_observed_ns(row) or 0, event))
                    if len(pending) > MAX_PENDING:
                        raise ConnectorError("file de relais pleine; arretez puis relancez Map et le connecteur")
            for warning in sorted(set(tail.warnings)):
                err("! " + warning)
            try:
                if not args.dry_run:
                    url = client.connect()
                    if url != connected_url or previous_error:
                        out("Map connecte : " + url)
                    connected_url = url
                while pending:
                    ns, event = pending[0]
                    if ns < time.time_ns() - MAX_AGE_SECONDS * 1_000_000_000:
                        expired += 1
                    elif args.dry_run:
                        out(json.dumps(event, ensure_ascii=False))
                    else:
                        try:
                            client.send(event)
                            sent += 1
                        except DeliveryUncertain:
                            # Un nouvel essai pourrait creer une seconde ligne d'outil.
                            pending.popleft()
                            uncertain += 1
                            err("! transmission non confirmee; evenement non rejoue (Map ne dedoublonne pas les hooks)")
                            if not args.follow:
                                return 2
                            continue
                    pending.popleft()
                previous_error = None
            except ConnectorError as exc:
                message = str(exc)
                if not args.follow:
                    err(message)
                    return 2
                if message != previous_error:
                    err("! " + message + "; nouvel essai au prochain passage")
                previous_error = message
            if not args.follow:
                if not args.dry_run:
                    out(f"relais : {sent} transmis, {uncertain} non confirme(s), {expired} expire(s)")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if not args.dry_run:
            out(f"relais arrete : {sent} transmis, {uncertain} non confirme(s), {expired} expire(s)")
        return 0
