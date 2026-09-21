# Les sept detecteurs

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
`agentwatch import-transcripts` (Claude Code) ou `import-rollouts` (Codex), qui lisent l'usage
ecrit par le client. `trends` les cumule par motif : c'est l'ordre de grandeur qu'un outil ou une
regle eviterait a chaque fois que le motif revient.

Trois natures de nombres, jamais melangees dans les rapports (precision du 2026-09-20) :

| Nature | Ce que c'est | Ou |
|---|---|---|
| mesure | releve ecrit par le client, un par reponse du modele (`token_usage_record`) ou par requete API (transcript) | totaux de session, tableau par fil |
| repartition calculee | part d'un releve attribuee a un appel d'outil par AgentWatch : entree non mise en cache de la reponse qui a consomme le resultat (au prorata des tailles) + part de la sortie de la reponse emettrice | cout observe d'un signalement, `trends` |
| estimation | aucune : rien n'est deduit d'octets, d'un tarif ou d'un modele de cout | nulle part |

Aucune donnee de facturation n'est lue : les rapports ne disent jamais ce qui a ete « paye ».

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
reprise). Une recherche a zero resultat n'est pas jugee inutile pour autant. Un resultat precedent « en
cours » ou « indisponible » ecarte aussi la paire : c'est un sondage ou un reessai, juge par G. L'epoque est
propre a chaque agent : la compaction d'un sous-agent ne separe pas deux lectures du fil principal.

Lots (`repeated_read_batch`, a partir de `batch_min` = 3 elements) : des relectures emises dans une meme
reponse du modele, de lectures emises ensemble dans une autre, forment un seul signalement qui garde chaque
cible. Preuve exacte exigee (requetes emettrices du rollout ou du transcript). Constate le 2026-09-19 : un
sous-agent Codex a relu d'un bloc 26 tickets Linear 27 s apres les avoir lus ; 26 signalements devenus un.

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

Reponse MCP "reussie" qui decrit une panne : une reponse JSON n'est un echec probable que si un champ
d'erreur est non vide ou un statut vaut `failed`/`error` (le serveur romeo repond `"erreur": null` quand
tout va bien : 188 attentes d'un job en file passaient pour des pannes) ; le texte libre garde la regle
textuelle.

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

Valeur (v1.2) : les allers-retours du modele entre les etapes, `round_trips_between_steps`, comptes par
occurrence d'apres la requete emettrice (transcript ou rollout), sinon les ecarts. Un motif que le modele
enchaine deja en une seule reponse (actions d'un meme `exec` Codex) n'est pas signale.
Constate le 2026-09-19 : 30 des 32 signalements d'une session Codex etaient faux (coordination, code
reecrit a chaque fois) ; "patch puis verification" (12 fois) tenait 11 fois sur 12 dans une reponse.

Economie demontree ou non (precision du 2026-09-20). `avoidable_round_trips` n'est renseigne que si
TOUTES les etapes sont mecaniques (`saving_demonstrated`). Des qu'une etape exige un jugement, l'aller-retour
qui la precede sert au modele a lire le resultat pour decider de la suite : la seule repetition d'une
structure ne demontre aucune economie, le signalement passe en confiance basse et sa proposition le dit.
Un contenu d'edition inconnu (empreinte absente) n'est jamais suppose identique : l'etape est de jugement.
Lire puis modifier, ou modifier puis verifier, est reconnu comme un `development_cycle` et titre comme tel :
c'est le travail normal d'un developpeur, pas une sequence a scripter.

| Confiance | Condition |
|---|---|
| high | au moins `min_occurrences` + 1 occurrences, toutes reussies, aucune etape de jugement |
| medium | aucune etape de jugement, occurrences au seuil ou echecs |
| low | au moins une etape de jugement (aucune economie demontree) |

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

## G. `G.repeated_calls` (v1.0) — appels repetes : pourquoi, a quel rythme, ameliorable ou non

Repond a « pourquoi l'agent refait-il cet appel, et puis-je l'aider ? ». Tous les outils : shell, MCP,
fonctions du client (`wait`, `wait_agent`, `clock.sleep`), lectures. Un groupe = un meme appel (meme agent,
meme outil, meme cible, memes parametres ; les delais demandes et les tailles de sortie ne comptent pas)
fait au moins `min_calls` (3) fois. Chaque reprise (appel precedent, appel suivant) recoit :

