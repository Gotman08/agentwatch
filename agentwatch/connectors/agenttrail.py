"""Relais local AgentWatch -> Agenttrail Kitchen, sans nouvelle collecte.

Le serveur Kitchen alpha.3 date les hooks a leur reception. On ne lui transmet
donc que des observations recentes, jamais un historique presente comme actif.
Les prompts, commandes, resultats, empreintes et tokens ne font pas partie du
contrat de cette API. Ils restent dans les rapports AgentWatch.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib import error, request

from agentwatch.core import schema as S

MAX_AGE_SECONDS = 120
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_PENDING = 4000
_PROVIDERS = {"codex": "codex", "claude-code": "claude"}
_KINDS = {"start": "tool-start", "end": "tool-end", "failure": "tool-end",
          "interrupt": "interrupted", "session_start": "session-start",
          "session_end": "session-end", "turn_start": "turn-start",
          "turn_end": "turn-end", "subagent_start": "session-start",
          "subagent_stop": "turn-end"}


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


def kitchen_events(event: dict[str, Any], project: Path) -> list[dict[str, Any]]:
    """Projection par liste blanche. Aucune inference de todo ou de livraison."""
    if S.validate_event(event):
        return []
    if not all(isinstance(event.get(key), str) for key in ("client", "phase", "event_id")):
        return []
    provider = _PROVIDERS.get(event.get("client"))
    kind = _KINDS.get(event.get("phase"))
    parent = _clean(event.get("session_id"), 200)
    child = _clean(event.get("agent_id"), 200)
    if child in ("main", parent):
        child = ""
    if not provider or not kind or not parent:
        return []
    if event.get("phase", "").startswith("subagent_") and not child:
        return []  # Une identite manquante ne doit pas terminer le parent.
    cwd = event.get("cwd") or event.get("project_dir")
    cwd_path = _local_path(cwd)
    if cwd_path is None or not _within(cwd_path, project):
        return []
    identity = "agentwatch:" + hashlib.sha256(
        (provider + ":" + str(event["event_id"])).encode("utf-8")).hexdigest()
    base: dict[str, Any] = {"id": identity, "provider": provider,
                           "sessionId": child or parent, "cwd": str(cwd_path), "kind": kind}
    if child and child != parent:
        base["parentId"] = parent
    for source, target, limit in (("turn_id", "turnId", 200), ("call_id", "toolId", 200),
                                  ("tool_name", "tool", 80)):
        value = _clean(event.get(source), limit)
        if value:
            base[target] = value
    if event.get("target_kind") == "path" and isinstance(event.get("target"), str):
        source_project = _local_path(event.get("project_dir")) or cwd_path
        file = _local_path(event["target"], source_project)
        if file is not None and _within(file, project):
            base["file"] = str(file)
    if kind == "tool-end":
        status = event.get("status")
        base["error"] = event.get("phase") == "failure" or status in ("error", "denied", "timeout")
        if status not in ("success", "error", "denied", "timeout") and event.get("phase") != "failure":
            # Fermer l'appel, puis conserver un etat inconnu/interrompu dans Kitchen.
            uncertain = dict(base, id=identity + ":state",
                             kind="interrupted" if status == "interrupted" else "unknown")
            uncertain.pop("error", None)
            return [base, uncertain]
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
    """Erreur locale expurgée : ne jamais exposer la cle ni le corps HTTP."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class KitchenClient:
    def __init__(self, state_dir: Path, project: Path) -> None:
        self.state_dir, self.project = state_dir, project
        self.registration: tuple[int, str] | None = None
        self.opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())

    def _http(self, port: int, route: str, data: dict[str, Any] | None = None,
              token: str | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        req = request.Request(f"http://127.0.0.1:{port}{route}", data=body, headers=headers)
        try:
            with self.opener.open(req, timeout=2) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ConnectorError("reponse Kitchen trop volumineuse")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except error.HTTPError as exc:
            raise ConnectorError(f"Kitchen a refuse la requete (HTTP {exc.code})") from None
        except (error.URLError, OSError, ValueError):
            raise ConnectorError("Kitchen est indisponible ou sa reponse est invalide") from None

    def connect(self) -> str:
        try:
            with (self.state_dir / "server.json").open("rb") as stream:
                raw = stream.read(16_385)
            if len(raw) > 16_384:
                raise ValueError
            config = json.loads(raw)
            port, token = config["port"], config["hookToken"]
            if (not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535
                    or not isinstance(token, str) or len(token) != 48
                    or any(c not in "0123456789abcdef" for c in token)):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError):
            raise ConnectorError("demarrez Kitchen avec le meme --state-dir (server.json absent ou invalide)") from None
        registration = (port, token)
        if registration != self.registration:
            state = self._http(port, "/api/state")
            if state.get("app") != "agenttrail-kitchen" or state.get("version") != 2:
                raise ConnectorError("le service enregistre n'est pas une Kitchen compatible (API version 2)")
            attached = self._http(port, "/api/attach", {"projects": [str(self.project)]}, token)
            if attached.get("app") != "agenttrail-kitchen" or str(self.project) not in attached.get("projects", []):
                raise ConnectorError("Kitchen n'a pas confirme le dossier de projet")
            self.registration = registration
        return f"http://127.0.0.1:{port}"

    def send(self, event: dict[str, Any]) -> bool:
        if self.registration is None:
            raise ConnectorError("Kitchen n'est pas connectee")
        port, token = self.registration
        result = self._http(port, "/api/hook", event, token)
        if not isinstance(result.get("accepted"), bool):
            raise ConnectorError("Kitchen n'a pas accuse reception de l'evenement")
        return result["accepted"]


