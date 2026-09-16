"""Chargement des sessions depuis le stockage et listage."""

from __future__ import annotations

from typing import Any

from agentwatch.collector.store import EventStore
from agentwatch.core.correlate import SessionView, build_session


def load_session(store: EventStore, client: str, skey: str, cfg: dict[str, Any]) -> SessionView:
    # * Lecture de centaines de petits fichiers = ~13 ms par premiere ouverture sous Windows
    #   (analyse antivirus). Au-dela du seuil, on fusionne d'abord en segment JSONL : ecriture
    #   atomique, sure meme si un hook ecrit en parallele (un fichier absent du listage est
    #   simplement lu la fois suivante).
    threshold = int(cfg.get("auto_compact_threshold", 0) or 0)
    if threshold > 0:
        try:
            store.maybe_compact(client, skey, threshold)
        except OSError:
            pass  # ? la lecture reste possible sans compaction
    events, warnings = store.read_session_events(client, skey)
    view = build_session(events, cfg)
    view.warnings = warnings + view.warnings
    if view.client == "unknown":
        view.client = client
    return view


def list_sessions(store: EventStore, cfg: dict[str, Any], client: str | None = None) -> list[dict[str, Any]]:
    """Resume de chaque session (lecture complete : acceptable en local pour la V1)."""
    rows: list[dict[str, Any]] = []
    for c, skey, _ in store.iter_sessions():
        if client and c != client:
            continue
        view = load_session(store, c, skey, cfg)
        errors = sum(1 for call in view.calls if call.status in ("error", "timeout", "denied"))
        rows.append({
            "client": c, "session_key": skey, "session_id": view.session_id, "model": view.model,
            "project_dir": view.project_dir, "first_time": view.first_time, "last_time": view.last_time,
            "events": view.counts.get("events", 0), "calls": len(view.calls), "errors": errors,
            "open_calls": view.counts.get("open_calls", 0), "turns": view.turns, "agents": len(view.agents),
        })
    rows.sort(key=lambda r: r.get("last_time") or "", reverse=True)
    return rows
