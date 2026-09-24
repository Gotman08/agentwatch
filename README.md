# AgentWatch

Observateur local et passif pour **Claude Code** et **Codex**. Il enregistre les appels
d'outils via les hooks officiels des deux clients, les normalise dans un schema commun,
puis produit des rapports Markdown/JSON avec sept detecteurs deterministes :

- **A** travail de lecture refait (meme fichier lu via `Read`, `cat` ou `sed -n`, meme
  recherche via `Grep` ou `rg`) et executions relancees sans changement ;
- **B** boucles d'erreurs ;
- **C** operations regroupables ;
- **D** sequences candidates a une automatisation deterministe ;
- **E** service externe manipule a la main (ssh, SLURM, curl, gh, cloud, docker) alors
  qu'un outil MCP lie existe ou manque ;
- **F** consignes redonnees a la main (utilisateur, orchestrateur de sous-agents), a partir des rollouts
  Codex : empreintes de paragraphes, jamais le texte ;
- **G** appels repetes, tous outils : pourquoi chacun est refait (attente, service indisponible, modification,
  compaction, rien), a quel rythme, ce qu'il a appris, et si l'agent ou l'outil peut faire mieux (cadence
  simulee, attente bloquante, delai plus long) ; plus le rythme de chaque outil.

## A quoi ca sert

