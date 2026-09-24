# Notifications et complements par run

Cette vue de `inspect` travaille sur les transcripts Claude Code deja disponibles,
en lecture seule. Elle compose les sorties consignees de Monitor, `watch_run.py`,
`t5_view.py` et `run_game_scenario.py` : elle ne lance ni n'importe ces scripts.
Les options Codex existantes restent inchangees. Aucun LLM, hook ou nouveau
stockage de collecte n'est requis.

Configurer `transcripts.claude_projects_dir` dans un home d'analyse pour viser
les copies figees, puis decouvrir des references sans exposer tous les messages :

```powershell
python -m agentwatch --home <analyse> inspect --client claude-code --session <session> --thread <fil> --list-runs --limit 20 --through-line <borne> --format jsonl --out runs.jsonl
python -m agentwatch --home <analyse> inspect --client claude-code --session <session> --thread <fil> --run <task-id-ou-label> --through-line <borne> --format jsonl --out run.jsonl
python -m agentwatch --home <analyse> inspect --client claude-code --session <session> --thread <fil> --run <task-id-ou-label> --through-line <borne> --field stream.MB --field stream.lv --field tests_failed --fields-only --format jsonl --out complements.jsonl
```

`--list-runs` retourne des pages de references et des apercus de 240 caracteres
maximum (reductibles avec `--max-chars`). `--offset` poursuit la decouverte.
Un label ambigu est refuse : choisir l'identifiant complet de l'ancrage ou le
task-id. Les identifiants d'appel, de tache et les cibles explicites dans leur
workspace etablissent les liens. La proximite temporelle n'en etablit aucun.
Une relance avec le meme label et un nouvel appel reste distincte; une simple
lecture par label devient alors ambigue. Une copie du meme appel conserve son
identite. Les cibles de scripts seulement mentionnees par `git add`/`echo` ne
sont pas des lancements.

Les filtres existants `--kind`, `--role`, `--contains`, `--source-line` s'appliquent
aux observations apres resolution des liens; ils recherchent le texte masque
avant bornage. Les ancrages anterieurs restent disponibles pour identifier le
run, sans etre presentes comme des faits appartenant a la selection.

## Frontieres et preuves

`--through-line` est inclusif et exige un `--thread` unique. Il convient aux
transcripts dont les horodatages reculent ou aux notifications sans date. Les
dates absentes restent inconnues. `--until` est exclusif; une observation sans
date est exclue de cette selection temporelle. Combiner les deux bornes exige
les deux conditions. Aucune version d'artefact presente aujourd'hui n'est ouverte
pour combler une absence historique.

Chaque valeur a une source physique, une date observee, un role et si disponible
un temps et une phase de scenario. La version HMAC identifie la **version masquee
de la sortie enregistree**, pas le
fichier original sur disque. `--fields-only` partage ces references dans une
table pour restituer les complements sans repeter la chronologie complete.
Les series numeriques donnent leur derniere valeur enregistree et leurs extrema
visibles. Ce ne sont pas les extrema garantis du run complet. Une contradiction
sur un meme role/temps/phase/champ reste comptee; une version plus recente ne l'efface
pas. Un role absent d'un `tail` reste indetermine.

Champs courants : `hitch_ms`, `arrival`, `stream.MB`, `stream.lv`, `stream.prof`,
`stream.gLive`, `stream.gRec`, `stream.maxFrame_ms`, `tests_failed`, `run_end`,
`watcher_end`, `process_status`, `process_exit`, `failure_event`, `pending.unplaced`,
`event_text`. Un `FAIL` visible est conserve meme si un `END` annonce zero echec;
ces deux faits ne sont pas arbitres ni fusionnes. Demander un champ
non observe produit `missing` et `null`, jamais zero. Les champs inconnus du
format restent dans le resultat complet et le compteur de lignes non interpretees.
`END 2 echec(s)` porte un compteur different d'une fin de processus code zero.
`run finished` concerne le watcher. Aucune acceptation metier n'est deduite.

La chronologie classe les faits comme nouveaux, deja fournis, plus recents,
modifies/contradictoires, ou relus apres compaction/contexte indetermine. Ces
classes sont calculees; elles ne jugent pas seules de la necessite d'une lecture.
L'analyste relie les champs demandes a une decision observee. Une verification
independante exige une preuve de source/methode independante; relire le meme
fichier de sortie ne la fournit pas.

`notification_recorded` atteste seulement une ligne du journal. Les champs
`reception_observed` et `api_consumption_proven` restent `null` : le transcript
ne fournit pas le payload API qui justifierait la seconde assertion. Une reponse
de l'assistant utilisant un fait peut etre citee separement comme indice de
reception. Le graphe et les frontieres de `claude-context` sont reutilises;
l'absence de compaction ne constitue pas davantage une preuve de retention.

Les references `full_result` permettent de reprendre l'inspection habituelle
avec `--kind resultat` ou `notification`, `--source-line` et une borne de texte
suffisante. L'export refuse d'ecraser les transcripts sources, y compris via
un lien physique. Le masquage recursif est celui de l'inspection existante.

## Rejeu reproductible

`examples/claude_run_replay.py` accepte un manifeste de cas JSON. Chaque cas
declare `id`, `question`, `run`, `start_line`, `end_line`, `fields`, des
`expected` (`field`, `value`, eventuellement `role`/`at`), des `expected_missing`
et des `decision_lines`. `common_lines` preserve les actions liees manuellement
par l'analyste (par exemple un arret de processus sans run-id dans la commande).
`retain_result_lines` conserve une lecture dont les champs extraits ne suffisent
pas a expliquer le resultat; les lignes de decision sans message public sont
signalees, sans inclure le raisonnement brut. `discovery: true` ajoute le cout
de la decouverte.

```powershell
python examples/claude_run_replay.py --home <analyse> --session <session> --thread <fil> --cases cas.json --out rejeu.json
```

Le parcours conserve les acquisitions, leurs arguments, les notifications,
erreurs et decisions selectionnees. Il compare leur restitution originale a
une projection deterministe des champs **apres** disponibilite des resultats.
Les commandes historiques ne sont jamais executees. Les sources sont hachees
avant et apres. L'absence de version historique des fichiers est explicite.

Les volumes sont des octets UTF-8 de JSON compact, arguments et sorties compris.
Un resultat negatif est conserve. Les tokens sont les compteurs des requetes
observees chevauchant la sequence complete, y compris leurs autres actions :
ils ne sont attribues ni a la lecture ni a sa projection. Des cas qui se
chevauchent ne s'additionnent pas. Aucun gain reel de session n'est deduit.
