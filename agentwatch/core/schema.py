"""Schema versionne de l'evenement commun.

# * Ce module est importe par le chemin chaud (hook) : uniquement des constantes
#   et des fonctions legeres, pas de dataclass ni de validation lourde.
# * Toute donnee absente reste None ("unknown"), jamais 0 ni "success" implicite.
"""

from __future__ import annotations

import os
import time
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

from agentwatch import SCHEMA_VERSION

# --- Phases d'un evenement (cycle de vie d'un appel ou d'une session) ---
PHASE_START = "start"                  # debut d'appel d'outil
PHASE_END = "end"                      # fin d'appel (le statut vient du contenu)
PHASE_FAILURE = "failure"              # echec signale par le client
PHASE_INTERRUPT = "interrupt"          # interruption (humaine ou client)
PHASE_OBSERVATION = "observation"      # mesure complementaire (telemetrie, import)
PHASE_USAGE = "usage"                  # usage en tokens de la session (import : transcript, JSONL) ; jamais un appel
PHASE_SESSION_START = "session_start"
PHASE_SESSION_END = "session_end"
PHASE_TURN_START = "turn_start"
PHASE_TURN_END = "turn_end"
PHASE_COMPACT_START = "compact_start"
PHASE_COMPACT_END = "compact_end"
PHASE_SUBAGENT_START = "subagent_start"
PHASE_SUBAGENT_STOP = "subagent_stop"
PHASE_UNKNOWN = "unknown"

# --- Statuts d'un appel ---
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_DENIED = "denied"
STATUS_INTERRUPTED = "interrupted"
STATUS_TIMEOUT = "timeout"
STATUS_UNKNOWN = "unknown"

# --- Categories normalisees d'outils ---
CAT_READ = "read"
CAT_SEARCH = "search"
CAT_LIST = "list"
CAT_EDIT = "edit"
CAT_WRITE = "write"
CAT_SHELL = "shell"
CAT_MCP = "mcp"
CAT_AGENT = "agent"
CAT_WEB = "web"
CAT_OTHER = "other"
CAT_UNKNOWN = "unknown"

# --- Sources d'evenement ---
SOURCE_HOOK = "hook"
SOURCE_REPLAY = "replay"
SOURCE_IMPORT = "import"

FINGERPRINT_METHOD_HMAC = "hmac-sha256"
# * 1.1 ajoute `content_fingerprint` (empreinte du contenu principal, normalise). Les
#   evenements 1.0 restent lisibles : le champ vaut simplement None.
COMPATIBLE_SCHEMA_VERSIONS = ("1.0", "1.1")

# * Ordre des cles du document : lisible en JSON, stable pour les tests.
EVENT_FIELDS: tuple[str, ...] = (
    "schema_version", "event_id", "source", "client", "client_version", "model",
    "session_id", "turn_id", "agent_id", "agent_type", "call_id",
    "phase", "hook_event_name", "event_time", "received_time", "received_time_ns",
    "tool_name", "tool_category", "mcp_server", "mcp_tool",
    "project_dir", "cwd", "target", "target_kind", "params",
    "input_fingerprint", "result_fingerprint", "content_fingerprint",
    "status", "exit_code", "error_signature", "error_summary",
    "output_size_bytes", "output_size_source", "output_truncated",
    "duration_ms", "duration_source",
    "resource_state", "result_paths", "usage",
    "session_meta", "warnings", "evidence",
)


def new_event_id() -> str:
    """Identifiant aleatoire (32 hex). Pas d'import uuid pour limiter le cout de demarrage."""
    return os.urandom(16).hex()


def now_iso(t: float | None = None) -> str:
    """Horodatage UTC ISO 8601 a la milliseconde."""
    t = time.time() if t is None else t
    frac = int((t - int(t)) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{frac:03d}Z"


def empty_event() -> dict[str, Any]:
    """Evenement vide : toutes les valeurs a None/vides, jamais de defaut trompeur."""
    ev: dict[str, Any] = {k: None for k in EVENT_FIELDS}
    ev["schema_version"] = SCHEMA_VERSION
    ev["event_id"] = new_event_id()
    ev["source"] = SOURCE_HOOK
    ev["received_time"] = now_iso()
    ev["received_time_ns"] = time.time_ns()
    ev["params"] = {}
    ev["warnings"] = []
    ev["evidence"] = {}
    return ev


def validate_event(ev: Any) -> list[str]:
    """Validation structurelle minimale (cote analyse). Retourne la liste des problemes."""
    problems: list[str] = []
    if not isinstance(ev, dict):
        return ["event is not an object"]
    for key in ("schema_version", "event_id", "client", "phase", "received_time"):
        if not ev.get(key):
            problems.append(f"missing {key}")
    if ev.get("schema_version") not in COMPATIBLE_SCHEMA_VERSIONS:
        problems.append(f"schema_version {ev.get('schema_version')!r} not in {COMPATIBLE_SCHEMA_VERSIONS}")
    if ev.get("params") is not None and not isinstance(ev.get("params"), dict):
        problems.append("params must be an object")
    return problems
