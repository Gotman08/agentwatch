"""Tests AgentWatch.

# ! Isolation : les tests n'importent jamais les rollouts Codex reels de la machine. L'import automatique
#   avant `sessions`, `report` et `trends` (actif par defaut en usage reel) est coupe ici ; les tests de
#   l'importeur (test_rollouts.py) l'exercent sur des rollouts synthetiques et un dossier dedie.
"""

from agentwatch import config as _config

_config.DEFAULTS["rollouts"]["auto_import"] = False
