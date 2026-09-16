<!-- Exemple genere a partir d'une session SYNTHETIQUE (agentwatch.selftest.Synth), pas d'une capture reelle. -->

# AgentWatch - rapport de session

- Client : `claude-code` ; session `exemple-synthetique` ; modele : `synthetic-model` (hook input (SessionStart / events))
- Projet : `C:\proj` ; periode : 2027-01-15T08:00:00.000Z -> 2027-01-15T08:00:02.799Z
- Tours : 1 ; epoques de contexte : 1 ; agents : main
- Version du client a la configuration : inconnue ; AgentWatch 0.1.0 ; schema 1.0

## Opportunites prioritaires

Classement : confiance (high > medium > low) > nombre d'appels concernes > octets de sortie observes. Aucun score global.

### 3 appels Read sequentiels sur des cibles differentes

- Regle : `C.batchable` v1.0 (sequential_similar_calls) ; identifiant `df3085664635e2c1`
- Confiance : **high** ; independance : toutes les cibles figurent dans le resultat de l'appel #0 (Glob). Le niveau de confiance est heuristique, pas une probabilite calibree.
- Appels concernes : #1 Read (success), #2 Read (success), #3 Read (success)
- Cout observe : 3 appel(s) ; 372 octets de sortie observes (3 appels mesures) ; 360 ms (reconstruite entre hooks, inclut la surcharge des hooks) ; tokens : non mesures

3 appels Read ont ete emis l'un apres l'autre (chacun apres la fin du precedent) sur 3 cibles distinctes, sans appel intermediaire.

Preuve locale :
- tool : Read
- targets : ['src/a.py', 'src/b.py', 'src/c.py']
- independence : high
- independence_basis : toutes les cibles figurent dans le resultat de l'appel #0 (Glob)
- sequential : oui
- grouped_tool : {'status': 'documented_not_verified', 'note': "Claude Code peut emettre plusieurs appels d'outil dans un meme tour ; non observe dans cette session"}
- durations_ms : [120, 120, 120]
- duration_sources : ['reconstructed_between_hooks']

Contre-indications :
- Si une cible a ete choisie d'apres le contenu lu juste avant, les appels ne sont pas independants.
- Un regroupement concatene les sorties : verifier que la taille totale reste acceptable pour le contexte.
- Les contraintes du client (limites de parallelisme, sandbox, quotas MCP) ne sont pas observees.

Donnees manquantes : contenu des resultats non conserve : la dependance entre lectures est inferee, pas observee

Amelioration proposee : Emettre ces lectures en une seule fois (appels paralleles dans le meme tour, ou un outil acceptant plusieurs cibles) lorsque l'independance est etablie.
Outil groupe : documented_not_verified - Claude Code peut emettre plusieurs appels d'outil dans un meme tour ; non observe dans cette session

Protocole de validation :
1. Verifier dans le transcript que la liste des cibles etait connue avant le premier appel.
2. Comparer la duree totale (source indiquee) avec celle d'un tour groupe apres modification.
3. Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.

### 3 echecs identiques : Bash sur 'python build.py'

- Regle : `B.error_loops` v1.0 (persistent) ; identifiant `91b5757321e53a8c`
- Confiance : **high** ; 3 echecs de meme signature sans aucune modification observee entre eux. Le niveau de confiance est heuristique, pas une probabilite calibree.
- Appels concernes : #5 Bash (error), #6 Bash (error), #7 Bash (error)
- Cout observe : 3 appel(s) ; 360 ms (reconstruite entre hooks, inclut la surcharge des hooks) ; tokens : non mesures

La meme operation (Bash, cible 'python build.py') a echoue 3 fois avec la signature "Exit code <n>: ModuleNotFoundError: No module named 'toolz'". Aucun succes ulterieur ni correction observable des preconditions.

Preuve locale :
- error_signature : Exit code <n>: ModuleNotFoundError: No module named 'toolz'
- error_summary_first : Exit code 1: ModuleNotFoundError: No module named 'toolz'
- statuses : ['error', 'error', 'error']
- exit_codes : [None, None, None]
- gaps_seconds : [0.2, 0.2]
- backoff_pattern : non
- unknown_effect_calls_between : []
- later_success_seq : inconnu
- target : python build.py
- params : {'command': 'python build.py', 'shell_kind': 'run', 'shell_heads': ['python'], 'shell_paths': ['build.py']}

