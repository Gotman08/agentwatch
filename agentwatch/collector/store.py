"""Persistance locale : spool d'evenements ecrits atomiquement + segments compactes.

# * Choix : chaque hook ecrit UN fichier JSON par evenement (ecriture dans un fichier
#   temporaire puis os.replace). Aucun verrou inter-processus n'est necessaire, ce qui
#   evite le point faible d'un append JSONL concurrent sous Windows. La commande
#   `agentwatch compact` fusionne ensuite les fichiers d'une session en un segment
#   JSONL (lecture plus rapide), toujours par ecriture atomique.
# * Arborescence : <home>/spool/<client>/<session_key>/<received_ns>-<pid>-<rand>.json
#                 <home>/segments/<client>/<session_key>/segment-<n>.jsonl
#                 <home>/diagnostics/<received_ns>-<pid>-<kind>.json
# * Chemin chaud (write_event, write_diagnostic) : os.path uniquement ; pathlib n'est
#   importe que par les methodes de lecture (cote analyse).
"""

from __future__ import annotations

import json
import os
import re
import time

from agentwatch.collector.privacy import sha256_hex

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Iterator

SPOOL_DIRNAME = "spool"
SEGMENTS_DIRNAME = "segments"
DIAG_DIRNAME = "diagnostics"
_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._\-]")


class StoreError(Exception):
    """Erreur de persistance (disque indisponible, quota, etc.)."""


def longpath(path: str) -> str:
    """Sous Windows, prefixe \\\\?\\ pour depasser la limite de 260 caracteres (MAX_PATH).

    # ! Verifie en direct : un dossier de donnees profond faisait echouer l'ecriture avec
    #   FileNotFoundError alors que le dossier existait. Le prefixe est retire des chemins
    #   renvoyes a l'utilisateur.
    """
    if os.name != "nt" or path.startswith("\\\\?\\") or not os.path.isabs(path):
        return path
    return "\\\\?\\" + os.path.abspath(path)


def shortpath(path: str) -> str:
    return path[4:] if path.startswith("\\\\?\\") else path


