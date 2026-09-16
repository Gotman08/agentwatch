# AgentWatch

Observateur local et passif pour **Claude Code** et **Codex**. Il enregistre les appels
d'outils via les hooks officiels des deux clients, les normalise dans un schema commun,
puis produit des rapports Markdown/JSON avec quatre detecteurs deterministes :

- **A** travail de lecture refait (meme fichier lu via `Read`, `cat` ou `sed -n`, meme
  recherche via `Grep` ou `rg`) et executions relancees sans changement ;
- **B** boucles d'erreurs ;
- **C** operations regroupables ;
- **D** sequences candidates a une automatisation deterministe.

L'analyse porte sur des **unites de travail** (operation normalisee sur une cible,
quel que soit l'outil ou la forme de la commande) avec une empreinte du contenu obtenu,
pas seulement sur des appels identiques. Voir `docs/detectors.md`.

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

Collecte automatique par hooks, stockage local borne, quatre detecteurs, rapports,
installation reversible, tests et smoke tests. Pas de second LLM, pas d'embeddings, pas
d'interface web, pas de lecture des journaux natifs, pas de profilage Unreal.
