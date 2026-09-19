"""Interface de commande AgentWatch.

Commandes : doctor, configure, uninstall, sessions, report, trends, import-transcripts, self-test,
ingest, replay, compact, prune, feedback, bench, import-usage. Les messages utilisateur sont en francais.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX, SUPPORTED_CLIENTS, __version__
from agentwatch.collector.health import install_meta_path as _install_meta_path
from agentwatch.collector.health import read_install_meta as _read_install_meta
from agentwatch.config import home_dir, load_config, write_default_config


# ---------------------------------------------------------------------------- utilitaires
def _out(msg: str = "") -> None:
    print(msg)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _under_appdata(path: Path) -> bool:
    """Vrai si `path` est sous %APPDATA% ou %LOCALAPPDATA% (zones redirigees pour les applications MSIX)."""
    try:
        target = os.path.normcase(os.path.abspath(str(path)))
    except (OSError, ValueError):
        return False
    for var in ("APPDATA", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            root = os.path.normcase(os.path.abspath(root))
            if target == root or target.startswith(root + os.sep):
                return True
    return False


def _health(home: Path, cfg: dict[str, Any], store: Any = None) -> list[dict[str, Any]]:
    """Avertissements de collecte (panne silencieuse) ; jamais d'exception."""
    from agentwatch.collector.health import check_collection
    try:
        return check_collection(home, cfg, store)
    except Exception as exc:  # noqa: BLE001 - un diagnostic ne doit pas empecher un rapport
        return [{"client": "?", "code": "health_failed", "level": "warn", "message": f"verification de la collecte impossible ({exc})"}]


def _print_health(items: list[dict[str, Any]]) -> None:
    from agentwatch.collector.health import format_lines
    for line in format_lines(items):
        _err(line)


def _auto_import_enabled(cfg: dict[str, Any]) -> bool:
    tcfg = cfg.get("transcripts")
    return bool(isinstance(tcfg, dict) and tcfg.get("auto_import"))


def _import_transcripts(store: Any, cfg: dict[str, Any], client: str, skey: str) -> dict[str, Any]:
    from agentwatch.collector.transcripts import import_session
    from agentwatch.core.session import load_session
    view = load_session(store, client, skey, cfg)
    events, _ = store.read_session_events(client, skey)
    existing = {e.get("event_id") for e in events if isinstance(e, dict) and e.get("event_id")}
    return import_session(store, cfg, client, skey, view, existing)


def _rollouts_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    r = cfg.get("rollouts")
    return r if isinstance(r, dict) else {}


def _rollout_paths(cfg: dict[str, Any], days: float | None, thread: str | None) -> list[str]:
    """Rollouts de la fenetre ; avec `thread`, ce fil et ses sous-agents (lus dans leur session_meta)."""
    from agentwatch.collector import rollouts as R
    paths = R.list_rollouts(cfg, days)
    if not thread:
        return paths
    keep = []
    for p in paths:
        if (R._thread_id_from_path(p) or "").startswith(thread):
            keep.append(p)
            continue
        meta = R.read_thread_meta(p) or {}
        if str(meta.get("root_id") or "").startswith(thread) or str(meta.get("parent_id") or "").startswith(thread):
            keep.append(p)
    return keep


def _import_rollouts(home: Path, cfg: dict[str, Any], store: Any, days: float | None = None, thread: str | None = None,
                     sink: Any = None) -> dict[str, Any]:
    """Import incremental des rollouts Codex (lecture seule, priorite d'arriere-plan). Jamais d'exception."""
    from agentwatch.collector import rollouts as R
    rcfg = _rollouts_cfg(cfg)
    if days is None:
        days = float(rcfg.get("days", 7) or 0) or None
    try:
        todo = R.pending_rollouts(str(home), _rollout_paths(cfg, days, thread))
        if not todo:
            return {"files": 0, "lines": 0, "events": 0, "bytes": 0, "sessions": {}, "errors": []}
        with R.background_priority(bool(rcfg.get("background_priority", True))):
            return R.import_rollouts(store, cfg, todo, sink=sink)
    except Exception as exc:  # noqa: BLE001 - un import rate ne doit pas empecher un rapport
        return {"files": 0, "lines": 0, "events": 0, "bytes": 0, "sessions": {}, "errors": [f"{type(exc).__name__}: {exc}"]}


def _auto_import_rollouts(home: Path, cfg: dict[str, Any], store: Any) -> None:
    """Avant `sessions`, `report`, `trends` : rollouts Codex modifies depuis le dernier import (defaut actif)."""
    if not _rollouts_cfg(cfg).get("auto_import", True):
        return
    out = _import_rollouts(home, cfg, store)
    if out["files"]:
        _err(f"(rollouts Codex : {out['files']} fichier(s) mis a jour, {out['events']} evenement(s) importe(s))")
    for e in out["errors"][:3]:
        _err(f"! import des rollouts Codex : {e}")


