# Compatibilite et preuves

Date de verification : 2026-09-15. Machine : Windows 11 Pro 10.0.26200, Python 3.14.4
(`C:\Python314\python.exe`), Git Bash et PowerShell 5.1 disponibles, WSL Ubuntu (Python
3.12.3) present mais non utilise pour les tests.

Vocabulaire des statuts : **documente** (lu dans la documentation officielle), **teste sur
fixture** (payload synthetique ecrit d'apres la documentation), **verifie reellement**
(observe avec le client installe), **indisponible** (non observable dans cet
environnement).

## Sources officielles consultees

| URL de depart | URL finale (redirection officielle) | Contenu utilise |
|---|---|---|
| https://code.claude.com/docs/en/hooks | (pas de redirection) | evenements, champs d'entree, forme exec `command`+`args`, `matcher`, codes de sortie, emplacements des settings, Windows/PowerShell |
| https://code.claude.com/docs/en/monitoring-usage | (pas de redirection) | OpenTelemetry : `claude_code.tool_result` (`tool_use_id`, `duration_ms`, tailles), `claude_code.api_request` (tokens), pas d'export fichier |
| https://developers.openai.com/codex/hooks | https://learn.chatgpt.com/docs/hooks (308) | evenements, `hooks.json`, `commandWindows`, confiance des hooks, `PostToolUse` apres code non nul, outils herberges hors hooks |
| https://developers.openai.com/codex/config-advanced | https://learn.chatgpt.com/docs/config-file/config-advanced (308) | `notify`, `[hooks]` inline, `[otel]`, `CODEX_HOME` |
| https://learn.chatgpt.com/docs/config-file/config-reference | — | `features.hooks`, `history.persistence`, `mcp_servers.<id>.default_tools_approval_mode`, `approval_policy` |

Complement non officiel (issue publique) : sous Windows, `commandWindows` est passe a
PowerShell comme une instruction, d'ou l'operateur d'appel `&` (confirme par le smoke
test reel ci-dessous).

## Clients presents sur cette machine

| Client | Version | Emplacement | Etat |
|---|---|---|---|
| Claude Code (CLI du PATH) | 2.1.87 | `~/.local/bin/claude.exe` | non authentifiee (« Not logged in — Please run /login ») : aucun hook ne se declenche avant l'authentification |
| Claude Code (application de bureau) | 2.1.270 | `%APPDATA%\Claude\claude-code\2.1.270\claude.exe` | authentification portee par l'application, non transmise a un sous-processus (« OAuth session expired and could not be refreshed ») ; les hooks de session se declenchent quand meme |
| Codex (application de bureau) | codex-cli 0.154.0-alpha.6.2 | `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe` (absent du PATH) | authentifie ; `features.hooks` = stable, actif par defaut |

## Matrice de preuve

