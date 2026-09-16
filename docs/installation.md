# Installation

AgentWatch est un programme Python (3.11 ou plus, bibliotheque standard uniquement) qui
enregistre passivement les appels d'outils de Claude Code et de Codex via leurs hooks
officiels, puis les analyse hors ligne.

## Prerequis

- Python 3.11+ (teste : 3.14.4 sous Windows 11 ; 3.12 sous WSL Ubuntu non teste).
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
  feedback.json        vos marques pertinent / faux positif
```

## Commandes

| Commande | Role |
|---|---|
| `doctor` | diagnostic complet |
| `configure --client X [--apply]` | installer les hooks (dry-run par defaut) |
| `uninstall --client X [--apply]` | retirer les hooks AgentWatch |
| `sessions [--client X] [--json]` | lister les sessions enregistrees |
| `report --session ID` ou `report --latest` `[--format auto|markdown|json|rich|html|svg] [--out F]` | analyser une session (ou la derniere) ; `auto` = Rich dans un terminal si installe, sinon Markdown |
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
pip install rich
```

Avec Rich installe, `report --latest` dans un terminal interactif affiche des panneaux
colores par confiance, des tableaux et des barres proportionnelles (appels, volumes,
durees). `--format html` ou `--format svg` avec `--out` exportent ce rendu ; `--format
markdown` et `--format json` restent les sorties canoniques et ne dependent de rien.
Le hook n'importe jamais Rich (verifie par un test).

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
