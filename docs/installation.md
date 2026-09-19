# Installation

AgentWatch est un programme Python (3.11 ou plus, bibliotheque standard uniquement) qui
enregistre passivement les appels d'outils de Claude Code et de Codex via leurs hooks
officiels, puis les analyse hors ligne.

## Prerequis

- Python 3.11+ (suite de tests verte sous 3.14.4 et 3.12.10, Windows 11 ; WSL Ubuntu non teste).
- Claude Code et/ou Codex installes. Versions verifiees : voir `docs/compatibility.md`.
- Aucune dependance a installer. Aucune connexion reseau n'est faite par AgentWatch.

## Deux facons de lancer la commande

Sans installation (depuis le depot) :

```bash
python -m agentwatch --help
```

Avec installation dans l'interpreteur courant (facultatif) :

```bash
pip install -e .
agentwatch --help
```

Les hooks n'ont pas besoin de l'installation pip : ils appellent directement
`agentwatch/hook_entry.py` avec le chemin absolu de l'interpreteur choisi au moment de
`configure`. Deplacer le depot ou changer d'interpreteur impose de relancer `configure`.

Sous Windows, l'interpreteur retenu par defaut est `pythonw.exe` (a cote du `python.exe`
courant) : il lit stdin redirige comme `python.exe`, coute le meme temps (~39 ms mesures)
et n'ouvre aucune fenetre console, alors que les applications de bureau (Codex notamment)
font apparaitre une console a chaque hook lance avec `python.exe`. Forcez un autre
interpreteur avec `--python <chemin>`.

## 1. Diagnostic

```bash
python -m agentwatch doctor
```

Affiche : version de Python, dossier de donnees, cle locale, point d'entree, clients
detectes et leur version, etat des hooks dans chaque fichier de configuration, incidents
de collecte (compteur de pertes).

## 2. Voir le diff, puis appliquer

Par defaut `configure` ne modifie rien : il affiche un diff unifie du fichier vise.

```bash
python -m agentwatch configure --client claude-code --dry-run
```

```bash
python -m agentwatch configure --client codex --dry-run
```

Application (une sauvegarde `<fichier>.bak-agentwatch-<horodatage>` est creee) :

```bash
python -m agentwatch configure --client claude-code --apply
```

```bash
python -m agentwatch configure --client codex --apply
```

Portees disponibles :

| Client | `--scope user` (defaut) | `--scope project` | `--scope local` |
|---|---|---|---|
| claude-code | `~/.claude/settings.json` | `<projet>/.claude/settings.json` | `<projet>/.claude/settings.local.json` |
| codex | `~/.codex/hooks.json` | `<projet>/.codex/hooks.json` | non disponible |

Ajoutez `--project-dir <chemin>` pour les portees de projet. Les chemins avec espaces et
accents sont supportes (forme "exec" cote Claude Code, guillemets simples PowerShell cote
Codex).

L'application est idempotente : relancer `--apply` remplace uniquement les entrees
AgentWatch (reconnues par le chemin `agentwatch/hook_entry.py`), sans toucher aux autres
hooks ni aux autres reglages.

### Codex : etape supplementaire obligatoire

Codex n'execute un hook non gere qu'apres que vous l'avez approuve : ouvrez Codex et
utilisez la commande `/hooks` pour examiner et approuver l'entree AgentWatch. Tant que
ce n'est pas fait, le hook est ignore silencieusement (comportement documente de Codex,
confiance enregistree par empreinte de la definition). Un changement de chemin ou de
version d'AgentWatch modifie l'empreinte et redemande l'approbation.

## 3. Desinstallation

```bash
python -m agentwatch uninstall --client claude-code --apply
```

```bash
python -m agentwatch uninstall --client codex --apply
```

Seules les entrees AgentWatch sont retirees. Aucun ancien fichier n'est restaure
aveuglement : les modifications faites entre-temps par vous ou d'autres outils sont
conservees. Les sauvegardes `.bak-agentwatch-*` restent a votre disposition.

## Dossier de donnees

Par defaut `~/.agentwatch` (`C:\Users\<vous>\.agentwatch` sous Windows), modifiable par
la variable `AGENTWATCH_HOME` ou l'option `--home`. Le chemin est fige dans la commande
du hook au moment de `configure`, donc les hooks ne dependent pas de l'environnement.

```
~/.agentwatch/
  config.json          reglages (voir examples/config/agentwatch.config.example.json)
  keys/hmac.key        cle locale des empreintes (ne pas partager)
  spool/<client>/<session>/*.json   un fichier par evenement
  segments/<client>/<session>/segment-*.jsonl   apres `compact`
  diagnostics/*.json   incidents de collecte (compteur de pertes)
  install/<client>.json  version du client relevee lors de `configure`
  import/codex-rollouts.json  etat de lecture des rollouts Codex (offsets, numeros de ligne, identifiants, compteurs
                       des lignes non interpretees et des doublons reconnus, empreintes courtes des derniers messages)
  feedback.json        vos marques pertinent / faux positif
```

