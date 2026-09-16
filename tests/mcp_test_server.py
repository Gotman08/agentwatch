"""Serveur MCP de test minimal (stdio, JSON-RPC 2.0, sans dependance).

Expose un seul outil `echo_path(path)` qui renvoie "echo: <path>". Sert uniquement a
verifier que les appels MCP declenchent bien les hooks des deux clients (smoke test).
# ! Ne rien ecrire d'autre sur stdout : le protocole MCP y transite.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2025-06-18"
TOOLS = [{
    "name": "echo_path",
    "description": "Returns 'echo: <path>' (AgentWatch test server).",
    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}]


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentwatch-test", "version": "0.1.0"}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            args = msg.get("params", {}).get("arguments", {}) or {}
            name = msg.get("params", {}).get("name")
            if name != "echo_path":
                _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"unknown tool {name!r}"}})
                continue
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": f"echo: {args.get('path')}"}], "isError": False}})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif msg_id is not None:
            _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}})
        # notifications (initialized, cancelled...) : ignorees
    return 0


if __name__ == "__main__":
    sys.exit(main())
