"""Confiance des hooks Codex, lue aupres de Codex lui-meme (`codex app-server`, methode `hooks/list`).

# * Codex n'execute un hook qu'apres approbation par l'utilisateur (/hooks, confiance par empreinte
#   de la definition). Un hooks.json complet ne prouve donc rien : constate le 2026-09-19 avec
#   codex-cli 0.155.0-alpha.9.2, les 11 hooks AgentWatch etaient `untrusted`, donc ignores, alors que
#   `doctor` annoncait des hooks complets.
# * `codex app-server` parle JSON-RPC (une ligne JSON par message) sur stdio. `initialize` puis
#   `hooks/list` ne sollicitent pas le modele et ne consomment aucun quota ; le processus est tue des
#   la reponse recue. Etats de confiance observes : untrusted, trusted, modified, managed.
# ! Lecture seule : AgentWatch n'approuve jamais un hook a la place de l'utilisateur.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

from agentwatch.installer import common as C

RUNNABLE_TRUST = ("trusted", "managed")
_TRUST_HELP = {
    "untrusted": "jamais approuves : Codex les ignore",
    "modified": "definition modifiee depuis l'approbation : Codex les ignore jusqu'a une nouvelle approbation",
    "trusted": "approuves",
    "managed": "imposes par une configuration geree",
}


def probe(argv: list[str], cwds: list[str], timeout: float = 30.0, cwd: str | None = None) -> dict[str, Any]:
    """Interroge `<argv> app-server` : {"ok", "error", "entries", "elapsed_ms"}. Jamais d'exception."""
    started = time.monotonic()
    out: dict[str, Any] = {"ok": False, "error": None, "entries": [], "elapsed_ms": None}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen([*argv, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=cwd, creationflags=flags)
    except OSError as exc:
        out["error"] = f"lancement de codex app-server impossible ({type(exc).__name__}: {exc})"
        return out
    lines: queue.Queue[bytes | None] = queue.Queue()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    deadline = started + timeout

    def send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def wait(req_id: int) -> dict[str, Any] | None:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                line = lines.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if line is None:
                return None   # * sortie fermee : le processus s'est arrete
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("id") == req_id and ("result" in obj or "error" in obj):
                return obj

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agentwatch-doctor", "version": "1"}}})
        r = wait(1)
        if r is None or "result" not in r:
            out["error"] = _rpc_error("initialize", r, timeout)
            return out
        send({"method": "initialized"})
        send({"id": 2, "method": "hooks/list", "params": {"cwds": list(cwds)}})
        r = wait(2)
        if r is None or "result" not in r:
            out["error"] = _rpc_error("hooks/list", r, timeout)
            return out
        data = (r.get("result") or {}).get("data")
        out["entries"] = data if isinstance(data, list) else []
        out["ok"] = True
        return out
    except (OSError, ValueError) as exc:
        out["error"] = f"dialogue avec codex app-server interrompu ({type(exc).__name__}: {exc})"
        return out
    finally:
        out["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.SubprocessError, OSError):
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def _rpc_error(method: str, resp: dict[str, Any] | None, timeout: float) -> str:
    if resp is None:
        return f"pas de reponse de codex app-server a {method} en {timeout:.0f} s"
    raw = resp.get("error")
    err: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return f"{method} refuse par Codex (code {err.get('code')}, {str(err.get('message'))[:160]}) : version sans cette methode ?"


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Hooks AgentWatch vus par Codex : nombre, etats de confiance, actifs, erreurs et avertissements."""
    ours: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        errors.extend(str(x) for x in e.get("errors") or [])
        warnings.extend(str(x) for x in e.get("warnings") or [])
        for h in e.get("hooks") or []:
            if isinstance(h, dict) and C.is_our_hook({"command": h.get("command")}):
                ours.append(h)
    trust: dict[str, int] = {}
    for h in ours:
        t = str(h.get("trustStatus"))
        trust[t] = trust.get(t, 0) + 1
    runnable = [h for h in ours if h.get("enabled") is not False and h.get("trustStatus") in RUNNABLE_TRUST]
    return {"hooks": len(ours), "trust": trust, "runnable": len(runnable),
            "disabled": sum(1 for h in ours if h.get("enabled") is False),
            "not_runnable_events": sorted({str(h.get("eventName")) for h in ours if h not in runnable}),
            "errors": errors, "warnings": warnings}


def format_lines(result: dict[str, Any]) -> list[str]:
    """Lignes pour `doctor` ; une ligne commencant par '!' signale une collecte impossible."""
    if not result.get("ok"):
        return [f"confiance des hooks (codex app-server) : controle impossible ({result.get('error')})"]
    s = summarize(result.get("entries") or [])
    if not s["hooks"]:
        return ["! confiance des hooks (codex app-server) : aucun hook AgentWatch vu par Codex ; "
                "verifier le fichier hooks.json et features.hooks"]
    detail = ", ".join(f"{n} {t} ({_TRUST_HELP.get(t, 'etat inconnu')})" for t, n in sorted(s["trust"].items()))
    lines = [f"confiance des hooks (codex app-server, {result.get('elapsed_ms')} ms) : {s['hooks']} hooks AgentWatch vus ; {detail}"]
    if s["runnable"] < s["hooks"]:
        lines.append(f"! Codex n'executera que {s['runnable']} hook(s) AgentWatch sur {s['hooks']} "
                     f"(non executes : {', '.join(s['not_runnable_events'])}) : aucun evenement correspondant ne sera capte ; "
                     "approuver les hooks dans Codex via /hooks (AgentWatch ne peut pas le faire a votre place)")
    for e in s["errors"][:5]:
        lines.append(f"! erreur signalee par Codex : {e[:200]}")
    for w in s["warnings"][:5]:
        lines.append(f"avertissement de Codex : {w[:200]}")
    return lines
