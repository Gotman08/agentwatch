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
    if sys.stdout is not None:        # * `pythonw` (suivi lance a l'ouverture de session) : pas de sortie standard
        print(msg)


def _err(msg: str) -> None:
    if sys.stderr is not None:
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


def _rollout_paths(cfg: dict[str, Any], days: float | None, thread: str | None,
                   state: dict[str, Any] | None = None) -> list[str]:
    """Rollouts de la fenetre ; avec `thread`, ce fil et ses sous-agents (lus dans leur session_meta).
    `state` (etat de l'import) : un fichier deja lu qui a grandi reste dans la fenetre malgre une date figee."""
    from agentwatch.collector import rollouts as R
    paths = R.list_rollouts(cfg, days, state=state)
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
        todo = R.pending_rollouts(str(home), _rollout_paths(cfg, days, thread, R.load_state(str(home))))
        if not todo:
            return {"files": 0, "lines": 0, "events": 0, "bytes": 0, "sessions": {}, "errors": []}
        with R.background_priority(bool(rcfg.get("background_priority", True))):
            return R.import_rollouts(store, cfg, todo, sink=sink)
    except Exception as exc:  # noqa: BLE001 - un import rate ne doit pas empecher un rapport
        return {"files": 0, "lines": 0, "events": 0, "bytes": 0, "sessions": {}, "errors": [f"{type(exc).__name__}: {exc}"]}