A faire gagner des tokens et du contexte au modele, pas a produire des statistiques. Le
gaspillage type : le modele refait a la main, en plusieurs appels couteux (chercher ou est
la cle, l'ecrire, `ssh` vers le calculateur, relire une sortie brute), ce qu'un outil MCP ou
une skill ferait en un appel structure (le serveur MCP du calculateur). Chaque appel evite
est une commande de moins a construire et une sortie de moins a lire ; un outil qui expose
l'operation donne en plus au modele un resultat structure, donc plus de contexte utile et
moins d'erreurs. AgentWatch cherche ces occasions dans les sessions reelles, les chiffre et
dit ce qui les remplacerait : un outil MCP a utiliser ou a ecrire, une skill, une regle de
projet. Il ne construit rien lui-meme.

L'analyse porte sur des **unites de travail** (operation normalisee sur une cible,
quel que soit l'outil ou la forme de la commande) avec une empreinte du contenu obtenu,
pas seulement sur des appels identiques. La commande `trends` regroupe ensuite les
signalements de toutes les sessions d'une fenetre par motif stable et compte dans combien
de sessions, de projets et de clients chacun revient. Voir `docs/detectors.md`.

AgentWatch ne bloque rien, ne reecrit rien, ne renvoie rien au modele, n'execute jamais
ce qu'il propose et ne quitte jamais la machine. Python 3.11+, bibliotheque standard.

## Demarrage

```bash
python -m agentwatch doctor
```

```bash
python -m agentwatch configure --client claude-code --dry-run
```

```bash
python -m agentwatch configure --client codex --dry-run
```

Ajoutez `--apply` pour ecrire (sauvegarde automatique). Codex exige ensuite d'approuver
le hook via `/hooks`. Puis, apres une session :

```bash
python -m agentwatch sessions
```

```bash
python -m agentwatch report --latest --format markdown
```

(`--session <id>` pour une session precise ; `--format json` pour l'export complet.)

Sur plusieurs sessions (un signalement isole ne justifie rien ; ce qui justifie un script,
une skill ou une regle de projet, c'est un motif qui revient) :

```bash
python -m agentwatch trends --days 7
```

Top des gaspillages recurrents, puis le detail par projet, par client et par session
(`--project <texte>`, `--client codex`, `--min-sessions 3`, `--format json`, `--days 0` = tout).

Ou part l'entree d'une longue session (Codex) : le rapport de session a une section « Contexte » qui mesure ce que
chaque sortie d'outil ajoute au contexte, combien de requetes la relisent avant la compaction, ce qui est relu apres
chaque compaction, ce que le client a coupe avant de le donner au modele, le quota consomme et les tours coupes par
le client. Descriptive, sans verdict : voir `docs/detectors.md`.

Ce qui se passe entre les agents d'une session : la section « Entre agents » rapproche chaque message envoye de sa
reception (par l'empreinte du contenu transmis, jamais par l'ordre), compte les requetes qui n'emettent que des
messages et ce qu'elles relisent, met la premiere requete de chaque fenetre face aux messages recus, et decrit les
ressources lues ou modifiees par plusieurs agents (reference commune, passation, ecriture partagee). Libelles
observables, sans verdict : le texte des messages, chiffre, reste indetermine.

Cout reel en tokens (Claude Code) : lu dans les transcripts du client, nombres et
identifiants seulement, jamais le texte ; les rapports classent alors par tokens mesures et
non par octets. `transcripts.auto_import: true` dans `config.json` pour le faire a chaque rapport.

```bash
python -m agentwatch import-transcripts --latest
```

Pour reconstruire une sequence Claude Code meme sans hooks, `inspect` relit les transcripts
en lecture seule et produit un export local avec messages, arguments, resultats, erreurs,
requetes et references fichier/ligne. Les secrets sont masques et le raisonnement est omis
par defaut. Le detail n'entre pas dans le stockage de collecte :

```bash
python -m agentwatch inspect --client claude-code --session <id> --since <instant> --until <instant> --format jsonl --out sequence.jsonl
```

Le client par defaut reste Codex. Les tokens absents restent non releves, avec couverture
explicite ; les parts par appel sont calculees. Des transcripts de reprises peuvent contenir
le meme historique : ne pas additionner leurs totaux comme des travaux independants.

`claude-context --session <origine> --session <reprise> --out contexte.json`
reunit maintenant ces copies par identite de requete et reconstruit les fenetres
observees autour des compactions. `inspect` peut filtrer les messages Claude par
type, role, texte ou ligne source avant troncature. Mesures, provenances et limites :
[`docs/claude-context.md`](docs/claude-context.md).

Codex sans hooks, sans effet sur Codex : ses rollouts (`~/.codex/sessions`) contiennent chaque action
(code de sortie, duree), l'usage en tokens de chaque reponse et les sous-agents. AgentWatch les lit en
lecture seule, de facon incrementale et en priorite d'arriere-plan, automatiquement avant `sessions`,
`report` et `trends` (`rollouts.auto_import`), ou en continu :

```bash
python -m agentwatch import-rollouts --follow
```

Panne silencieuse : `doctor`, `sessions`, `report` et `trends` avertissent quand la collecte
est probablement cassee (Python des hooks desinstalle, depot deplace, entrees retirees,
client actif sans aucun evenement recu), a partir des seules dates de modification des
journaux du client.

## Verification

```bash
python -m unittest discover -s tests -t .
```

```bash
python -m agentwatch self-test
```

Etat des preuves (documente / fixture / verifie reellement / indisponible) :
`docs/compatibility.md`. Exemple de rapport sur une session synthetique :
`examples/reports/session-synthetique.md`.

## Documentation

| Fichier | Sujet |
|---|---|
| `docs/installation.md` | commandes, portees, reglages, desinstallation |
| `docs/architecture.md` | sous-systemes, persistance, correlation |
| `docs/events.md` | schema d'evenement 1.0, parametres conserves, import d'usage |
| `docs/detectors.md` | regles, seuils, niveaux de confiance, exemples et contre-exemples |
| `docs/compatibility.md` | versions verifiees, matrice de preuve, surcharge mesuree, limites |
| `docs/privacy.md` | masquage, empreintes HMAC, liste de champs autorises |

## Perimetre V1

Collecte automatique par hooks et, pour Codex, par lecture passive des rollouts (appels, statuts,
durees, tokens, sous-agents, messages en empreintes), stockage local borne, sept detecteurs, rapports par session
et multi-sessions, cout en tokens depuis les transcripts Claude Code (sur demande), sante de
la collecte, installation reversible, tests et smoke tests. Pas de second LLM, pas
d'embeddings, pas d'interface web, pas de profilage Unreal.