| Capacite | Claude Code | Codex |
|---|---|---|
| Installation des hooks (diff, apply, idempotence, retrait, reglages etrangers preserves) | teste sur fixture (unittest) ; forme exec `command`+`args` verifiee reellement sur 2.1.270 | teste sur fixture ; commande PowerShell `& '...'` verifiee reellement via surcharges `-c` (equivalentes a `hooks.json`) |
| `SessionStart` / `SessionEnd` | **verifie reellement** (2.1.270 : cles `cwd, hook_event_name, session_id, source, transcript_path` ; fin : `reason`) | **verifie reellement** |
| `UserPromptSubmit` / `Stop` | **verifie reellement** (`prompt`, `prompt_id`, `permission_mode`) ; `Stop` non observe (echec d'authentification avant reponse) | **verifie reellement** |
| `PreToolUse` / `PostToolUse` (shell) | **verifie reellement** depuis une session de l'application de bureau (2.1.270) : `Bash`, `Write`, `Read`, `Glob`, `Grep` ; `tool_use_id` = `toolu_...` ; `durationMs` observe sur `Glob` | **verifie reellement** : outil `Bash`, `tool_use_id` = `exec-<uuid>`, `turn_id`, `model` sur chaque evenement, `transcript_path`, `permission_mode` |
| Reponse de l'outil `Agent` | **verifie reellement** (2.1.270) : `agentId`, `agentType`, `resolvedModel`, `status`, `toolStats`, `totalDurationMs`, `totalToolUseCount`, `totalTokens`, `usage` -> lien exact parent/sous-agent et usage rapporte par le client (portee agent) | sans objet |
| Duree client sur `PostToolUse` | **verifie reellement** (2.1.270) : cle `duration_ms` (la documentation cite `duration`) ; `durationMs` dans la reponse de `Glob` | absent |
| Rechargement des hooks a chaud | **verifie reellement** (2.1.270) : un `.claude/settings.local.json` ecrit par `configure --apply` est pris en compte dans la session en cours, sans redemarrage | non teste |
| `PostToolUseFailure` | documente + teste sur fixture (`error`, `is_interrupt`) | n'existe pas (documente) |
| Code de sortie d'une commande | absent (documente : `stdout`, `stderr`, `interrupted`) | **absent, verifie reellement** : `tool_response` est une chaine de sortie sans code ; AgentWatch laisse le statut `unknown` et pose un indice textuel d'erreur (heuristique) |
| Appel MCP | documente (`mcp__<serveur>__<outil>`) + teste sur fixture | **verifie reellement** avec `tests/mcp_test_server.py` : `tool_response = {content, isError}` ; en mode `exec` il a fallu `mcp_servers.<id>.default_tools_approval_mode="approve"` (sinon refus « approval policy is never » et appel sans fin) |
| Duree fournie par le client | partiel : `duration` sur `PostToolUse` a partir de 2.1.267 (documente, non observe) | absent |
| Sous-agents | **verifie reellement** (2.1.270 : `SubagentStart`/`SubagentStop` avec `agent_type`, `agent_id` sur chaque appel du sous-agent, contexte separe du fil principal) | documente (`SubagentStart/Stop`) + fixture |
| Compaction / reprise | documente (`PreCompact`, `PostCompact`, `session_start_type`) + fixture | documente + fixture |
| Interruptions | documente (`is_interrupt`) + fixture | evenement `Interrupt` de session (documente + fixture) ; pas de statut par appel |
| Commandes longues / polling | un appel = un debut + une fin ; aucun evenement intermediaire (documente) | idem |
| Outils herberges (recherche web) | outils locaux `WebFetch`/`WebSearch` vus par les hooks (documente) | hors hooks (documente) |
| Usage de tokens | absent des hooks ; **verifie reellement** dans le transcript (2.1.275 : `message.usage` par requete, `requestId` repete sur les lignes d'une meme reponse, blocs `tool_use` / `tool_result`, sous-agents `agent-<id>.jsonl` avec `agentId`) via `import-transcripts` ; import JSONL (`import-usage`) | absent des hooks ; `[otel]` exporte vers OTLP seulement ; rollouts non lus |
| Confiance des hooks | non requise | **requise** : approbation via `/hooks` (empreinte de la definition) ; en `exec`, `--dangerously-bypass-hook-trust` ; etat par hook **verifie reellement** (0.155.0-alpha.9.2) via `codex app-server`, methode `hooks/list` (`trustStatus` : `untrusted`, `trusted`, `modified`, `managed`), lu par `doctor` |

## Rollouts Codex (lecture passive, sans hooks)

Verifie reellement le 2026-09-19 sur la session en cours (codex-cli 0.153.4 a 0.155.0-alpha.9.2, fil
principal et 3 sous-agents, 288 Mo de rollouts) : 24 742 lignes lues en 4,5 s, 14 410 evenements,
4 120 appels (fil principal 1 946, sous-agents 751, 584 et 807), chaque identifiant d'action du
rollout retrouve dans un appel (seules manquaient les lignes ecrites par Codex apres la lecture), aucun
doublon, aucune fin orpheline, aucun appel reste ouvert, aucun statut inconnu (3 912 succes,
208 erreurs issues des codes de sortie et de `isError`), usage en tokens attribue a 4 083 appels,
85 tours et 45 compactions. Relecture incrementale : 0 ligne quand rien n'a change.

Particularites observees : un appel de fonction MCP (`mcp__cua_repl.js`) produit aussi un item
`McpToolCall` de meme identifiant (un seul appel) ; un sous-agent recopie le `session_meta` de son
parent apres le sien ; le dossier de travail des commandes est une URL `file:///` ; GPT-6 Astra
n'emet qu'un appel de haut niveau par reponse et parallelise a l'interieur d'`exec`.

Ce que les rapports d'une telle session ne doivent pas dire (corrige le 2026-09-20). L'origine de chaque
evenement est enregistree (`Call.origin`, `SessionView.collection`) et l'en-tete l'annonce. Pour une session
lue dans les rollouts : aucune base de couverture ne cite de hook (table `CAPABILITIES_ROLLOUT`), la surcharge
des hooks est declaree sans objet, une duree reconstruite l'est « entre les horodatages du rollout » et la
version du client est celle ecrite dans son `session_meta`, pas celle relevee a l'installation des hooks.

Faits du rollout repris dans le tableau des agents : surnom et role (`session_meta.thread_spawn`), fil parent
(`parent_thread_id`), modele (`turn_context` du fil), tokens du fil (`token_usage_record`). L'arret d'un
sous-agent est ecrit dans le fil du PARENT (item `SubAgentActivity`, `kind: completed`) et signifie « tache
rendue » : un meme sous-agent peut en recevoir une autre ensuite (observe 5 arrets pour un sous-agent le
2026-09-20). Un arret n'est donc retenu comme fin que s'il suit le dernier appel de l'agent ; sinon la fin
est declaree inconnue. L'appel de lancement n'est relie par aucun identifiant : il reste « non etabli ».

## Smoke tests reels

Script : `python tests/live_smoke.py --client codex|claude-code`. Il cree un dossier
temporaire (README.txt, serveur MCP de test), lance le client avec les hooks AgentWatch
dans un dossier de donnees temporaire, puis resume les evenements. Il utilise le compte
deja connecte du client et ne modifie aucune configuration globale.

Codex (2026-09-15, deux executions, la seconde avec approbation MCP) : 12 evenements
(`SessionStart` 1, `UserPromptSubmit` 1, `PreToolUse` 4, `PostToolUse` 4, `Stop` 1,
`SessionEnd` 1), 4 appels correles (3 shell, 1 MCP), aucun incident de collecte, cout
dans le processus du hook 54 a 99 ms, duree du tour 39 s, 24 014 tokens rapportes par
Codex lui-meme (non collectes par AgentWatch).

Claude Code (2026-09-15) : impossible d'obtenir un tour complet (authentification), donc
**aucune preuve reelle de `PreToolUse`/`PostToolUse`/`PostToolUseFailure` sur cette
machine**. Preuve partielle avec 2.1.270 : 3 evenements de session enregistres, aucun
incident, cout 26 a 48 ms. Pour completer : executer `claude login` (ou `/login`) dans un
terminal, puis relancer le smoke test.

## Mesure de la surcharge

Protocole : `python -m agentwatch bench --runs 20` (sous-processus complet, payload
`PreToolUse` de 200 octets, dossier temporaire, machine peu chargee) et `python -m
agentwatch self-test`. Environnement ci-dessus.

| Mesure | Valeur |
|---|---|
| temps mural median du hook, 20 executions | 44,6 ms (max 60,1 ms) |
| interpreteur seul (`python -I -c pass`), meme protocole | 24,2 ms |
| meme mesure pendant une charge concurrente (smoke tests en cours), 5 executions | 168 a 202 ms |
| temps dans le processus (`evidence.hook_ms`), Codex reel | 54 a 99 ms |
| temps dans le processus, Claude Code 2.1.270 reel | 26 a 48 ms |

Chaque appel d'outil coute donc deux hooks (debut et fin), soit de l'ordre de 0,1 s a
0,4 s selon la charge, sans compter le temps de spawn propre au client (non mesure). La
surcharge n'est pas nulle et depend de la machine ; les valeurs ci-dessus sont des
echantillons, pas des garanties. `pythonw.exe` (defaut sous Windows) : 39 ms median sur
10 executions, identique a `python.exe`.

Cote analyse (pas dans le hook) : sur une session synthetique de 1 073 appels (2 148
evenements), la correlation prend 0,04 s et les quatre detecteurs 0,03 s, mais la
premiere lecture de 2 148 petits fichiers prenait 28 s (13 ms par premiere ouverture,
analyse antivirus Windows ; 0 ms a la seconde ouverture). D'ou la compaction automatique
en segment JSONL au-dela de `auto_compact_threshold` fichiers : 802 evenements relus en
23 ms apres compaction.

Robustesse du hook verifiee en sous-processus (code 0, stdout vide, evenement ou
incident enregistre) : JSON imbrique sur 3 000 niveaux, UTF-8 invalide, types inattendus
(identifiants non textuels), chemins accentues avec espaces, sortie de 2 Mo, stdin vide,
stdin tronque par `max_stdin_bytes`, depot casse (erreur d'import : incident `hook_crash`,
aucun contenu du payload).

## Observations de cycle de vie (Claude Code 2.1.270, session de bureau)

- Un appel `Edit` refuse a la validation de l'entree (« No changes to make », ancien et
  nouveau texte identiques) a emis `PreToolUse` mais ni `PostToolUse` ni
  `PostToolUseFailure` : l'appel reste `open` dans AgentWatch. Observe une fois ; un
  appel ouvert n'est donc pas forcement en cours.
- Un sous-agent interrompu par le chien de garde du client (« no progress for 600s »)
  n'a emis aucun `SubagentStop` ; sa reprise (`SendMessage`) a emis un second
  `SubagentStart` avec le meme `agent_id`. AgentWatch garde le premier debut, compte les
  reprises et considere l'agent en cours tant qu'aucun arret ne suit le dernier debut.

## Defauts Windows trouves par les tests repetes

- Descripteur `os.open` sans `O_BINARY` : mode texte, `0x0A` ecrit comme `0x0D 0x0A`.
  Consequence observee : cle HMAC de 33 octets une fois sur ~15 (coherente entre hooks
  car tous relisent le fichier, mais incorrecte). Corrige ; test deterministe ajoute.
- Course a la creation de la cle : `os.replace` pouvait ecraser la cle d'un hook
  concurrent, et un echec transitoire laissait un hook signer avec une cle jamais
  persistee. Corrige : creation sans ecrasement (`os.rename` Windows / `os.link` POSIX),
  relecture systematique du disque, incident journalise sinon. Test a 8 processus.
- `os.replace` refuse par un `PermissionError` transitoire (antivirus) : retente.

## Applications de bureau empaquetees (MSIX) : redirection d'AppData

Constate le 2026-09-18 avec Claude de bureau (paquet `Claude_pzs8sxrjxfjjc`). Windows
redirige les ecritures sous `%APPDATA%` et `%LOCALAPPDATA%` faites par l'application et ses
processus enfants vers `AppData\Local\Packages\<paquet>\LocalCache\...`. Consequences :

- Un `pip install --user` lance PAR L'AGENT installe le paquet dans ce dossier prive : il est
  visible des outils de l'agent, pas de votre terminal. Rich a ainsi ete "installe" sans
  l'etre pour l'utilisateur. Installez les dependances optionnelles depuis votre propre
  terminal (`python -m pip install rich`).
- Le dossier de donnees d'AgentWatch ne doit pas etre sous AppData, sinon les hooks (lances
  par l'application) et la commande `report` (lancee dans votre terminal) ne verraient pas
  les memes fichiers. Le defaut `~/.agentwatch` est hors AppData, donc non redirige ;
  `doctor` avertit si `--home` ou `AGENTWATCH_HOME` pointe sous AppData.

## Ce qui n'a pas ete verifie en direct

- Codex : le chargement de `~/.codex/hooks.json` est verifie (2026-09-19, codex-cli
  0.155.0-alpha.9.2, `hooks/list` : les 11 hooks AgentWatch vus, variante `commandWindows`,
  aucune erreur ; avertissement : delais de `SessionEnd` et `Interrupt` ramenes a 3 s), mais
  les 11 etaient `untrusted`, donc ignores : un fichier complet ne prouve pas la collecte,
  d'ou le controle de confiance de `doctor`. La portee projet `.codex/hooks.json` exige en
  plus que le projet soit marque de confiance.
- Codex : l'approbation d'un hook depuis l'application de bureau (2026-09-19, 11 h 50) n'a ete
  enregistree nulle part (ni `config.toml`, ni etat global, ni bases SQLite) : une instance fraiche
  voyait toujours les 11 hooks `untrusted` et le Codex en cours n'a execute aucun hook (19 actions,
  0 evenement). En `exec`, la confiance a ete contournee par `--dangerously-bypass-hook-trust`.
  D'ou la lecture des rollouts, qui ne depend d'aucune approbation.
