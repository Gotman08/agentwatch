# Les six detecteurs

Tous sont deterministes, independants, executes sur la vue de session correlee. Les
seuils sont dans `config.json` sous `detectors`. Chaque signalement porte : identifiant de
regle et version, confiance (`high`/`medium`/`low`) et sa justification, appels concernes,
preuve locale, explication, contre-indications, donnees manquantes, cout observe,
proposition et protocole de validation.

Le niveau de confiance est une heuristique documentee, pas une probabilite calibree.
Classement des rapports : confiance, puis nombre d'appels, puis tokens mesures (si les
transcripts ont ete importes), puis octets de sortie observes. Aucun score global.

## Cout observe et tokens

Le cout observe d'un signalement somme, par provenance, les octets de sortie et les durees
des appels concernes. Les tokens ne sont jamais deduits des octets : ils viennent de
`agentwatch import-transcripts` (Claude Code), qui lit dans le transcript du client l'usage
de chaque requete API et l'attribue aux appels (part de l'entree non mise en cache de la
requete qui a consomme le resultat + part de la sortie de la requete qui a emis l'appel ;
methode dans `docs/events.md`). Quand ils sont mesures, le rapport les affiche a cote de la
methode, et `trends` les cumule par motif : c'est ce qu'un outil ou une regle eviterait a
chaque fois que le motif revient.

## Unites de travail (prealable a A et D)

Compter des appels identiques ne suffit pas : le meme travail peut etre refait par des
moyens differents. `core/intent.py` traduit chaque appel en une **operation normalisee**
independante de l'outil et de la forme de commande :

| Operation | Formes reconnues |
|---|---|
| `read` | outil Read ; `cat`, `type`, `Get-Content`, `head`, `tail`, `sed -n 'a,bp'`, `nl`, `less` |
| `search` | outil Grep ; `rg`, `grep`, `ag`, `ack`, `Select-String`, `findstr` |
| `list` | outil Glob/LS ; `ls`, `dir`, `Get-ChildItem`, `tree`, `find`, `fd` |
| `vcs_read` | `git status/diff/log/show/branch/rev-parse/ls-files/blame` |
| `run_tests` | `pytest`, `python -m pytest`, `python -m unittest`, `npm test`, `cargo test`, `go test`, `ctest`, ... |
| `build` | `make`, `cmake`, `msbuild`, `cargo build`, `npm run build`, `dotnet build`, `tsc`, ... |
| `run_script` | `python script.py ...`, `node script.js ...`, `python -m paquet ...` (cible `module:paquet`) |
| `edit`, `write`, `mcp`, `agent`, `web` | outils natifs |
| `unknown` | toute commande hors liste blanche, ou tube contenant un filtre inconnu |

Une commande `unknown` n'est comparee qu'a elle-meme (texte normalise) : aucune
equivalence semantique generale n'est pretendue. La preuve qu'un travail refait a
produit la meme chose est l'**empreinte du contenu obtenu** (texte normalise : CRLF ->
LF, espaces de fin supprimes), identique entre Read et `cat` pour un meme fichier.

Le rapport liste les « unites de travail refaites » (operation, cible, nombre de fois,
outils, agents, contenus distincts obtenus) avant meme tout signalement.

## A. `A.redundant_reads` (v2.0) — travail de lecture redondant, executions relancees

Regle (`repeated_read`) : meme unite de travail de lecture/recherche/listage (operation,
cible, parametres de comparaison), meme agent, meme epoque de contexte, dans une fenetre
(`window_calls` = 60 appels, `window_seconds` = 900), sans modification observee de la
cible entre les deux. Le sous-type `repeated_read_cross_tool` signale le meme travail
fait par des outils differents (ex. Read puis `cat`).

Regle (`repeated_run`) : meme execution (`run_tests`, `build`, `run_script`) relancee
au moins deux fois avec le meme statut, sans aucune ecriture, edition ni patch observe
entre les lancements. Confiance `medium` si les sorties sont identiques (empreinte de
contenu), `low` sinon ; un echec suivi d'un succes n'est pas signale (progres).