def session_key(session_id: str | None) -> str:
    """Nom de dossier sur (ASCII, borne) derive de l'identifiant de session."""
    if not session_id:
        return "_no-session"
    safe = _SAFE_SESSION.sub("_", session_id)
    if safe != session_id or len(safe) > 64:
        digest = sha256_hex(session_id.encode("utf-8", "surrogateescape"))[:12]
        safe = safe[:48] + "-" + digest
    return safe


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Ecrit `data` dans `path` via un fichier temporaire + os.replace (atomique par volume)."""
    path_s = longpath(os.fspath(path))
    tmp = path_s + f".tmp-{os.getpid()}-{os.urandom(3).hex()}"
    # ! O_BINARY : sous Windows, un descripteur ouvert sans ce drapeau est en mode texte
    #   et traduit 0x0A en 0x0D 0x0A, ce qui corromprait les segments JSONL.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass  # ? fsync indisponible sur certains volumes : on garde l'ecriture
        # ? Sous Windows, un antivirus peut tenir le fichier tout juste ecrit quelques
        #   millisecondes : os.replace echoue alors par PermissionError transitoire.
        for attempt in range(6):
            try:
                os.replace(tmp, path_s)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.01 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike[str], obj: Any) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class EventStore:
    """Acces au spool et aux segments d'un dossier AgentWatch."""

    def __init__(self, home: str | os.PathLike[str], config: dict[str, Any] | None = None) -> None:
        self.home_s = os.fspath(home)
        self.config = config or {}
        self.spool_s = os.path.join(self.home_s, SPOOL_DIRNAME)
        self.segments_s = os.path.join(self.home_s, SEGMENTS_DIRNAME)
        self.diagnostics_s = os.path.join(self.home_s, DIAG_DIRNAME)

    # ------------------------------------------------------------------ proprietes Path (analyse)
    @property
    def home(self) -> Path:
        from pathlib import Path as _P
        return _P(self.home_s)

    @property
    def spool(self) -> Path:
        from pathlib import Path as _P
        return _P(self.spool_s)

    @property
    def segments(self) -> Path:
        from pathlib import Path as _P
        return _P(self.segments_s)

    @property
    def diagnostics(self) -> Path:
        from pathlib import Path as _P
        return _P(self.diagnostics_s)

    # ------------------------------------------------------------------ ecriture (chemin chaud)
    def session_dir_str(self, client: str, session_id: str | None) -> str:
        return os.path.join(self.spool_s, client, session_key(session_id))

    def session_dir(self, client: str, session_id: str | None) -> Path:
        from pathlib import Path as _P
        return _P(self.session_dir_str(client, session_id))

    def write_event(self, event: dict[str, Any]) -> str:
        """Ecrit un evenement dans le spool. Leve StoreError si le quota est atteint."""
        client = event.get("client") or "unknown"
        sdir = longpath(self.session_dir_str(client, event.get("session_id")))
        os.makedirs(sdir, exist_ok=True)
        # * Quota par session verifie par echantillonnage (1/16) pour borner le cout du listage.
        max_events = int(self.config.get("max_session_events", 0) or 0)
        if max_events and (os.urandom(1)[0] & 0x0F) == 0:
            count = sum(1 for _ in os.scandir(sdir))
            if count >= max_events:
                raise StoreError(f"quota atteint : {count} evenements dans {os.path.basename(sdir)}")
        ns = event.get("received_time_ns") or time.time_ns()
        name = f"{ns:020d}-{os.getpid()}-{os.urandom(3).hex()}.json"
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        max_bytes = int(self.config.get("max_event_bytes", 0) or 0)
        if max_bytes and len(data) > max_bytes:
            raise StoreError(f"evenement trop volumineux : {len(data)} > {max_bytes} octets")
        path = os.path.join(sdir, name)
        atomic_write_bytes(path, data)
        return shortpath(path)

    def write_diagnostic(self, kind: str, detail: dict[str, Any]) -> str | None:
        """Journal d'incidents borne. Ne leve jamais (dernier recours du hook)."""
        try:
            diag_dir = longpath(self.diagnostics_s)
            os.makedirs(diag_dir, exist_ok=True)
            limit = int(self.config.get("max_diagnostics_files", 1000) or 1000)
            if sum(1 for _ in os.scandir(diag_dir)) >= limit:
                return None
            ns = time.time_ns()
            path = os.path.join(diag_dir, f"{ns:020d}-{os.getpid()}-{kind}.json")
            payload = {"time_ns": ns, "kind": kind, **detail}
            atomic_write_json(path, payload)
            return shortpath(path)
        except Exception:  # noqa: BLE001 - ! jamais d'exception depuis un hook
            return None

    # ------------------------------------------------------------------ lecture (analyse)
    def iter_sessions(self) -> Iterator[tuple[str, str, Path]]:
        """(client, session_key, dossier) pour chaque session presente (spool ou segments)."""
        seen: set[tuple[str, str]] = set()
        for root in (self.spool, self.segments):
            if not root.is_dir():
                continue
            for client_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                for sdir in sorted(p for p in client_dir.iterdir() if p.is_dir()):
                    key = (client_dir.name, sdir.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield client_dir.name, sdir.name, sdir

    def read_session_events(self, client: str, skey: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Lit tous les evenements d'une session (segments puis spool). Retourne (events, warnings)."""
        events: list[dict[str, Any]] = []
        warnings: list[str] = []
        seg_dir = self.segments / client / skey
        if seg_dir.is_dir():
            for seg in sorted(seg_dir.glob("segment-*.jsonl")):
                try:
                    with seg.open("r", encoding="utf-8") as fh:
                        for lineno, line in enumerate(fh, 1):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                events.append(json.loads(line))
                            except ValueError:
                                warnings.append(f"{seg.name}:{lineno}: ligne JSON invalide ignoree")
                except OSError as exc:
                    warnings.append(f"{seg.name}: lecture impossible ({exc})")
        sdir = self.spool / client / skey
        if sdir.is_dir():
            for f in sorted(sdir.iterdir()):
                if not f.name.endswith(".json") or f.name.startswith("_"):
                    continue
                try:
                    events.append(json.loads(f.read_text(encoding="utf-8")))
                except (OSError, ValueError) as exc:
                    warnings.append(f"{f.name}: evenement illisible ignore ({type(exc).__name__})")
        return events, warnings

    def find_session(self, session_id_or_key: str, client: str | None = None) -> list[tuple[str, str]]:
        """Resout un identifiant (complet, prefixe ou cle de dossier) vers [(client, key)]."""
        matches: list[tuple[str, str]] = []
        wanted_key = session_key(session_id_or_key)
        for c, key, _ in self.iter_sessions():
            if client and c != client:
                continue
            if key == wanted_key or key == session_id_or_key or key.startswith(session_id_or_key):
                matches.append((c, key))
        return matches

    # ------------------------------------------------------------------ compaction / retention
    def compact_session(self, client: str, skey: str) -> int:
        """Fusionne les fichiers du spool en un segment JSONL. Retourne le nombre compacte."""
        sdir = self.spool / client / skey
        if not sdir.is_dir():
            return 0
        files = sorted(f for f in sdir.iterdir() if f.name.endswith(".json") and not f.name.startswith("_"))
        if not files:
            return 0
        seg_dir = self.segments / client / skey
        seg_dir.mkdir(parents=True, exist_ok=True)
        # * Nom unique (horodatage + pid) : deux compactions concurrentes produisent deux segments
        #   au lieu de s'ecraser ; un evenement present dans les deux est dedoublonne a l'analyse
        #   par son event_id.
        seg_path = seg_dir / f"segment-{time.time_ns():020d}-{os.getpid()}.jsonl"
        lines: list[bytes] = []
        kept: list[Path] = []
        for f in files:
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # ? fichier illisible : on le laisse en place pour diagnostic
            lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            kept.append(f)
        if not lines:
            return 0
        atomic_write_bytes(seg_path, b"\n".join(lines) + b"\n")
        # * Les originaux ne sont supprimes qu'apres l'ecriture atomique du segment.
        removed = 0
        for f in kept:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def maybe_compact(self, client: str, skey: str, threshold: int) -> int:
        """Compacte si le spool de la session contient au moins `threshold` fichiers."""
        sdir = self.spool / client / skey
        if not sdir.is_dir():
            return 0
        count = 0
        for entry in os.scandir(sdir):
            if entry.name.endswith(".json"):
                count += 1
                if count >= threshold:
                    return self.compact_session(client, skey)
        return 0

    def prune(self, retention_days: int, now: float | None = None) -> list[str]:
        """Supprime les sessions dont le dernier evenement est plus ancien que la retention."""
        now = now or time.time()
        cutoff_ns = int((now - retention_days * 86400) * 1e9)
        removed: list[str] = []
        for client, skey, _ in list(self.iter_sessions()):
            latest = 0
            for root in (self.spool, self.segments):
                d = root / client / skey
                if not d.is_dir():
                    continue
                for f in d.iterdir():
                    try:
                        latest = max(latest, int(f.stat().st_mtime * 1e9))
                    except OSError:
                        pass
            if latest and latest < cutoff_ns:
                for root in (self.spool, self.segments):
                    d = root / client / skey
                    if d.is_dir():
                        for f in list(d.iterdir()):
                            try:
                                f.unlink()
                            except OSError:
                                pass
                        try:
                            d.rmdir()
                        except OSError:
                            pass
                removed.append(f"{client}/{skey}")
        return removed

    def stats(self) -> dict[str, Any]:
        """Statistiques descriptives du stockage (pour doctor)."""
        out: dict[str, Any] = {"sessions": 0, "spool_files": 0, "segment_files": 0, "bytes": 0, "diagnostics": 0}
        out["sessions"] = sum(1 for _ in self.iter_sessions())
        for root, field in ((self.spool, "spool_files"), (self.segments, "segment_files")):
            if root.is_dir():
                for f in root.rglob("*"):
                    if f.is_file():
                        out[field] += 1
                        try:
                            out["bytes"] += f.stat().st_size
                        except OSError:
                            pass
        if self.diagnostics.is_dir():
            out["diagnostics"] = sum(1 for _ in self.diagnostics.iterdir())
        return out

    def read_diagnostics(self, limit: int = 200) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.diagnostics.is_dir():
            return items
        for f in sorted(self.diagnostics.iterdir(), reverse=True)[:limit]:
            try:
                items.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return items