| Element | Contenu |
|---|---|
| raison observee | la premiere qui s'applique : `same_response` (meme reponse du modele : boucle de script, appels paralleles ; aucun aller-retour), `context_loss` (compaction de CET agent entre les deux), `new_input` (message de l'utilisateur ou d'un autre agent, nouveau tour), `after_change` (ecriture sur la cible ; action non-lecture sur le meme serveur MCP ; message ou tache donnee a un sous-agent pour une attente), `unavailable` (le resultat precedent disait le service indisponible), `after_failure`, `waiting` (le resultat precedent disait « en cours », ou l'attente est arrivee a echeance), `possible_change` (action a effet inconnu du meme agent entre les deux), `none` |
| raison completee | si rien n'est observe : un outil dont la fonction est d'attendre attend encore (`waiting`, base « nature de l'outil ») ; sinon la raison annoncee par l'agent (`unavailable`, `waiting`, base « annoncee ») |
| raison annoncee | categories reconnues dans les commentaires de l'agent entre les deux appels : `retry`, `wait`, `unavailable`, `in_progress`, `verify`, `after_change`, `fix`, `explore` (rollouts Codex ; jamais le texte) |
| apport | etat du resultat identique ou change (empreinte `state_fp`, horodatages et durees neutralises ; a defaut empreinte du contenu) ; changement de phase |
| rythme | intervalle depuis l'appel precedent (debut a debut), temps mort (fin a debut), episodes (ecart > `episode_gap_s`, 20 min), pics par minute et par 10 minutes |
| cout | allers-retours du modele, tokens mesures, contexte relu pour decider (entree totale des reponses emettrices, dont cache, une fois par reponse) |

La phase d'un resultat (`result_phase`) est lue a l'ingestion : `unavailable` (champ `error`, `message`,
`status`... ou drapeau `available`/`connected`... a faux ; tete ou fin d'une sortie texte), `in_progress`
(champ `status`/`state` a `RUNNING`, `PENDING`...; `timed_out` d'une attente ; « Script running with cell ID »
de la fonction `wait` de Codex), `failed`, `done`. Elle n'est lue dans le texte que pour un outil dont le nom
annonce un etat (`status`, `wait`, `job`, `health`, `check`...) ou une commande d'etat (`squeue`, `docker`,
`kubectl`, `curl`...) : un ticket Linear « In Progress » relu n'est pas un traitement en cours, un fichier lu
qui contient « unavailable » n'est pas un service indisponible.

Verdict par groupe, sur les reprises qui ont coute un aller-retour :

| Verdict | Quand | Proposition |
|---|---|---|
| ameliorable : agent | reprises sans raison ni apport (`unexplained_repeats`) ; sondage avec un outil d'etat alors qu'un outil d'attente du meme serveur sert dans la session (`polling_instead_of_wait`) ; attente d'un outil du client relancee a chaque echeance (`wait_timeout`) ; suivi periodique par le shell (`polling_cadence`) | consigne : reutiliser le resultat, attendre avec l'outil d'attente, demander un delai plus long, boucler dans un seul appel |
| ameliorable : outil | sondage ou attente d'un serveur MCP (`polling_cadence`, `wait_timeout`) | attente bloquante jusqu'au changement d'etat, delai maximal releve, cadence minimale |
| environnement | reessais apres « indisponible » (`retry_cadence`) : legitimes | cote outil : rendre un delai de reprise ou attendre la disponibilite ; cadence simulee |
| apres echec | reprises apres echec | detecteur B |
| candidat a examiner | attente relancee a echeance, mais une partie des reprises suit une consigne, une modification ou une compaction : leur utilite n'est pas jugeable ici | aucune attribution ; les faits, ce qui reste a etablir, et l'alternative d'attente si elle est connue |
| justifie | reprises expliquees (modification, nouvelle consigne, perte de contexte) ou qui ont appris quelque chose | aucune ; si les compactions dominent : garder l'information hors du contexte |
| sans aller-retour | toutes les repetitions dans une meme reponse | aucune |
| indetermine | apport inconnu pour la majorite | aucune |

Pause sans cible ni evenement corrole (precision du 2026-09-21). Le resultat d'une pause (`clock.sleep`) dit qu'elle
est terminee, pas ou en est le travail attendu : sans cible, sans etat rapporte et sans `timed_out`
(`describes_external_state`), les appels et leurs reprises restent des OBSERVATIONS dans le tableau, avec le verdict
`indetermine` et aucun signalement. Le rapport dit ce qu'il faudrait pour juger : relier chaque pause a ce qu'elle
attend. Deux consequences du meme constat : la duree demandee entre dans la cle de groupe des outils d'attente (une
pause de 5 s et une de 30 s ne sont pas le meme appel repete), et les intervalles ne sont calcules qu'a l'interieur
d'un episode (l'ecart entre deux episodes n'est pas une cadence de reprise). Constate sur 15 `clock.sleep` de 5 a
30 s, repartis en 5 episodes, lus comme quinze fois le meme appel relance toutes les minutes.