| Confiance | Condition |
|---|---|
| high | empreintes de resultat identiques, aucun appel a effet inconnu ni ecriture intercale |
| medium | empreintes identiques mais un appel a effet inconnu (shell `run`/inconnu, MCP, agent) s'est intercale ; ou aucun changement observe mais empreinte manquante |
| low | empreinte manquante et appel a effet inconnu |

Exclusions : ecriture observee sur la cible (pour une commande shell : toute ecriture),
premier appel en echec (domaine de B), resultats differents (relecture justifiee),
parametres differents (pagination, autre plage), autre agent, autre epoque (compaction,
reprise). Une recherche a zero resultat n'est pas jugee inutile pour autant.

Limite toujours rappelee : la presence du resultat precedent dans le contexte du modele
n'est pas observable ; une modification externe non plus.

Images vues (rollouts Codex) : l'empreinte vient de l'image rendue dans la sortie de l'`exec`, quand il y a
exactement une image par vue ; sinon aucune empreinte (jamais celle d'une reponse fabriquee). Constate le
2026-09-19 : 3 signalements "image vue plusieurs fois" etaient faux, l'image avait ete re-rendue par
Unreal entre les vues ; avec l'empreinte reelle, ils disparaissent.

Exemple positif : `Read a.py`, `git status`, `Read a.py` (contenu identique) -> high.
Contre-exemples : `Read b.py`, `Edit b.py`, `Read b.py` ; lecture par un sous-agent ;
lecture apres `PostCompact` ; `Read big.log offset=1` puis `offset=101`.

## B. `B.error_loops` (v1.1) — boucles d'erreurs

Regle : au moins `min_failures` (3) echecs de meme signature sur la meme operation (outil,
cible, parametres) dans la fenetre (40 appels / 1800 s), sans correction observable entre
deux echecs (ecriture touchant la cible ou un chemin de la commande).

| Type | Confiance | Sens |
|---|---|---|
| `persistent` | high | echecs identiques, aucune correction ni appel a effet inconnu |
| `persistent` | medium | un appel a effet inconnu s'est intercale (correction possible non observable) |
| `persistent_probable` | medium max | Codex : statut non expose, echec infere du texte de sortie |
| `transient_recovered` | low | la meme operation a fini par reussir sans correction (retry, backoff detecte si les intervalles croissent) |
| `repeated_denial` | medium | refus repetes ; un refus n'est pas une erreur d'execution |

`same_failure_different_inputs` (v1.1, medium ; low si l'outil a reussi entre-temps) : meme outil, meme
signature d'erreur, au moins `min_failures` fois dans la fenetre, sur au moins deux entrees differentes :
changer l'entree ne change pas l'erreur (service deconnecte, chemins devines). La regle par operation
identique ne voyait pas ce cas.

Codes de sortie (a l'analyse, hooks et rollouts) : `rg`, `grep`, `Select-String`, `findstr` en code 1
sans texte d'erreur = aucune correspondance ; `git diff --no-index`, `--exit-code`, `--quiet` en code 1
sans probleme signale = differences trouvees. Ni l'un ni l'autre n'est un echec (statut `success`,
`evidence.exit_status_meaning`). Constate le 2026-09-19 : 73 des 211 "echecs" d'une session Codex.

Les interruptions (`interrupted`) ne sont jamais comptees. Les causes proposees sont des
hypotheses tirees de la signature (`No such file`, `permission`, `timeout`, ...).

Exemple positif : trois `python run.py` avec `ModuleNotFoundError`. Contre-exemple :
echec, `Write cfg.json`, echec, succes -> la correction observee casse la chaine.

## C. `C.batchable` (v1.2) — operations regroupables

Regle : au moins `min_group` (3) appels consecutifs du meme outil de lecture (`read`,
`list`, `search`, MCP cible par chemin, commande shell traduite en lecture, recherche ou
listage) sur des cibles differentes, emis dans des reponses
successives du modele (un aller-retour par appel), sans erreur, avec au plus
`max_gap_calls` (0) appels intercales.

Reponses distinctes : un client peut executer l'un apres l'autre des appels que le modele a
emis ensemble (Claude Code le fait pour `Read`) ; le modele a alors deja regroupe, rien ne
lui est reproche. La separation se prouve par la requete emettrice lue dans le transcript
(`import-transcripts`, exact) ; sans transcript, par l'ecart entre la fin d'un appel et le
debut du suivant : en dessous de `same_response_gap_ms` (2 000 ms), les deux appels sont
tenus pour emis ensemble. Mesure du 2026-09-19 sur 249 paires reelles (Claude Code, transcript
comme reference) : lectures d'une meme reponse separees de -119 a 1 967 ms (surcout des
hooks), reponses distinctes d'au moins 2 559 ms. Le seuil est une heuristique : une machine
tres chargee allonge l'ecart des hooks, un modele tres rapide raccourcit l'aller-retour.
Faux positifs corriges par cette regle : 3 `Read` d'une reponse (session `38f05d3c`) et
23 `Read` emis en 3 reponses (session `57258ee2`), signales avant v1.1 comme 3 et 23
allers-retours. Des appels emis ensemble ailleurs dans la session du meme outil valent preuve
que le regroupement est possible (`verified_in_session`).

Preuve : `round_trips`, `gaps_ms` (ecarts entre appels) et `separation_basis` (transcript
ou rollout, sinon heuristique) ; `missing_data` le rappelle quand ni l'un ni l'autre n'est importe.

Commandes shell (v1.2) : Codex lit tout par le shell (`Get-Content`, `rg`, `Get-ChildItem`) ;
avant v1.2 la regle ne voyait donc jamais rien sur Codex. Une commande compte si `core/intent.py`
la traduit en `read`, `search` ou `list` ; une serie ne melange pas les operations et compare les
fichiers vises (deux commandes sur un meme fichier = meme cible). Sur Codex, les actions d'un meme
`exec` partagent leur reponse emettrice (rollout) : elles sont emises ensemble et ne sont jamais
signalees ; quatre `exec` successifs de lecture le sont, avec 3 allers-retours evitables.

Independance (elle fixe la confiance) :

| Preuve | Confiance |
|---|---|
| toutes les cibles figurent dans `result_paths` d'un listage/recherche juste avant | high |
| meme dossier ou meme extension | medium |
| sinon | low (« independance non etablie ») |

Outil groupe : annonce `verified_in_session` seulement si des appels du meme outil emis
ensemble (chevauchants, ou dans une meme reponse) ont ete observes dans la session ;
`candidate_observed` si un outil MCP du meme serveur au nom evocateur (batch, many, multi,
all, bulk, list) a ete vu ; sinon `documented_not_verified` (Claude Code) ou `proposal`.
Des appels deja chevauchants ou emis dans une meme reponse ne sont pas signales.

## D. `D.automation_candidates` (v1.2) — sequences candidates a une automatisation

Regle : motifs de 2 a 6 appels (`min_pattern_len`, `max_pattern_len`) repetes au moins
`min_occurrences` (3) fois, par agent, sur les `max_calls` (2000) derniers appels. La
signature d'un appel = outil, categorie, forme de la cible (extension, tete de commande,
serveur/outil MCP) et noms des parametres. Seuls les motifs fermes (non inclus dans un
motif plus long de meme frequence) et comportant au moins deux signatures distinctes sont
retenus. Complexite : O(appels x longueur max).

