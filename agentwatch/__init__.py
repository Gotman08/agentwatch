"""AgentWatch : observateur local passif pour Claude Code et Codex.

# * Le paquet n'a aucune dependance hors bibliotheque standard (Python >= 3.11).
# * Les modules du chemin chaud (hook) doivent rester legers : voir collector/ingest.py.
"""

__version__ = "0.1.0"

# * Version du schema d'evenement commun. Incrementer a chaque changement de champ.
SCHEMA_VERSION = "1.1"

CLIENT_CLAUDE_CODE = "claude-code"
CLIENT_CODEX = "codex"
SUPPORTED_CLIENTS = (CLIENT_CLAUDE_CODE, CLIENT_CODEX)
