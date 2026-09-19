"""Sante de la collecte : detecter une panne silencieuse des hooks.

# * Le hook ne fait jamais echouer le client (voulu) ; le revers est qu'une collecte cassee
#   ne se voit pas : Python desinstalle, depot deplace, entrees retirees a la main, hook
#   Codex jamais approuve. On cherche des preuves indirectes, sans lire aucun contenu :
#   - l'interpreteur fige dans les hooks n'existe plus ;
#   - le point d'entree fige ne correspond plus a celui qui s'execute (depot deplace) ;
#   - les entrees AgentWatch ont disparu du fichier du client, ou disableAllHooks est actif ;
#   - le client a ete actif (date de modification de ses journaux natifs) apres le dernier
#     evenement recu : silence anormal.
# ! Seules les dates de modification des journaux natifs sont lues, jamais leur contenu.
"""

from __future__ import annotations

import calendar
import glob
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX, SUPPORTED_CLIENTS
from agentwatch.collector.store import EventStore

INSTALL_DIRNAME = "install"
LEVEL_WARN = "warn"


def install_meta_path(home: Path, client: str) -> Path:
    return home / INSTALL_DIRNAME / f"{client}.json"


def read_install_meta(home: Path, client: str) -> dict[str, Any] | None:
    p = install_meta_path(home, client)
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    v = cfg.get(name)
    return v if isinstance(v, dict) else {}


def codex_sessions_dir(cfg: dict[str, Any]) -> Path:
    custom = _section(cfg, "health").get("codex_sessions_dir")
    return Path(os.path.expanduser(str(custom))) if custom else Path.home() / ".codex" / "sessions"


def _newest(paths: Iterable[str]) -> float | None:
    best: float | None = None
    for p in paths:
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def _newest_rollout_activity(paths: Iterable[str]) -> float | None:
    """Activite la plus recente des rollouts : date de modification, ou heure de la derniere ligne ecrite. Sous Windows,
    la date d'un rollout que Codex garde ouvert reste celle de sa creation (constate le 2026-09-19)."""
    from agentwatch.collector.rollouts import last_line_ns
    best: float | None = None
    for p in paths:
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        ns = last_line_ns(p)
        a = max(m, ns / 1e9) if ns else m
        if best is None or a > best:
            best = a
    return best


def client_activity_mtime(client: str, cfg: dict[str, Any]) -> float | None:
    """Derniere activite des journaux natifs du client (transcripts Claude Code, rollouts Codex)."""
    if client == CLIENT_CLAUDE_CODE:
        from agentwatch.collector.transcripts import claude_projects_dir
        return _newest(glob.glob(os.path.join(str(claude_projects_dir(cfg)), "*", "*.jsonl")))
    root = codex_sessions_dir(cfg)
    # * Rollouts ranges par <annee>/<mois>/<jour> de CREATION du fil. Un fil repris continue d'ecrire
    #   dans son rollout d'origine (constate le 2026-09-19 : fil du 15 repris le 19) : les trois derniers
    #   dossiers ne suffisent pas, on regarde tous les rollouts des `health.codex_days` (30) derniers jours.
    span = int(_section(cfg, "health").get("codex_days", 30))
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - span * 86400))
    days = [d for d in glob.glob(os.path.join(str(root), "*", "*", "*")) if _day_key(d) >= cutoff]
    return _newest_rollout_activity(f for d in days for f in glob.glob(os.path.join(d, "*.jsonl")))


def _day_key(day_dir: str) -> str:
    """'AAAAMMJJ' d'un dossier <annee>/<mois>/<jour> ; '99999999' si le nom ne suit pas ce format (garde)."""
    parts = os.path.normpath(day_dir).split(os.sep)[-3:]
    key = "".join(parts)
    return key if len(key) == 8 and key.isdigit() else "99999999"


