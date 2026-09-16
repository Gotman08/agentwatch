# Schema d'evenement (version 1.0)

Chaque evenement est un objet JSON ecrit par le hook. Toute donnee absente vaut `null` ;
un statut absent est `unknown`, jamais `success`.

| Champ | Type | Sens |
|---|---|---|
| `schema_version` | str | `"1.0"` |
| `event_id` | str | 32 hex ; aleatoire pour un hook, derive du contenu pour un rejeu |
| `source` | str | `hook`, `replay`, `import` |
| `client` | str | `claude-code`, `codex` |
| `client_version` | str/null | jamais renseigne par les hooks ; la version relevee a `configure` est dans le rapport |
| `model` | str/null | tel que fourni par le client (Codex : sur chaque evenement ; Claude Code : `SessionStart` seulement) |
| `session_id`, `turn_id`, `agent_id`, `agent_type` | str/null | identifiants du client |
| `call_id` | str/null | `tool_use_id` |
| `phase` | str | `start`, `end`, `failure`, `interrupt`, `observation`, `session_start`, `session_end`, `turn_start`, `turn_end`, `compact_start`, `compact_end`, `subagent_start`, `subagent_stop`, `unknown` |
| `hook_event_name` | str | nom d'origine (`PreToolUse`, ...) |
| `event_time` | str/null | horodatage fourni par le client (aucun des deux clients n'en fournit en V1) |
| `received_time`, `received_time_ns` | str, int | horodatage de reception par le hook (base d'ordre) |
| `tool_name` | str/null | nom d'origine |
| `tool_category` | str | `read`, `search`, `list`, `edit`, `write`, `shell`, `mcp`, `agent`, `web`, `other`, `unknown` |
| `mcp_server`, `mcp_tool` | str/null | pour `mcp__<serveur>__<outil>` |
| `project_dir`, `cwd` | str/null | dossier utilisateur remplace par `~` |
| `target`, `target_kind` | str/null | cible normalisee : `path` (relative au projet), `command` (texte normalise, masque), `pattern`, `url` (sans requete), `mcp`, `patch`, `unknown` |
| `params` | objet | parametres autorises (liste par outil) ; autres cles : `_fp` = empreinte courte de la valeur |
| `input_fingerprint`, `result_fingerprint` | {method, value}/null | HMAC-SHA256 du JSON canonique |
| `status` | str/null | `success`, `error`, `denied`, `interrupted`, `timeout`, `unknown` |
| `exit_code` | int/null | seulement si le client l'expose (aucun des deux en V1) |
| `error_signature`, `error_summary` | str/null | signature normalisee (nombres, chemins, hex remplaces) ; resume masque et borne |
| `output_size_bytes`, `output_size_source` | int/null, str | taille du `tool_response` serialise tel que recu |
| `output_truncated` | bool/null | si le client le signale, ou si stdin a ete tronque |
| `duration_ms`, `duration_source` | int/null, str | `client`, `client_mcp_durationMs` ; la reconstruction entre hooks est faite a l'analyse |
| `resource_state` | {revision, source, freshness}/null | pour `Read` (Claude Code) : empreinte du contenu lu |
| `result_paths` | liste/null | chemins retournes par un listage/recherche (borne) |
| `usage` | objet/null | tokens importes (`import-usage`), jamais deduits |
| `session_meta` | objet/null | `start_type`, `end_type`, `compact_type`, ... |
| `warnings` | liste | limites rencontrees pour cet evenement |
| `evidence` | objet | `payload_keys`, `tool_response_keys`, `stdin_bytes`, `hook_ms`, indices (`error_hint`, `status_basis`, ...) |

## Correspondance des evenements

| Client | Evenement de hook | Phase |
|---|---|---|
| Claude Code | PreToolUse / PostToolUse / PostToolUseFailure | start / end / failure |
| Claude Code | SessionStart / SessionEnd | session_start / session_end |
| Claude Code | UserPromptSubmit / Stop | turn_start / turn_end |
| Claude Code | PreCompact / PostCompact | compact_start / compact_end |
| Claude Code | SubagentStart / SubagentStop | subagent_start / subagent_stop |
| Codex | PreToolUse / PostToolUse | start / end |
| Codex | SessionStart / SessionEnd | session_start / session_end |
| Codex | UserPromptSubmit / Stop / Interrupt | turn_start / turn_end / interrupt |
| Codex | PreCompact / PostCompact | compact_start / compact_end |
| Codex | SubagentStart / SubagentStop | subagent_start / subagent_stop |

Le contenu des prompts n'est jamais conserve (seulement `evidence.prompt_chars`).

## Parametres conserves par outil (Claude Code)

Read : offset, limit, pages. Grep : pattern, path, glob, type, output_mode, options. Glob :
pattern, path. Bash : commande normalisee et masquee, timeout, run_in_background, classe
`shell_kind` (read/write/run/unknown), tetes de commande, chemins reconnus. Edit/Write :
chemin, longueurs et empreintes des contenus (jamais les contenus). WebFetch : URL sans
requete. Agent/Task : type de sous-agent, modele. MCP : cles de la liste
`mcp_param_allowlist` en clair (bornees), les autres en empreinte.

## Parametres conserves par outil (Codex)

Bash/shell : commande normalisee et masquee, timeout, workdir. apply_patch : chemins
extraits des en-tetes `*** Update/Add/Delete File`, longueur et empreinte du patch. MCP :
comme ci-dessus.

## Format d'import d'usage (`agentwatch import-usage`)

Fichier JSONL, une observation par ligne :

```json
{"client": "claude-code", "session_id": "...", "scope": "call|turn|session", "tool_use_id": "optionnel",
 "model": "optionnel", "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
 "cache_creation_tokens": 0, "source": "otel-collector|manuel", "timestamp": "optionnel"}
```

Les compteurs sont pris tels quels (jamais convertis depuis des octets) et affiches avec
leur source et leur perimetre. Sans import, le rapport affiche « non mesure ».
