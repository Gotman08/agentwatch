"""Chemin chaud : recevoir un evenement de hook, l'extraire, le proteger, l'ecrire, sortir.

# ! Regles absolues :
#   - jamais d'ecriture sur stdout (un JSON sur stdout serait interprete par le client) ;
#   - toujours sortir avec le code 0 (un code 2 bloquerait l'appel chez les deux clients) ;
#   - aucune analyse, recherche web ou appel reseau ici ;
#   - imports minimaux (pas de pathlib/typing/hashlib au chargement).
"""

from __future__ import annotations

import os
import sys
import time

from agentwatch import SUPPORTED_CLIENTS
from agentwatch.config import home_str, load_config
from agentwatch.core import schema as S

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


def parse_args(argv: list[str]) -> dict[str, Any]:
    """Analyse minimale des arguments (pas d'argparse : cout de demarrage)."""
    opts: dict[str, Any] = {"client": None, "home": None, "source": S.SOURCE_HOOK}
    it = iter(argv)
    for arg in it:
        if arg == "--client":
            opts["client"] = next(it, None)
        elif arg == "--home":
            opts["home"] = next(it, None)
        elif arg == "--source":
            opts["source"] = next(it, None) or S.SOURCE_HOOK
    return opts


def read_stdin_bounded(limit: int) -> tuple[bytes, bool]:
    """Lit stdin en binaire jusqu'a `limit` octets. Retourne (donnees, tronque?)."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = stream.read(min(65536, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            chunks.append(chunk[: limit - (total - len(chunk))])
            truncated = True
            # * On draine le reste pour ne pas bloquer le client sur un tube plein.
            while stream.read(65536):
                pass
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


def sanitize_event(ev: dict[str, Any], key: bytes, cfg: dict[str, Any]) -> dict[str, Any]:
    """Applique la confidentialite : masquage, bornage, dossier utilisateur."""
    from agentwatch.collector import privacy as P
    from agentwatch.core.normalize import make_error_signature

    home = os.path.expanduser("~") if cfg.get("mask_home_dir", True) else None
    max_cmd = int(cfg.get("max_command_chars", 2000))
    max_err = int(cfg.get("max_error_chars", 400))
    for field in ("cwd", "project_dir"):
        if isinstance(ev.get(field), str):
            ev[field] = P.mask_home(P.mask_secrets(ev[field], key), home)
    if isinstance(ev.get("target"), str):
        masked, trunc = P.sanitize_text(ev["target"], key, max_cmd)
        ev["target"] = P.mask_home(masked, home) if ev.get("target_kind") == "path" else masked
        if trunc:
            ev["warnings"].append("target truncated")
    ev["params"] = P.scrub_tree(ev.get("params") or {}, key, max_cmd, home)
    if isinstance(ev["params"].get("shell_paths"), list):
        ev["params"]["shell_paths"] = [P.mask_home(p, home) for p in ev["params"]["shell_paths"]]
    if isinstance(ev["params"].get("patch_paths"), list):
        ev["params"]["patch_paths"] = [P.mask_home(p, home) for p in ev["params"]["patch_paths"]]
    if ev.get("error_summary") is not None:
        masked, _ = P.sanitize_text(ev["error_summary"], key, max_err)
        ev["error_summary"] = P.mask_home(masked, home)
        ev["error_signature"] = make_error_signature(masked)
    if isinstance(ev.get("result_paths"), list):
        ev["result_paths"] = [P.mask_home(P.mask_secrets(p, key), home) for p in ev["result_paths"]]
    ev["evidence"] = P.scrub_tree(ev.get("evidence") or {}, key, 512, home)
    ev["session_meta"] = P.scrub_tree(ev.get("session_meta"), key, 200) if ev.get("session_meta") else ev.get("session_meta")
    return ev


def ingest_payload(payload: Any, client: str, home: str | os.PathLike[str], cfg: dict[str, Any] | None = None,
                   source: str = S.SOURCE_HOOK, started_ns: int | None = None,
                   stdin_bytes: int | None = None, stdin_truncated: bool = False) -> tuple[str | None, str | None]:
    """Transforme et persiste un payload deja decode. Retourne (chemin ecrit, incident)."""
    from agentwatch.adapters import AdapterContext, get_adapter
    from agentwatch.collector import privacy as P
    from agentwatch.collector.store import EventStore, StoreError

    home_s = os.fspath(home)
    cfg = cfg or load_config(home_s)
    store = EventStore(home_s, cfg)
    if not isinstance(payload, dict):
        store.write_diagnostic("malformed", {"client": client, "reason": "payload is not a JSON object",
                                             "stdin_bytes": stdin_bytes})
        return None, "malformed"
    try:
        key = P.ensure_key(home_s)
    except OSError as exc:
        store.write_diagnostic("key_error", {"client": client, "error": type(exc).__name__})
        return None, "key_error"
    ctx = AdapterContext(cfg, lambda s: P.fingerprint(key, s), os.environ.get("CLAUDE_PROJECT_DIR") or None)
    try:
        ev = get_adapter(client).parse_hook_payload(payload, ctx)
    except Exception as exc:  # noqa: BLE001 - ! un payload inattendu ne doit pas perdre l'evenement
        # * Repli minimal : on conserve les identifiants (sans aucun contenu) pour que l'appel
        #   reste comptable a l'analyse, avec l'erreur en avertissement. Vecu en direct : une
        #   edition en deux temps du code du hook a provoque un NameError pendant 2 appels.
        ev = S.empty_event()
        ev["client"] = client
        name = payload.get("hook_event_name") if isinstance(payload.get("hook_event_name"), str) else None
        ev["hook_event_name"] = name
        ev["phase"] = {"PreToolUse": S.PHASE_START, "PostToolUse": S.PHASE_END,
                       "PostToolUseFailure": S.PHASE_FAILURE}.get(name or "", S.PHASE_UNKNOWN)
        for src, dst in (("session_id", "session_id"), ("tool_use_id", "call_id"), ("tool_name", "tool_name"),
                         ("agent_id", "agent_id"), ("agent_type", "agent_type"), ("turn_id", "turn_id"), ("cwd", "cwd")):
            v = payload.get(src)
            if isinstance(v, str) and v:
                ev[dst] = v
        if ev["tool_name"]:
            from agentwatch.core.normalize import categorize_tool
            ev["tool_category"], ev["mcp_server"], ev["mcp_tool"] = categorize_tool(client, ev["tool_name"])
        ev["status"] = S.STATUS_UNKNOWN if ev["phase"] in (S.PHASE_END, S.PHASE_FAILURE) else None
        ev["target_kind"] = "unknown"
        ev["warnings"].append(f"adapter error: {type(exc).__name__}: {str(exc)[:120]}")
    ev["source"] = source
    if source == S.SOURCE_REPLAY:
        # * Rejeu : identifiant derive du contenu -> un import repete du meme payload est
        #   reconnu comme doublon a l'analyse (les hooks reels gardent un identifiant aleatoire).
        from agentwatch.core.normalize import canonical_json
        ev["event_id"] = P.sha256_hex(f"{client}|{canonical_json(payload)}".encode("utf-8"))[:32]
    if started_ns is not None:
        ev["received_time_ns"] = started_ns
        ev["received_time"] = S.now_iso(started_ns / 1e9)
    ev["evidence"]["stdin_bytes"] = stdin_bytes
    if stdin_truncated:
        ev["warnings"].append("stdin truncated by max_stdin_bytes")
        ev["output_truncated"] = True
    for w in cfg.get("_config_warnings", []):
        ev["warnings"].append(w)
    ev = sanitize_event(ev, key, cfg)
    if started_ns is not None:
        ev["evidence"]["hook_ms"] = round((time.time_ns() - started_ns) / 1e6, 1)
    try:
        return store.write_event(ev), None
    except StoreError as exc:
        # ? Quota ou taille : on tente une version reduite avant d'abandonner.
        if "quota" in str(exc):
            store.write_diagnostic("drop", {"client": client, "reason": str(exc)[:200],
                                            "session_id": ev.get("session_id"), "phase": ev.get("phase")})
            return None, "drop"
        ev["params"] = {"_dropped": "event too large"}
        ev["result_paths"] = None
        ev["error_summary"] = None
        ev["warnings"].append(f"store: {exc}")
        try:
            return store.write_event(ev), None
        except StoreError as exc2:
            store.write_diagnostic("drop", {"client": client, "reason": str(exc2)[:200],
                                            "session_id": ev.get("session_id"), "phase": ev.get("phase")})
            return None, "drop"
    except OSError as exc:
        store.write_diagnostic("write_error", {"client": client, "error": type(exc).__name__,
                                               "session_id": ev.get("session_id")})
        return None, "write_error"


def run_hook(argv: list[str], started_ns: int | None = None) -> int:
    """Corps du hook. Retourne toujours 0."""
    started_ns = started_ns or time.time_ns()
    opts = parse_args(argv)
    client = opts["client"]
    home = home_str(opts["home"])
    try:
        cfg = load_config(home)
    except Exception:  # noqa: BLE001
        cfg = {}
    if client not in SUPPORTED_CLIENTS:
        _diag(home, cfg, "bad_client", {"client": client})
        return 0
    try:
        raw, truncated = read_stdin_bounded(int(cfg.get("max_stdin_bytes", 8 * 1024 * 1024)))
    except OSError:
        raw, truncated = b"", False
    if not raw.strip():
        _diag(home, cfg, "empty_stdin", {"client": client})
        return 0
    try:
        import json
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError) as exc:
        # ! RecursionError n'herite pas de ValueError : un JSON imbrique au-dela de la limite de
        #   l'interpreteur (3000 niveaux sous Python 3.12) serait sinon perdu sans incident compte.
        _diag(home, cfg, "malformed", {"client": client, "stdin_bytes": len(raw),
                                       "truncated": truncated, "error": type(exc).__name__})
        return 0
    try:
        ingest_payload(payload, client, home, cfg, opts["source"], started_ns, len(raw), truncated)
    except Exception as exc:  # noqa: BLE001 - dernier filet
        _diag(home, cfg, "ingest_error", {"client": client, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return 0


def _diag(home: str, cfg: dict[str, Any], kind: str, detail: dict[str, Any]) -> None:
    try:
        from agentwatch.collector.store import EventStore
        EventStore(home, cfg).write_diagnostic(kind, detail)
    except Exception:  # noqa: BLE001
        pass