Etapes classees `mechanical` (structure stable, cible substituee) ou `judgment` (contenu
d'edition variable, commande de structure variable, commande non reconnue dont le texte change
(script en ligne), contenu compose par le modele a chaque occurrence : code d'un appel MCP, message,
requete ; statut variable selon l'occurrence). Les appels de coordination (messages et attentes entre
agents, plan, questions) n'entrent pas dans les sequences. Un meme cycle n'est rapporte qu'une fois
(A -> B et B -> A).

Valeur (v1.2) : les allers-retours du modele qu'une automatisation eviterait, `avoidable_round_trips`,
comptes par occurrence d'apres la requete emettrice (transcript ou rollout), sinon les ecarts. Un motif
que le modele enchaine deja en une seule reponse (actions d'un meme `exec` Codex) n'est pas signale ; un
motif dont moins de la moitie des occurrences coute un aller-retour de plus est en confiance basse.
Constate le 2026-09-19 : 30 des 32 signalements d'une session Codex etaient faux (coordination, code
reecrit a chaque fois) ; "patch puis verification" (12 fois) tenait 11 fois sur 12 dans une reponse.

| Confiance | Condition |
|---|---|
| high | au moins `min_occurrences` + 1 occurrences, toutes reussies, aucune etape de jugement |
| medium | au plus une etape de jugement ou occurrences au seuil |
| low | plusieurs etapes de jugement |