Trois durees d'une attente, jamais confondues (precision du 2026-09-20) : le delai DEMANDE a l'outil
(parametre `timeout_ms`...), la duree OBSERVEE de l'appel, et l'INTERVALLE OBSERVE entre deux appels
(duree de l'attente + temps de reponse du modele). Le rapport donne les trois (`wait_timing`) : une attente
de 50 s relancee toutes les 1 min 45 s ne se resume pas a « relancee toutes les 50 s ».

Verdict aligne sur les preuves (precision du 2026-09-21). Une reprise n'est inutile que si rien n'a change entre
les deux appels. Des qu'une partie des reprises d'un groupe d'attente suit une consigne, une modification ou une
compaction, le verdict devient `candidat a examiner` : le signalement reste, son titre commence par « Candidat a
examiner », et il ne designe personne. Les propositions enoncent alors un fait observe et une piste, jamais une
instruction de modifier l'agent observe ou sa configuration. L'alternative d'attente est dite explicitement
inconnue quand aucun outil d'attente n'a ete vu dans la session, et l'utilite des reprises expliquees figure dans
les donnees manquantes.

Ce qu'un resultat identique ou une compaction ne prouvent pas (precision du 2026-09-20). Un resultat
identique a la reprise est un CANDIDAT a verifier, pas un gaspillage demontre : verifier qu'un etat n'a
pas bouge donne aussi un resultat identique, et le raisonnement du modele entre les deux appels n'est pas
observe. Une reprise apres compaction est attendue, puisque le contexte a ete remplace ; elle ne se discute
que si le contenu relu est stable et volumineux. Les propositions le disent, au lieu de conclure.

Attente relancee (`wait_timeout`) : G distingue l'attente qui va jusqu'au delai demande (demander plus long ;
le rapport cite le plus long delai que l'outil a respecte dans la session, preuve qu'il est accepte) de celle
qui rend la main avant (la sortie intermediaire du traitement attendu la reveille : journal dans un fichier,
une seule attente de la fin). Constate sur 117 sessions Codex : `wait_agent` respecte 300 s et 600 s, mais
l'agent demande 30 s dans la moitie des cas (818 attentes, 627 encore en cours au retour).

Un suivi periodique est reconnu meme quand la sortie change (compteurs de progression) : au moins 5
reprises, intervalle regulier (coefficient de variation < 0,5), rien d'observe entre deux pour 60 % d'entre
elles, phase inchangee. Qui peut changer la cadence decide entre agent et outil : un serveur MCP se modifie ;
un outil du client (shell, `wait`, `wait_agent`) non, c'est l'agent qui choisit sa boucle et ses delais.

Cadence simulee (sondages, reessais) : pour chaque delai minimal de `cooldowns_s` (10 s a 10 min), appels
gardes, evites (dont allers-retours) et retard ajoute a la detection de chaque changement de phase. Modele :
un appel arrive avant la fin du delai est retenu jusqu'a cette fin puis servi, les appels intermediaires
disparaissent ; retard <= delai. Delai suggere : le plus long dont le retard reste sous
max(`min_tolerated_delay_s`, `tolerated_delay_ratio` x attente typique), soit 10 % de la duree typique d'un
episode. Sans changement de phase observe, le retard vaut `None` et s'affiche « non estimable », jamais 0 ms qui se
lirait comme une absence de retard ; le rapport ajoute alors que rien n'etablit que les appels en moins auraient ete
retirables sans consequence sur le travail attendu. Les colonnes disent « appels restants » et « appels en moins »,
et non « evites » : la simulation compte des appels, elle ne demontre pas qu'ils etaient superflus.

Signalements : un par habitude (nature x outil), qui rassemble ses groupes ; seulement pour les verdicts
ameliorables, avec au moins `min_avoidable_calls` (3) appels evitables pour une cadence. Les lectures et
executions locales refaites sans raison restent au detecteur A (qui ecarte desormais une reprise apres un
resultat « en cours » ou « indisponible »), les boucles d'echec au detecteur B. Le rapport ajoute la section
« Appels repetes : pourquoi, a quel rythme » : tous les groupes, justifies compris (ce qu'on ne peut pas
ameliorer se lit la), la cadence simulee des trois premiers sondages, et le rythme de chaque outil
(appels, intervalle median, pics par minute et par 10 minutes, appels identiques a un precedent et sans
apport). Cle de motif pour `trends` : nature + outil.

Constate le 2026-09-19 sur la session Codex `01a08ca6` (13 403 appels, analyse en 0,25 s) : 202 groupes,
1 231 reprises avec aller-retour (justifiees 696, agent 509, outil 26). La fonction `wait` a relance 233 fois
l'attente d'un script encore en cours (`yield_time_ms` de 1 s a 60 s) : 38,2 millions de tokens de contexte
relus pour decider ces reprises. `wait_agent` : 116 attentes relancees. Les 188 `wait_for_job` du serveur
romeo n'ont coute que 26 allers-retours : les autres s'enchainaient a moins de 2 s, sans reponse du modele
entre deux (boucle hors modele, `timeout_seconds=60`).
Deux scripts de suivi relances a la main toutes les 34 s et 55 s (43 et 98 fois) : une boucle dans un seul
appel aurait suffi. Les relectures de tickets Linear suivaient des compactions (39 dans le fil principal) :
justifiees.

Limites : le raisonnement du modele n'est pas observe (seuls les faits et les categories annoncees) ; un
etat identique a l'empreinte peut cacher un detail utile ; la simulation suppose qu'un etat observe persiste
jusqu'a l'appel suivant. Reglages : `detectors.repeated_calls` (`min_calls`, `episode_gap_s`,
`min_avoidable_calls`, `cooldowns_s`, `min_tolerated_delay_s`, `tolerated_delay_ratio`, `rhythm_top`,
`report_top`).

## Contexte : sorties relues, reprises apres compaction, quota (section du rapport, sans verdict)

Pas un detecteur : aucune regle, aucun signalement, aucune confiance. Une section descriptive du rapport de session
(`stats.context` dans l'export JSON), calculee a partir des releves par reponse deja importes (`usage` de portee
`response`) et des parts par appel (`usage` de portee `call`). Rien a reimporter pour l'essentiel ; seuls les faits de
coupe, le quota et les tours coupes demandent des lignes lues par l'import qui les releve (2026-09-21).

Pourquoi : chaque requete du modele relit tout son contexte. Le cout d'une sortie n'est pas sa taille, c'est sa taille
multipliee par le nombre de requetes qui la relisent avant la compaction. Constate sur la session Codex `01a0bf95`
(646 M tokens d'entree, 4 548 requetes, 71 compactions) : 47,8 % de l'entree est la relecture de sorties d'outils
encore en contexte ; `Get-Content` 15,5 %, `codegraph_explore` 13,2 %, `rg` 5,8 %.

| Nombre | Nature | Definition |
|---|---|---|
| Socle | MESURE | entree de la premiere requete de chaque fil (instructions, outils, skills, consigne), relue par toutes ses requetes ; et entree de la premiere requete d'une fenetre apres compaction (socle + resume). Session citee : 34 500 tokens par fil, 24,4 % de l'entree ; 53 335 tokens (mediane) apres compaction |
| Tokens ajoutes par une sortie | MESURE (releves du client) | entree(reponse qui consomme la sortie) - entree(reponse precedente) - sortie(reponse precedente), meme agent, meme fenetre de contexte. Verifie sur 262 sorties de moins de 120 caracteres : ecart median de 24 tokens |
| Part d'un appel dans ce gain | CALCUL | quand une reponse consomme plusieurs sorties : prorata des parts deja calculees a l'import (taille livree) |
| Requetes suivantes de la fenetre | MESURE | rang de la derniere reponse de la fenetre (demande de compaction comprise : elle relit tout) - rang de la reponse consommatrice |
| Relus ensuite (fois-tokens) | CALCUL | tokens ajoutes x requetes suivantes ; en cache pour l'essentiel, mais compte dans l'entree et dans le quota |
| Debut de fenetre | MESURE + seuil | tokens ajoutes par les sorties consommees dans les `report.context.recovery_responses` (8) premieres reponses apres une compaction |
| Relecture apres compaction | FAIT | meme agent, meme ressource (chaque chemin d'une lecture shell `Get-Content`/`cat`/`type`..., cible d'une lecture, outil MCP de lecture et ses parametres), deja lue dans une fenetre ANTERIEURE |

Etat d'une relecture : `modifie entre-temps` (une ecriture du chemin, par n'importe quel agent, observee entre les deux
lectures) ; `contenu identique` (les deux appels ne lisent que cette ressource et ont la meme empreinte de contenu) ;
`contenu different ou autre extrait` (memes conditions, empreintes differentes : `-Tail 160` puis `-TotalCount 88`) ;
`aucune modification observee` (le reste : une commande qui lit plusieurs fichiers n'a qu'une empreinte ; ne prouve
pas un contenu identique).

Limites, dites dans le rapport : un message (utilisateur, autre agent) entre entre les deux reponses est compte avec la
sortie (`mixed` : 1 126 appels et 1,7 M tokens sur 9,0 M dans la session citee) ; la premiere reponse d'une fenetre
n'a pas de reponse precedente comparable ; les relectures entre agents ne sont pas comparees (chaque agent a son
contexte). Relire apres une compaction est attendu : la section dit ce que cela coute et ce qui revient a chaque
fenetre (le brief colle par l'utilisateur relu a l'identique 15 fois, un document de module jamais modifie relu 41
fois par 4 agents), pour decider quoi rendre durable ou plus court. Elle ne dit pas qu'une relecture etait inutile.

