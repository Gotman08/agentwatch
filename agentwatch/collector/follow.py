"""Suivi continu des rollouts : un seul collecteur a la fois, avec un etat et un journal verifiables.

# * Verrou : un octet verrouille par le systeme dans `<home>/import/follow.lock`, tenu tant que le processus vit. Un
#   processus tue ou une machine redemarree le liberent d'eux-memes : jamais de verrou perime a nettoyer, jamais de
#   test « ce PID vit-il ? » (sous Windows, `os.kill(pid, 0)` TERMINE le processus vise).
# * Etat : `<home>/import/follow.json` (PID, demarrage, depot execute, dernier passage, dernier import, erreurs),
#   reecrit a chaque cycle. Seul le verrou dit si le collecteur vit ; l'etat dit ce qu'il a fait.
# * Journal : `<home>/logs/follow.log`, une ligne datee par evenement utile, borne (un fichier `.1` garde au-dela de
#   5 Mo). Constate le 2026-09-21 : le suivi lance le 19 avait disparu au redemarrage du 20 sans laisser de trace.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from agentwatch.collector.store import atomic_write_json, longpath

LOCK_FILENAME = "follow.lock"
STATUS_FILENAME = "follow.json"
LOG_FILENAME = "follow.log"
LOG_MAX_BYTES = 5 * 1024 * 1024


def _import_dir(home: str) -> str:
    return os.path.join(home, "import")


def status_path(home: str) -> str:
    return os.path.join(_import_dir(home), STATUS_FILENAME)


def log_path(home: str) -> str:
    return os.path.join(home, "logs", LOG_FILENAME)


class FollowLock:
    """Verrou exclusif non bloquant, libere par le systeme a la mort du processus."""

    def __init__(self, home: str) -> None:
        self.home = home
        self.path = os.path.join(_import_dir(home), LOCK_FILENAME)
        self._fh: Any = None

    def acquire(self) -> bool:
        os.makedirs(longpath(_import_dir(self.home)), exist_ok=True)
        fh = open(longpath(self.path), "a+b")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def is_running(home: str) -> bool:
    """Un collecteur tient-il le verrou ? (essai de prise, aussitot rendu)"""
    lock = FollowLock(home)
    if lock.acquire():
        lock.release()
        return False
    return True


def read_status(home: str) -> dict[str, Any]:
    try:
        with open(longpath(status_path(home)), encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(home: str, status: dict[str, Any]) -> None:
    os.makedirs(longpath(_import_dir(home)), exist_ok=True)
    atomic_write_json(status_path(home), status)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_log(home: str, line: str) -> None:
    """Une ligne datee (heure locale) ; ne leve jamais : un journal illisible n'arrete pas la collecte."""
    path = log_path(home)
    try:
        os.makedirs(longpath(os.path.dirname(path)), exist_ok=True)
        try:
            if os.path.getsize(longpath(path)) > LOG_MAX_BYTES:
                os.replace(longpath(path), longpath(path + ".1"))
        except OSError:
            pass
        with open(longpath(path), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line.rstrip("\n") + "\n")
    except OSError:
        pass


def describe(home: str, now: float | None = None) -> dict[str, Any]:
    """Etat lisible du suivi : en marche (verrou tenu) ou arrete, et ce que dit son dernier etat ecrit."""
    st = read_status(home)
    running = is_running(home)
    now = time.time() if now is None else now
    age = None
    if st.get("last_tick_epoch"):
        age = max(0.0, now - float(st["last_tick_epoch"]))
    return {"running": running, "pid": st.get("pid") if running else None, "started": st.get("started"),
            "last_tick": st.get("last_tick"), "last_tick_age_s": age, "interval_s": st.get("interval_s"),
            "cycles": st.get("cycles"), "last_import": st.get("last_import"), "errors": st.get("errors") or [],
            "repo": st.get("repo"), "version": st.get("version"), "stopped": st.get("stopped") if not running else None,
            "log": log_path(home), "status_file": status_path(home)}
