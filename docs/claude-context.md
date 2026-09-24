# Reconstruire les reprises Claude Code

`claude-context` analyse un ensemble explicite de transcripts en lecture seule,
sans import dans le stockage de collecte et sans action sur le client observe.
La configuration `transcripts.claude_projects_dir` peut viser un dossier de copies
figees. Les sous-agents presents dans le dossier de chaque session sont inclus.

```powershell
python -m agentwatch --home <home-analyse> claude-context --session <origine> --session <reprise> --out contexte.json
```

Le JSON conserve les requetes, leurs occurrences fichier/ligne, les compactions,
les fenetres et segments de modele/configuration, les contenus volumineux,
notifications, lectures et changements de contexte. Les extraits sont masques et
bornes. Les prompts systeme, instructions et blocs de raisonnement ne sont pas
reproduits : tailles, empreintes et references suffisent a retrouver les preuves.
Une sortie qui viserait un transcript selectionne, y compris par lien physique,
est refusee avant ecriture.

## Mesures et limites

- Une identite `requestId` (ou le repli `message.id` du parseur existant) compte une
  fois dans tout l'ensemble selectionne. Deux requetes distinctes avec le meme
  contenu comptent deux fois. La selection des sessions reste explicite.
- Le parseur privilegie les observations terminales (`stop_reason`) aux blocs de
  streaming provisoires. Sans bloc terminal, il cherche leur accord. Les valeurs
  contradictoires, y compris entre le compteur principal et une unique iteration
  `message`, restent inconnues avec les valeurs et lignes en preuve. Aucun maximum
  arbitraire, aucune somme des iterations non documentee. Les messages explicites
  `model="<synthetic>"` restent inspectables mais ne comptent pas comme appels API.
  Un conflit dans une copie reste un conflit global du champ, meme si une autre
  copie fournit une valeur coherente. Une absence simple peut etre completee par
  une observation connue ; une contradiction ne le peut pas.
- Les compteurs `input_tokens`, `cache_creation_tokens`, `cache_read_tokens` et
  `output_tokens` restent separes. L'entree totale d'une requete est la somme des
  trois premiers. Une lecture du cache reste une entree observee ; ce n'est ni une
  economie mesuree ni un prix. `observed_totals` est un sous-total quand la
  couverture est partielle ; le total strict vaut alors `null`.
- Les parents sont resolus a l'occurrence et a la ligne considerees. Une copie
  reapparentee apres compaction ne modifie pas l'ascendance historique. Les
  evenements de compaction precedents dans le fichier ET dans le temps bornent
  les fenetres observees, avec la chaine des parents conservee separement. Cette
  segmentation n'affirme pas connaitre le contenu exact envoye a chaque requete.
- Les UUID de `preservedMessages` attestent une conservation a la compaction,
  sans preuve de maintien indefini. `preTokens` et `postTokens` sont des releves
  de l'evenement ; `postTokens` n'est pas assimile a l'entree de la requete suivante.
- `input_delta` compare deux requetes datees successives de la meme frontiere,
  du meme modele et de la meme configuration observee. Le residu retranche la
  sortie precedente. Ces calculs ne mesurent pas le poids exact d'un resultat.
  Une date absente ou identique ne permet pas ce delta.
- Les contenus sont rattaches a leurs occurrences de frontiere et a un intervalle
  temporel. Le premier segment inclut le prompt initial ; le dernier inclut les
  blocs terminaux. Un changement de configuration coupe cet intervalle a la
  premiere reponse portant la nouvelle valeur. Le contenu precedent n'est pas
  automatiquement attribue a cette nouvelle configuration. Les contenus sans
  date ou frontiere exploitable sont comptes separement.
- `candidate_results` suit l'ordre du transcript ; `useful_result_candidates`
  conserve les derniers messages visibles de l'assistant. Ces candidats ne
  prouvent ni la retention exacte ni la reussite effective du projet observe.

Les types et lignes non interpretes restent visibles dans la couverture. Un
debut de graphe observe n'est pas une preuve de demarrage a contexte vide.
Les champs de configuration absents ne sont pas reconstitues.

## Recuperer une consigne historique

L'inspection existante accepte maintenant des filtres Claude conjonctifs, appliques
avant troncature sur le texte masque, sans recherche dans les metadonnees :

```powershell
python -m agentwatch --home <home-analyse> inspect --client claude-code --session <id> --thread <id> --kind message --role user --contains "terme distinctif" --max-chars 30000 --format jsonl --out consigne.jsonl
```

`--kind` est repetable ; `--source-line` selectionne une ligne physique lorsqu'une
reference est deja connue. Le role `user` exclut les resultats d'outils. Il evite
ainsi que la recherche retrouve sa propre commande plutot que la consigne.
`summary.selection` (API Python) et la sortie CLI indiquent les nombres examines
et retenus. Le record `fil` conserve la provenance. Les releves de tokens de toute
la periode sont omis du fichier filtre pour ne pas les attribuer a la selection ;
ils restent disponibles dans le resume API et dans l'export sans filtre.

Les options historiques et l'inspection Codex par defaut restent disponibles.
Les nouveaux filtres sont explicitement refuses pour Codex dans cette version.
Le rejeu d'une extraction mesure des volumes et une preservation du contenu ;
il ne demontre pas une economie de tokens en session.