- Claude Code en sous-processus (`claude -p`) : impossible faute d'authentification ;
  en revanche une session de l'application de bureau (2.1.270) observee en direct le
  2026-09-16 a fourni : 20 evenements, 9 appels du fil principal (Bash, Write, Glob) et
  4 appels d'un sous-agent `Explore` (Read x3, Grep), `UserPromptSubmit`, `Stop`,
  `SubagentStart`/`SubagentStop`, cout dans le hook 17,6 ms median avec `pythonw.exe`.
  Restent non observes : `PostToolUseFailure`, `PostToolUse.duration`, compaction,
  interruptions. Le lien entre un sous-agent et l'appel `Agent` qui l'a lance n'est pas
  fourni par les hooks ; d'autres identifiants d'agent apparaissent avec un seul
  `SubagentStop` sans type ni appel (agents auxiliaires internes probables).
- `pythonw.exe` : verifie avec des tubes stdin/stdout crees par Python ; non observe
  lance par Codex ou Claude Code eux-memes.

## Limites connues

- Les hooks n'observent pas : le contenu des raisonnements, la presence d'un resultat
  dans le contexte du modele, les modifications externes, les outils herberges de Codex,
  la sortie d'une commande en arriere-plan apres son lancement.
- Codex par les hooks : pas de code de sortie ni de duree ; statut d'une commande shell `unknown`
  (indice textuel seulement) ; approbation manuelle des hooks obligatoire. Par les rollouts (voie
  utilisee aujourd'hui) : code de sortie et duree presents, statut ecrit par le client.
- Erreurs : seule la fin de la sortie est conservee (400 caracteres, masques). Dans une chaine de
  commandes, l'etape en echec peut preceder cette fin. Le rapport distingue le statut de l'outil, le
  code de sortie de la commande et le resultat que le script ecrit dans sa propre sortie
  (`"ExitCode": 6` d'une compilation alors que la commande rend 1).
- Relecture de la source (2026-09-21) : quand la cause manque au resume, la ligne du rollout qui a
  produit l'appel est relue (`evidence.source_end`), bornee a 2 000 caracteres, secrets masques,
  source citee dans le rapport. Deux usages, tous deux en lecture seule et sans rien reimporter :
  completer une erreur non classee, et verifier qu'un code 1 de recherche est bien une absence de
  correspondance avant de le requalifier en succes. Aucune classification n'est forcee : sans forme
  reconnue dans la source, l'erreur reste non classee. Effet sur la session du 2026-09-20 : 34 non
  classees ramenees a 26, et 12 echecs `rg` sur 50 qui n'etaient plus comptes comme des erreurs le
  redeviennent (leur cause s'affichait avant les 400 derniers caracteres).
- Claude Code : `duration` et `prompt_id` dependent de la version (>= 2.1.267 et
  >= 2.1.196) ; le CLI du PATH est en 2.1.87.
- Linux / macOS / WSL : non executes.
- Journaux natifs : les transcripts Claude Code ne sont lus que pour l'usage en tokens, sur
  demande (`import-transcripts`) ; les rollouts Codex sont lus par `import-rollouts` (et avant
  les rapports). Leurs formats internes ne sont pas garantis stables (verifies sur 2.1.275 et
  codex-cli 0.153.4 a 0.155.0-alpha.9.2) : une ligne de forme inconnue est ignoree, pas mal lue.
- Sante de la collecte : le silence d'un client est deduit des dates de modification de ses
  journaux (Codex : tous les rollouts des `health.codex_days` = 30 derniers jours, car un fil
  repris ecrit dans le dossier de son jour de creation) ; un client qui ecrirait ailleurs (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) exige de
  renseigner `transcripts.claude_projects_dir` / `health.codex_sessions_dir`.
