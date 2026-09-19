"""Point d'entree des hooks (Claude Code et Codex).

Usage (genere par `agentwatch configure`) :
    python -I <chemin>/agentwatch/hook_entry.py ingest --client claude-code --home <dossier>

# * Autonome : ajoute la racine du paquet a sys.path, donc aucune installation pip
#   n'est requise. Ne lit stdin qu'une fois, n'ecrit jamais sur stdout, sort en 0.
"""

import os
import sys
import time

_STARTED_NS = time.time_ns()
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> int:
    argv = sys.argv[1:]
    try:
        if argv and argv[0] == "ingest":
            argv = argv[1:]
        from agentwatch.collector.ingest import run_hook
        return run_hook(argv, started_ns=_STARTED_NS)
    except BaseException as exc:  # noqa: BLE001 - ! ne jamais faire echouer le client
        _crash_note(argv, exc)
        return 0


def _crash_note(argv: list, exc: BaseException) -> None:
    """Incident `hook_crash` pour un echec hors de run_hook (import casse, depot en cours de mise a jour).

    # ! Sans cette trace, une erreur d'import dans le depot coupe la collecte sans aucun message :
    #   le hook sort en 0 (voulu) et rien n'est ecrit. Bibliotheque standard seulement, aucun module
    #   AgentWatch (ils sont peut-etre justement casses), meme nommage et meme plafond (1000 fichiers)
    #   que store.write_diagnostic, jamais d'exception. Aucun contenu du payload n'est lu.
    """
    try:
        import json
        home = None
        for i, a in enumerate(argv[:-1]):
            if a == "--home":
                home = argv[i + 1]
        home = os.path.expanduser(home or os.environ.get("AGENTWATCH_HOME") or os.path.join("~", ".agentwatch"))
        diag = os.path.join(home, "diagnostics")
        os.makedirs(diag, exist_ok=True)
        if len(os.listdir(diag)) >= 1000:
            return
        client = argv[argv.index("--client") + 1] if "--client" in argv[:-1] else None
        error = f"{type(exc).__name__}: {str(exc)[:160]}".replace(os.path.expanduser("~"), "~")
        ns = time.time_ns()
        with open(os.path.join(diag, f"{ns:020d}-{os.getpid()}-hook_crash.json"), "x", encoding="utf-8") as fh:
            json.dump({"time_ns": ns, "kind": "hook_crash", "client": client, "error": error}, fh)
    except BaseException:  # noqa: BLE001
        pass


if __name__ == "__main__":
    code = main()
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)