def last_event_mtime(store: EventStore, client: str) -> float | None:
    paths: list[str] = []
    for root in (store.spool, store.segments):
        d = root / client
        if d.is_dir():
            paths.extend(str(p) for p in d.rglob("*") if p.is_file())
    return _newest(paths)


def _parse_iso(s: Any) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _w(client: str, code: str, message: str) -> dict[str, Any]:
    return {"client": client, "code": code, "level": LEVEL_WARN, "message": message}


def check_collection(home: Path, cfg: dict[str, Any], store: EventStore | None = None) -> list[dict[str, Any]]:
    """Avertissements sur la collecte, par client configure ici. Liste vide = rien a signaler."""
    hcfg = _section(cfg, "health")
    if not hcfg.get("enabled", True):
        return []
    silence = float(hcfg.get("silence_minutes", 30)) * 60
    store = store or EventStore(home, cfg)
    from agentwatch.installer import claude_code as IC
    from agentwatch.installer import codex as IX
    from agentwatch.installer import common as C
    out: list[dict[str, Any]] = []
    for client in SUPPORTED_CLIENTS:
        meta = read_install_meta(home, client)
        if not meta:
            continue   # * client jamais configure depuis ce dossier de donnees : rien a surveiller
        py = meta.get("python")
        if isinstance(py, str) and py and not os.path.isfile(py):
            out.append(_w(client, "interpreter_missing",
                          f"l'interpreteur fige dans les hooks n'existe plus ({py}) : plus aucun evenement ne peut arriver ; "
                          f"relancer `agentwatch configure --client {client} --apply`"))
        entry = meta.get("hook_entry")
        current = str(C.hook_entry_path())
        if isinstance(entry, str) and entry and os.path.normcase(os.path.abspath(entry)) != os.path.normcase(os.path.abspath(current)):
            out.append(_w(client, "entry_moved",
                          f"les hooks pointent vers {entry} mais AgentWatch s'execute depuis {current} (depot deplace ?) ; "
                          f"relancer `agentwatch configure --client {client} --apply`"))
        cfg_path = meta.get("config_path")
        if isinstance(cfg_path, str) and cfg_path:
            try:
                obj, _ = C.read_json_file(Path(cfg_path))
                st = IC.status(obj) if client == CLIENT_CLAUDE_CODE else IX.status(obj)
                if not st["installed_events"]:
                    out.append(_w(client, "hooks_missing", f"aucune entree AgentWatch dans {cfg_path} (retiree a la main ?)"))
                elif not st["complete"]:
                    out.append(_w(client, "hooks_partial", f"hooks incomplets dans {cfg_path} : manquent {st['missing_events']}"))
                if st.get("disable_all_hooks"):
                    out.append(_w(client, "hooks_disabled", f"disableAllHooks=true dans {cfg_path} : aucun hook ne s'execute"))
            except (OSError, ValueError) as exc:
                out.append(_w(client, "config_unreadable", f"{cfg_path} illisible ({exc})"))
        activity = client_activity_mtime(client, cfg)
        last = last_event_mtime(store, client)
        configured = _parse_iso(meta.get("configured_at"))
        # ? Le client a ecrit dans ses journaux apres l'installation, mais rien n'est arrive ici.
        if activity is not None and (configured is None or activity > configured + silence):
            if last is None:
                hint = " ; Codex n'execute le hook qu'apres approbation via /hooks" if client == CLIENT_CODEX else ""
                out.append(_w(client, "never_received",
                              f"journaux du client modifies le {_fmt(activity)} mais aucun evenement jamais recu{hint}"))
            elif activity - last > silence:
                out.append(_w(client, "silent_since_activity",
                              f"journaux du client modifies le {_fmt(activity)} mais dernier evenement recu le {_fmt(last)} "
                              f"({int((activity - last) / 60)} min de silence) : collecte probablement cassee, voir `agentwatch doctor`"))
    return out


def format_lines(items: list[dict[str, Any]]) -> list[str]:
    return [f"! collecte {it['client']} : {it['message']}" for it in items]
