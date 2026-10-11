# Connecter AgentWatch a Agenttrail Kitchen

AgentWatch peut alimenter la vue Kitchen avec les observations de son stockage
local. Agenttrail assure l'affichage; AgentWatch conserve la collecte et les
analyses. Aucun prompt, commande shell, resultat brut, raisonnement, empreinte,
token ou secret de connexion n'est transmis dans les evenements du relais.

La connexion utilise l'API locale version 2 de **Kitchen 0.1.0-alpha.3**.
Elle ne concerne pas le service Map. Kitchen et Map ont actuellement des
adaptateurs distincts, avec des limites de coexistence documentees en amont.

## Demarrage

Installer la Kitchen officielle dans un dossier reserve au compagnon:

```powershell
npm install --prefix C:\tools\agenttrail-runtime --ignore-scripts --no-audit --no-fund agenttrail-kitchen@0.1.0-alpha.3
```

Depuis le depot AgentWatch, lancer une Kitchen dediee au relais:

```powershell
node examples/agenttrail_kitchen.mjs --package C:\tools\agenttrail-runtime\node_modules\agenttrail-kitchen --project C:\dev\mon-projet --state-dir C:\tools\agenttrail-state
```

Le lanceur utilise le paquet officiel sans le modifier, desactive son observation
des journaux natifs et isole son dossier personnel interne. Le suivi des fichiers
du projet reste disponible. Il reutilise une Kitchen dediee deja en marche avec
le meme dossier d'etat. Il refuse de reutiliser une Kitchen qui collecte deja
les journaux, afin de garder un seul flux d'observations.

Dans un autre terminal:

```powershell
python -m agentwatch agenttrail --project-dir C:\dev\mon-projet --state-dir C:\tools\agenttrail-state --follow
```

Ouvrir l'URL localhost imprimee par Kitchen. Le relais lit uniquement le spool
et les segments AgentWatch. La collecte doit donc etre active: hooks Claude Code
deja configures ou `python -m agentwatch import-rollouts --follow` pour Codex.
Ne pas lancer un second importeur si le suivi AgentWatch est deja en marche.

Le relais n'installe aucun hook et n'execute aucune commande observee. Ses
modules ne sont pas importes par le chemin chaud de collecte.

## Contrat de projection

| AgentWatch | Kitchen |
|---|---|
| `codex`, `claude-code` | fournisseurs `codex`, `claude` |
| session, identite de sous-agent observee | session et lien parent; identite manquante ignoree |
| debut/fin de session et de tour | cycle de vie observe |
| debut/fin/echec d'outil | activite, fichier, etat d'erreur |
| fin d'outil sans statut etabli | appel ferme, puis etat inconnu |
| arret d'un sous-agent | tour rendu; aucune fin definitive de session supposee |
| fichier situe dans le projet choisi | chemin du fichier; aucune cible hors projet |

Les chemins locaux masques sous la forme `~/...` sont resolus pour le serveur
local. Les cibles relatives gardent leur ancrage au projet source, meme lorsqu'un
outil a travaille depuis un sous-dossier.

Les phases non prises en charge sont ignorees. Le stockage normalise ne conserve
pas le texte des todos natifs: le relais ne fabrique donc ni tickets ni plats.
Les findings A-G, tokens, couts et propositions de remplacement restent dans les
rapports AgentWatch (`report`, `trends`). L'API Kitchen actuelle ne les affiche pas.
Une fin de tour n'etablit ni livraison, ni validation, ni deploiement.

## Fraicheur, transport et diagnostic

Par defaut le relais transmet seulement les evenements posterieurs a son
demarrage. Il peut prendre explicitement jusqu'a 120 secondes de contexte:

```powershell
python -m agentwatch agenttrail --project-dir C:\dev\mon-projet --recent-seconds 120 --dry-run
```

`--dry-run` produit le JSONL par liste blanche sans contacter Kitchen. Sans
`--follow`, la commande effectue un seul passage. Le suivi sonde toutes les deux
secondes (`--interval`, minimum 0,5 s), sans reecrire ou compacter les sources.

Les identifiants sont stables, y compris apres compaction. Une panne de transport
conserve les evenements en attente et retente l'envoi; ceux de plus de 120 secondes
sont abandonnes et comptes. La file est bornee a 4 000 evenements. Un redemarrage
du relais repart de l'heure actuelle et ne rejoue pas un historique complet.
Apres redemarrage de Kitchen, la nouvelle cle locale est relue et le projet est
rattache. Les refus et doublons de l'API sont comptes ensemble, sans les declarer
acceptes.

Le serveur amont date `/api/hook` a la **reception** et indique sa source comme
`hook`, meme pour un evenement relaye depuis un rollout. Les heures et la source
originales restent consultables dans AgentWatch; Kitchen montre ici l'heure de
relais. La borne de fraicheur empeche de presenter de vieux travaux comme actifs.

La cle est lue dans `<state-dir>/server.json`, jamais dans les arguments de
commande. Seule l'adresse HTTP `127.0.0.1` est construite. Les proxies et
redirections sont desactives; un service incompatible est refuse avant l'envoi
de la cle. Ctrl+C arrete chaque processus; rien n'est installe au demarrage du PC.

## Verification

```powershell
python -m unittest tests.test_agenttrail
$env:AGENTTRAIL_KITCHEN_PACKAGE = 'C:\tools\agenttrail-runtime\node_modules\agenttrail-kitchen'
python -m unittest tests.test_agenttrail_integration
```

Le test optionnel lance la vraie API du paquet fourni sur un projet temporaire
avec des evenements synthetiques. Il verifie les identites, les erreurs, les
statuts inconnus, l'absence de todos inventes et la conservation du projet.
Cela verifie le transport et le contrat; le rendu WebGL est a verifier dans le
navigateur. Le projet amont indique encore une validation Windows en attente.

Sources amont: [observabilite](https://github.com/sodiumsun/agenttrail/blob/main/docs/OBSERVABILITY.md),
[API locale](https://github.com/sodiumsun/agenttrail/blob/main/packages/kitchen/src/server.mjs),
[release](https://github.com/sodiumsun/agenttrail/blob/main/docs/kitchen/RELEASE.md).