Le signalement contient une recette (entrees, preconditions, etapes, sortie, tests,
risques) a valider par un humain. AgentWatch ne genere ni n'execute ce script, et ne
conclut jamais qu'une tache « n'a pas besoin d'intelligence ».

## E. `E.tool_gap` — service externe manipule a la main

Regle : au moins `min_calls` (3) commandes shell d'une meme famille de service vers la
meme cible, par le meme agent, dans la session. Familles et cibles :

| Famille | Commandes | Cible comparee |
|---|---|---|
| `ssh` | ssh, scp, sftp, rsync, ssh-keygen, ssh-add, ssh-copy-id | hote (apres `user@`, avant `:`) |
| `slurm` | sbatch, squeue, srun, sacct, scancel, sinfo, scontrol, salloc, seff | `slurm` |
| `http` | curl, wget, http(ie), Invoke-WebRequest, Invoke-RestMethod | hote de l'URL |
| `github` | gh | `github` |
| `cloud` | az, aws, gcloud, kubectl, helm, terraform | la commande |
| `container` | docker, podman, docker-compose, nerdctl | la commande |

Chaque commande coute un tour complet (la construire, relire une sortie brute) ; un outil
MCP ou une skill qui expose l'operation rend la meme information en un appel structure,
avec moins de tokens et plus de contexte pour le modele. Exemple vise : chercher la cle,
l'ecrire, `ssh` vers le calculateur, alors que le serveur MCP du calculateur fait tout.

| Confiance | Condition |
|---|---|
| high | un serveur MCP lie a la cible a ete observe dans la session (nom du serveur citant la cible, ou nom / outils du serveur portant un mot de la famille : `job`, `ssh`, `upload`, `github`, `docker`, ...) : il etait disponible et n'a pas servi |
| medium | au moins `strong_calls` (6) commandes, ou des echecs dans la serie |
| low | sinon |

La proposition nomme le serveur MCP a utiliser (ou a etendre), sinon l'outil a ecrire, et
compte les etapes remplacees. `trends` ajoute les serveurs MCP lies vus dans n'importe
quelle session de la fenetre. Rien n'est construit.

Exemple positif : `mcp__romeo__romeo_status`, puis `ssh romeo 'squeue'`, `scp job.sh romeo:`,
`ssh romeo 'sbatch job.sh'` -> high. Contre-exemples : deux `ssh a` et deux `ssh b` ; trois
`python run.py` (pas un service).

## F. `F.repeated_guidance` (v1.0) — consignes redonnees a la main

Repond a « ou lui rappelle-t-on les memes conseils ? ». Source : messages lus dans les rollouts Codex
(marqueurs `message` : role, longueur, empreintes HMAC courtes des paragraphes d'au moins 40 caracteres ;
jamais le texte).

| Type | Regle | Confiance |
|---|---|---|
| `user_repeated_guidance` | meme(s) paragraphe(s) dans au moins `min_user_messages` (2) messages distincts de l'utilisateur, fil principal | high a partir de 3 messages, medium sinon |
| `orchestrator_repeated_guidance` | meme(s) paragraphe(s) dans au moins `min_instructions` (3) consignes de l'orchestrateur a ses sous-agents (`message` de send_message, followup_task, spawn_agent) | medium |

