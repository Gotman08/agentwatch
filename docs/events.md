# Schema d'evenement (version 1.1)

Chaque evenement est un objet JSON ecrit par le hook. Toute donnee absente vaut `null` ;
un statut absent est `unknown`, jamais `success`. Les evenements 1.0 restent lus (le
champ ajoute en 1.1, `content_fingerprint`, vaut alors `null`).

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
| `phase` | str | `start`, `end`, `failure`, `interrupt`, `observation`, `usage` (usage de session importe : un marqueur, jamais un appel), `session_start`, `session_end`, `turn_start`, `turn_end`, `compact_start`, `compact_end`, `subagent_start`, `subagent_stop`, `unknown` |
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
| `content_fingerprint` | {method, value, chars}/null | schema 1.1 : HMAC du texte principal normalise (contenu lu, stdout, resultats, blocs MCP) ; identique entre outils pour un meme texte |
| `status` | str/null | `success`, `error`, `denied`, `interrupted`, `timeout`, `unknown` |
| `exit_code` | int/null | seulement si le client l'expose (aucun des deux en V1) |
| `error_signature`, `error_summary` | str/null | signature normalisee (nombres, chemins, hex remplaces) ; resume masque et borne |
| `output_size_bytes`, `output_size_source` | int/null, str | taille du `tool_response` serialise tel que recu |
| `output_truncated` | bool/null | si le client le signale, ou si stdin a ete tronque |
| `duration_ms`, `duration_source` | int/null, str | `client`, `client_mcp_durationMs` ; la reconstruction entre hooks est faite a l'analyse |
| `resource_state` | {revision, source, freshness}/null | pour `Read` (Claude Code) : empreinte du contenu lu |
| `result_paths` | liste/null | chemins retournes par un listage/recherche (borne) |
| `usage` | objet/null | tokens rapportes par le client (Claude Code : reponse de l'outil `Agent`, portee `agent`, observe en 2.1.270), lus dans le transcript (`import-transcripts`, source `claude-code:transcript`, portee `call` ou `session`) ou importes (`import-usage`) ; jamais deduits |
| `session_meta` | objet/null | `start_type`, `end_type`, `compact_type`, `transcript_path` (Claude Code, masque par `~`), ... |
| `warnings` | liste | limites rencontrees pour cet evenement |
| `evidence` | objet | `payload_keys`, `tool_response_keys`, `stdin_bytes`, `hook_ms`, indices (`error_hint`, `status_basis`, ...) |

## Sous-agents (Claude Code)

Le rapport contient une section `agents` : identifiant, type, classification
(`subagent` : demarrage et/ou appels observes ; `stop_only` : seulement un `SubagentStop`,
sans appel, agent interne du client ou demarre avant l'installation des hooks), appels,
debut et fin, appel `Agent` parent et base du lien :

- `exact:tool_response.agentId` : la reponse de l'outil `Agent` porte l'identifiant du
  sous-agent (observe sur 2.1.270, avec `agentType`, `resolvedModel`, `status`,
  `toolStats`, `totalDurationMs`, `totalTokens`, `usage`) ;
- `temporal_containment (heuristique)` : un seul appel `Agent` du fil principal englobe
  la vie du sous-agent ;
- `ambiguous (...)` : plusieurs appels `Agent` candidats, aucun lien retenu.

Les detecteurs ne comparent jamais deux agents entre eux. La cle `duration_ms` du
`PostToolUse` (observee en direct ; la documentation cite `duration`) fournit la duree client.

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
leur source et leur perimetre. Sans import, le rapport affiche « non mesure ». Une ligne
sans `tool_use_id` devient un marqueur `usage` de session, jamais un appel fictif.

## Import des transcripts Claude Code (`agentwatch import-transcripts`)

Format observe en direct (Claude Code 2.1.275) dans `~/.claude/projects/<projet>/<session>.jsonl`,
ou `<projet>` est le dossier du projet avec tout caractere non alphanumerique remplace par
`-` (`G:\UnrealEngine\Unearthed` -> `G--UnrealEngine-Unearthed`) ; le chemin exact est
aussi transmis par le hook `SessionStart` (`session_meta.transcript_path`, prioritaire).
Les sous-agents ont leur propre fichier `<session>/subagents/**/agent-<id>.jsonl`
(`agentId` sur chaque ligne, `isSidechain: true`).

- Une reponse API = plusieurs lignes `assistant` (un bloc de contenu par ligne) avec le
  meme `requestId` et le meme `message.usage` (`input_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `output_tokens`) : dedoublonnage par `requestId`.
- Les blocs `tool_use` (`id` = `tool_use_id` des hooks) sont emis par une requete ; les
  blocs `tool_result` (lignes `user`, un par ligne quand les appels sont paralleles) sont
  consommes par la requete `assistant` suivante.
- Attribution par appel : part de l'entree non mise en cache (`input` + `cache_creation`)
  de la requete consommatrice, partagee a parts egales entre les resultats consommes
  ensemble, plus la part de la sortie de la requete emettrice. Cette entree non mise en
  cache contient aussi ce qui s'est ajoute au contexte au meme moment (rappels systeme,
  sortie precedente) : c'est le cout reellement paye, pas le poids exact du seul resultat.
  Un `tool_use_id` sans evenement de hook (appel anterieur a l'installation) est seulement
  compte dans l'usage de session ; aucun appel fictif n'est cree.
- Chaque observation a un identifiant derive de son contenu : reimporter un transcript qui
  a grandi n'ajoute que le nouveau. Les evenements importes ne changent pas les dates de
  debut et de fin de la session.
- Rien d'autre que des nombres et des identifiants n'est lu : ni prompt, ni reponse, ni
  resultat d'outil. Rollouts Codex : non lus.
