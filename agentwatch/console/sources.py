"""Relecture explicite des sorties natives, masquage et conservation dans le projet."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agentwatch.collector import privacy, rollouts, transcripts
from agentwatch.config import load_config
from .storage import Project, encode, within


def tables(project: Project):
    with project.db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS outputs(call TEXT PRIMARY KEY,text TEXT,complete INTEGER,"
                   "source TEXT,captured_ns INTEGER,limit_chars INTEGER)")


def saved(project: Project, cid: str) -> dict[str, Any] | None:
    tables(project)
    with project.db() as db:
        row = db.execute("SELECT * FROM outputs WHERE call=?", (cid,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["complete"] = bool(data["complete"])
    data["source"] = json.loads(data["source"])
    return data


def readable(value: Any) -> str:
    """Expose MCP text blocks as text while keeping other structured fields visible."""
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        remaining = {k: v for k, v in value.items() if k != "content"}
        parts = [readable(v) for v in value["content"]]
        if remaining:
            parts.append(json.dumps(remaining, ensure_ascii=False, indent=2))
        return "\n\n".join(parts)
    if isinstance(value, dict) and value.get("type") == "text" and isinstance(value.get("text"), str):
        remaining = {k: v for k, v in value.items() if k not in ("type", "text")}
        return value["text"] + ("\n" + json.dumps(remaining, ensure_ascii=False, indent=2) if remaining else "")
    return json.dumps(value, ensure_ascii=False, indent=2)


def capture(project: Project, cid: str, source_home: Path, limit: int = 20000) -> dict[str, Any]:
    limit = min(200000, max(1000, int(limit)))
    with project.db() as db:
        row = db.execute("SELECT data FROM calls WHERE id=?", (cid,)).fetchone()
        if not row:
            raise KeyError("Appel inconnu dans ce projet.")
        call = json.loads(row[0])
        ids = call.get("event_ids") or []
        events = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE client=? AND event_id IN ("
                  + ",".join("?" for _ in ids) + ")", [call["client"], *ids])] if ids else []
    cfg = load_config(source_home)
    key = privacy.ensure_key(str(project.home))
    text, complete, proof = None, False, {}
    if call["client"] == "codex":
        end = next((e for e in events if e.get("phase") in ("end", "failure")), None)
        evidence = (end or {}).get("evidence") or {}
        src = evidence.get("source") or call.get("evidence", {}).get("source_end")
        ident = evidence.get("source_identity") or {}
        if not isinstance(src, dict) or not src.get("file") or not isinstance(src.get("offset"), int):
            raise ValueError("Aucune source de sortie native enregistree pour cet appel.")
        name = str(src["file"])
        if Path(name).name != name or src["offset"] < 0:
            raise ValueError("Reference de source invalide.")
        path = rollouts.find_rollout(cfg, name)
        if not path or not within(path, rollouts.sessions_dir(cfg)):
            raise ValueError("Le journal natif est absent ou deplace. Les observations normalisees restent conservees.")
        with open(path, "rb") as fh:
            fh.seek(src["offset"])
            raw = fh.readline(rollouts.MAX_SOURCE_LINE_BYTES)
        if len(raw) >= rollouts.MAX_SOURCE_LINE_BYTES:
            raise ValueError("Ligne native trop grande pour une relecture bornee.")
        record = json.loads(raw)
        payload = record.get("payload") or {}
        item = payload.get("item") or {}
        candidate = item.get("id") or payload.get("call_id") or payload.get("id")
        expected = ident.get("item_id") or call.get("call_id")
        if not expected or candidate != expected or (ident.get("record_type") and record.get("type") != ident["record_type"]):
            raise ValueError("La source a change : l'identite de l'appel ne correspond plus. Aucun extrait conserve.")
        if ident.get("thread_id") and payload.get("thread_id") and payload["thread_id"] != ident["thread_id"]:
            raise ValueError("La source appartient a un autre fil.")
        text, complete = rollouts.read_source_output(path, src["offset"], key, cfg, limit)
        if text is None:
            # MCP results may be structured rather than string output.
            for container in (item, payload):
                for field in ("output", "result", "content"):
                    value = container.get(field)
                    if isinstance(value, (dict, list)):
                        value = readable(value)
                        text, complete = privacy.mask_secrets(value[:limit], key), len(value) <= limit
                        break
                if text is not None:
                    break
        proof = {"provider": "codex", **src, "identity": ident}
    elif call["client"] == "claude-code":
        original = events[0] if events else {}
        main, children = transcripts.find_transcripts(original.get("project_dir"), original.get("session_id"), cfg)
        for path in ([main] if main else []) + children:
            if not within(path, transcripts.claude_projects_dir(cfg)):
                continue
            with path.open("rb") as fh:
                offset, scanned = 0, 0
                while scanned < 20_000_000:
                    raw = fh.readline(2_097_153)
                    if not raw:
                        break
                    scanned += len(raw)
                    try:
                        obj = json.loads(raw)
                        message = obj.get("message") or {}
                        content = message.get("content") or []
                        for part in content if isinstance(content, list) else []:
                            if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("tool_use_id") == call.get("call_id"):
                                value = part.get("content")
                                value = value if isinstance(value, str) else encode(value)
                                text = privacy.mask_secrets(value[:limit], key)
                                complete = len(value) <= limit
                                proof = {"provider": "claude-code", "file": path.name, "offset": offset,
                                         "tool_use_id": call["call_id"]}
                                break
                    except (ValueError, TypeError, AttributeError):
                        pass
                    if text is not None:
                        break
                    offset = fh.tell()
            if text is not None:
                break
    if text is None:
        raise ValueError("Sortie native non disponible dans la source conservee. Aucun contenu n'est invente.")
    from .chat import _redact
    text = _redact(text, key)
    tables(project)
    with project.db() as db:
        db.execute("INSERT OR REPLACE INTO outputs VALUES(?,?,?,?,?,?)",
                   (cid, text, int(complete), encode(proof), time.time_ns(), limit))
    return saved(project, cid)
