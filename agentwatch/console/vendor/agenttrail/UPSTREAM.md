# AgentTrail Map

Source: https://github.com/sodiumsun/agenttrail
Revision: e4ba2daa270abd7ecb51565bbfb12386f47ea418
License: MIT, Copyright (c) 2026 Kelly Sun (see LICENSE).

Forked for the AgentWatch project console. The map renderer and filesystem watcher
are retained. Setup/hook/spawn APIs, sibling discovery and automatic initialization
are disabled. State is stored under the selected project's .agentwatch folder.
File notifications are appended to this project's journal, and watcher mode/errors
are exposed to the parent. Historical hook sessions are not replayed into the map.
AgentWatch supplies correlated sessions, tools, MCP names and measured durations
in its surrounding UI.
The console owns project selection, history, analysis, timeline and chat.
