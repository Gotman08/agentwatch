# Architecture

```
hooks du client ──stdin JSON──▶ agentwatch/hook_entry.py (python -I, ~45-200 ms)
                                   │  collector/ingest.py : lecture bornee, adaptateur,
                                   │  confidentialite, ecriture atomique, exit 0
                                   ▼
                     <home>/spool/<client>/<session>/<ns>-<pid>-<rand>.json
                                   │
      agentwatch report ───────────┤  core/session.py + core/correlate.py
                                   │  (dedoublonnage, tri, appariement debut/fin)
                                   ▼
                          detectors/ (A, B, C, D) ──▶ reports/ (Markdown, JSON)
```

## Sous-systemes

| Dossier | Role | Chemin chaud ? |
|---|---|---|
| `agentwatch/hook_entry.py` | point d'entree autonome des hooks (ajoute la racine a `sys.path`) | oui |
| `collector/ingest.py` | lire stdin (borne), decoder, appeler l'adaptateur, masquer, ecrire | oui |
| `collector/privacy.py` | masquage en une passe, HMAC-SHA256 local (`_sha2`), bornage | oui |
| `collector/store.py` | spool atomique, segments, diagnostics, quotas, retention | ecriture : oui |
| `adapters/claude_code.py`, `adapters/codex.py` | payload de hook -> evenement du schema commun | oui |
| `adapters/base.py` | parametres autorises, cibles, deduction de statut | oui |
| `core/schema.py` | schema versionne (`SCHEMA_VERSION = "1.0"`), constantes | oui |
| `core/normalize.py` | categories d'outils, chemins, commandes shell, signatures d'erreur | oui |
| `core/correlate.py` | evenements -> appels (`Call`), marqueurs, agents (`SessionView`) | non |
| `core/intent.py` | appel -> unite de travail (operation normalisee, cible, parametres) independante de l'outil ; liste blanche de formes shell, `cd ... &&` gere, reste `unknown` | non |
| `detectors/*.py` | quatre regles independantes, seuils configurables | non |
| `reports/*.py` | statistiques, matrice de couverture, Markdown, JSON, retours locaux | non |
| `installer/*.py` | diff / apply / remove des hooks, sauvegardes | non |
| `selftest.py` | constructeur de sessions synthetiques + scenarios de reference | non |
| `cli.py` | commandes | non |

Le chemin chaud n'importe ni `pathlib`, ni `typing`, ni `hashlib` (mesure par
`python -X importtime` : ces trois modules coutaient ~60 ms sur ~120 ms). Le HMAC est
calcule avec le module natif `_sha2` et verifie identique a `hmac`/`hashlib` par les tests.

## Choix de persistance

Un fichier par evenement, ecrit dans un fichier temporaire puis renomme par `os.replace`
(atomique sur un meme volume, y compris NTFS). Motivations :

- plusieurs hooks peuvent s'executer en meme temps (appels paralleles, sous-agents) ;
  un append JSONL concurrent n'est pas atomique sur toutes les plateformes ;
- aucun verrou inter-processus, donc aucune attente ni interblocage dans le hook ;
- un fichier corrompu n'affecte que lui-meme (ignore avec avertissement a la lecture).

`agentwatch compact` fusionne ensuite les fichiers en segments JSONL (lecture plus
rapide), toujours par ecriture atomique, et ne supprime les originaux qu'apres. La
lecture (`report`, `sessions`) declenche cette compaction d'elle-meme au-dela de
`auto_compact_threshold` fichiers : mesure sous Windows, la premiere ouverture d'un petit
fichier coute ~13 ms (analyse antivirus), soit 28 s pour 2 148 evenements, contre 23 ms
pour 802 evenements relus depuis un segment.

Concurrence de la compaction : les segments portent un nom unique (horodatage + pid) ;
deux compactions simultanees produisent deux segments qui peuvent se chevaucher, et
l'analyse dedoublonne par `event_id`. Un hook qui ecrit pendant la compaction cree un
fichier absent du listage, lu la fois suivante. `os.replace` est retente quelques fois en
cas de `PermissionError` transitoire (antivirus tenant le fichier tout juste ecrit).

Sous Windows, tous les chemins d'ecriture recoivent le prefixe `\\?\` : un dossier de
donnees profond depassait 260 caracteres et faisait echouer les ecritures (constate en
direct avec Claude Code 2.1.270).

## Correlation

- Cle d'appel : `(agent_id ou "main", tool_use_id)`. Un debut, une fin et une
  observation avec la meme cle forment un seul appel ; deux appels identiques avec des
  identifiants differents restent deux appels.
- Sans identifiant : appariement heuristique (meme outil, meme empreinte d'entree, debut
  encore ouvert), appel marque `ambiguous`.
- Doublons d'evenement (`event_id` identique, par exemple rejeu d'un import) : elimines
  et comptes. Les identifiants des evenements rejoues sont derives du contenu.
- Evenements hors ordre : tri par horodatage de reception ; un evenement de fin avant
  son debut est tolere.
- Sans evenement de fin : statut `open`, jamais `error`.
- Duree : `client` si le client la fournit, sinon `reconstructed_between_hooks`
  (inclut la surcharge du hook de debut et l'ordonnancement).
- Epoques de contexte : incrementees a chaque `PostCompact` et a chaque `SessionStart`
  apres le premier (reprise, clear, compaction).
- Tours : `turn_id` (Codex, `prompt_id` Claude Code >= 2.1.196) ou `UserPromptSubmit`.

## Ce qui n'est volontairement pas fait

- pas de second LLM, pas d'embeddings, pas d'interface web, pas de reseau ;
- aucune modification des parametres d'appel, aucun blocage, aucun cache substitue ;
- aucune generation ni execution des scripts proposes par le detecteur D ;
- pas de lecture des journaux natifs des clients (transcripts, rollouts) : les formats
  internes ne sont pas stables ; seule l'interface de hooks documentee est utilisee.
