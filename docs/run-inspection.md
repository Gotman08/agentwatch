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

`--run <call-id-complet>` permet aussi de commencer par un appel. Avec `--thread`,
sa portee est le fichier de ce fil. Sans lien unique, la reponse
`agentwatch.run-call.v1` montre ses recus, lectures, notifications, candidats et
contradictions; ses champs restent `not_attributed_to_a_run`. Un candidat n'est
jamais utilise comme une preuve. `--list-runs` expose aussi `launchers` et les
comptes `complete`, `partial`, `ambiguous`, `not_demonstrable`, avec pagination.
`complete` indique une chaine de references avec tache, notification et resultat;
ce statut ne signifie ni fin de scenario ni succes.

Le lanceur `sh`/`bash` est resolu dans un sous-ensemble volontairement borne :
version `Write` consignée et confirmee, chemin complet, `cd` literal, branche
`case "$N"` litterale et argument `--label`. Les editions suivantes invalident
la version; commandes conditionnelles, substitutions et cas dynamiques non pris
en charge restent non resolus. Les corps de heredoc et commandes citees ne sont
pas des invocations. Aucun script actuel n'est lu, importe ou execute.

Pour relier un lanceur a un watcher par son dossier, l'adaptateur recherche dans
les lectures historiques du **meme chemin Python** le calcul borne
`ROOT = pathlib.Path(__file__).resolve().parents[N]` puis
`out = ROOT / '...' / args.label`. Le dossier normalise doit correspondre
exactement a la cible du watcher; le basename ou le label seul ne suffit pas.
Les extraits portent leurs references et versions masquees. Ils ne prouvent
pas l'absence de modifications hors journal ni un hash d'execution du script.

La vue complete contient `links`, `launch_evidence`, `chain` et `readings`.
Chaque arete garde les deux identifiants complets, la portee, la regle, les
sources, les limites et `known_from`. Plusieurs taches peuvent appartenir au
meme appel et plusieurs appels au meme run. Deux proprietaires contradictoires
d'une tache restent ambigus, meme si leurs labels coincident. Une association
apprise tard peut enrichir une vue ulterieure, jamais le prefixe anterieur.
Les liens explicites de manifeste du rejeu restent distincts de ce graphe.

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
Cette reponse courte garde aussi la chaine et les dix derniers complements
bornes avec `full_result`, y compris une sortie sans fait interprete. Les
complements anterieurs sont comptes; la vue complete les conserve tous.
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
`readings` ajoute un classement par lecture : recu, erreur, nouveaux faits,
etat plus recent, aucun nouveau fait extrait, ou preuve non interpretee a
examiner. `usefulness_judgement` et `independent_verification` restent inconnus.
Un delta de faits vide ne signifie pas une lecture inutile : une structure
terminale non reconnue peut etre la preuve utile qui manquait.

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

## Choisir la restitution selon la question

Commencer par les options deja disponibles, sans ajouter une projection si le
message suffit. Pour retrouver une notification courte dont on connait la ligne :

```powershell
python -m agentwatch --home <analyse> inspect --client claude-code --session <session> --thread <fil> --kind notification --source-line <ligne> --max-chars <borne> --format jsonl --out notification.jsonl
```

Verifier que cette ligne est disponible a la frontiere et prendre `max_chars`
dans la reference `full_result`. Le filtre porte sur une ligne physique exacte;
`--contains` selectionne un evenement entier, pas un extrait autour du mot.
`--role` est le role du message (`assistant`, etc.), pas celui du processus
`host`/`client`. Pour un complement deja acquis, les options de champs peuvent
se combiner avec `--kind resultat --source-line <ligne>` et `--through-line`.
Un champ manquant dans cette selection n'est pas necessairement absent du prefixe.

Le meme rejeu produit maintenant `adaptive` : il conserve directement les
notifications et les petites sorties, et compare chaque complement a la vue
par champs existante. Les champs, roles, temps, phases, versions, limites et
preuves servent a la selection; les valeurs `expected` servent uniquement aux
verifications finales. Un fait terminal exactement deja notifie n'est pas
repete dans le complement. Les acquisitions et leurs arguments restent dans
les deux parcours : aucune commande historique n'est executee ou supprimee.

Une sortie peut contenir un traceback meme si `is_error` est faux. Les erreurs,
evenements d'echec, structures non interpretees et `retain_result_lines` restent
directement visibles. Un fragment JSON lifecycle coupe n'est pas reconstruit.
Les absences sont indiquees separement pour la sequence et pour le prefixe connu;
elles signifient « non extrait », pas « preuve d'absence ».

```powershell
python examples/claude_run_replay.py --home <analyse> --session <session> --thread <fil> --cases cas.json --out audit.json --answer <id-du-cas>
```

`--answer` rend uniquement le parcours adapte du cas demande; `audit.json`
conserve tous les textes, les projections candidates, leurs vues completes et
les commandes `detail_commands` sous forme de listes d'arguments executables
avec `subprocess.run`. Ces commandes inspectent les copies, jamais les outils
observes. Les preuves sont partagees dans la reponse pour eviter de repeter
le contexte a chaque complement. Le rendu adapte est donc une composition
locale de l'inspection existante, pas le texte brut inchange de `--fields-only`.

`rendered_utf8_bytes` distingue le parcours enregistre, la projection fixe,
le parcours adapte et le direct avec le **meme contexte de preuve**. Une reponse
directe peut rester plus longue que le texte historique a cause des versions,
absences et references ajoutees; cet ecart reste visible. La decouverte, les
arguments, les resultats, les complements, les erreurs et les decisions
selectionnees sont inclus. Les commandes de detail restees dans l'audit ne sont
pas comptees comme executees; toute lecture supplementaire doit etre mesuree.

Les temps locaux du rejeu complet et de sa selection sont mesures avec
`perf_counter`; ce ne sont ni la latence du modele ni celle des acquisitions.
Les extractions manuelles remplacees comptent les groupes champ/resultat rendus
par projection, une attribution calculee et non un temps humain observe. Le
nombre d'acquisitions historiques evitees reste zero. Les complements dits
necessaires sont ceux qui ajoutent des faits demandes absents des observations
precedentes de la sequence; cela ne juge pas seul de leur necessite metier.

Une ligne de `retain_result_lines` sans resultat associe est refusee. Si un
lanceur non reconnu empeche le lien automatique, verifier ses references puis
declarer explicitement les appels/resultats dans `common_lines`. Le lien garde
la qualification `manifest_common_line`; il n'enrichit pas automatiquement les
faits du run. Les differents blocs d'une meme ligne restent distincts. Toutes
ces references doivent appartenir aux bornes du cas.
