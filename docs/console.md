# Console AgentWatch par projet

La console utilise AgentTrail **Map**, repris sous licence MIT a la revision
`e4ba2daa270abd7ecb51565bbfb12386f47ea418`. Elle possede sa selection de projet,
son historique, ses analyses, sa chronologie et son chat. Le renderer et la licence
d'origine sont livres dans `agentwatch/console/vendor/agenttrail/`.

## Demarrer et choisir le dossier

```powershell
python -m agentwatch console
```

La page locale est `http://127.0.0.1:5330/`. Choisir le dossier racine du travail,
avec le champ de chemin ou le navigateur de dossiers. Le bouton **+** ajoute un autre
projet. Les projets enregistres restent suivis lorsque l'on change la vue ou le projet actif.
Un dossier sans depot Git fonctionne aussi. Deux racines imbriquees sont distinctes :
une observation explicite est attribuee a la racine enregistree la plus precise.

```powershell
python -m agentwatch --home C:\Observations\AgentWatch console --project-dir C:\Projets\Exemple --state-dir C:\Etat\Console --port 5330
```

`--home` designe le stockage **source** du collecteur AgentWatch existant. La console le
lit sans compaction, sans modification et sans rejouer de commande. Elle lit aussi les
observations normalisees presentes dans le `.agentwatch` d'un projet. Elle ne configure
pas de hooks automatiquement. Le suivi natif reste soumis a la configuration et a
l'intervalle du collecteur existant ; son etat est visible dans le bouton de suivi.

`examples/console.ps1` fournit le meme lancement au premier plan sous Windows.
L'arret par Ctrl+C termine les observateurs Map et les reponses de chat appartenant
a cette console. Le collecteur preexistant reste independant.

## Stockage et attribution

Chaque racine possede :

- `.agentwatch/history.sqlite3` : observations dedupliquees, appels correles, sessions,
  analyses, changements de fichiers, extraits natifs conserves et conversations ;
- `.agentwatch/reports/` : derniers rapports de session Markdown/JSON et instantanes
  de tendances/comparaisons, relisibles dans Analyses apres redemarrage ;
- `.agentwatch/file-events.jsonl` : journal des notifications de fichiers du watcher Map,
  y compris les rafales depassant son apercu de douze fichiers ;
- `.agentwatch/map-state.json` et `map.log` : etat et diagnostic de l'observateur ;
- `.agentwatch/feedback.json` : avis pertinents/faux positifs, compatibles avec AgentWatch.

Le dossier est exclu via `git info/exclude`, y compris pour les worktrees. Aucun fichier
de configuration suivi par Git n'est reecrit. Pour un depot sans Git, aucune exclusion
n'est necessaire. Le dossier d'etat central conserve seulement la liste des racines et
des references de lecture/attribution, pas les traces ou les conversations des projets.
Un verrou empeche deux consoles de consommer simultanement les memes curseurs.

Le routage utilise le workdir/projectPath explicite, puis le cwd, la cible ou le project_dir.
Une fin d'appel revient au dossier de son debut. Une session native dont le cwd est
ailleurs peut fournir un workdir explicite : les appels suivants sans dossier exploitable
sont alors attribues a ce workdir **a la date de l'observation**. Ce cas porte la mention
`inferred_session_workdir` dans les preuves ; le cwd original est conserve. Une observation
qui ne peut pas etre attribuee reste non attribuee, jamais rattachee au projet affiche.

Le bouton de suivi permet **Arreter ce suivi** : il retire la racine de la console et
arrete son observateur. Toutes les donnees du `.agentwatch` restent dans ce dossier.
Ajouter de nouveau la meme racine retrouve l'historique. Il n'y a pas de purge automatique.
Au demarrage, une racine temporairement indisponible est signalee dans le suivi et reste enregistree,
meme si un autre projet est ajoute pendant son absence.

## Map, Timeline et Analyses

Map associe le `PLAN.md` du projet aux changements observes et presente les derniers
appels ainsi que les MCP utilises. Les APIs de setup, hooks, lancement d'agents et
decouverte de projets voisins du fork Map sont desactivees. Sans `PLAN.md`, l'arbre et
les appels restent disponibles. Aucun plan ou fichier AGENTS/CLAUDE n'est genere au demarrage.

Timeline filtre par session, client, resultat, MCP, texte et dates. Le pas peut aller
d'une seconde a une semaine. Lire/Pause et la vitesse rejouent les intervalles : **Cet
intervalle** montre les appels qui le traversent, **Cumul depuis le debut** montre
l'accumulation. Le curseur et **Tous les appels** permettent de revenir a la plage complete.
Une duree conserve sa source ; la taille de sortie est en octets, pas en tokens.