Contre-indications :
- Un test intentionnel (verifier qu'une commande echoue) produirait le meme motif.
- Une correction hors des outils observes (edition manuelle, service externe) n'est pas visible.
- Les causes proposees sont des hypotheses etayees par la signature, pas des certitudes.

Donnees manquantes : code de sortie absent

Amelioration proposee : Verifier la precondition (chemin, dependance, permission, delai) avant de relancer, ou limiter le nombre de tentatives identiques dans les instructions du projet.
Hypotheses de cause : dependance Python absente ; commande en echec : voir le code de sortie

Protocole de validation :
1. Relire le resume d'erreur masque et confirmer que la signature designe bien la meme cause.
2. Verifier dans le transcript si une correction a eu lieu hors des outils observes.
3. Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.

### Read repete 2 fois sur 'src/a.py'

- Regle : `A.redundant_reads` v1.0 (repeated_read) ; identifiant `49d95bbc3f66b177`
- Confiance : **high** ; empreintes de resultat identiques, aucune modification ni appel a effet inconnu observe entre les appels, meme agent et meme epoque de contexte. Le niveau de confiance est heuristique, pas une probabilite calibree.
- Appels concernes : #1 Read (success), #4 Read (success)
- Cout observe : 1 appel(s) ; 124 octets de sortie observes (1 appels mesures) ; 120 ms (reconstruite entre hooks, inclut la surcharge des hooks) ; tokens : non mesures

L'appel Read sur la meme cible avec les memes parametres a ete emis 2 fois dans la fenetre (60 appels / 900 s). Le resultat observe etait identique a chaque fois.

Preuve locale :
- target : src/a.py
- target_kind : path
- params : {}
- result_fingerprints_distinct : 1
- result_fingerprints_known : oui
- statuses : ['success', 'success']
- intervening_notable_calls : [[]]
- context_epoch : 0
- agent : main

Contre-indications :
- Aucune verification necessaire n'a ete observee, mais l'intention du modele n'est pas visible.
- La disponibilite du resultat precedent dans le contexte du modele n'est pas observable par les hooks : seule une compaction connue est prise en compte.
- Une modification externe (autre processus, editeur, git) n'est pas observable.

Amelioration proposee : Reutiliser le resultat de la premiere lecture tant qu'aucune modification de la cible n'est observee ; si une verification est voulue apres une commande a effet inconnu, preferer une lecture ciblee (plage de lignes ou filtre) plutot qu'une relecture complete.

Protocole de validation :
1. Ouvrir le transcript du client a l'horodatage des appels et verifier que le premier resultat etait encore visible.
2. Comparer les empreintes de resultat (identiques => contenu identique au moment des appels).
3. Marquer le signalement : agentwatch feedback --finding <id> --mark relevant|false-positive.

## Autres signalements

### D.automation_candidates : 2 signalement(s)

- `04ab98f662626379` Motif recurrent (3x) : Bash -> Read -> Bash - confiance medium
- `21c16da7c7ebcfb4` Motif recurrent (3x) : Bash -> Bash -> Read - confiance medium

## Statistiques descriptives

- Evenements : 37 ; appels correles : 17 ; statuts : {'success': 14, 'error': 3}
- Correlation : {'events': 37, 'duplicate_events': 0, 'invalid_events': 0, 'orphan_ends': 0, 'duplicate_phases': 0, 'open_calls': 0, 'heuristic_pairs': 0, 'unmapped_events': 0}
- Surcharge des hooks (dans le processus, hors demarrage de l'interpreteur) : {'n': 37, 'median': -10520028554.6, 'p90': -10520027411.4, 'max': -10520027216.0, 'note': "temps dans le processus du hook (hors demarrage de l'interpreteur)"}
- Usage de tokens : non mesure (aucune donnee d'usage fournie par les hooks ; voir agentwatch import-usage)

| Outil | Appels | Erreurs | Statut inconnu | Ouverts | Sortie (octets, mesures) | Duree client mediane (n) | Duree reconstruite mediane (n) |
|---|---|---|---|---|---|---|---|
| Bash | 9 | 3 | 0 | 0 | 432 (6) | inconnu (0) | 120 (9) |
| Read | 7 | 0 | 0 | 0 | 880 (7) | inconnu (0) | 120 (7) |
| Glob | 1 | 0 | 0 | 0 | 133 (1) | 5 (1) | inconnu (0) |

Signatures d'erreur les plus frequentes :
- 3x `Exit code <n>: ModuleNotFoundError: No module named 'toolz'`

Sorties les plus volumineuses (une sortie volumineuse n'est pas, seule, un gaspillage) :
- #0 Glob '.' : 133 octets
- #9 Read 'out/m1.json' : 128 octets
- #12 Read 'out/m2.json' : 128 octets
- #15 Read 'out/m3.json' : 128 octets
- #1 Read 'src/a.py' : 124 octets

Appels les plus longs (source de la duree indiquee) :
- #1 Read 'src/a.py' : 120 ms (reconstructed_between_hooks)
- #2 Read 'src/b.py' : 120 ms (reconstructed_between_hooks)
- #3 Read 'src/c.py' : 120 ms (reconstructed_between_hooks)
- #4 Read 'src/a.py' : 120 ms (reconstructed_between_hooks)
- #5 Bash 'python build.py' : 120 ms (reconstructed_between_hooks)

## Couverture

| Capacite | Documente | Observe dans cette session | Base |
|---|---|---|---|
| tool_start | supported | observed | PreToolUse |
| tool_end | supported | observed | PostToolUse (succes uniquement) |
| tool_failure | supported | observed | PostToolUseFailure (erreur, refus, interruption) |
| exit_code | absent | absent | tool_response de Bash expose stdout/stderr/interrupted, pas de code de sortie |
| client_duration | partial | observed | PostToolUse.duration a partir de v2.1.267 ; durationMs pour certains outils MCP |
| output_size | supported | observed | taille du tool_response serialise tel que recu par le hook |
| result_fingerprint | supported | observed | empreinte HMAC du tool_response |
| mcp_calls | supported | not_observed | outils nommes mcp__<serveur>__<outil> |
| subagents | supported | not_observed | agent_id / agent_type dans l'entree des hooks |
| compaction | supported | not_observed | PreCompact / PostCompact |
| interrupts | supported | not_observed | PostToolUseFailure.is_interrupt |
| turn_boundaries | supported | observed | UserPromptSubmit / Stop (prompt_id a partir de v2.1.196) |
| background_commands | partial | not_observed | run_in_background : la fin reelle passe par BashOutput, non correlee |
| hosted_tools | partial | not_observed | WebFetch/WebSearch sont des outils locaux vus par les hooks ; pas de detail reseau |
| long_commands_polling | partial | not_observed | un appel long = un PreToolUse puis un PostToolUse ; le polling interne n'est pas visible |
| token_usage | absent | absent | non fourni aux hooks ; interface d'import OpenTelemetry seulement |

Les capacites non listees ne sont pas observees : aucun pourcentage de couverture globale n'est calcule.

