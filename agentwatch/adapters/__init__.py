"""Adaptateurs propres a chaque client (Claude Code, Codex).

# * Chaque adaptateur transforme un payload de hook (dict brut) en evenement du schema
#   commun. Il n'applique PAS le masquage : c'est le role de collector/ingest.py.
"""

from __future__ import annotations

from agentwatch import CLIENT_CLAUDE_CODE, CLIENT_CODEX

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Protocol

    class Adapter(Protocol):
        client: str

        def parse_hook_payload(self, payload: dict[str, Any], ctx: "AdapterContext") -> dict[str, Any]:
            """Retourne un evenement partiel (cles du schema) a partir du payload brut."""
            ...


class AdapterContext:
    """Contexte fourni par l'ingestion : configuration et fonction d'empreinte."""

    __slots__ = ("config", "fp", "project_dir_env")

    def __init__(self, config: dict[str, Any], fp: Any, project_dir_env: str | None) -> None:
        self.config = config
        self.fp = fp                      # Callable[[str], str] : empreinte HMAC hex
        self.project_dir_env = project_dir_env


def get_adapter(client: str) -> Adapter:
    """Import paresseux : le hook ne charge que l'adaptateur du client concerne."""
    if client == CLIENT_CLAUDE_CODE:
        from agentwatch.adapters import claude_code
        return claude_code.ClaudeCodeAdapter()
    if client == CLIENT_CODEX:
        from agentwatch.adapters import codex
        return codex.CodexAdapter()
    raise ValueError(f"client inconnu : {client!r}")