Le **temps actif** est l'union des intervalles observes. Le **temps outils** est leur
somme et peut etre plus grand a cause du parallelisme. Les appels ouverts, les durees
inconnues et les horaires juges non fiables ne sont pas transformes en temps mesure.
Le detail expose les observations et la provenance. **Lire et conserver la sortie**
relit un extrait de la source native verifiee, masque les motifs de secrets connus, puis
l'enregistre dans ce projet ; aucune commande historique n'est executee.

Analyses reprend les calculs AgentWatch : detecteurs A-G, statistiques, agents,
couverture, echanges/contexte, graphe observe, remplacements sous conditions, tendances
et comparaisons. Les avis peuvent etre marques pertinents ou faux positifs, puis effaces.
Les exports de session sont complets pour la revision analysee. L'export d'une tendance
ou comparaison correspond au resultat choisi. Un rapport en cours de recalcul est signale.

## Chat et contexte

Choisir Codex ou Claude et, si souhaite, un modele accepte par la CLI installee.
La connexion est celle de la CLI du fournisseur. La detection de l'executable ne prouve
pas que son authentification est valide ; une erreur de connexion est affichee et conservee.

Le chat fonctionne en consultation : repertoire d'execution neutre, outils de shell,
MCP, hooks et personnalisations de projet desactives. Codex utilise le sandbox read-only
avec la configuration utilisateur ignoree ; Claude utilise safe-mode et une liste
d'outils vide. Les traces sont traitees comme des preuves, pas comme des instructions.

Choisir les sections de contexte, la plage et le nombre d'appels. Depuis Analyses, la
session affichee sert de perimetre ; depuis Timeline, les filtres de la page sont repris.
**Preparer le contexte** montre le JSON exact, le budget, les omissions et son empreinte.
Envoyer conserve cet instantane immuable avec le message. **Voir le contexte** sur un
ancien message retrouve ce qui a ete transmis a ce moment-la, meme si de nouvelles
observations sont arrivees. Les conversations precedentes du meme projet peuvent etre reprises.

Les sorties natives ne sont jointes que si elles ont ete conservees et si leur section
est selectionnee. Le masque protege des motifs connus de secrets ; il ne garantit pas
la detection de toute information sensible. Le contexte choisi est transmis au fournisseur
uniquement lors de l'envoi. Aucun agent n'est autorise a agir dans le dossier depuis ce chat.

## Bornes visibles et interpretation

- Les observations attribuees sont conservees sans limite de retention. Le backfill est
  incremental, avec une passe prioritaire pour les arrivees recentes. Un historique volumineux
  peut prendre du temps ; les analyses indiquent leur revision et leur perimetre.
- L'analyse d'une session utilise par defaut les 50 000 dernieres observations. Le rapport
  annonce le nombre conserve, analyse et la troncature. Ce plafond peut etre configure dans
  `.agentwatch/config.json` avec `console_analysis_event_limit` ; augmenter le plafond augmente
  le cout des detecteurs. Le stockage brut reste disponible pour une analyse ulterieure.
- La timeline contient au plus 720 intervalles ; une plage trop grande exige un pas plus
  large. Les appels sont pages, avec un curseur stable. Les selecteurs de session affichent
  les 500 plus recentes ; les autres restent en stockage.
- Les tendances examinent par defaut les 100 sessions recentes, jusqu'a 1 000 sur choix.
  Les 100 instantanes historiques les plus recents sont proposes dans le selecteur ; tous
  les fichiers de resultats restent dans reports. Les arbres structures affichent 120
  elements par niveau et signalent cette borne ; le JSON permet de lire le reste.
- La carte affiche un arbre borne a 4 000 fichiers et huit niveaux. Les notifications
  de fichiers reposent sur le watcher du systeme, qui peut fusionner des ecritures. Elles
  ne constituent pas un historique du contenu ou des diffs. Le mode du watcher est expose
  dans ses donnees ; sur une plateforme sans watcher recursif, le suivi se limite au plan.
- La relecture d'une sortie est bornee a 20 000 caracteres dans l'interface ; l'API permet
  jusqu'a 200 000. Le resultat annonce sa completude et sa source. Un journal deplace ou
  une identite qui ne correspond plus produit une erreur explicite. Le contexte joint
  possede lui aussi un budget et une estimation de tokens, jamais une mesure de facturation.
- Les tokens natifs sont des releves de session. Pour une session traversant plusieurs
  racines, ils ne deviennent pas un cout mesure par projet. Les comparaisons descriptives
  et les scenarios de remplacement ne prouvent pas un gain causal.

La verification comprend des tests de routage, deduplication/reprise, ecritures concurrentes,
durations paralleles, pagination, contexte immuable, sorties natives, isolation HTTP et
deux vrais observateurs Node. Un passage sur ces scenarios ne prouve pas les performances
d'un depot arbitrairement grand ou l'acceptation de l'interface par son proprietaire.