def run_bridge(args: Any, home: Path, out: Any = print, err: Any = print) -> int:
    project = Path(args.project_dir).resolve()
    if not project.is_dir():
        raise ValueError("--project-dir doit designer un dossier existant")
    if (not math.isfinite(args.interval) or args.interval < 0.5
            or not math.isfinite(args.recent_seconds) or not 0 <= args.recent_seconds <= MAX_AGE_SECONDS):
        raise ValueError("--interval >= 0.5 s et --recent-seconds entre 0 et 120 s")
    tail = EventTail(home, time.time_ns() - int(args.recent_seconds * 1_000_000_000))
    client = KitchenClient(Path(args.state_dir).expanduser().resolve(), project)
    pending: deque[tuple[int, dict[str, Any]]] = deque()
    sent = duplicates = expired = 0
    previous_error = None
    connected_url = None
    if not args.dry_run:
        out("AgentWatch -> Agenttrail Kitchen : lecture locale; seuls les evenements nouveaux sont relayes"
            if not args.recent_seconds else f"AgentWatch -> Kitchen : observations des {args.recent_seconds:g} dernieres secondes")
    try:
        while True:
            for row in tail.poll():
                for event in kitchen_events(row, project):
                    pending.append((_observed_ns(row) or 0, event))
                    if len(pending) > MAX_PENDING:
                        raise ConnectorError("file de relais pleine; arretez puis relancez Kitchen et le connecteur")
            for warning in sorted(set(tail.warnings)):
                err("! " + warning)
            try:
                if not args.dry_run:
                    url = client.connect()
                    if url != connected_url or previous_error:
                        out("Kitchen connectee : " + url)
                    connected_url = url
                while pending:
                    ns, event = pending[0]
                    if ns < time.time_ns() - MAX_AGE_SECONDS * 1_000_000_000:
                        expired += 1
                    elif args.dry_run:
                        out(json.dumps(event, ensure_ascii=False))
                    elif client.send(event):
                        sent += 1
                    else:
                        duplicates += 1  # API : deja vu ou refuse; jamais un succes suppose.
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
                    out(f"relais : {sent} accepte(s), {duplicates} deja vu(s) ou refuse(s), {expired} expire(s)")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if not args.dry_run:
            out(f"relais arrete : {sent} accepte(s), {duplicates} deja vu(s) ou refuse(s), {expired} expire(s)")
        return 0