Dans `compare` (reference et avant/apres), les memes calculs, jamais refaits, ranges par NATURE dans trois blocs separes : **releves exacts** (releves du client et comptes de faits : entree par requete, socle, premiere requete apres compaction, tokens ajoutes et relus au total, debut de fenetre, execs coupes, points de quota, tours coupes ; les totaux ajoutes et relus ne dependent pas du partage entre appels), **attributions reconstruites** (partage d'une reponse entre ses appels, ressource reconnue d'une fenetre a l'autre, etat d'une relecture, familles d'outils) et **scenarios d'economie** (hypotheses en bornes hautes, derivees des attributions, jamais des gains). Deux lignes sont testees avec intervalle de confiance : relectures apres compaction pour 1 000 reponses (grappes : fenetres concernees) et execs coupes pour 1 000 appels (seulement si les coupes sont relevees sur toute la periode : sinon un faux zero). Sur une tranche de temps, le socle n'est releve que pour un fil dont la premiere requete est dans la tranche, et la premiere fenetre visible d'un fil entame est ecartee des mesures de debut de fenetre. Un gain ne se lit que dans les releves exacts de deux periodes de travail comparable.

Quota et tours coupes : premier et dernier pourcentage du quota ecrit par le client, et tours dont la ligne de fin
porte une erreur du client (`usage_limit_exceeded`...). Cout mesure sur la meme session (261 Mo de rollouts, mediane
de 3 passes) : import 7,43 s -> 7,52 s, +328 evenements (+1,2 %), stockage 50 -> 51 Mo ; section calculee en 0,05 s,
rapport 0,94 s -> 0,98 s.

## Entre agents : messages, requetes qui n'emettent que des messages, ressources partagees (section du rapport, sans verdict)

Pas un detecteur : aucune regle, aucun signalement. Les detecteurs raisonnent agent par agent ; cette section decrit ce
qui se passe ENTRE les agents d'une session (`stats.exchanges` dans l'export JSON). Absente pour une session a un seul
agent. Constate sur la session Codex `01a0bf95` (4 agents, 1 141 messages, 4 548 requetes) : 1 140 requetes (25,4 % de
l'entree) n'emettent qu'un message ; 517 messages s'enchainent sans appel d'outil hors messagerie entre deux ; la
premiere requete d'une fenetre d'un sous-agent passe de 40 000 a 76 000 tokens a mesure qu'il recoit des messages
(r = 0,997), pas celle du fil principal (r = 0,92, pente 20 fois plus faible).

Libelles strictement observables. Le texte des messages est chiffre par le fournisseur : il reste semantiquement
indetermine, et AgentWatch ne cherche pas a le lire. « N'emet que des messages » ne dit pas « ne travaille pas » (le
modele raisonne aussi dans ces requetes) ; « message adresse a un autre agent apres une reception » ne dit pas que le
meme contenu est relaye ; « message adresse a l'emetteur » ne dit pas qu'il lui repond.

