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
| `phase` | str | `start`, `end`, `failure`, `interrupt`, `observation`, `usage` (usage de session importe : un marqueur, jamais un appel), `session_start`, `session_end`, `turn_start`, `turn_end`, `compact_start`, `compact_end`, `subagent_start`, `subagent_stop`, `message` (import de rollout : role, longueurs, empreintes ; jamais le texte), `activity` (import de rollout : fait de session sans appel ni message, `session_meta.kind` = `settings`, `subagent_activity`, `reasoning`, `compaction_item`, `realtime`, `quota` ; types, identifiants, tailles), `unknown` |
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
  resultat d'outil.

## Import des rollouts Codex (`agentwatch import-rollouts`)

Sans effet sur Codex : lecture seule de `~/.codex/sessions/AAAA/MM/JJ/rollout-*.jsonl` (ou
`health.codex_sessions_dir`), incrementale (etat par fichier dans `<home>/import/codex-rollouts.json` :
octets deja lus et contexte en cours ; seules les lignes completes nouvelles sont lues), en priorite
d'arriere-plan (processeur et disque sous Windows). Faite automatiquement avant `sessions`, `report` et
`trends` (`rollouts.auto_import`, fenetre `rollouts.days` = 7 jours) ; `--follow` suit en direct.
Un fil repris ecrit dans le rollout de son jour de creation : la fenetre porte sur l'activite du fichier,
pas sur le dossier. Sous Windows, la date de modification d'un rollout que Codex garde ouvert reste celle de
sa creation (constate le 2026-09-19 : 16:36:20 alors que Codex ecrivait a 17:18:49) : un fichier plus ancien
que la fenetre y entre quand meme s'il a grandi depuis la derniere lecture, si sa derniere ligne lue est
recente ou, jamais lu, si l'heure de sa derniere ligne ecrite l'est (fin du fichier seulement). Une session
touchee dans la fenetre est lue en entier : ses premiers sous-agents, termines, sont souvent dans des
fichiers plus anciens (constate le 2026-09-19 : 41 fils sur 71 d'une session commencee 9 jours plus tot).

Format observe (codex-cli 0.153.4 a 0.155.0-alpha.9.2, rollouts du 2026-09-14 au 2026-09-19) : une
ligne JSON `{timestamp, ordinal, type, payload}`.

| Ligne du rollout | Evenement AgentWatch |
|---|---|
| `session_meta` du fil (le premier ; un sous-agent recopie ensuite celui de son parent) | `session_start` (fil principal) ou `subagent_start` (parent, role, surnom, `agent_path`, profondeur) |
| `turn_context` | modele, dossier et `turn_id` courants |
| `event_msg/task_started`, `task_complete`, `turn_aborted` | `turn_start`, `turn_end` (duree, delai du premier token), `interrupt` (raison). Un `task_complete` qui porte une erreur (`error.codex_error_info` : `usage_limit_exceeded`, `server_overloaded`, `cyber_policy`, `other` ; 19 tours sur 4 386 au 2026-09-21) est un tour COUPE par le client, pas un travail termine : `turn_end` garde la categorie dans `error_kind` (jamais le message) ; `compare` le compte comme une INTERRUPTION par le client, nommee a part de celles de l'utilisateur et gardee au denominateur du taux de taches terminees : ni tache terminee, ni ligne qui disparait du bilan |
| `compacted` | `compact_end` (numero de fenetre, rang de la reponse ou elle tombe `response_index`, entree de la derniere reponse avant elle `input_tokens_before`, `compaction_response_id`, taille de l'historique de remplacement `replacement_items`) : Codex a remplace l'historique ; nouvelle epoque de contexte de CET agent (les epoques sont propres a chaque agent : la compaction d'un sous-agent ne vide pas le contexte du fil principal). Seule cette ligne etablit le remplacement de l'historique |
| `event_msg/item_completed` : `CommandExecution`, `McpToolCall`, `FileChange`, `ImageView`, `Extension`, `WebSearch` | un appel (debut a `started_at_ms`, fin a `completed_at_ms`) : `Bash`, `mcp__<serveur>__<outil>`, `apply_patch`, `view_image`, `web_search` (requete jamais conservee ; `evidence.web_action` : `search`, `open_page`, `find_in_page`, `other` ; resultats : taille et empreinte). Un element `WebSearch` de meme identifiant qu'un appel de fonction `web.run` (210 fois sur 210, avant ou apres sa sortie) ne fait pas un second appel : il le complete (`web_action`, nombre de resultats `web_results`) sans changer son debut, sa fin ni son statut, qui viennent de la fonction |
| `event_msg/item_completed:FunctionCallOutput` | un appel `codex_app.<nom>` (`send_message_to_thread`, `automation_update`, `create_thread` : fonctions de l'application Codex sans ligne `function_call`) : sortie seulement (taille, empreinte, statut par texte d'erreur), pas d'arguments |
| `event_msg/item_completed:CollabAgentToolCall` | meme identifiant qu'un appel de fonction deja lu (818 fois sur 831 au 2026-09-19) : le complete (`evidence.collab_receivers`, `collab_status`, `collab_sender`), pas de second appel ; sinon (appel fait depuis un script `exec`) : un appel `collaboration.<outil>` et, s'il porte une consigne (`prompt`), un marqueur `message` de role `agent_instruction` |
| `response_item/function_call` et `function_call_output` | un appel : `collaboration.send_message`, `collaboration.spawn_agent`, `wait`, ... ; un appel de fonction MCP et son item `McpToolCall` de meme identifiant forment un seul appel |
| `response_item/tool_search_call` et `tool_search_output` | un appel `tool_search` (requete jamais conservee ; `evidence.result_count` : groupes d'outils rendus, `result_tools` : outils qu'ils contiennent ; la taille entiere des definitions rendues compte pour la consommation) |
| `response_item/web_search_call`, `image_generation_call` | meme identifiant que l'element `WebSearch` ou `Extension` (`image_gen.generation`) ecrit juste avant (32 fois sur 32 au 2026-09-19) : doublon reconnu, pas un second appel ; sinon un appel `web_search` (`web_action`, adresse et requete en empreinte seulement) ou `image_generation` (taille de l'image `image_chars`) ; outils heberges par le fournisseur du modele : resultat consomme dans la meme reponse, sans part de tokens ; statut `generating` d'une image rendue : pas un echec |
| `token_usage_record` | usage de la reponse (voir ci-dessous) ; plus un releve `usage` de portee `response` par reponse : rang, identifiant, fenetre de contexte, entree (dont cache), sortie (dont raisonnement), sorties consommees et appels emis, `format`, et `reasoning` (voir plus bas). Ce releve n'entre pas dans les totaux (ceux des fils font foi) : il sert au cout de residence en contexte. Le releve designe par `compaction_response_id` d'une ligne `compacted` est une DEMANDE de compaction (requete au modele pour resumer l'historique : des tokens reels, pas une reponse a la conversation), comptee a part par les rapports ; la ligne `compacted`, et elle seule, etablit que l'historique a ete remplace |
| `event_msg/token_count` | fil sans aucun `token_usage_record` (rollouts de juin a aout) : seule mesure, releve `usage` de portee `response` au `format` `token_count` (`last_token_usage`) ; un releve dont le cumul du fil n'a pas bouge (quota seulement : 822 fois) n'est pas une reponse. Fil avec des `token_usage_record` (qui viennent toujours en premier : 189 fichiers sur 189) : meme mesure, doublon reconnu. Les demandes de compaction ne sont pas mesurees dans ce format. Quota du compte (`rate_limits.primary`), lu sur chaque releve, doublon ou non : marqueur `activity` de sorte `quota` (`used_percent`, `window_minutes`, `resets_at`, `limit_id` ; des nombres) quand le pourcentage consomme change d'au moins un point (constate le 2026-09-21 : une session de 12 h 27 a porte le quota hebdomadaire de 19 a 100 %, puis Codex a coupe ses 4 fils) |
| `response_item/reasoning` et `event_msg/item_completed:Reasoning` | pas d'evenement par bloc (100 000 blocs) : tailles rattachees au releve `usage` de la reponse qui suit, `usage.reasoning` = `blocks`, `summary_chars`, `encrypted_chars` (lignes response_item), `item_blocks`, `item_summary_chars`, `item_raw_chars` (elements), `lines` (lignes sources, 20 au plus, puis `lines_more`). Un raisonnement sans releve apres lui dans le tour : marqueur `activity` de sorte `reasoning` a la fin du tour. Jamais le texte |
| `event_msg/thread_settings_applied` | marqueur `activity` de sorte `settings` : modele, fournisseur, niveau de service, effort et resume du raisonnement, politique d'approbation, relecteur, personnalite, mode de collaboration (valeurs courtes, en clair) ; le modele devient le modele courant |
| `inter_agent_communication_metadata` | `trigger_turn` (le message suivant entre agents declenche-t-il un tour) sur le marqueur `message` de role `agent` qui suit |
| `realtime_item` | marqueur `activity` de sorte `realtime` (mode vocal) : type (`transcript_segment`, `bem_item_promoted`, ouverture, fermeture), role, issue, identifiants, longueur du texte transcrit (jamais le texte) |
| `response_item/message`, `agent_message`, arguments `message` des fonctions de collaboration | marqueur `message` : role, identifiant du message, tour, longueur, empreintes et longueurs des paragraphes ; pour un message `user`, blocs injectes par Codex (`AGENTS.md`, balises) exclus et comptes (`injected_chars`, `injected_blocks`) ; pour un message `user`, `agent` ou une consigne a un sous-agent, `paragraph_sigs` : une signature de similarite par paragraphe (MinHash, 32 valeurs de 16 bits, sur les mots sans accents tronques a 6 lettres, hachage cle), pour reconnaitre une consigne reformulee ; pour un message `assistant`, `declared` : categories de raison annoncees (`retry`, `wait`, `unavailable`, `in_progress`, `verify`, `after_change`, `fix`, `explore`), reconnues par motifs ; pour un message entre agents, des deux cotes (argument `message`/`task`/`prompt` de l'appel de collaboration a l'envoi, `encrypted_content` ou texte apres « Payload: » de la ligne `agent_message` a la reception) : `payload_fp` (empreinte du contenu transmis, identique a l'envoi et a la reception : c'est elle qui rapproche un envoi de sa reception), `payload_chars`, `encrypted` (jeton chiffre par le fournisseur : ni paragraphes ni `paragraph_sigs`, le texte n'existe pas en clair dans le rollout) ; a la reception, `message_kind` (`MESSAGE`, `NEW_TASK`, `FINAL_ANSWER`, lu dans l'en-tete) ; pour une commande en echec, `error_summary` est la fin de la sortie APRES retrait des lignes de compte rendu qu'un post-traitement y ajoute (`{"resumes_ecrits": ...}`, `Etat courant regenere` ; motifs : `error_trailer_patterns`) : constate le 2026-09-21, ces lignes prenaient la place du diagnostic (`"ExitCode": 6` juste au-dessus). `evidence.error_trailer_lines_removed` compte les lignes retirees ; s'il ne reste rien, `error_summary` est vide et `evidence.error_cause` dit « unknown » : l'echec reste un echec, de cause inconnue. Pour un import anterieur, un resume stocke qui n'est qu'une telle ligne est lu comme cause inconnue (ou partielle), avec un avertissement ; un reimport cible la retrouve ; sur le marqueur `compact_end` d'une ligne `compacted` : `replacement_items` (elements gardes) et `replacement_agent_messages` (parmi eux, messages d'autres agents gardes tels quels : des comptes, jamais leur contenu) |
| `world_state` | marqueur `message` de role `context` : longueur, empreinte et taille d'`AGENTS.md` et des skills injectes |
| `event_msg/item_completed:AgentMessage`, `UserMessage` | le plus souvent le meme message qu'une ligne `response_item` voisine (27 991 fois sur 29 705 au 2026-09-19, 3 lignes d'ecart au plus, avant ou apres ; texte identique ou inclus) : doublon reconnu. Sinon, seule trace du message (1 279 messages d'agent, dont 1 221 de sous-agents) : marqueur `message` (`origin` = `element AgentMessage`) rattache a la ligne de l'element, apres 10 lignes sans ligne de meme texte ou quand le fichier, lu jusqu'au bout, n'a plus ete ecrit depuis 10 min ; en attendant, l'element est "en attente de rapprochement" (empreinte et longueurs seulement dans l'etat) |
| `event_msg/item_completed:SubAgentActivity` (`completed`, `interrupted`) | `subagent_stop` |
| `event_msg/item_completed:SubAgentActivity` (`started`, `interacted`) | marqueur `activity` de sorte `subagent_activity` (fil enfant, activite, `agent_path`) : vue du parent ; pas un `subagent_start` (celui-ci vient du `session_meta` du fil enfant) |
| `event_msg/item_completed:ContextCompaction` | marqueur `activity` de sorte `compaction_item` (identifiant seulement ; sans statut, il n'etablit rien a lui seul) |
| tout autre type de ligne, d'evenement ou d'element | aucun evenement ; compte par type dans l'etat (`uninterpreted`) et affiche par `doctor` : jamais perdu en silence |

Source de chaque evenement : `evidence.source` = `{file, line, offset}` (nom du rollout, numero de ligne a partir
de 1, octet de debut), les memes numeros `L<n>` que l'export `inspect`. Un marqueur la porte dans `meta.source` ;
un appel dans `evidence.source_start` (ligne qui l'ouvre), `source_end` (ligne qui le ferme : resultat, statut,
duree) et `source_observations` (releve de tokens de la reponse qui a consomme sa sortie, element complementaire ;
5 au plus). Seul le total d'un fil (`usage` de portee `thread`) n'a pas de ligne unique. Un etat de lecture
anterieur aux numeros de ligne est complete une fois (comptage des lignes deja lues).

Trois etats distincts, affiches par `doctor` : importe (lu et traduit en evenements), en attente (octets pas
encore lus, avec le retard entre la derniere ligne ecrite par Codex et la derniere lue ; elements de message en
attente de rapprochement) et lu mais non interprete (compte par type). Les doublons reconnus (`duplicates` :
meme fait deja importe sous une autre forme, ou element vide) ne sont pas des pertes.

- Le modele appelle `exec` (du code) ou une fonction ; les actions imbriquees d'un `exec` portent un
  identifiant `exec-<uuid>`, le meme que `tool_use_id` dans les hooks Codex. `exec` lui-meme n'est pas
  un appel AgentWatch : chaque action garde `evidence.exec_call_id`.
- Statut : code de sortie de chaque commande (`exit_code`, `status: failed`), `isError` ou
  `status: failed` d'un appel MCP ; fonctions de collaboration : succes sans texte d'erreur, erreur
  sinon (`wait` rend la sortie d'une commande en cours : son texte ne decide pas du statut).
- Duree : `duration` de l'item (source `client`).
- Horodatages non fiables : constate le 2026-09-19, 190 rollouts de juin a aout ont toutes leurs lignes a la
  meme milliseconde (reecrits d'un bloc par Codex, sans releve de tokens). Un fil dont tous les appels et
  marqueurs tiennent en moins de 2 s, avec au moins 3 appels, est signale (`timing_unreliable_agents`,
  avertissement de lecture) : ses durees, intervalles et cadences sont ignores (detecteur G). 117 fils de 8
  sessions sont concernes ; leurs appels, statuts et empreintes restent valables.
- Faits de resultat (detecteur G), pour tout appel sauf les editions : `evidence.result_phase`
  (`unavailable`, `in_progress`, `failed`, `done`, `unknown`) et `evidence.state_fp` (empreinte HMAC de
  l'etat : JSON canonique ou texte, horodatages, durees et compteurs de temps ecoule neutralises ; `empty`
  pour une sortie vide ; absente pour un contenu lu, dont l'empreinte de contenu fait foi). Calcules de la
  meme facon par les hooks Claude Code et Codex. Une attente arrivee a echeance garde `evidence.timed_out`.
- Ce que le modele recoit n'est pas ce que l'outil produit (constate le 2026-09-21, session `01a0bf95`) :
  `output_size_bytes` reste la taille BRUTE de la sortie ; s'y ajoutent `evidence.delivered_chars` (longueur de
  `formatted_output`, ce qu'`exec_command` rend au script), `output_truncated` et `evidence.original_token_count`
  quand Codex a coupe la sortie de la commande (`Warning: truncated output (original token count: N)` : 54
  commandes, jusqu'a 195 820 tokens d'origine), et, sur l'observation d'usage de chaque action d'un exec,
  `evidence.exec_delivered_chars`, `exec_images` et `exec_truncated_tokens` (sortie entiere de l'exec coupee AU
  MILIEU au-dela de 12 000 tokens, `...N tokens truncated...` entre points de suspension typographiques : 252 execs
  sur 3 222 ; la fin d'une sortie et le debut de la suivante sont perdus). Un resultat MCP qui contient une image
  encodee (directe, ou dans le JSON d'un bloc de texte) garde `evidence.image_parts` et `text_chars`. Le partage
  des tokens d'une reponse entre les actions d'un exec se fait au prorata de ce qui est LIVRE (texte rendu ; 6 000
  caracteres nominaux par image, mesuree entre 1 088 et 2 265 tokens), plus au prorata du brut. Effet mesure sur
  cette session : 45 appels dont la part change de plus de moitie, 86 372 tokens deplaces sur 20,8 M (0,4 %), somme
  inchangee ; statuts, tailles brutes et empreintes inchanges (6 049 appels sur 6 049).
- Delais demandes (`timeout_ms`, `yield_time_ms`, `timeout_seconds`, `poll_seconds`...) : un nombre, garde en
  clair meme hors liste blanche, et exclu de la cle de comparaison des appels.
- Commandes : `pwsh.exe -Command <script>` ; seul le script est analyse, comme une commande de hook
  (normalisation, masquage des secrets, traduction en unite de travail). Dossier : URL `file:///` ou
  prefixe `\\?\` convertis.
- Tokens : une reponse du modele porte au plus un appel de haut niveau (observe). Par appel : part de
  l'entree non mise en cache (`input_tokens - cached_input_tokens`) de la reponse qui a consomme sa
  sortie, au prorata de la taille des sorties consommees ensemble, plus la part de la sortie de la
  reponse qui l'a emis ; pour un `exec`, repartition sur ses actions au prorata de leurs sorties
  (parts entieres, somme exacte). `usage.emitter_request_id` sert aussi a C.batchable (appels emis
  dans une meme reponse). `usage.emitter_input_tokens` et `emitter_cached_input_tokens` : entree totale de
  la reponse emettrice (non partagee : a compter une fois par reponse), le contexte relu pour decider
  l'appel. Session : dernier releve de chaque fil (principal et sous-agents), sommes.
- Sous-agents : rattaches a la session du fil racine (`session_id` du `session_meta`), avec
  `agent_id` = identifiant de leur fil et `agent_type` = role.
- Identifiants d'evenement derives du contenu (fil + identifiant d'appel + phase) : un reimport ne
  cree pas de doublon. Les lots importes sont ecrits dans des segments ; au-dela de
  `rollouts.max_segments` (30) ils sont fusionnes et dedoublonnes.
