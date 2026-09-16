# Les quatre detecteurs

Tous sont deterministes, independants, executes sur la vue de session correlee. Les
seuils sont dans `config.json` sous `detectors`. Chaque signalement porte : identifiant de
regle et version, confiance (`high`/`medium`/`low`) et sa justification, appels concernes,
preuve locale, explication, contre-indications, donnees manquantes, cout observe,
proposition et protocole de validation.

Le niveau de confiance est une heuristique documentee, pas une probabilite calibree.
Classement des rapports : confiance, puis nombre d'appels, puis octets de sortie observes.
Aucun score global.

## A. `A.redundant_reads` — lectures ou recherches probablement redondantes

Regle : meme outil, meme cible, memes parametres de comparaison (plage, filtre, motif),
meme agent, meme epoque de contexte, dans une fenetre (`window_calls` = 60 appels,
`window_seconds` = 900), sans modification observee de la cible entre les deux.

| Confiance | Condition |
|---|---|
| high | empreintes de resultat identiques, aucun appel a effet inconnu ni ecriture intercale |
| medium | empreintes identiques mais un appel a effet inconnu (shell `run`/inconnu, MCP, agent) s'est intercale ; ou aucun changement observe mais empreinte manquante |
| low | empreinte manquante et appel a effet inconnu |

Exclusions : ecriture observee sur la cible (pour une commande shell : toute ecriture),
premier appel en echec (domaine de B), resultats differents (relecture justifiee),
parametres differents (pagination, autre plage), autre agent, autre epoque (compaction,
reprise). Une recherche a zero resultat n'est pas jugee inutile pour autant.

Limite toujours rappelee : la presence du resultat precedent dans le contexte du modele
n'est pas observable ; une modification externe non plus.

Exemple positif : `Read a.py`, `git status`, `Read a.py` (contenu identique) -> high.
Contre-exemples : `Read b.py`, `Edit b.py`, `Read b.py` ; lecture par un sous-agent ;
lecture apres `PostCompact` ; `Read big.log offset=1` puis `offset=101`.

## B. `B.error_loops` — boucles d'erreurs

Regle : au moins `min_failures` (3) echecs de meme signature sur la meme operation (outil,
cible, parametres) dans la fenetre (40 appels / 1800 s), sans correction observable entre
deux echecs (ecriture touchant la cible ou un chemin de la commande).

| Type | Confiance | Sens |
|---|---|---|
| `persistent` | high | echecs identiques, aucune correction ni appel a effet inconnu |
| `persistent` | medium | un appel a effet inconnu s'est intercale (correction possible non observable) |
| `persistent_probable` | medium max | Codex : statut non expose, echec infere du texte de sortie |
| `transient_recovered` | low | la meme operation a fini par reussir sans correction (retry, backoff detecte si les intervalles croissent) |
| `repeated_denial` | medium | refus repetes ; un refus n'est pas une erreur d'execution |

Les interruptions (`interrupted`) ne sont jamais comptees. Les causes proposees sont des
hypotheses tirees de la signature (`No such file`, `permission`, `timeout`, ...).

Exemple positif : trois `python run.py` avec `ModuleNotFoundError`. Contre-exemple :
echec, `Write cfg.json`, echec, succes -> la correction observee casse la chaine.

## C. `C.batchable` — operations regroupables

Regle : au moins `min_group` (3) appels consecutifs du meme outil de lecture (`read`,
`list`, `search`, MCP cible par chemin) sur des cibles differentes, chacun commence apres
la fin du precedent, sans erreur, avec au plus `max_gap_calls` (0) appels intercales.

Independance (elle fixe la confiance) :

| Preuve | Confiance |
|---|---|
| toutes les cibles figurent dans `result_paths` d'un listage/recherche juste avant | high |
| meme dossier ou meme extension | medium |
| sinon | low (« independance non etablie ») |

Outil groupe : annonce `verified_in_session` seulement si des appels chevauchants du meme
outil ont ete observes dans la session ; `candidate_observed` si un outil MCP du meme
serveur au nom evocateur (batch, many, multi, all, bulk, list) a ete vu ; sinon
`documented_not_verified` (Claude Code) ou `proposal`. Des appels deja chevauchants ne
sont pas signales.

## D. `D.automation_candidates` — sequences candidates a une automatisation

Regle : motifs de 2 a 6 appels (`min_pattern_len`, `max_pattern_len`) repetes au moins
`min_occurrences` (3) fois, par agent, sur les `max_calls` (2000) derniers appels. La
signature d'un appel = outil, categorie, forme de la cible (extension, tete de commande,
serveur/outil MCP) et noms des parametres. Seuls les motifs fermes (non inclus dans un
motif plus long de meme frequence) et comportant au moins deux signatures distinctes sont
retenus. Complexite : O(appels x longueur max).

Etapes classees `mechanical` (structure stable, cible substituee) ou `judgment` (contenu
d'edition variable, commande de structure variable, statut variable selon l'occurrence).

| Confiance | Condition |
|---|---|
| high | au moins `min_occurrences` + 1 occurrences, toutes reussies, aucune etape de jugement |
| medium | au plus une etape de jugement ou occurrences au seuil |
| low | plusieurs etapes de jugement |

Le signalement contient une recette (entrees, preconditions, etapes, sortie, tests,
risques) a valider par un humain. AgentWatch ne genere ni n'execute ce script, et ne
conclut jamais qu'une tache « n'a pas besoin d'intelligence ».

## Retours locaux

`agentwatch feedback --finding <id> --mark relevant|false-positive|clear` enregistre la
marque dans `<home>/feedback.json`. Un faux positif est exclu des opportunites
prioritaires mais reste liste. Aucun apprentissage.
