# AgentWatch

Observateur local et passif pour **Claude Code** et **Codex**. Il enregistre les appels
d'outils via les hooks officiels des deux clients, les normalise dans un schema commun,
puis produit des rapports Markdown/JSON avec cinq detecteurs deterministes :

- **A** travail de lecture refait (meme fichier lu via `Read`, `cat` ou `sed -n`, meme
  recherche via `Grep` ou `rg`) et executions relancees sans changement ;
- **B** boucles d'erreurs ;
- **C** operations regroupables ;
- **D** sequences candidates a une automatisation deterministe ;
- **E** service externe manipule a la main (ssh, SLURM, curl, gh, cloud, docker) alors
  qu'un outil MCP lie existe ou manque.

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

Cout reel en tokens (Claude Code) : lu dans les transcripts du client, nombres et
identifiants seulement, jamais le texte ; les rapports classent alors par tokens mesures et
non par octets. `transcripts.auto_import: true` dans `config.json` pour le faire a chaque rapport.

```bash
python -m agentwatch import-transcripts --latest
```

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
durees, tokens, sous-agents), stockage local borne, cinq detecteurs, rapports par session
et multi-sessions, cout en tokens depuis les transcripts Claude Code (sur demande), sante de
la collecte, installation reversible, tests et smoke tests. Pas de second LLM, pas
d'embeddings, pas d'interface web, pas de profilage Unreal.