## Commandes

| Commande | Role |
|---|---|
| `doctor` | diagnostic complet, dont la sante de la collecte (panne silencieuse : interpreteur des hooks disparu, depot deplace, entrees retirees, client actif sans evenement recu) et, pour Codex, l'approbation de chaque hook lue aupres de Codex (`codex app-server`, methode `hooks/list`, sans appel au modele), seulement avec `--codex-trust` : la sonde lance un processus Codex, jamais par defaut, et jamais si aucun hook AgentWatch n'est installe ainsi que l'etat de la lecture des rollouts : derniere lecture du collecteur (passage qui a trouve des lignes nouvelles ; sans ecriture de Codex, un passage ne laisse pas de trace) et ses erreurs, donnees importees (fichiers suivis, sessions), en attente (rollouts et octets pas encore lus, retard entre la derniere ligne ecrite par Codex et la derniere lue, elements de message a rapprocher) et lues mais non interpretees (par type), plus les doublons reconnus |
| `configure --client X [--apply]` | installer les hooks (dry-run par defaut) |
| `uninstall --client X [--apply]` | retirer les hooks AgentWatch |
| `sessions [--client X] [--json]` | lister les sessions enregistrees |
| `report --session ID` ou `report --latest` `[--format auto|markdown|json|rich|html|svg] [--out F]` | analyser une session (ou la derniere) ; `auto` = Rich dans un terminal si installe, sinon Markdown |
| `report ... --day AAAA-MM-JJ` ou `--since T --until T` | la meme analyse sur une tranche de temps (heure locale sans fuseau ; `Z` ou `+02:00` acceptes) : un fil vit plusieurs jours ; tokens recalcules a partir des releves par reponse de la tranche |
| `inspect --session ID [--thread ID] [--day D ou --since T --until T] [--max-chars N] [--reasoning] [--format markdown|jsonl] [--out F]` | export detaille et lisible d'une session Codex, relu dans ses rollouts a la demande (lecture seule) : arbre des agents et liens parent-enfant, consignes et messages, chaque appel avec arguments, script ou commande, resultat, erreur et duree (mesuree ou reconstruite, fiabilite indiquee), tokens mesures par reponse et parts calculees par appel ; source de chaque evenement (fichier, ligne) ; secrets masques ; signalements [masque], [tronque], [absent], [non compris], [omis], [chiffre], [horodatage non fiable] ; conclusions des detecteurs en annexe separee. Fichier local par defaut dans `<donnees>/exports/` : contenu en clair, a ne pas partager sans relecture |
| `compare --save-reference F.json --since T --until T [--client X] [--project TXT]` | enregistrer une reference sur une periode explicite : mesures (reprises sans apport, tokens en cache et hors cache, taches terminees ou interrompues et leur duree, erreurs), economies estimees (hypotheses), gains constates (aucun avant comparaison) |
| `compare --reference F.json --since T [--until T] [--client X] [--project TXT]` | comparer une periode a la reference : taux rapportes a l'activite, rapport apres/avant ou ecart avec IC 95 % ; un ecart n'est dit demontre que si l'intervalle exclut l'absence d'effet |
| `compare --at T` ou `--at agents-md:<empreinte>` ; `--list-agents-md` | meme comparaison autour d'un instant, ou de la premiere apparition d'une version d'`AGENTS.md` (empreinte du texte injecte par Codex) |
| `trends [--days N] [--client X] [--project TXT] [--min-sessions N] [--import-transcripts] [--format markdown|json] [--out F]` | motifs recurrents sur plusieurs sessions : top global, puis par projet, par client et par session (`--days 0` = toutes les sessions) |
| `import-rollouts [--days N] [--all] [--thread ID] [--follow] [--interval S]` | lire les rollouts Codex (lecture seule, incrementale, priorite d'arriere-plan) : appels, statuts, durees, tokens, sous-agents, messages en empreintes ; `--follow` resume en direct ce que fait Codex ; automatique avant `sessions`, `report`, `trends` (`rollouts.auto_import`) |
| `import-transcripts --session ID` ou `--latest` ou `--all` | lire l'usage en tokens des transcripts Claude Code (nombres et identifiants seulement) et l'attacher aux appels ; idempotent |
| `self-test [--runs N]` | scenarios synthetiques + hook reel en sous-processus |
| `bench [--runs N]` | mesurer la surcharge du hook |
| `replay --client X <fichiers>` | reanalyser des payloads enregistres (rien n'est execute) |
| `compact [--session ID]` | fusionner le spool en segments JSONL |
| `prune [--days N] [--dry-run]` | appliquer la retention |
| `feedback --finding ID --mark relevant|false-positive|clear` | marquer un signalement |
| `import-usage <fichier.jsonl>` | importer des observations d'usage (tokens) |
| `ingest --client X` | commande interne appelee par les hooks |

## Affichage enrichi (optionnel)

```bash
python -m pip install rich
```

Rich doit etre installe pour l'interpreteur qui lance AgentWatch. Une machine Windows a
souvent plusieurs Python (python.org, Microsoft Store, environnement virtuel) et un
`pip install rich` nu peut en viser un autre : utilisez toujours `python -m pip`, avec le
meme `python` que celui de `python -m agentwatch`. En cas d'absence, `--format rich`
affiche le chemin exact de l'interpreteur et la commande d'installation correspondante ;
`doctor` indique aussi si Rich est disponible.

Lancez cette installation depuis votre propre terminal, pas depuis un agent : une application
de bureau empaquetee (Claude sous Windows) redirige ses ecritures AppData vers un dossier
prive, et le paquet resterait invisible pour vous (voir `docs/compatibility.md`).

Avec Rich installe, `report --latest` dans un terminal interactif affiche des panneaux
colores par confiance, des tableaux et des barres proportionnelles (appels, volumes,
durees). `--format html` ou `--format svg` avec `--out` exportent ce rendu ; `--format
markdown` et `--format json` restent les sorties canoniques et ne dependent de rien.
Le hook n'importe jamais Rich (verifie par un test).

Pour ecrire un rapport dans un fichier, preferez `--out rapport.json` a la redirection `>` :
`--out` ecrit toujours de l'UTF-8, alors que Windows PowerShell 5.1 reencode tout ce qui
passe par `>` en UTF-16. Les fichiers `rapport*` a la racine du depot sont ignores par git,
car ils contiennent des donnees de session.

## Reglages utiles (config.json)

| Cle | Defaut | Effet |
|---|---|---|
| `max_stdin_bytes` | 8 Mio | lecture bornee du payload de hook |
| `max_event_bytes` | 256 Kio | taille maximale d'un evenement stocke |
| `max_command_chars` / `max_error_chars` | 2000 / 400 | bornes des textes conserves (masques) |
| `max_session_events` | 20000 | quota par session (verifie par echantillonnage) |
| `retention_days` | 30 | applique par `prune` |
| `auto_compact_threshold` | 200 | a la lecture (`report`, `sessions`), fusion du spool en segment au-dela de N fichiers (0 = jamais) ; la premiere ouverture d'un petit fichier coute ~13 ms sous Windows a cause de l'antivirus |
| `report.max_listed_per_rule` | 15 | nombre de signalements secondaires listes par regle dans le Markdown (tous sont dans le JSON) |
| `trends.days` / `trends.min_sessions` | 7 / 2 | `trends` : fenetre en jours (0 = toutes les sessions) et nombre de sessions distinctes a partir duquel un motif est recurrent ; `trends.max_top` (5) motifs detailles, `trends.max_examples` (8) signalements cites par motif |
| `health.silence_minutes` | 30 | silence tolere entre une modification des journaux du client et le dernier evenement recu avant d'avertir ; `health.enabled` (true), `health.codex_sessions_dir` (defaut `~/.codex/sessions`) |
| `transcripts.auto_import` | false | lire les transcripts Claude Code a chaque `report` / `trends` ; `transcripts.claude_projects_dir` (defaut `~/.claude/projects`), `transcripts.max_bytes` (64 Mio) |
| `detectors.tool_gap.min_calls` / `strong_calls` | 3 / 6 | detecteur E : commandes de service vers une meme cible a partir desquelles on signale / a partir desquelles la confiance monte sans serveur MCP observe |
| `mask_home_dir` | true | remplace le dossier utilisateur par `~` |
| `detailed_excerpts` | false | extraits bornes de sortie (a activer explicitement) |
| `store_tool_descriptions` | false | descriptions libres des appels |
| `detectors.*` | voir `docs/detectors.md` | seuils des quatre regles |

## Plateformes

- Windows 11 / PowerShell : verifie (voir `docs/compatibility.md`).
- WSL / Linux / macOS : le code n'utilise que des API portables (os.path, os.replace,
  chemins POSIX pour le champ `command` de Codex) mais **n'a pas ete execute** sur ces
  systemes dans cette version. Le champ Codex `command` (POSIX) utilise `python3` par
  defaut, modifiable par `--posix-python`.
