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
    try:
        argv = sys.argv[1:]
        if argv and argv[0] == "ingest":
            argv = argv[1:]
        from agentwatch.collector.ingest import run_hook
        return run_hook(argv, started_ns=_STARTED_NS)
    except BaseException:  # noqa: BLE001 - ! ne jamais faire echouer le client
        return 0


if __name__ == "__main__":
    code = main()
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)