def _client_version(client: str) -> tuple[str | None, str | None]:
    """(version, chemin executable) ; (None, None) si indisponible. Jamais d'exception."""
    exe = shutil.which("claude" if client == CLIENT_CLAUDE_CODE else "codex")
    if exe is None and client == CLIENT_CODEX and os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        if base.is_dir():
            cands = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            if cands:
                exe = str(cands[0])
    if exe is None:
        return None, None
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        line = (r.stdout or r.stderr).strip().splitlines()
        return (line[0] if line else None), exe
    except (OSError, subprocess.SubprocessError):
        return None, exe


def _resolve_session(store: Any, ident: str, client: str | None) -> tuple[str, str]:
    matches = store.find_session(ident, client)
    if not matches:
        raise SystemExit(f"session introuvable : {ident!r} (voir `agentwatch sessions`)")
    if len(matches) > 1:
        raise SystemExit("identifiant ambigu : " + ", ".join(f"{c}/{k}" for c, k in matches) + " (precisez --client ou l'identifiant complet)")
    return matches[0]


# ---------------------------------------------------------------------------- commandes
def cmd_ingest(args: argparse.Namespace) -> int:
    from agentwatch.collector.ingest import run_hook
    argv = ["--client", args.client]
    if args.home:
        argv += ["--home", args.home]
    return run_hook(argv)


def cmd_replay(args: argparse.Namespace) -> int:
    """Rejoue des payloads enregistres (JSON objet, liste ou JSONL). Ne rien executer."""
    from agentwatch.collector.ingest import ingest_payload
    from agentwatch.core import schema as S
    home = home_dir(args.home)
    cfg = load_config(home)
    files: list[Path] = []
    for p in args.paths:
        path = Path(p)
        if path.is_dir():
            files += sorted(x for x in path.iterdir() if x.suffix in (".json", ".jsonl"))
        else:
            files.append(path)
    written = dropped = 0
    ns = time.time_ns()
    for f in files:
        text = f.read_text(encoding="utf-8")
        payloads: list[Any] = []
        if f.suffix == ".jsonl":
            payloads = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            obj = json.loads(text)
            payloads = obj if isinstance(obj, list) else [obj]
        for payload in payloads:
            if isinstance(payload, dict):
                payload.pop("_note", None)
                if args.session_id:
                    payload["session_id"] = args.session_id
            path, incident = ingest_payload(payload, args.client, home, cfg, source=S.SOURCE_REPLAY, started_ns=ns)
            ns += 50_000_000
            if path:
                written += 1
            else:
                dropped += 1
    _out(f"rejoue : {written} evenement(s) ecrit(s), {dropped} incident(s) (dossier {home})")
    return 0 if dropped == 0 else 1