def _rollout_state_lines(cfg: dict[str, Any], rstate: dict[str, Any]) -> list[str]:
    """Trois etats distincts des rollouts : importe (lu et traduit), en attente (pas encore lu, ou element a rapprocher),
    lu mais non interprete (ligne comptee, aucun evenement). Plus les doublons reconnus, qui ne sont pas des pertes."""
    from agentwatch.collector import rollouts as R
    tracked = rstate.get("files") or {}
    lines: list[str] = []
    read_lines = sum(int((st or {}).get("line_no") or 0) for st in tracked.values())
    counted = [st for st in tracked.values() if isinstance((st or {}).get("uninterpreted"), dict)]
    lines.append(f"importees : {len(tracked)} rollout(s) suivi(s), lus jusqu'a leur derniere ligne complete"
                 + (f" ({read_lines} ligne(s) numerotee(s) pour la source des evenements)" if read_lines else ""))
    try:
        pending = R.pending_with_sizes(rstate, R.list_rollouts(cfg, float(_rollouts_cfg(cfg).get("days", 7) or 7), state=rstate))
    except OSError:
        pending = []
    lag_s = 0.0
    waiting_bytes = 0
    for p, size in pending:
        st = tracked.get(os.path.normcase(os.path.abspath(p))) or {}
        waiting_bytes += max(0, size - int(st.get("offset", 0) or 0))
        last_written = R.last_line_ns(p)
        if last_written and st.get("last_ns"):
            lag_s = max(lag_s, (last_written - int(st["last_ns"])) / 1e9)
        elif last_written:
            lag_s = max(lag_s, time.time() - last_written / 1e9)
    lines.append(f"en attente de lecture : {len(pending)} rollout(s), {waiting_bytes} octet(s) pas encore lus"
                 + (f", retard {lag_s:.0f} s (heure de la derniere ligne ecrite par Codex moins celle de la derniere ligne lue)"
                    if pending else "") + " ; reprise a l'octet pres apres une interruption")
    items = sum(len((st or {}).get("pending_items") or []) for st in tracked.values())
    if items:
        lines.append(f"en attente de rapprochement : {items} element(s) de message (importes s'ils n'ont pas de ligne "
                     "de meme texte dans les 10 lignes suivantes, ou apres 10 min sans ecriture)")

    def total(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for st in tracked.values():
            for k, n in ((st or {}).get(key) or {}).items():
                out[k] = out.get(k, 0) + int(n or 0)
        return out

    unint, dups = total("uninterpreted"), total("duplicates")
    scope = (f"sur {len(counted)} rollout(s) lus par cette version"
             + (f" ; {len(tracked) - len(counted)} lus avant, non comptes (import-rollouts --all pour les recompter)"
                if len(counted) < len(tracked) else ""))
    lines.append("lues mais non interpretees (" + scope + ") : "
                 + (", ".join(f"{k} x{n}" for k, n in sorted(unint.items(), key=lambda kv: -kv[1])) if unint else "aucune"))
    if dups:
        lines.append("reconnues sans nouvel evenement (meme fait deja importe, ou vide) : "
                     + ", ".join(f"{k} x{n}" for k, n in sorted(dups.items(), key=lambda kv: -kv[1])[:8]))
    mixed = sum(1 for st in tracked.values() if (st or {}).get("usage_format") == "mixte")
    if mixed:
        lines.append(f"! {mixed} fil(s) aux releves de tokens de format mixte (token_count avant token_usage_record) : totaux a verifier")
    return lines


def _follow_state_lines(home: Path) -> list[str]:
    """Suivi continu : en marche (verrou tenu) ou arrete, dernier passage, journal et etat a verifier."""
    from agentwatch.collector import follow as F
    st = F.describe(str(home))
    if st["running"]:
        age = st.get("last_tick_age_s")
        late = isinstance(age, (int, float)) and isinstance(st.get("interval_s"), (int, float)) and age > 4 * st["interval_s"] + 60
        return [f"suivi continu : EN MARCHE, un seul collecteur (pid {st.get('pid')}, demarre {st.get('started')}, "
                f"{st.get('cycles')} cycle(s), dernier passage {st.get('last_tick')}"
                + (f", il y a {age:.0f} s" if isinstance(age, (int, float)) else "") + ")"
                + (" ; ! dernier passage ancien : collecteur bloque ?" if late else ""),
                f"suivi continu : etat {st['status_file']} ; journal {st['log']}"]
    stopped = st.get("stopped") or {}
    if st.get("started"):
        return [f"suivi continu : ARRETE (dernier demarrage {st.get('started')}, dernier passage {st.get('last_tick')}"
                + (f", arret consigne {stopped.get('time')} : {stopped.get('reason')}" if stopped else
                   ", aucun arret consigne : processus tue ou machine redemarree") + ") ; la lecture automatique avant "
                "sessions, report et trends rattrape ; relance : python -m agentwatch import-rollouts --follow"]
    return ["suivi continu : jamais lance avec cet etat (python -m agentwatch import-rollouts --follow)"]


def _auto_import_rollouts(home: Path, cfg: dict[str, Any], store: Any) -> None:
    """Avant `sessions`, `report`, `trends` : rollouts Codex modifies depuis le dernier import (defaut actif)."""
    if not _rollouts_cfg(cfg).get("auto_import", True):
        return
    from agentwatch.collector import follow as F
    lock = F.FollowLock(str(home))
    if not lock.acquire():
        # * Un seul lecteur a la fois : le suivi continu (ou un autre import) tient le verrou et lit deja ; un import
        #   concurrent reecrirait le meme etat. Le retard eventuel est celui d'un cycle du suivi (30 s par defaut).
        return
    try:
        out = _import_rollouts(home, cfg, store)
    finally:
        lock.release()
    if out["files"]:
        _err(f"(rollouts Codex : {out['files']} fichier(s) mis a jour, {out['events']} evenement(s) importe(s))")
    for e in out["errors"][:3]:
        _err(f"! import des rollouts Codex : {e}")


def _codex_version_from_rollouts(cfg: dict[str, Any] | None = None) -> str | None:
    """Version de Codex ecrite par Codex lui-meme dans son dernier rollout (`cli_version`) : lecture d'un fichier,
    aucun processus lance."""
    from agentwatch.collector import rollouts as R
    try:
        paths = R.list_rollouts(cfg or {}, 30, whole_sessions=False)
    except OSError:
        return None
    # * La version est celle du client au moment ou le fil a ete CREE (premiere ligne) : un fil repris plus tard garde
    #   la sienne. On prend donc le fil cree le plus recemment (le nom du rollout commence par sa date de creation).
    paths.sort(key=lambda p: os.path.basename(p))
    for path in reversed(paths[-5:]):
        try:
            with open(path, "rb") as fh:
                for i, line in enumerate(fh):
                    if i >= 20:
                        break
                    if b'"session_meta"' in line and b'"cli_version"' in line:
                        v = json.loads(line).get("payload", {}).get("cli_version")
                        if isinstance(v, str) and v:
                            return f"codex-cli {v} (version du fil Codex le plus recemment cree, lue dans son rollout, sans lancer Codex)"
        except (OSError, ValueError, AttributeError):
            continue
    return None


def _client_version(client: str, cfg: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    """(version, chemin executable) ; (None, None) si indisponible. Jamais d'exception.

    # ! Codex : jamais de `codex --version` (consigne du 2026-09-19 : AgentWatch ne lance pas Codex). La version vient
    #   du dernier rollout ; l'executable n'est que localise.
    """
    exe = shutil.which("claude" if client == CLIENT_CLAUDE_CODE else "codex")
    if exe is None and client == CLIENT_CODEX and os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        if base.is_dir():
            cands = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            if cands:
                exe = str(cands[0])
    if client == CLIENT_CODEX:
        return _codex_version_from_rollouts(cfg), exe
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


def _slice_bounds(args: argparse.Namespace) -> tuple[int | None, int | None]:
    """Tranche demandee (--day, --since, --until), en nanosecondes UTC ; (None, None) sinon."""
    from agentwatch.core.timeslice import day_bounds, parse_when
    since = until = None
    try:
        if getattr(args, "day", None):
            since, until = day_bounds(args.day)
        if getattr(args, "since", None):
            since = parse_when(args.since)
        if getattr(args, "until", None):
            until = parse_when(args.until)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if since is not None and until is not None and until <= since:
        raise SystemExit("--until doit suivre --since")
    return since, until


def _analyse(home: Path, cfg: dict[str, Any], client: str, skey: str,
             since: int | None = None, until: int | None = None) -> dict[str, Any]:
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
    if since is not None or until is not None:
        from agentwatch.core.timeslice import slice_view
        view = slice_view(view, since, until)
    findings = run_detectors(view, cfg)
    report = build_report(view, compute_stats(view, cfg), coverage_matrix(view), findings, cfg, load_feedback(home),
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
    report = _analyse(home, cfg, client, skey, *_slice_bounds(args))
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


def cmd_compare(args: argparse.Namespace) -> int:
    """Avant / apres une correction : memes mesures sur deux periodes, rapportees a l'activite, avec IC 95 %."""
    import json as _json
    from agentwatch.collector.store import EventStore
    from agentwatch.core import schema as S
    from agentwatch.core.session import list_sessions, load_session
    from agentwatch.core.timeslice import parse_when, slice_view
    from agentwatch.reports import compare as CMP
    home = home_dir(args.home)
    cfg = load_config(home)
    store = EventStore(home, cfg)
    if args.client in (None, CLIENT_CODEX):
        _auto_import_rollouts(home, cfg, store)
    views = [load_session(store, r["client"], r["session_key"], cfg) for r in list_sessions(store, cfg, client=args.client)]
    if args.project:
        views = [v for v in views if args.project.lower() in str(v.project_dir or "").lower()]
    unreliable = [v for v in views if v.timing_unreliable_agents]
    views = [v for v in views if not v.timing_unreliable_agents]     # * leur heure reelle est inconnue : aucune periode sure

    def period_measure(since_ns: int | None, until_ns: int | None) -> dict[str, Any]:
        parts = [CMP.measure(slice_view(v, since_ns, until_ns), cfg) for v in views if v.calls
                 and not (since_ns and (v.last_ns or 0) < since_ns) and not (until_ns and (v.first_ns or 0) >= until_ns)]
        return CMP.merge(parts)

    def bounds() -> tuple[int | None, int | None]:
        try:
            return (parse_when(args.since) if args.since else None), (parse_when(args.until) if args.until else None)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None

    if args.save_reference:
        since, until = bounds()
        if since is None or until is None:
            raise SystemExit("une reference exige une periode explicite : --since et --until")
        m = period_measure(since, until)
        in_period = [r for r in CMP.agents_md_versions(views)]
        ref = {"kind": "agentwatch-reference", "compare_version": CMP.COMPARE_VERSION, "agentwatch_version": __version__,
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "period": {"since": S.now_iso(since / 1e9), "until": S.now_iso(until / 1e9), "since_ns": since, "until_ns": until},
               "filters": {"client": args.client or "tous", "project": args.project},
               "excluded_unreliable_sessions": len(unreliable), "measure": m,
               "agents_md_versions": [{k: r[k] for k in ("fingerprint", "chars", "first_time", "last_time")} for r in in_period
                                      if r["first_time"] and r["first_time"] < S.now_iso(until / 1e9)
                                      and r["last_time"] and r["last_time"] >= S.now_iso(since / 1e9)]}
        Path(args.save_reference).write_text(_json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
        text = CMP.render_reference(ref)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            _out(f"reference ecrite : {args.save_reference} ; rapport : {args.out}")
        else:
            _out(text)
            _out(f"reference ecrite : {args.save_reference}")
        return 0
    if args.reference:
        try:
            ref = _json.loads(Path(args.reference).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"reference illisible : {exc}") from None
        since, until = bounds()
        if since is None:
            raise SystemExit("--since est requis avec --reference : debut de la periode a comparer (ex. date de l'ajout a AGENTS.md)")
        if since < int(ref["period"]["until_ns"]):
            raise SystemExit("la periode comparee doit commencer apres la fin de la reference")
        b, a = ref["measure"], period_measure(since, until)
        result = {"client": args.client or "tous", "at_ns": since,
                  "at_label": f"reference du {ref['period']['since']} au {ref['period']['until']} ; periode comparee a partir du "
                              f"{S.now_iso(since / 1e9)}" + (f" jusqu'au {S.now_iso(until / 1e9)}" if until else ""),
                  "before": b, "after": a, "excluded_unreliable_sessions": len(unreliable), "comparison": CMP.compare(b, a)}
        text = _json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else CMP.render_markdown(result)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            _out(f"comparaison ecrite : {args.out}")
        else:
            _out(text)
        return 0
    if args.list_agents_md:
        versions = CMP.agents_md_versions(views)
        if not versions:
            _out("aucune version d'AGENTS.md observee (marqueurs world_state des rollouts Codex)")
            return 0
        _out("Versions d'AGENTS.md vues par Codex (empreinte du texte injecte, jamais le texte) :")
        for r in versions:
            _out(f"- agents-md:{r['fingerprint'][:8]} : {r['chars']} caracteres ; vue du {r['first_time']} au {r['last_time']} ; "
                 f"{r['sessions']} session(s)")
        _out("Comparer avant / apres une version : agentwatch compare --at agents-md:<empreinte>")
        return 0
    if not args.at:
        raise SystemExit("precisez --at <instant> ou --at agents-md:<empreinte> (voir --list-agents-md)")
    if args.at.startswith("agents-md:"):
        pref = args.at.split(":", 1)[1]
        match = [r for r in CMP.agents_md_versions(views) if r["fingerprint"].startswith(pref)]
        if len(match) != 1:
            raise SystemExit(f"empreinte AGENTS.md {pref!r} : {len(match)} version(s) correspondante(s) (voir --list-agents-md)")
        at, at_label = int(match[0]["first_ns"]), f"premiere apparition d'AGENTS.md {pref} ({match[0]['first_time']})"
    else:
        try:
            at = parse_when(args.at)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        at_label = S.now_iso(at / 1e9)
    try:
        since = parse_when(args.since) if args.since else None
        until = parse_when(args.until) if args.until else None
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    before, after = [], []
    for v in views:
        if not v.calls or (since and (v.last_ns or 0) < since) or (until and (v.first_ns or 0) >= until):
            continue
        if (v.first_ns or 0) < at:
            before.append(CMP.measure(slice_view(v, since, at), cfg))
        if (v.last_ns or 0) >= at:
            after.append(CMP.measure(slice_view(v, at, until), cfg))
    b, a = CMP.merge(before), CMP.merge(after)
    result = {"client": args.client or "tous", "at_ns": at, "at_label": at_label, "before": b, "after": a,
              "excluded_unreliable_sessions": len(unreliable), "comparison": CMP.compare(b, a)}
    text = _json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else CMP.render_markdown(result)
    if unreliable and args.format != "json":
        text += f"\n{len(unreliable)} session(s) ecartee(s) : horodatages reecrits d'un bloc, periode reelle inconnue.\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        _out(f"comparaison ecrite : {args.out}")
    else:
        _out(text)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Export detaille relu dans les journaux du client, secrets masques, fichier local."""
    from agentwatch.collector.store import EventStore
    from agentwatch.reports import inspect as INS
    home = home_dir(args.home)
    cfg = load_config(home)
    filters = {k: getattr(args, k, None) for k in ("kinds", "role", "contains", "source_line")}
    if args.client == "claude-code":
        from agentwatch.reports import inspect_claude as INS
    else:
        if any(v is not None for v in filters.values()):
            raise SystemExit("les filtres types de cette version concernent --client claude-code")
        _auto_import_rollouts(home, cfg, EventStore(home, cfg))    # * parts calculees par appel a jour
    since, until = _slice_bounds(args)
    ext = "md" if args.format == "markdown" else "jsonl"
    out_path = Path(args.out) if args.out else home / "exports" / f"{args.session[:13]}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"
    # * L'export ne doit jamais tronquer son propre journal source (ni un autre
    #   fil de la session), meme via un lien ou un chemin relatif equivalent.
    try:
        for item in INS.session_files(cfg, args.session):
            source_path = Path(item[0] if isinstance(item, tuple) else item)
            same = out_path.resolve() == source_path.resolve()
            if not same and out_path.exists():
                same = out_path.samefile(source_path)
            if same:
                raise ValueError("le fichier de sortie est un journal source : export refuse pour le preserver")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            summary = INS.export_session(cfg, str(home), args.session, fh, thread=args.thread, since=since, until=until,
                                         max_chars=args.max_chars, reasoning=args.reasoning, fmt=args.format,
                                         conclusions=not args.no_conclusions, **(filters if args.client == "claude-code" else {}))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    flags = ", ".join(f"{k.strip('[]')} {v}" for k, v in summary["flags"].items() if v) or "aucun"
    _out(f"export ecrit : {out_path} ({out_path.stat().st_size} octets) ; {len(summary['threads'])} fil(s) ; "
         f"{sum(summary['kinds'].values())} evenement(s) ; signalements : {flags}")
    _out("contenu en clair (secrets masques) : fichier local, a ne pas partager sans relecture")
    if summary.get("selection", {}).get("active"):
        selection = summary["selection"]
        _out(f"selection : {selection['selected']}/{selection['examined']} evenement(s) ; releves de tokens non attribues a la selection")
    return 0


def cmd_claude_context(args: argparse.Namespace) -> int:
    """Analyse passive d'un ensemble explicite de transcripts, sans import ni collecte."""
    from agentwatch.reports import claude_context as CC
    home = home_dir(args.home)
    cfg = load_config(home)
    out_path = Path(args.out)
    try:
        for source in CC.source_files(cfg, args.session):
            if out_path.resolve() == source.resolve() or (out_path.exists() and out_path.samefile(source)):
                raise ValueError("le fichier de sortie est un journal source : export refuse pour le preserver")
        result = CC.analyse(cfg, str(home), args.session)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _out(f"contexte Claude : {result['usage']['requests']} requetes uniques, {len(result['windows'])} fenetres/segments, "
         f"{len(result['compactions'])} compactions distinctes ; {out_path}")
    _out("deltas calcules ; retention par requete indeterminee hors preuves explicites, aucune economie de session deduite")
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
            token_text = f"{s['total_tokens']} tokens" if s['total_tokens'] is not None else "total de tokens non releve"
            if s['total_tokens'] is None and s.get('observed_tokens') is not None:
                token_text += f" (somme partielle : {s['observed_tokens']})"
            _out(f"{client}/{skey} : {s['requests']} requetes API, {token_text} ; usage attribue a {s['calls_matched']} appel(s), "
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

    from agentwatch.collector import follow as F
    if getattr(args, "stop_follow", False):
        return _stop_follow(home, args.reason or "demande de l'operateur")
    lock = F.FollowLock(str(home))
    if not lock.acquire():
        # * Un collecteur tient deja le verrou (suivi continu) : ni second suivi, ni import concurrent du meme etat.
        st = F.describe(str(home))
        who = (f"le suivi continu lit deja les rollouts (pid {st.get('pid')}, demarre {st.get('started')}, dernier passage "
               f"{st.get('last_tick')})" if st.get("pid") else "un autre import AgentWatch lit deja les rollouts")
        _err(f"{who} : rien n'est lance, un seul collecteur a la fois. Etat : {st['status_file']} ; journal : {st['log']}")
        return 3
    try:
        if not args.follow:
            out = once()
            show(out, False)
            _out("rien d'autre que des nombres, des identifiants et des empreintes n'a ete extrait ; les rollouts ne sont jamais modifies")
            return 0 if not out["errors"] else 1
        return _follow(home, args, once, show, digest)
    finally:
        lock.release()


def _stop_follow(home: Path, reason: str, timeout: float = 90.0) -> int:
    """Demande au suivi continu de s'arreter apres son cycle, et attend qu'il ait rendu le verrou. 0 : arrete (ou deja
    arrete) ; 4 : toujours en marche apres le delai (suivi anterieur a cette demande : il ne la lit pas)."""
    from agentwatch.collector import follow as F
    if not F.is_running(str(home)):
        _out("suivi continu : deja arrete, rien a faire")
        return 0
    F.request_stop(str(home), reason)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not F.is_running(str(home)):
            st = F.read_status(str(home)).get("stopped") or {}
            _out(f"suivi continu : arrete ({st.get('reason') or 'arret non consigne'})")
            return 0
        time.sleep(0.5)
    F.clear_stop(str(home))
    _err(f"suivi continu : toujours en marche apres {timeout:g} s (un suivi anterieur a cette commande ne lit pas la demande)")
    return 4


def _follow(home: Path, args: argparse.Namespace, once: Any, show: Any, digest: Any) -> int:
    """Boucle du suivi continu : etat reecrit a chaque cycle, journal date, arret propre consigne."""
    from agentwatch import __version__
    from agentwatch.collector import follow as F
    interval = max(2.0, float(args.interval))
    status: dict[str, Any] = {"pid": os.getpid(), "started": F.now_iso(), "interval_s": interval, "cycles": 0,
                              "repo": str(Path(__file__).resolve().parent.parent), "version": __version__,
                              "thread": args.thread, "last_import": None, "errors": [], "stopped": None}

    def cycle() -> None:
        out = once()
        status["cycles"] += 1
        status["last_tick"], status["last_tick_epoch"] = F.now_iso(), time.time()
        if out["events"]:
            status["last_import"] = {"time": status["last_tick"], "files": out["files"], "events": out["events"]}
        if digest.active():
            line = digest.line()
            _out(time.strftime("%H:%M:%S ") + line)
            F.append_log(str(home), line)
        elif status["cycles"] == 1:
            show(out, True)
        for e in out["errors"]:
            _err(f"  ! {e}")
            F.append_log(str(home), f"! {e}")
            status["errors"] = ([{"time": status["last_tick"], "error": str(e)[:300]}] + status["errors"])[:20]
        digest.reset()
        F.write_status(str(home), status)

    F.clear_stop(str(home))          # * une demande restee d'un suivi precedent ne doit pas arreter celui-ci
    F.append_log(str(home), f"suivi demarre (pid {os.getpid()}, toutes les {interval:g} s, depot {status['repo']}, AgentWatch {__version__})")
    reason = "interruption clavier"
    try:
        while True:
            cycle()
            waited = 0.0
            while waited < interval:                     # * la demande d'arret est lue entre deux cycles, a la seconde
                asked = F.stop_requested(str(home))
                if asked is not None:
                    reason = f"arret demande : {asked.get('reason') or 'sans motif'}"
                    F.clear_stop(str(home))
                    return 0
                step = min(1.0, interval - waited)
                time.sleep(step)
                waited += step
    except KeyboardInterrupt:
        return 0
    except BaseException as exc:      # noqa: BLE001 - la cause de l'arret doit etre dans le journal, puis relancee
        reason = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        status["stopped"] = {"time": F.now_iso(), "reason": reason}
        F.append_log(str(home), f"suivi arrete : {reason}")
        try:
            F.write_status(str(home), status)
        except OSError:
            pass


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
        version, exe = _client_version(client, cfg)
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
            codex_hooks_present = False
            for scope in ("user", "project"):
                p = IX.hooks_path(scope)
                try:
                    obj, _ = C.read_json_file(p)
                except ValueError as exc:
                    _out(f"    {scope}: {p} : JSON invalide ({exc})")
                    continue
                s = IX.status(obj)
                codex_hooks_present = codex_hooks_present or bool(s["installed_events"])
                if s["installed_events"] or p.is_file():
                    _out(f"    {scope}: {p} : hooks AgentWatch {'complets dans le fichier' if s['complete'] else ('partiels ' + str(s['missing_events']) if s['installed_events'] else 'absents')}"
                         f" ; groupes etrangers preserves : {sum(s['foreign_groups'].values())}")
            # * Un fichier complet ne prouve rien : seul Codex sait si l'utilisateur a approuve chaque hook. Mais la sonde
            #   LANCE un processus Codex (`codex app-server`) : jamais par defaut, seulement sur demande explicite
            #   (--codex-trust ou health.codex_trust_probe = true), et seulement si des hooks AgentWatch sont installes.
            probe_on = bool(getattr(args, "codex_trust", False)) or (isinstance(cfg.get("health"), dict)
                                                                   and cfg["health"].get("codex_trust_probe") is True)
            if not codex_hooks_present:
                _out("    aucun hook AgentWatch installe pour Codex : collecte par la lecture passive des rollouts ; "
                     "Codex n'est jamais lance par AgentWatch")
            elif exe and meta and probe_on and not args.no_codex_trust:
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
                     "etat de confiance non verifie" + (" (--no-codex-trust)" if args.no_codex_trust else
                                                         " (la sonde lance codex app-server : --codex-trust pour l'autoriser)"))
            # * Collecte sans hooks : lecture passive des rollouts (collector/rollouts.py).
            from agentwatch.collector import rollouts as R
            rstate = R.load_state(str(home))
            tracked = rstate.get("files") or {}
            roots = {((st or {}).get("meta") or {}).get("root_id") for st in tracked.values()} - {None}
            _out(f"    rollouts : lecture passive {'automatique avant sessions, report et trends' if _rollouts_cfg(cfg).get('auto_import', True) else 'sur demande (import-rollouts)'}"
                 f" (lecture seule, sans hooks, sans effet sur Codex) ; {len(tracked)} fichier(s) suivi(s), {len(roots)} session(s) Codex importee(s)")
            lr, li = rstate.get("last_run") or {}, rstate.get("last_import") or {}
            if lr:
                # * Trace ecrite par un passage qui a trouve des lignes nouvelles : sans ecriture de Codex, les passages
                #   (toutes les 30 s en suivi) ne reecrivent pas l'etat. Le retard de lecture ci-dessous dit si rien n'attend.
                _out(f"    derniere lecture du collecteur : {lr.get('time')} ; {lr.get('files_checked')} rollout(s) examine(s), "
                     f"{lr.get('files_read')} lu(s), {lr.get('events')} evenement(s), {lr.get('errors')} erreur(s)"
                     + (f" ; dernier import d'evenements : {li.get('time')} ({li.get('events')} evenement(s))" if li else ""))
            for line in _rollout_state_lines(cfg, rstate):
                _out(f"    {line}")
            for line in _follow_state_lines(home):
                _out(f"    {line}")
            for e in (rstate.get("errors") or [])[:3]:
                _out(f"    ! erreur de collecte du {e.get('time')} : {e.get('error')}")
    health = trust_alerts + _health(home, cfg, store)
    if health:
        _out("- Sante de la collecte (panne silencieuse) :")
        for h in health:
            _out(f"    ! {h['client']} : {h['message']}")
    else:
        # * Ne pas dire « hooks presents » pour un client qui n'en a pas : Codex est lu dans ses rollouts, sans hook.
        _out("- Sante de la collecte : rien a signaler (interpreteur et point d'entree en place ; pour chaque client, la voie "
             "de collecte decrite ci-dessus, hooks ou lecture des rollouts, est en place ; aucun silence apres une activite "
             "du client)")
    tcfg = cfg.get("transcripts") if isinstance(cfg.get("transcripts"), dict) else {}
    _out(f"- Transcripts Claude Code (usage en tokens) : {'import automatique a chaque rapport' if tcfg.get('auto_import') else 'sur demande (agentwatch import-transcripts ; transcripts.auto_import=true pour automatiser)'}")
    _out("Rappel : AgentWatch observe les evenements de hooks (Claude Code) et lit passivement les rollouts Codex, sans jamais "
         "lancer ni interroger Codex ; voir docs/compatibility.md pour les limites par client.")
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
        version, exe = _client_version(args.client, load_config(home_dir(args.home)))
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
    s.add_argument("--codex-trust", action="store_true",
                   help="interroger codex app-server (hooks/list) sur l'approbation des hooks : LANCE un processus Codex")
    s.add_argument("--no-codex-trust", action="store_true",
                   help="ne jamais interroger codex app-server (comportement par defaut, garde pour compatibilite)")
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
    s.add_argument("--day", help="seulement ce jour (AAAA-MM-JJ, jour local)")
    s.add_argument("--since", help="seulement a partir de cet instant (AAAA-MM-JJ[THH:MM], heure locale sans fuseau)")
    s.add_argument("--until", help="seulement avant cet instant (meme format)")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("inspect", help="export detaille d'une session (messages, appels, resultats, tokens), "
                                       "relu dans les journaux du client, secrets masques")
    s.add_argument("--client", choices=SUPPORTED_CLIENTS, default="codex", help="client source (defaut : codex)")
    s.add_argument("--kind", dest="kinds", action="append", help="Claude : type canonique a garder (message, appel, resultat, notification...), repetable")
    s.add_argument("--role", choices=("user", "assistant", "tool"), help="Claude : role du contenu, resultats d'outils separes")
    s.add_argument("--contains", help="Claude : texte litteral insensible a la casse, recherche avant bornage")
    s.add_argument("--source-line", type=int, help="Claude : numero de ligne physique dans le fichier source")
    s.add_argument("--session", required=True, help="identifiant (ou prefixe) du fil racine de la session")
    s.add_argument("--thread", help="seulement ce fil (identifiant ou prefixe)")
    s.add_argument("--day", help="seulement ce jour (AAAA-MM-JJ, jour local)")
    s.add_argument("--since", help="seulement a partir de cet instant (heure locale sans fuseau ; Z ou +02:00 acceptes)")
    s.add_argument("--until", help="seulement avant cet instant")
    s.add_argument("--max-chars", type=int, default=4000, help="texte affiche par champ (defaut 4000) ; au-dela : [tronque]")
    s.add_argument("--reasoning", action="store_true", help="inclure le raisonnement brut quand le client l'ecrit en clair")
    s.add_argument("--no-conclusions", action="store_true", help="ne pas ajouter l'annexe des conclusions des detecteurs")
    s.add_argument("--format", default="markdown", choices=("markdown", "jsonl"))
    s.add_argument("--out", help="fichier de sortie (defaut : <donnees>/exports/<session>-<date>.md)")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("claude-context", help="fenetres de contexte, compactions et requetes uniques depuis les transcripts Claude")
    s.add_argument("--session", action="append", required=True, help="session ou prefixe, repetable pour dedoublonner les reprises")
    s.add_argument("--out", required=True, help="rapport JSON local avec mesures, limites et references")
    s.set_defaults(func=cmd_claude_context)

    s = sub.add_parser("compare", help="avant / apres une correction : les pertes ont-elles reellement baisse ? (IC 95 %%)")
    s.add_argument("--at", help="instant de la correction (AAAA-MM-JJ[THH:MM], heure locale) ou agents-md:<empreinte>")
    s.add_argument("--since", help="debut de la periode 'avant' (defaut : tout l'historique)")
    s.add_argument("--until", help="fin de la periode 'apres' (defaut : maintenant)")
    s.add_argument("--client", choices=SUPPORTED_CLIENTS)
    s.add_argument("--list-agents-md", action="store_true", help="lister les versions d'AGENTS.md vues par Codex")
    s.add_argument("--project", help="seulement les sessions dont le dossier de projet contient ce texte")
    s.add_argument("--save-reference", help="enregistrer la mesure de [--since, --until[ comme reference (fichier JSON)")
    s.add_argument("--reference", help="comparer [--since, --until[ a une reference enregistree")
    s.add_argument("--format", default="markdown", choices=("markdown", "json"))
    s.add_argument("--out", help="fichier de sortie (sinon stdout)")
    s.set_defaults(func=cmd_compare)

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
    s.add_argument("--stop-follow", action="store_true",
                   help="demander au suivi continu de s'arreter apres son cycle (arret consigne, verrou rendu) ; ne lit rien")
    s.add_argument("--reason", help="motif de l'arret demande, ecrit dans l'etat et le journal")
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