Les paragraphes repetes ensemble dans les memes messages forment un signalement ; il faut au moins
`min_chars` (80) caracteres repetes. Messages dedoublonnes par identifiant (un sous-agent recopie
l'historique de son parent) ; seuls les messages de l'utilisateur du fil principal comptent. Les blocs que
Codex injecte lui-meme dans un message `user` (instructions `AGENTS.md`, `<environment_context>` et autres
balises) sont exclus des empreintes et comptes a part (`injected_chars`), un titre Markdown seul aussi.
Constate le 2026-09-19 : sans cette exclusion, `AGENTS.md` reinjecte a la reprise du fil passait pour une
consigne redonnee par l'utilisateur. Sur les deux sessions Codex du 10 au 19 septembre, apres correction :
aucun signalement (25 messages de l'utilisateur, 2 116 consignes aux sous-agents).

Limite : une consigne reformulee n'est pas reconnue (empreinte exacte au paragraphe, apres normalisation
des espaces et de la casse). Proposition : inscrire la consigne dans `AGENTS.md`, une skill ou la
definition du sous-agent. Cle de motif pour `trends` : role + empreinte du premier paragraphe.

## Vue multi-sessions : `agentwatch trends`

Un signalement isole dans une session ne justifie rien : un `Read` en double ne merite pas
qu'on y touche. Ce qui justifie un script, une skill ou une regle dans `CLAUDE.md` /
`AGENTS.md`, c'est un motif qui revient dans plusieurs sessions, eventuellement sur
plusieurs projets. `trends` reanalyse chaque session de la fenetre (`--days`, appliquee au
dernier evenement de la session ; `0` = toutes) avec les quatre detecteurs, puis regroupe
les signalements par une **cle de motif** stable, independante de la session, du client et
des identifiants d'appel :

| Regle | Cle de motif | Pourquoi |
|---|---|---|
| A | operation + cible normalisee (chemin relatif au projet quand il est dessous) ; `repeated_read` et `repeated_read_cross_tool` fusionnes | le meme fichier relu dans plusieurs projets est un seul motif ; le compte de projets le montre |
| B | type de boucle (`persistent`, `transient_recovered`, `repeated_denial` ; suffixe `_probable` fusionne) + outil + signature d'erreur, sans la cible | la meme erreur sur des cibles differentes est le meme probleme |
| C | outil | le motif est l'habitude de lire en serie, pas les fichiers lus |
| D | sequence des formes d'appels | c'est la recette candidate elle-meme |
| E | famille de service + cible (hote, URL, service) | l'outil qui manque ou qui n'a pas servi |

Un motif est **recurrent** s'il apparait dans au moins `trends.min_sessions` (2) sessions
distinctes, les signalements marques faux positifs ne comptant pas. Classement : sessions
distinctes, puis confiance maximale, occurrences, tokens mesures (transcripts importes),
appels concernes, octets de sortie ; aucun score global. Le rapport donne :

- le top global, avec pour les premiers motifs les sessions les plus recentes, la
  proposition du signalement le plus sur et les identifiants a marquer via `feedback` ;
- la meme table restreinte a chaque **projet** puis a chaque **client** (le seuil s'applique
  a l'interieur du groupe : un projet avec une seule session n'a pas de recurrence mesurable) ;
- une ligne par **session** : appels, erreurs, signalements A/B/C/D et nombre de motifs de
  cette session qui reviennent ailleurs dans la fenetre.

Rien de nouveau n'est detecte : ce sont les signalements par session, agreges. Leurs
contre-indications restent valables et `report --session <id>` donne la preuve.

## Retours locaux

`agentwatch feedback --finding <id> --mark relevant|false-positive|clear` enregistre la
marque dans `<home>/feedback.json`. Un faux positif est exclu des opportunites
prioritaires mais reste liste. Aucun apprentissage.
