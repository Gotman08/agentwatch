"""Configuration locale d'AgentWatch.

# * Un seul fichier JSON : <AGENTWATCH_HOME>/config.json. Les valeurs absentes
#   prennent les valeurs par defaut ci-dessous. Aucune valeur n'est lue depuis
#   l'environnement en dehors de AGENTWATCH_HOME (choix de confidentialite).
# * Module du chemin chaud : pas de pathlib ni de typing a l'import (cout de demarrage).
"""

from __future__ import annotations

import json
import os

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

DEFAULT_HOME_DIRNAME = ".agentwatch"
CONFIG_FILENAME = "config.json"

# * Valeurs par defaut, documentees dans docs/installation.md.
DEFAULTS: dict[str, Any] = {
    # --- collecte ---
    "max_stdin_bytes": 8 * 1024 * 1024,      # lecture bornee du payload de hook
    "max_event_bytes": 256 * 1024,           # taille max d'un evenement sanitise
    "max_command_chars": 2000,               # commande shell conservee (masquee)
    "max_error_chars": 400,                  # resume d'erreur conserve (masque)
    "max_result_paths": 200,                 # chemins retenus d'un resultat de recherche/listage
    "max_session_events": 20000,             # quota par session (verifie par echantillonnage)
    "max_diagnostics_files": 1000,           # borne du journal d'incidents
    "retention_days": 30,                    # applique par `agentwatch prune`
    "auto_compact_threshold": 200,           # a la lecture : fusion du spool en segment au-dela de N fichiers
    "mask_home_dir": True,                   # remplace le prefixe du dossier utilisateur par ~
    "store_tool_descriptions": False,        # description libre des appels : desactive par defaut
    "detailed_excerpts": False,              # extraits bornes de sortie : desactive par defaut
    "detailed_excerpt_chars": 240,
    # --- normalisation ---
    "case_insensitive_paths": os.name == "nt",
    # --- parametres MCP conserves en clair (les autres : cle + empreinte) ---
    "mcp_param_allowlist": [
        "path", "file_path", "filePath", "file", "files", "paths", "directory", "dir",
        "cwd", "pattern", "glob", "name", "id", "limit", "offset", "page", "cursor",
        "projectPath", "query",
    ],
    "mcp_param_value_max_chars": 300,
    # --- detecteurs (seuils configurables, voir docs/detectors.md) ---
    "detectors": {
        "redundant_reads": {"enabled": True, "window_calls": 60, "window_seconds": 900, "batch_min": 3},
        "error_loops": {"enabled": True, "min_failures": 3, "window_calls": 40, "window_seconds": 1800},
        "batchable": {"enabled": True, "min_group": 3, "max_gap_calls": 0, "same_response_gap_ms": 2000},
        "automation_candidates": {"enabled": True, "min_occurrences": 3, "min_pattern_len": 2,
                                  "max_pattern_len": 6, "max_calls": 2000},
        "tool_gap": {"enabled": True, "min_calls": 3, "strong_calls": 6},
        "repeated_guidance": {"enabled": True, "min_user_messages": 2, "min_instructions": 3, "min_chars": 80,
                              "max_findings": 10},
        # * G : appels refaits a l'identique ; raison, rythme, verdict. Cadence simulee sur `cooldowns_s` ; retard de
        #   detection tolere = max(min_tolerated_delay_s, tolerated_delay_ratio x attente typique).
        "repeated_calls": {"enabled": True, "min_calls": 3, "episode_gap_s": 1200, "min_avoidable_calls": 3,
                           "cooldowns_s": [10, 30, 60, 120, 300, 600], "min_tolerated_delay_s": 30,
                           "tolerated_delay_ratio": 0.1, "rhythm_top": 15, "report_top": 15},
    },
    # * `context` : section « Contexte » du rapport (debut de fenetre = N premieres reponses apres une compaction).
    "report": {"max_top_findings": 3, "max_listed_per_rule": 15,
               "context": {"recovery_responses": 8, "top_outputs": 10, "top_families": 12, "top_resources": 15},
               # * `exchanges` : section « Entre agents » (livraison lente = delai envoi -> entree chez le destinataire).
               "exchanges": {"top_routes": 12, "top_resources": 10, "top_chains": 3, "slow_delivery_seconds": 60,
                             "retained_min_correlation": 0.95}},
    # --- vue multi-sessions (`agentwatch trends`) ---
    "trends": {"days": 7, "min_sessions": 2, "max_top": 5, "max_examples": 8},
    # --- sante de la collecte (panne silencieuse) : dates de modification seulement ---
    "health": {"enabled": True, "silence_minutes": 30, "codex_sessions_dir": None},
    # --- transcripts Claude Code (usage en tokens) : lus a l'analyse, jamais par le hook ---
    "transcripts": {"auto_import": False, "claude_projects_dir": None, "max_bytes": 64 * 1024 * 1024},
    # --- rollouts Codex : lus a l'analyse (lecture seule, incrementale, priorite d'arriere-plan), jamais par le hook ---
    "rollouts": {"auto_import": True, "days": 7, "background_priority": True, "max_segments": 30, "max_bytes_per_run": 0},
}


def home_str(explicit: str | os.PathLike[str] | None = None) -> str:
    """Dossier de donnees en chaine (explicite > AGENTWATCH_HOME > ~/.agentwatch)."""
    if explicit:
        return os.path.expanduser(os.fspath(explicit))
    env = os.environ.get("AGENTWATCH_HOME")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser("~"), DEFAULT_HOME_DIRNAME)


def home_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Meme chose sous forme de Path (cote analyse / CLI)."""
    from pathlib import Path as _Path
    return _Path(home_str(explicit))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(home: str | os.PathLike[str]) -> dict[str, Any]:
    """Charge config.json fusionne avec DEFAULTS. Un fichier invalide est ignore
    (avec un avertissement dans la config retournee) pour ne jamais bloquer un hook."""
    path = os.path.join(os.fspath(home), CONFIG_FILENAME)
    cfg = json.loads(json.dumps(DEFAULTS))  # copie profonde sans dependance
    cfg["_config_path"] = path
    cfg["_config_warnings"] = []
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("config.json doit contenir un objet JSON")
        cfg = _deep_merge(cfg, raw)
    except (OSError, ValueError) as exc:  # ! Ne jamais lever depuis un hook
        cfg["_config_warnings"].append(f"config.json ignore : {type(exc).__name__}: {exc}")
    return cfg


def write_default_config(home: str | os.PathLike[str]) -> str:
    """Ecrit un config.json par defaut s'il n'existe pas et retourne son chemin."""
    home_s = os.fspath(home)
    path = os.path.join(home_s, CONFIG_FILENAME)
    if not os.path.exists(path):
        os.makedirs(home_s, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DEFAULTS, indent=2, ensure_ascii=False) + "\n")
    return path