def cmd_sessions(args: argparse.Namespace) -> int:
    from agentwatch.collector.store import EventStore
    from agentwatch.core.session import list_sessions
    home = home_dir(args.home)
    cfg = load_config(home)
    store = EventStore(home, cfg)
    if args.client in (None, CLIENT_CODEX):
        _auto_import_rollouts(home, cfg, store)
    rows = list_sessions(store, cfg, client=args.client)
    _print_health(_health(home, cfg, store))
    if args.json:
        _out(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        _out(f"aucune session enregistree dans {home}")
        return 0
    _out(f"{'client':<12} {'session':<40} {'debut':<24} {'fin':<24} {'evts':>5} {'appels':>6} {'erreurs':>7} {'ouverts':>7}")
    for r in rows:
        _out(f"{r['client']:<12} {(r['session_id'] or r['session_key'])[:40]:<40} {(r['first_time'] or ''):<24} {(r['last_time'] or ''):<24} "
             f"{r['events']:>5} {r['calls']:>6} {r['errors']:>7} {r['open_calls']:>7}")
    return 0


def _analyse(home: Path, cfg: dict[str, Any], client: str, skey: str) -> dict[str, Any]:
    from agentwatch.collector.store import EventStore
    from agentwatch.core.session import load_session
    from agentwatch.detectors import run_detectors
    from agentwatch.reports.feedback import load_feedback
    from agentwatch.reports.json_report import build_report
    from agentwatch.reports.stats import compute_stats, coverage_matrix
    store = EventStore(home, cfg)
    if _auto_import_enabled(cfg):
        _import_transcripts(store, cfg, client, skey)   # * usage en tokens depuis le transcript, si demande dans config.json
    view = load_session(store, client, skey, cfg)
    findings = run_detectors(view, cfg)
    report = build_report(view, compute_stats(view), coverage_matrix(view), findings, cfg, load_feedback(home),
                          _read_install_meta(home, client))
    report["collection_health"] = _health(home, cfg, store)
    return report


def cmd_report(args: argparse.Namespace) -> int:
    from agentwatch.collector.store import EventStore
    from agentwatch.reports.markdown import render_markdown
    home = home_dir(args.home)
    cfg = load_config(home)
    store = EventStore(home, cfg)
    if args.client in (None, CLIENT_CODEX):
        _auto_import_rollouts(home, cfg, store)
    if args.latest:
        from agentwatch.core.session import list_sessions
        rows = list_sessions(store, cfg, client=args.client)
        if not rows:
            raise SystemExit("aucune session enregistree")
        client, skey = rows[0]["client"], rows[0]["session_key"]
    else:
        if not args.session:
            raise SystemExit("precisez --session <id> ou --latest")
        client, skey = _resolve_session(store, args.session, args.client)
    report = _analyse(home, cfg, client, skey)
    fmt = args.format
    if fmt == "auto":
        # * Rich (optionnel) dans un terminal interactif, Markdown partout ailleurs (tubes, fichiers).
        from agentwatch.reports.rich_view import rich_available
        fmt = "rich" if rich_available() and sys.stdout.isatty() and not args.out else "markdown"
    if fmt in ("rich", "html", "svg"):
        from agentwatch.reports import rich_view
        if not rich_view.rich_available():
            raise SystemExit(rich_view.missing_rich_message())
        if fmt == "rich" and not args.out:
            rich_view.render_to_terminal(report)
            return 0
        text = rich_view.export(report, "text" if fmt == "rich" else fmt, width=args.width)
    elif fmt == "json":
        text = json.dumps(report, indent=2, ensure_ascii=False)
    else:
        text = render_markdown(report)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        _out(f"rapport ecrit : {args.out}")
    else:
        _out(text)
    return 0


def cmd_trends(args: argparse.Namespace) -> int:
    """Vue multi-sessions : motifs recurrents par projet, client et session (rien de nouveau n'est detecte)."""
    from agentwatch.collector.store import EventStore
    from agentwatch.reports.feedback import load_feedback
    from agentwatch.reports.trends import build_trends, render_trends_markdown
    home = home_dir(args.home)
    cfg = load_config(home)
    tcfg = cfg.get("trends") if isinstance(cfg.get("trends"), dict) else {}
    days = args.days if args.days is not None else int(tcfg.get("days", 7))
    min_sessions = args.min_sessions if args.min_sessions is not None else int(tcfg.get("min_sessions", 2))
    store = EventStore(home, cfg)
    if args.client in (None, CLIENT_CODEX):
        _auto_import_rollouts(home, cfg, store)
    importer = (lambda c, k: _import_transcripts(store, cfg, c, k)) if (_auto_import_enabled(cfg) or args.import_transcripts) else None
    report = build_trends(store, cfg, load_feedback(home), days=days, client=args.client,
                          project=args.project, min_sessions=min_sessions, importer=importer)
    report["collection_health"] = _health(home, cfg, store)
    text = json.dumps(report, indent=2, ensure_ascii=False) if args.format == "json" else render_trends_markdown(report)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        _out(f"rapport ecrit : {args.out}")
    else:
        _out(text)
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    from agentwatch.reports.feedback import set_feedback
    home = home_dir(args.home)
    data = set_feedback(home, args.finding, args.mark, args.note)
    _out(f"retour enregistre pour {args.finding} : {args.mark} ({len(data)} retour(s) au total dans {home / 'feedback.json'})")
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    from agentwatch.collector.store import EventStore
    home = home_dir(args.home)
    store = EventStore(home, load_config(home))
    total = 0
    for client, skey, _ in list(store.iter_sessions()):
        if args.session and not (skey == args.session or skey.startswith(args.session)):
            continue
        n = store.compact_session(client, skey)
        if n:
            _out(f"{client}/{skey} : {n} fichier(s) fusionne(s) en segment")
        total += n
    _out(f"compaction terminee : {total} fichier(s)")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from agentwatch.collector.store import EventStore
    home = home_dir(args.home)
    cfg = load_config(home)
    days = args.days if args.days is not None else int(cfg.get("retention_days", 30))
    store = EventStore(home, cfg)
    if args.dry_run:
        _out(f"(dry-run) retention {days} jours : rien n'est supprime")
        return 0
    removed = store.prune(days)
    _out(f"retention {days} jours : {len(removed)} session(s) supprimee(s)" + (" : " + ", ".join(removed) if removed else ""))
    return 0


def cmd_import_usage(args: argparse.Namespace) -> int:
    """Interface d'import d'usage (tokens) au format JSONL documente dans docs/events.md.

    Chaque ligne : {"client", "session_id", "scope": "call|turn|session", "tool_use_id"?, "model"?,
    "input_tokens"?, "output_tokens"?, "cache_read_tokens"?, "cache_creation_tokens"?, "source", "timestamp"?}
    """
    from agentwatch.collector.store import EventStore
    from agentwatch.core import schema as S
    home = home_dir(args.home)
    cfg = load_config(home)
    store = EventStore(home, cfg)
    written = rejected = 0
    for line in Path(args.file).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            rejected += 1
            continue
        if not isinstance(row, dict) or row.get("client") not in SUPPORTED_CLIENTS or not row.get("session_id") or not row.get("source"):
            rejected += 1
            continue
        ev = S.empty_event()
        # * Sans tool_use_id, l'observation porte sur la session : un marqueur, jamais un appel fictif.
        ev.update({"source": S.SOURCE_IMPORT, "client": row["client"], "session_id": row["session_id"],
                   "call_id": row.get("tool_use_id"), "phase": S.PHASE_OBSERVATION if row.get("tool_use_id") else S.PHASE_USAGE,
                   "hook_event_name": "usage_import",
                   "model": row.get("model"), "event_time": row.get("timestamp"),
                   "usage": {k: row.get(k) for k in ("scope", "input_tokens", "output_tokens", "cache_read_tokens",
                                                     "cache_creation_tokens", "source")}})
        ev["evidence"]["import_file"] = Path(args.file).name
        store.write_event(ev)
        written += 1
    _out(f"usage importe : {written} observation(s), {rejected} ligne(s) rejetee(s)")
    return 0


def cmd_import_transcripts(args: argparse.Namespace) -> int:
    """Lire les transcripts de Claude Code (usage en tokens seulement) pour une, la derniere ou toutes les sessions."""
    from agentwatch.collector.store import EventStore
    from agentwatch.core.session import list_sessions
    home = home_dir(args.home)
    cfg = load_config(home)
    store = EventStore(home, cfg)
    targets: list[tuple[str, str]] = []
    if args.all:
        targets = [(c, k) for c, k, _ in store.iter_sessions() if c == CLIENT_CLAUDE_CODE]
    elif args.latest:
        rows = list_sessions(store, cfg, client=CLIENT_CLAUDE_CODE)
        if not rows:
            raise SystemExit("aucune session claude-code enregistree")
        targets = [(rows[0]["client"], rows[0]["session_key"])]
    elif args.session:
        targets = [_resolve_session(store, args.session, CLIENT_CLAUDE_CODE)]
    else:
        raise SystemExit("precisez --session <id>, --latest ou --all")
    failures = 0
    for client, skey in targets:
        s = _import_transcripts(store, cfg, client, skey)
        if s["transcript"]:
            _out(f"{client}/{skey} : {s['requests']} requetes API, {s['total_tokens']} tokens ; usage attribue a {s['calls_matched']} appel(s), "
                 f"{s['calls_without_hook_events']} appel(s) du transcript sans evenement de hook ; {s['subagent_transcripts']} transcript(s) "
                 f"de sous-agent ; {s['written']} observation(s) ecrite(s), {s['skipped']} deja presente(s)")
        else:
            failures += 1
        for w in s["warnings"]:
            _err(f"  ! {client}/{skey} : {w}")
    _out(f"import termine : {len(targets) - failures}/{len(targets)} session(s) avec transcript lu ; rien d'autre que des nombres et des "
         "identifiants n'a ete extrait")
    return 0 if failures == 0 else 1


def cmd_import_rollouts(args: argparse.Namespace) -> int:
    """Lire les rollouts Codex (lecture seule, incrementale, priorite d'arriere-plan) ; --follow pour suivre en direct."""
    from agentwatch.collector.store import EventStore
    home = home_dir(args.home)
    cfg = load_config(home)
    store = EventStore(home, cfg)
    days = None if args.all else (args.days if args.days is not None else float(_rollouts_cfg(cfg).get("days", 7) or 0) or None)

    from agentwatch.collector.rollouts import LiveDigest
    digest = LiveDigest(str(home))

    def once() -> dict[str, Any]:
        return _import_rollouts(home, cfg, store, days=days if days is not None else 0, thread=args.thread, sink=digest.feed)

    def show(out: dict[str, Any], stamp: bool) -> None:
        prefix = time.strftime("%H:%M:%S ") if stamp else ""
        sess = " ; ".join(f"{sid[:13]} : {s['threads']} fil(s), {s['events']} evenement(s)" for sid, s in out["sessions"].items())
        _out(f"{prefix}rollouts Codex : {out['files']} fichier(s) lu(s), {out['lines']} ligne(s), {out['events']} evenement(s), "
             f"{round(out['bytes'] / 1e6, 1)} Mo" + (f" ; {sess}" if sess else ""))
        for e in out["errors"]:
            _err(f"  ! {e}")

    out = once()
    show(out, args.follow)
    digest.reset()
    if not args.follow:
        _out("rien d'autre que des nombres, des identifiants et des empreintes n'a ete extrait ; les rollouts ne sont jamais modifies")
        return 0 if not out["errors"] else 1
    try:
        while True:
            time.sleep(max(2.0, float(args.interval)))
            out = once()
            if digest.active():
                _out(time.strftime("%H:%M:%S ") + digest.line())
            for e in out["errors"]:
                _err(f"  ! {e}")
            digest.reset()
    except KeyboardInterrupt:
        return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    from agentwatch.selftest import run_selftest
    ok, lines = run_selftest(runs=args.runs)
    for line in lines:
        _out(line)
    _out("self-test : " + ("OK" if ok else "ECHEC"))
    return 0 if ok else 1


def cmd_bench(args: argparse.Namespace) -> int:
    import tempfile
    from agentwatch.selftest import hook_subprocess_check
    with tempfile.TemporaryDirectory(prefix="agentwatch-bench-") as tmp:
        res = hook_subprocess_check(Path(tmp), runs=args.runs)
    _out(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from agentwatch.collector import privacy as P
    from agentwatch.collector.store import EventStore
    from agentwatch.installer import claude_code as IC
    from agentwatch.installer import codex as IX
    from agentwatch.installer import common as C
    home = home_dir(args.home)
    cfg = load_config(home)
    _out(f"AgentWatch {__version__} - diagnostic")
    _out(f"- Python : {sys.version.split()[0]} ({sys.executable}) ; plateforme {sys.platform}")
    _out(f"- Dossier de donnees : {home} ({'present' if home.is_dir() else 'absent, sera cree'})")
    if _under_appdata(home):
        _out("    ! ce dossier est sous AppData : une application de bureau empaquetee (MSIX, ex. Claude) redirige ses "
             "ecritures AppData vers un dossier prive, donc les hooks et votre terminal ne verraient pas les memes fichiers. "
             "Choisissez un dossier hors AppData (defaut : ~/.agentwatch).")
    _out(f"- Config : {cfg['_config_path']}" + (" ; avertissements : " + "; ".join(cfg["_config_warnings"]) if cfg["_config_warnings"] else ""))
    key_ok = (home / P.KEY_DIRNAME / P.KEY_FILENAME).is_file()
    _out(f"- Cle HMAC locale : {'presente' if key_ok else 'absente (creee au premier evenement)'}")
    entry = C.hook_entry_path()
    _out(f"- Point d'entree des hooks : {entry} ({'ok' if entry.is_file() else 'INTROUVABLE'})")
    from agentwatch.reports import rich_view
    if rich_view.rich_available():
        _out(f"- Affichage Rich (optionnel) : disponible pour cet interpreteur (rich {rich_view.rich_version() or 'version inconnue'})")
    else:
        _out(f"- Affichage Rich (optionnel) : absent pour cet interpreteur ; installer avec : {rich_view.install_command()}")
    # * doctor ne cree rien : le test d'ecriture ne se fait que si le dossier existe deja.
    if home.is_dir():
        try:
            probe = home / f".probe-{os.getpid()}"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            _out("- Ecriture dans le dossier de donnees : ok")
        except OSError as exc:
            _out(f"- Ecriture dans le dossier de donnees : ECHEC ({exc})")
    else:
        _out("- Ecriture dans le dossier de donnees : non testee (dossier cree par `configure --apply` ou au premier evenement)")
    store = EventStore(home, cfg)
    st = store.stats()
    _out(f"- Stockage : {st['sessions']} session(s), {st['spool_files']} fichier(s) spool, {st['segment_files']} segment(s), {st['bytes']} octets")
    diags = store.read_diagnostics()
    if diags:
        kinds: dict[str, int] = {}
        for d in diags:
            kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
        _out(f"- Incidents de collecte (pertes comptees, {len(diags)} plus recents) : {kinds}")
    else:
        _out("- Incidents de collecte : aucun")
    trust_alerts: list[dict[str, Any]] = []
    for client in SUPPORTED_CLIENTS:
        version, exe = _client_version(client)
        _out(f"- {client} : " + (f"version {version or 'inconnue'} ({exe})" if exe else "executable introuvable"))
        meta = _read_install_meta(home, client)
        if meta:
            _out(f"    configure le {meta.get('configured_at')} dans {meta.get('config_path')} (version alors : {meta.get('client_version')})")
        if client == CLIENT_CLAUDE_CODE:
            for scope in ("user", "project", "local"):
                p = IC.settings_path(scope)
                try:
                    obj, _ = C.read_json_file(p)
                except ValueError as exc:
                    _out(f"    {scope}: {p} : JSON invalide ({exc})")
                    continue
                s = IC.status(obj)
                if s["installed_events"] or p.is_file() and scope == "user":
                    _out(f"    {scope}: {p} : hooks AgentWatch {'complets' if s['complete'] else ('partiels ' + str(s['missing_events']) if s['installed_events'] else 'absents')}"
                         f" ; groupes etrangers preserves : {sum(s['foreign_groups'].values())}"
                         + (" ; disableAllHooks=true !" if s["disable_all_hooks"] else ""))
        else:
            toml = IX.config_toml_status()
            _out(f"    config.toml : {'present' if toml['exists'] else 'absent'} ; features.hooks={toml['features_hooks']} (None = defaut actif) ; hooks inline : {toml['inline_hooks_events'] or 'aucun'}")
            for scope in ("user", "project"):
                p = IX.hooks_path(scope)
                try:
                    obj, _ = C.read_json_file(p)
                except ValueError as exc:
                    _out(f"    {scope}: {p} : JSON invalide ({exc})")
                    continue
                s = IX.status(obj)
                if s["installed_events"] or p.is_file():
                    _out(f"    {scope}: {p} : hooks AgentWatch {'complets dans le fichier' if s['complete'] else ('partiels ' + str(s['missing_events']) if s['installed_events'] else 'absents')}"
                         f" ; groupes etrangers preserves : {sum(s['foreign_groups'].values())}")
            # * Un fichier complet ne prouve rien : seul Codex sait si l'utilisateur a approuve chaque hook.
            probe_off = isinstance(cfg.get("health"), dict) and cfg["health"].get("codex_trust_probe") is False
            if exe and meta and not args.no_codex_trust and not probe_off:
                from agentwatch.installer import codex_trust as XT
                cwds = [os.getcwd()]
                if meta.get("scope") == "project" and isinstance(meta.get("config_path"), str):
                    proj = str(Path(meta["config_path"]).parent.parent)
                    if proj not in cwds:
                        cwds.append(proj)
                result = XT.probe([exe], cwds, cwd=os.getcwd())
                for line in XT.format_lines(result):
                    _out(f"    {line}")
                    if line.startswith("! Codex n'executera"):
                        msg = line[2:]
                        if _rollouts_cfg(cfg).get("auto_import", True):
                            msg += " ; sans hooks, la lecture des rollouts couvre deja Codex (voir la ligne rollouts)"
                        trust_alerts.append({"client": CLIENT_CODEX, "code": "hooks_untrusted", "level": "warn", "message": msg})
                if not result.get("ok"):
                    _out("    rappel : Codex n'execute un hook qu'apres que vous l'avez approuve via la commande /hooks (confiance par empreinte).")
            else:
                _out("    rappel : Codex n'execute un hook qu'apres que vous l'avez approuve via la commande /hooks (confiance par empreinte) ; "
                     "etat de confiance non verifie" + (" (--no-codex-trust)" if args.no_codex_trust else ""))
            # * Collecte sans hooks : lecture passive des rollouts (collector/rollouts.py).
            from agentwatch.collector import rollouts as R
            tracked = R.load_state(str(home)).get("files") or {}
            roots = {((st or {}).get("meta") or {}).get("root_id") for st in tracked.values()} - {None}
            _out(f"    rollouts : lecture passive {'automatique avant sessions, report et trends' if _rollouts_cfg(cfg).get('auto_import', True) else 'sur demande (import-rollouts)'}"
                 f" (lecture seule, sans hooks, sans effet sur Codex) ; {len(tracked)} fichier(s) suivi(s), {len(roots)} session(s) Codex importee(s)")
    health = trust_alerts + _health(home, cfg, store)
    if health:
        _out("- Sante de la collecte (panne silencieuse) :")
        for h in health:
            _out(f"    ! {h['client']} : {h['message']}")
    else:
        _out("- Sante de la collecte : rien a signaler (interpreteur et point d'entree en place, hooks presents, "
             "aucun silence apres une activite du client)")
    tcfg = cfg.get("transcripts") if isinstance(cfg.get("transcripts"), dict) else {}
    _out(f"- Transcripts Claude Code (usage en tokens) : {'import automatique a chaque rapport' if tcfg.get('auto_import') else 'sur demande (agentwatch import-transcripts ; transcripts.auto_import=true pour automatiser)'}")
    _out("Rappel : AgentWatch n'observe que les evenements de hooks ; voir docs/compatibility.md pour les limites par client.")
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    from agentwatch.installer import claude_code as IC
    from agentwatch.installer import codex as IX
    from agentwatch.installer import common as C
    home = home_dir(args.home).resolve()
    if args.apply and not args.remove:
        write_default_config(home)   # * jamais en dry-run : aucune ecriture avant --apply
    python = args.python or C.hook_python_executable()
    entry = C.hook_entry_path()
    if not entry.is_file():
        raise SystemExit(f"point d'entree introuvable : {entry}")
    project_dir = Path(args.project_dir).resolve() if args.project_dir else None
    if args.client == CLIENT_CLAUDE_CODE:
        path = IC.settings_path(args.scope, project_dir)
        current, raw = C.read_json_file(path)
        proposed, summary = (IC.plan_uninstall(current) if args.remove else IC.plan_install(current, python, entry, home))
    else:
        if args.scope == "local":
            raise SystemExit("Codex : scopes disponibles user|project")
        path = IX.hooks_path(args.scope, project_dir)
        current, raw = C.read_json_file(path)
        proposed, summary = (IX.plan_uninstall(current) if args.remove else IX.plan_install(current, python, entry, home, args.posix_python))
    new_text = C.dump_json(proposed)
    diff = C.unified_diff(raw, new_text, path)
    action = "retrait" if args.remove else "installation"
    _out(f"{action} des hooks AgentWatch pour {args.client} ({args.scope}) : {path}")
    _out(f"resume : {summary}")
    if not diff:
        _out("aucun changement necessaire (deja a jour).")
    else:
        _out(diff)
    if not args.apply:
        _out("mode dry-run : rien n'a ete ecrit. Ajoutez --apply pour appliquer (une sauvegarde sera creee).")
        return 0
    if diff:
        bak = C.backup(path)
        C.write_json_atomic(path, proposed)
        _out(f"ecrit : {path}" + (f" (sauvegarde : {bak})" if bak else ""))
    meta_path = _install_meta_path(home, args.client)
    if args.remove:
        if meta_path.is_file():
            meta_path.unlink()
    else:
        version, exe = _client_version(args.client)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({"client": args.client, "client_version": version, "client_executable": exe,
                                         "configured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                         "config_path": str(path), "scope": args.scope, "python": python,
                                         "hook_entry": str(entry), "home": str(home)}, indent=2), encoding="utf-8")
        if args.client == CLIENT_CODEX:
            _out("Codex : ouvrez Codex et approuvez le hook via /hooks (confiance par empreinte) ; sinon il est ignore.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    args.remove = True
    args.python = None
    args.posix_python = "python3"
    return cmd_configure(args)


# ---------------------------------------------------------------------------- parseur
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentwatch", description="Observateur local passif pour Claude Code et Codex.")
    p.add_argument("--home", help="dossier de donnees (defaut : $AGENTWATCH_HOME ou ~/.agentwatch)")
    p.add_argument("--version", action="version", version=f"agentwatch {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("doctor", help="diagnostic de l'installation et des clients")
    s.add_argument("--no-codex-trust", action="store_true",
                   help="ne pas interroger codex app-server (hooks/list) sur l'approbation des hooks")
    s.set_defaults(func=cmd_doctor)

    for name, func, help_ in (("configure", cmd_configure, "installer les hooks (dry-run par defaut)"),
                              ("uninstall", cmd_uninstall, "retirer uniquement les hooks AgentWatch (dry-run par defaut)")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--client", required=True, choices=SUPPORTED_CLIENTS)
        s.add_argument("--scope", default="user", choices=("user", "project", "local"))
        s.add_argument("--project-dir", help="racine du projet pour les scopes project/local")
        s.add_argument("--apply", action="store_true", help="ecrire reellement (sauvegarde automatique)")
        s.add_argument("--dry-run", action="store_true", help="(defaut) afficher le diff sans ecrire")
        if name == "configure":
            s.add_argument("--python", help="interpreteur du hook (defaut : pythonw.exe a cote de celui-ci sous Windows, sinon celui-ci)")
            s.add_argument("--posix-python", default="python3", help="interpreteur pour le champ POSIX de Codex")
            s.add_argument("--remove", action="store_true", help=argparse.SUPPRESS)
        s.set_defaults(func=func)

    s = sub.add_parser("sessions", help="lister les sessions enregistrees")
    s.add_argument("--client", choices=SUPPORTED_CLIENTS)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("report", help="analyser une session et produire un rapport")
    s.add_argument("--session", help="identifiant (complet ou prefixe) ou cle de dossier")
    s.add_argument("--latest", action="store_true", help="derniere session enregistree (filtre --client possible)")
    s.add_argument("--client", choices=SUPPORTED_CLIENTS)
    s.add_argument("--format", default="auto", choices=("auto", "markdown", "json", "rich", "html", "svg"),
                   help="auto = rich dans un terminal si Rich est installe, sinon markdown ; html/svg = rendu Rich exporte")
    s.add_argument("--width", type=int, default=120, help="largeur du rendu Rich exporte (html/svg/rich vers fichier)")
    s.add_argument("--out", help="fichier de sortie (sinon stdout)")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("trends", help="gaspillages recurrents sur plusieurs sessions : par motif, projet, client et session")
    s.add_argument("--days", type=int, help="fenetre en jours sur le dernier evenement de chaque session (defaut : trends.days = 7 ; 0 = toutes)")
    s.add_argument("--client", choices=SUPPORTED_CLIENTS)
    s.add_argument("--project", help="ne garder que les sessions dont le chemin de projet contient ce texte")
    s.add_argument("--min-sessions", type=int, help="sessions distinctes a partir desquelles un motif est recurrent (defaut : trends.min_sessions = 2)")
    s.add_argument("--import-transcripts", action="store_true", help="importer d'abord l'usage en tokens des transcripts Claude Code des sessions retenues")
    s.add_argument("--format", default="markdown", choices=("markdown", "json"))
    s.add_argument("--out", help="fichier de sortie (sinon stdout)")
    s.set_defaults(func=cmd_trends)

    s = sub.add_parser("import-transcripts", help="lire l'usage en tokens des transcripts Claude Code (nombres et identifiants seulement)")
    s.add_argument("--session", help="identifiant (complet ou prefixe) ou cle de dossier")
    s.add_argument("--latest", action="store_true", help="derniere session claude-code enregistree")
    s.add_argument("--all", action="store_true", help="toutes les sessions claude-code enregistrees")
    s.set_defaults(func=cmd_import_transcripts)

    s = sub.add_parser("import-rollouts", help="lire les rollouts Codex : appels, statuts, durees, tokens, sous-agents (lecture seule, sans effet sur Codex)")
    s.add_argument("--days", type=float, help="rollouts modifies dans les N derniers jours (defaut : rollouts.days = 7)")
    s.add_argument("--all", action="store_true", help="tous les rollouts, quelle que soit leur date")
    s.add_argument("--thread", help="un fil (identifiant ou prefixe) et ses sous-agents")
    s.add_argument("--follow", action="store_true", help="suivre en direct : relire les lignes nouvelles toutes les --interval secondes")
    s.add_argument("--interval", type=float, default=30.0, help="periode du suivi en secondes (defaut 30, minimum 2)")
    s.set_defaults(func=cmd_import_rollouts)

    s = sub.add_parser("self-test", help="scenarios synthetiques + hook reel en sous-processus")
    s.add_argument("--runs", type=int, default=5)
    s.set_defaults(func=cmd_self_test)

    s = sub.add_parser("ingest", help="(commande des hooks) lire un evenement sur stdin")
    s.add_argument("--client", required=True, choices=SUPPORTED_CLIENTS)
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("replay", help="reanalyser des payloads enregistres (aucune commande n'est executee)")
    s.add_argument("--client", required=True, choices=SUPPORTED_CLIENTS)
    s.add_argument("--session-id", help="forcer l'identifiant de session")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("compact", help="fusionner le spool d'une session en segment JSONL")
    s.add_argument("--session")
    s.set_defaults(func=cmd_compact)

    s = sub.add_parser("prune", help="appliquer la retention")
    s.add_argument("--days", type=int)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("feedback", help="marquer un signalement (pertinent / faux positif)")
    s.add_argument("--finding", required=True)
    s.add_argument("--mark", required=True, choices=("relevant", "false-positive", "clear"))
    s.add_argument("--note")
    s.set_defaults(func=cmd_feedback)

    s = sub.add_parser("bench", help="mesurer la surcharge du hook (sous-processus complet)")
    s.add_argument("--runs", type=int, default=20)
    s.set_defaults(func=cmd_bench)

    s = sub.add_parser("import-usage", help="importer des observations d'usage (JSONL documente)")
    s.add_argument("file")
    s.set_defaults(func=cmd_import_usage)
    return p


def _configure_stdout() -> None:
    """Consoles Windows en cp1252 : ne jamais planter sur un chemin accentue ou un symbole.

    # * Vers un tube ou un fichier (`> rapport.json`), la sortie est toujours en UTF-8 : le JSON
    #   et le Markdown doivent etre relisibles partout, pas seulement avec la page de code locale.
    #   Dans une vraie console, l'encodage detecte par Python est conserve.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _stdout_pipe_closed(exc: BaseException) -> bool:
    """Vrai si l'erreur vient d'un tube de sortie ferme par son lecteur (EPIPE, ou EINVAL sous Windows).

    # ? EINVAL est ambigu : on ne conclut a un tube ferme que si stdout n'est pas une console ET
    #   qu'un nouveau flush echoue aussi. Stdout est alors redirige vers le neant pour eviter le
    #   message "Exception ignored" a l'arret de l'interpreteur.
    """
    import errno
    if not isinstance(exc, OSError) or exc.errno not in (errno.EPIPE, errno.EINVAL):
        return False
    if not isinstance(exc, BrokenPipeError):  # EPIPE est sans ambiguite ; EINVAL demande une preuve
        try:
            if sys.stdout.isatty():
                return False
            sys.stdout.flush()
            return False
        except (OSError, ValueError):
            pass
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass
    return True


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - message explicite, pas de trace brute pour l'utilisateur
        if _stdout_pipe_closed(exc):
            return 0  # * `... | head`, `... | Select-Object -First 5` : le lecteur est parti, ce n'est pas une erreur
        _err(f"erreur : {type(exc).__name__}: {exc}")
        if os.environ.get("AGENTWATCH_DEBUG"):
            raise
        return 2
