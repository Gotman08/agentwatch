# Connecter AgentWatch au tableau AgentTrail Map

AgentWatch relaie ses observations locales Codex et Claude vers le tableau
de projet Map. Map affiche les composants et dependances de `PLAN.md`, l'arbre
des fichiers, les modifications observees sur disque et les sessions reliees.
AgentWatch conserve la collecte et ses analyses.

## Lancer le tableau

Prerequis : Python 3.11+, Node 20+ et la source officielle AgentTrail Map.
La revision verifiee est `e4ba2daa270abd7ecb51565bbfb12386f47ea418`.
Le paquet npm `agenttrail@0.2.0` etiquetait tous les hooks comme Claude ;
le lanceur exige une source avec `ev.agent` pour afficher Codex correctement.

```powershell
git clone https://github.com/sodiumsun/agenttrail.git C:\tools\agenttrail-map
git -C C:\tools\agenttrail-map checkout --detach e4ba2daa270abd7ecb51565bbfb12386f47ea418
```

Depuis AgentWatch, lancer le serveur officiel avec un dossier d'etat reserve :

```powershell
node examples/agenttrail_map.mjs --package C:\tools\agenttrail-map --project C:\dev\mon-projet --state-dir C:\tools\agenttrail-map-state --port 5330
```

Le lanceur utilise le serveur Map sans le modifier. Il isole son etat dans
`--state-dir/observer-home/.agenttrail/`, ecrit la registration locale dans
`--state-dir/server.json` et n'installe aucun hook. Le port peut augmenter si
5330 est occupe : utiliser celui du journal ou de `server.json`.

Dans un second terminal :

```powershell
python -m agentwatch --home C:\chemin\agentwatch-data agenttrail --project-dir C:\dev\mon-projet --port 5330 --follow
```

Ouvrir `http://127.0.0.1:5330/`. Ctrl+C arrete chaque processus ;
le collecteur AgentWatch est independant. Le tableau fonctionne aussi sans
plan, mais la carte de composants exige un `PLAN.md` adapte au projet.
Un plan du depot AgentWatch est fourni, avec fichiers de reference, dependances
et statut explicite des taches. Le connecteur ne change pas ces coches.

## Contrat des observations

| Observation AgentWatch | Hook local Map |
|---|---|
| Debut de session, de tour ou de sous-agent | SessionStart |
| Debut d'appel | PreToolUse |
| Fin d'appel ou echec | PostToolUse, avec result=success/error/denied/timeout/interrupted/unknown dans le detail |
| Fin de tour, interruption ou fin de sous-agent | Stop |
| Fin de session | SessionEnd |

Chaque identite de session est stable, hachee et separee par client ; les
sous-agents ont leurs propres cartes. Un nouveau tour reactive la meme carte.
Map rassemble les fins de tour et de session dans son etat `ended` :
cela n'etablit pas la terminaison du processus agent ni la livraison d'une tache.
Sa vue n'expose pas la hierarchie parent/enfant du schema AgentWatch.

Seuls le client, l'identite de session, le dossier local du projet, le nom de
l'outil, sa cible fichier interne et son statut sont transmis. Prompts,
commandes shell, parametres, resultats bruts, empreintes, tokens et diagnostics
restent dans AgentWatch. Une cible de fichier est un detail d'outil ; aucun
`file_path` n'est envoye au hook Map, car Map l'interpreterait comme une activite
de fichier meme pour une lecture. Les surbrillances reposent sur son observateur
de fichiers. Cette precaution limite aussi l'attribution automatique d'une
session a un composant et ses traces de passage entre composants.

Le relais lit le spool et les segments sans ecriture ni compaction. Il
dedoublonne leurs copies par identifiant d'evenement. Il filtre par dossier
d'execution ou cible de fichier explicitement comprise dans le projet. Pour une
cible relative, il utilise le projet source de l'evenement comme ancrage.
Les chemins `~/...` masques par la collecte sont resolves localement.

Par defaut, le relais commence au lancement. `--recent-seconds 120` peut inclure
une fenetre de demarrage bornee ; les observations vieilles de plus de deux
minutes ne sont jamais envoyees. Map les date a la reception : ses durees
d'outils refletent le relais et ne constituent pas des mesures de performance.
Un outil parallele peut remplacer l'outil courant dans la carte native Map.

Avant les envois, le relais verifie `/whoami`, le projet, le port et `/model`.
Il se connecte exclusivement a `127.0.0.1`, sans proxy ni redirection. L'API
`/hook` de Map n'utilise pas d'authentification et ne confirme pas l'acceptation
de chaque evenement : un HTTP 200 est compte comme transmis. Les observations
attendent pendant une indisponibilite detectee avant l'envoi, dans la limite
de leur fraicheur. Un POST dont la reception est incertaine n'est pas rejoue,
car Map ne dedoublonne pas ces hooks ; un avertissement indique cette lacune.

Ne pas doubler les hooks natifs Map et le relais pour les memes sessions.
Le connecteur n'exporte pas de todos et ne transforme pas un appel termine
en tache terminee. Les detecteurs A-G, les couts et les gains restent dans
les rapports AgentWatch ; ils ne sont pas ajoutes au tableau Map.

## Verification

```powershell
$env:AGENTTRAIL_MAP_PACKAGE = 'C:\tools\agenttrail-map'
python -m unittest tests.test_agenttrail tests.test_agenttrail_integration
python -m unittest discover -s tests -t .
```

Le test avec le vrai serveur utilise un depot et un stockage temporaires :
sessions Codex/Claude correctement nommees, resultat inconnu et erreur visibles,
aucune lecture presentee comme modification, modification reelle detectee,
coches du plan conservees, sources intactes, aucun hook installe et arret propre.

Documentation amont :
[AgentTrail Map](https://github.com/sodiumsun/agenttrail#run-the-project-map),
[contrat d'observabilite](https://github.com/sodiumsun/agenttrail/blob/main/docs/OBSERVABILITY.md).