| Nombre | Nature | Definition |
|---|---|---|
| Rapprochement envoi -> reception | FAIT | meme `payload_fp` des deux cotes (le jeton chiffre ecrit a l'envoi est celui ecrit a la reception : 1 139 sur 1 139). Jamais par l'ordre : par file emetteur -> destinataire il se trompe 107 fois sur 1 139, et encore 17 fois en separant les nouvelles taches des messages. Sans empreinte des deux cotes (import anterieur), rien n'est rapproche et le rapport le dit |
| Envoi jamais entre | FAIT | envoi sans reception de meme empreinte (fin de session, destinataire arrete). Le statut de l'appel d'envoi est joint : un appel que le client a REFUSE (`collab tool failed: agent thread limit reached`, constate sur `01a0bb58`) est dit « appel d'envoi en echec : rien n'a ete envoye », pas un message perdu |
| Reception sans envoi | FAIT | reponse finale d'un tour (`FINAL_ANSWER`) : le client la remet au parent, aucun appel d'envoi n'existe |
| Delai d'entree | MESURE | instant de la ligne `agent_message` chez le destinataire - instant de l'appel d'envoi ; un message n'entre qu'a la requete suivante du destinataire (une commande longue le retarde). `report.exchanges.slow_delivery_seconds` (60) |
| Croisement | FAIT | A ecrit a B alors qu'un message de B pour A est parti et n'est pas encore entre chez A |
| Requete qui n'emet que des messages | MESURE | reponse du modele dont TOUS les appels emis portent un message a un autre agent ; son entree est lue dans son releve : c'est tout le contexte de l'agent, relu pour un seul message |
| Premiere reponse apres l'entree d'un message | FAIT (temps) | par ce qu'elle EMET : `tool_call` (au moins un appel d'outil hors messagerie), `message_to_sender` (que des messages, dont un a l'emetteur), `message_to_other_agent` (que des messages, aucun a l'emetteur), `text_only`, `no_later_response` ; un enchainement dans le temps, pas un lien de sens |
| Chaine | FAIT (temps) | messages rapproches enchaines ou la premiere reponse de chaque destinataire n'emet que des messages ; tokens d'entree des reponses intermediaires. « Sans appel d'outil hors messagerie », pas « sans travail » |
| Debut de fenetre face aux messages recus | MESURE + CALCUL | entree de la premiere requete de chaque fenetre apres compaction, face au cumul des caracteres transmis recus jusque-la : pente (token par caractere) et correlation, a partir de 3 fenetres. Pente nette et r proche de 1 : compatible avec des messages recus conserves a travers les compactions de cet agent |
| Messages gardes par la compaction | FAIT | `replacement_agent_messages` de chaque ligne `compacted` (messages d'autres agents gardes tels quels), face aux messages recus jusque-la. Constate sur trois sessions : un sous-agent les garde tous (24 sur 24, 29 sur 29, 210 sur 210), le fil principal aucun (0 sur 95, 0 sur 584). Releve a l'import depuis le 2026-09-21 ; absent d'un import anterieur |
| Estimation du contexte conserve | ESTIMATION | surplus de la premiere requete de chaque fenetre par rapport a la premiere fenetre apres compaction, multiplie par les requetes de la fenetre ; somme des agents dont la compaction garde des messages d'agents (fait ci-dessus) ou, quand ce fait n'a pas ete importe, dont la correlation atteint `report.exchanges.retained_min_correlation` (0,95) sur au moins `retained_min_windows` (5) fenetres : sur 3 points, |r| >= 0,95 arrive une fois sur cinq au hasard (constate sur `01a0bb58` : un agent retenu sur r = 0,999 avec 3 fenetres). Session citee : 47,3 M tokens (3 sous-agents). Recouvre en partie l'entree des requetes qui n'emettent que des messages (elles relisent deja ce contexte) : les deux nombres ne s'additionnent jamais, aucun n'est un gain, et aucun scenario d'economie n'en est derive |
| Reference commune | FAIT + MESURE | ressource lue par au moins 2 agents et jamais modifiee dans la session ; tokens ajoutes par les lecteurs autres que le premier |
| Passation | FAIT + MESURE | lecture par un agent d'une ressource dont la DERNIERE modification observee est d'un autre agent ; tokens ajoutes ; la lecture suit-elle un message de l'auteur au lecteur entre entre l'ecriture et la lecture ? |
| Ecriture partagee | FAIT | fichier modifie par au moins 2 agents, avec les suites d'ecritures consecutives de chaque agent, calculees sur TOUTES les ecritures (couper la liste aux 12 premieres montrait un seul agent : constate sur `01a0bb58`) |

Les ressources sont celles de la section « Contexte » (chemins d'une lecture shell, cible d'une lecture, outil MCP de
lecture et ses parametres ; chemins d'un patch). Les tokens d'un appel qui lit plusieurs ressources sont partages a
parts egales entre elles. Limites, dites dans le rapport : le texte des messages est chiffre par le fournisseur (ou
garde en empreinte), donc leur objet n'est pas connu et une consigne redonnee d'un message a l'autre n'y est pas
detectable ; une lecture apres l'ecriture d'un autre agent ne dit pas si elle etait necessaire (relire le travail d'un
autre avant de l'executer est souvent voulu) ; aucune de ces lignes n'est un jugement.

Dans `compare`, trois lectures ne se confondent plus (constate le 2026-09-21 sur une tranche ou un seul fil travaillait : un tiret partout, alors que « 0 message envoye » et « 4 messages entres » etaient des faits) : un **nombre**, zero compris, est un fait observe ; **`non releve`** = la donnee manque (import anterieur au releve, mesure enregistree avant l'indicateur : la cle est absente) ; **`sans objet`** = l'indicateur n'a pas de sens pour la periode (aucun echange entre agents, aucun message rapproche a enchainer, aucune compaction, aucun point de quota). Une tranche ou un seul fil travaille garde sa section « Entre agents » des qu'un message d'agent y est envoye OU recu : les receptions sont conservees meme sans aucun envoi.

Dans `compare` (reference et avant/apres), memes calculs, jamais refaits, ranges par nature : **releves exacts**
(messages pour 100 requetes, requetes qui n'emettent que des messages pour 100 requetes, leur entree en tokens et en
part de l'entree, part des messages enchaines, part des premieres reponses qui n'emettent que des messages, part des
livraisons lentes ; `-` quand le rapprochement n'est pas disponible) ; **attributions reconstruites** (estimation du
contexte conserve, en tokens et par requete des fenetres concernees ; tokens des lectures apres l'ecriture d'un autre
agent ; tokens des lectures d'une reference commune par un autre que le premier lecteur). Aucun scenario d'economie.

Releve et estimation restent separes. Les messages gardes par une compaction sont des COMPTES releves (`kept_messages_fact`, et dans `compare` la ligne « messages d'agents gardes a la derniere compaction, sur 100 recus ») ; l'estimation du contexte conserve est un CALCUL en tokens, qui dit sa base agent par agent (`agent_basis` : « fait releve a la compaction » ou « correlation »).
Un champ non importe n'est jamais un zero : pour une session importee avant ce releve, `compactions` vaut `null`, le rapport dit « NON RELEVES … ce n'est pas un zero » et nomme la methode disponible (correlation, avec sa limite) ; dans `compare`, une session ou une reference sans ce fait affiche `-`. La reference du 2026-09-21 (`01a0bf95`) est dans ce cas : estimation par correlation sur 17 a 18 fenetres, fait confirme par lecture directe de ses rollouts (210/210, 176/176, 184/184 ; principal 0/584) mais non importe. Si une comparaison exige un jour une methode homogene, deux voies, a preparer seulement alors et en gardant la reference precedente : reimport cible de cette seule session (le fait s'y ajoute, l'estimation ne change pas de valeur), ou recalcul de la periode comparee par correlation.

Cout mesure sur la meme session (261 Mo de rollouts, mediane de 3 passes) : import 7,67 s -> 7,39 s, meme nombre
d'evenements (27 577), stockage 52,85 -> 52,70 Mo (un contenu chiffre ne porte plus de signature de similarite) ;
statistiques du rapport 0,16 s -> 0,33 s. Une session importee avant ce releve doit etre reimportee pour le
rapprochement ; le reste de la section (requetes d'echange, ressources partagees) se calcule sans reimport.

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
