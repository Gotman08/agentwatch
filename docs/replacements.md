# Remplacements sous conditions

Les rapports A-G comparent des contrats de remplacement sans ajouter de detecteur,
de LLM ou de STGNN. Ils expliquent les conclusions d'AgentWatch et leurs preuves,
pas le raisonnement interne de Claude ou de Codex. L'inspiration methodologique
ne constitue pas une validation scientifique.

## Utilisation et perimetre

Les commandes `report` et `trends` habituelles suffisent. Le JSON de session
passe en version **1.1** :

- `observed_dependency_graph` : noeuds, aretes prouvees et references non resolues ;
- `findings[].replacement_analysis` : contrats, preconditions, transformations,
  couts et variations de sensibilite ;
- `finding_cost_union` : cout observe des appels distincts couverts par les
  signalements actifs ; `config_warnings` : reglages ignores ou invalides ;
- Markdown, Rich, HTML et SVG : contrats et conditions refutees ou inconnues ;
- `trends` : exemple lie a sa session d'origine, choisi dans le perimetre courant.
  Ses preconditions ne sont jamais transferees aux autres sessions.

Cette couche travaille sur les appels deja normalises en memoire, sans lecture
de journal supplementaire, execution de commande historique ou decouverte reseau.
Les imports et compactages habituels de la CLI gardent leurs propres reglages.

## Faits et graphe partiel

Un fait vaut `established`, `refuted` ou `unknown`. Les noeuds conservent acteur,
contexte, operation, ressource, perimetre, empreintes et revision observee.
La revision d'une lecture peut etre l'empreinte de son contenu ; ce n'est pas
necessairement une version globale ou immuable du fichier.

| Relation | Preuve | Depend du producteur ? |
|---|---|---|
| `explicit_call_dependency` | `source_call_id` ou `depends_on_call_ids`, source unique reussie et anterieure, meme session/acteur/contexte | Oui |
| `explicit_delegation` | lien de sous-agent exact deja etabli par la correlation | Oui |
| `observed_content_equality` | meme unite de travail et empreinte de contenu | Non |
| `shared_resource_reference` | meme identifiant de job/tache/cellule ou son empreinte, dans la meme portee | Non |

Les references explicites ne sont utilisables que si elles figurent deja dans
les parametres conserves. Cette extension n'elargit pas la collecte.
Proximite temporelle, nom de serveur ou identifiant de job partage ne suffisent
pas a inventer le lien soumission-consultation. Les references aux producteurs
futurs, echoues, ambigus ou hors contexte restent non resolues.

Retirer un producteur en conservant un consommateur prouve rejette la
transformation. Le graphe reste **partiel** : absence d'arete ne prouve pas
l'independance ; absence d'ecriture ne prouve pas la stabilite ; empreinte egale
ne prouve ni disponibilite du resultat ni inutilite d'une verification.

## Scenarios

| Scenario | Rapports | Conditions principales |
|---|---|---|
| Conserver | A-G | Reference observee intacte |
| Reutiliser | A, G | Meme perimetre, version valide, information disponible, fraicheur et verifications preservees |
| Regrouper | C | Parametres connus au depart, independance, resultats et erreurs identifiables, volume compatible |
| Cadence | G | Politique fixee avant le rejeu, delai de reaction, etats necessaires preserves |
| Attendre | G | Meme job, delai maximal, identifiant et etat terminal, erreurs et annulation |
| Capacite structuree | D, E | Memes operations, droits, resultats, erreurs et effets |
| Skill/regle | B, D, E, F | Procedure a valider ; aucune baisse automatique des appels |

Une precondition refutee donne `rejected` ; au moins une inconnue donne
`indeterminate` ; toutes etablies donnent `admissible`.
Tests, builds, scripts et ecritures ne sont pas supprimables comme des lectures.
Un outil structure peut les conserver, sous condition d'equivalence.

**Cette version ne valide pas automatiquement** la disponibilite persistante du
contexte, la stabilite externe, la necessite d'une verification ou l'equivalence
operationnelle d'un outil. Avec les journaux actuels, ces faits restent souvent
inconnus : conserver le travail est alors la seule reference admissible, meme
si le detecteur a une confiance elevee dans son signalement.

## Catalogue local explicite

Extrait facultatif du `config.json` d'AgentWatch :

~~~json
{
  "replacements": {
    "enabled": true,
    "reaction_deadline_s": 60,
    "capabilities": [
      {
        "id": "cluster-wait",
        "rule_id": "G.repeated_calls",
        "strategy": "wait",
        "client": "codex",
        "tool_name": "mcp__cluster__wait_for_job",
        "contract": "Attendre au plus 60 s ; retourner job_id, etat final et erreurs.",
        "replacement_calls": 1
      }
    ]
  }
}
~~~

Une entree lie une regle exacte et une strategie (`batch`, `wait`, `structured`).
`client` et `target` sont des filtres facultatifs ; `target` est la cible du
signalement. `max_items` declare la limite d'un regroupement.
`replacement_calls` declare le nombre d'appels du scenario, sans le mesurer.
Un regroupement ou une attente uniques proposent un appel ; une substitution
structuree sans nombre explicite reste non chiffree.

| Disponibilite | Interpretation |
|---|---|
| `observed_available` | outil exact ayant reussi avant le travail, pour le meme acteur/contexte |
| `declared_unverified` | catalogue local sans cette observation |
| `to_create` | entree explicitement marquee `"status": "to_create"` |
| `unknown` | aucune entree applicable |

L'equivalence reste independante de la disponibilite, meme avec un contrat
declare. Un outil vu seulement apres le travail n'est pas retroactivement
disponible. L'absence d'observation ne signifie pas qu'il faut creer l'outil.
Le catalogue ne doit contenir que des metadonnees partageables, sans secrets.

### Allonger une attente deja utilisee

Une entree `strategy: "wait"` peut porter un `wait_contract` de version `1.0`.
Il decrit l'augmentation de `timeout_seconds`, en conservant `poll_seconds`.
Ce n'est pas une simulation de cadence : le temps de maintien de l'appel ouvert,
les sondages internes et la disponibilite de l'agent sont des proprietes distinctes.
La premiere attente est conservee pour etablir l'acces avant la continuation.
Les attentes sont toujours separees par job, acteur, session et epoque de contexte,
y compris en l'absence de contrat.

Le champ `scope` doit etre exactement celui produit par
`agentwatch.reports.wait_contracts.scope_for(calls)` pour le groupe concerne :
client, session, acteur, epoque, outil, identifiant de job ou empreinte, et SHA-256
des identifiants/evenements/parametres/horodatages. Un contrat ne se transfere pas
a un autre job ni a une autre trace. `reviewed_policy` contient les deux valeurs
`timeout_seconds` et `poll_seconds` effectivement examinees ; changer la politique
sans actualiser l'audit refute sa compatibilite.

`implementation_sha256` identifie le code audite. Son application doit etre liee
aux appels par une empreinte deja disponible dans leurs metadonnees
`tool_implementation_sha256`, ou par un `deployment_binding` de type
`deployment_record_review`. Cet audit de deploiement doit nommer la meme empreinte
d'implementation et le meme `calls_sha256`, avec sources. **Le collecteur n'ajoute
pas cette metadonnee automatiquement.** Le nom de l'outil, une version installee
aujourd'hui ou l'absence de changement observe ne suffisent pas. Une contradiction
dans une empreinte observee prime sur l'audit de deploiement.

Les conclusions d'audit sont dans `facts` :

| Cle | Conclusion a etablir |
|---|---|
| `terminal_return` | Retour apres detection terminale |
| `results_errors_identity` | Identifiant, etats, echecs, expiration et annulation restituables |
| `effects_preserved` | Effets requis locaux et distants conserves |
| `timeout_layers_compatible` | Limites client, serveur, transport et reprises compatibles |
| `intermediate_work_preserved` | Decisions et interruptions intermediaires preservees |
| `timeout_parameter_limit` | `value_seconds` : plafond du parametre, distinct du temps mural |
| `end_to_end_return_bound` | `value_seconds` et `includes_retries_and_locking: true` : borne de reprise du controle |

Chaque conclusion porte `state`, `basis` et `kind` (`local_source_review` ou
`local_configuration_review` ; `task_requirement_review` pour les decisions
intermediaires), et une liste `sources` non vide. Chaque source comporte un
`locator`, `sha256`, `line_start` et `line_end`. Les donnees mal formees ou absentes
restent inconnues. Une borne numerique n'est utilisable que si sa conclusion est
etablie. Un maximum observe ne fournit jamais une telle borne.

Il s'agit d'un **catalogue explicitement renseigne par un auditeur**, pas d'une
verification automatique du code : les empreintes et lignes assurent sa
tracabilite ; AgentWatch ne relit ni n'execute ces sources. Une chaine descriptive
ou une preference seule ne leve aucune garantie du service. Les conclusions
d'audit connues restent visibles meme si leur application historique est inconnue.
La sensibilite conserve leurs implications : supposer la bonne version ne peut
pas effacer une incompatibilite deja relevee dans cette version.

`control_return_requirement` separe la preference choisie (`kind:
"declared_preference"`, `max_seconds`, `basis`) de la borne technique auditee.
Sans preference ou sans borne applicable, cette condition reste inconnue.
Les controles de dependance, de succes et de perimetre ne sont pas remplacables
par des affirmations du catalogue. Aucun nombre d'appels de remplacement, gain de
tokens ou temps mural n'est infere du seul allongement du timeout : la trace ne
contient pas necessairement les etats et sondages internes requis pour le simuler.

`tests/test_wait_contracts.py` contient un contrat complet **synthetique** :
admissible, puis indetermine lorsqu'une seule preuve est retiree, puis rejete
lorsqu'elle est contredite. Il ne certifie aucun serveur reel.

### Conformite, equivalence et degradation

`wait_contract.assessment` separe trois questions : conformite au contrat de
sortie et d'identification (`results_errors_identity`, audit et application aux
appels), equivalence du service avec la politique actuelle, et defaut introduit
par le remplacement. Les deux dernieres restent `unknown` : les faits du contrat
ne constituent pas une comparaison des deux politiques. Un rejet pour absence
d'identifiant dans une erreur ne prouve pas que l'allongement produit ce defaut.
Meme un scenario admissible satisfait le contrat fourni ; il ne constitue pas
une mesure comparative du comportement de l'agent.

Le catalogue peut preciser `identifier_requirement` avec `mode`, `consumer` et
`basis`. Deux besoins distincts sont possibles :

- `self_contained_output` : l'identifiant doit figurer dans chaque sortie autonome ;
- `explicit_call_association` : une association explicite appel-arguments-reponse
  identifie la ressource sans ambiguite et suffit au consommateur nomme.

Sans declaration, le besoin reste `unspecified` ; AgentWatch n'impose pas une
exigence plus forte. Cette declaration descriptive ne change aucune conclusion
d'audit. Passer au second mode ne leve pas une contradiction : il faut justifier
le besoin du consommateur, la disponibilite de l'association et auditer le
contrat correspondant. Un identifiant d'appel observe ne prouve pas cette
suffisance a lui seul. Aucun contrat historique n'est reecrit automatiquement.

Pour la provenance, distinguer version declaree, empreinte de sources locales
et preuve liant le code au processus des appels. `cli_version` du rollout est
une version du **client**, pas du serveur MCP. Conserver une version presente
dans une configuration ne prouve pas qu'un processus l'a chargee. Le collecteur
n'effectue ni decouverte reseau ni lancement d'outil pour remplir ces inconnues.

## Cout, sensibilite et cadence

Les couts du scenario principal sont chiffres seulement apres admissibilite,
avec perimetre et unite explicites : appels, octets et couverture, parts de tokens
allouees. Les couts inconnus restent `null`. Aucune conversion octets/tokens,
aucune somme des durees paralleles en temps mural economisable.

La sensibilite fait varier chaque precondition inconnue, puis les suppose toutes
etablies ensemble. Cette derniere variante reste **hypothetique**, meme si un
compteur calculable apparait. Une condition refutee n'est jamais effacee.
Cette exploration n'est pas exhaustive : la coherence des hypotheses conjointes
n'est pas verifiee et le resultat n'est pas une borne superieure de gain.
Les fourchettes ne comparent que des perimetres identiques. Scenarios alternatifs
et signalements chevauchants ne s'additionnent pas.

Les syntheses comptent l'union des appels par client et session, puis par motif
dans `trends`. Les tableaux par projet/client limitent aussi les couts, exemples
et classements a ce perimetre. Les motifs differents restent non additionnables ;
`finding_cost_union` donne leur union globale, hors faux positifs marques. Les
occurrences incluent les signalements marques pour permettre leur revue.
Ce perimetre comprend tous les appels references, acquisition initiale incluse.
Le cout historique de G peut ne porter que sur les reprises avec aller-retour :
son champ `observed_cost.calls` donne ce denominateur distinct. Il ne doit pas etre
compare au cout de l'union comme s'il s'agissait du meme segment.
Les parts de tokens restent celles de l'import : sortie de la reponse emettrice
repartie entre ses appels, et entree non cachee de la reponse consommatrice au
prorata des resultats. Une reponse a plusieurs appels n'est pas recopiee sur chacun.
La reference conserver donne zero variation sur les couts connus du segment,
sans inventer une mesure de temps mural ni une mesure de tokens manquante.

`agentwatch.reports.token_coverage.allocation_coverage(view, calls)` explique la
couverture a partir des seules metadonnees numeriques : allocation absente,
correlation emettrice manquante, reponse absente/ambigue, compteur absent ou
allocation absente malgre un compteur lie. Un element MCP sans lien vers un
appel emetteur ne recoit pas les tokens de la reponse temporellement la plus
proche. Des compteurs presents dans le meme fil ne sont pas un cout attribuable
a cet appel. Ce diagnostic ne modifie ni les tokens ni les journaux.

Les nouveaux imports Codex conservent les identifiants effectivement exposes
dans `evidence.source_identity` : enregistrement, appel, element, session, fil,
tour et reponse, selon leur presence dans la source. La vue correlee garde les
identites de debut et de fin, ainsi que `allocation_correlation` avec les lignes
de la reponse emettrice, de la consommatrice et du parent retenu. Aucun texte de
prompt, resultat ou script n'est ajoute a ces metadonnees.

Une allocation numeriquement complete n'est pas une preuve de lien explicite.
`matching_function_call_id` indique une egalite d'identifiant de fonction ;
`last_open_exec_in_log` indique la regle existante de rattachement au dernier
exec ouvert. Le rattachement aux releves d'usage repose sur l'ordre des
enregistrements (`pending_calls_at_usage_record` et
`outputs_since_previous_usage_record`). Ces regles sont affichees comme telles,
sans ajouter d'appariement par distance temporelle. Un element termine apres la
fermeture de son exec reste sans emetteur s'il n'a aucun lien conserve. Un tour
commun ou des compteurs voisins ne comblent pas cette absence. Un identifiant
de reponse genere est distingue d'un champ explicitement present dans la source.

Le diagnostic `token_coverage` 1.1 expose cette provenance sans changer les
montants. Les anciens imports restent lisibles, avec provenance non enregistree
(`null` / `not_recorded`). Un import de verification se fait dans un magasin
distinct ; il ne reecrit jamais un corpus fige. Les parts de tokens restent une
allocation selon une regle, pas des couts marginaux ni des economies mesurees.

G utilise `cooldowns_s` et le budget fixe `min_tolerated_delay_s` (30 s par defaut).
L'ancien `tolerated_delay_ratio` est ignore **avec un avertissement** dans la
configuration, les rapports et `trends`, indiquant le budget applique et la
politique `fixed_configuration_before_replay`. La duree finale du job ne choisit
plus la politique. Ce budget concerne le lancement, pas la reception du resultat.
Le delai demande vient de `reaction_deadline_s` :

- `reaction_deadline_kind: "result_received"` (defaut) exige
  `cooldown + response_latency_bound_s + scheduling_jitter_bound_s <= delai` ;
- `reaction_deadline_kind: "poll_start"` exige `cooldown + gigue <= delai` et
  ne garantit pas quand le resultat sera recu.

Ces bornes sont des hypotheses de contrat declarees localement, pas des maxima
deduits de la trace. La borne de reponse doit couvrir l'obtention d'un resultat
utilisable, **timeouts et reprises compris** (`response_bound_includes_retries:
true`). Toute borne absente laisse la compatibilite inconnue ; une duree observee
qui contredit une borne declaree la refute. Une compatibilite arithmetique ne
prouve ni la persistance des etats ni la validite effective du contrat du service.
La consultation differee finale est servie et comptee. Sans changement d'etat
observe, le retard reste inconnu. La persistance des etats entre consultations
reste une hypothese. Modifier une trace ne demontre pas le comportement d'un LLM.

Chaque inconnue porte une demande `verification_requests` avec preuve manquante,
verification concrete et decision qu'elle permettrait de prendre. Ces demandes
sont affichees une fois par type de condition dans le resume. Elles ne permettent
pas de promouvoir une hypothese en preuve ni d'ignorer les autres conditions.

## Export JSON avec references

`report --format json` conserve le format **1.1**, sans modifier les lecteurs
existants. `report --format json-refs` choisit explicitement le format **2.0** :

~~~json
{
  "report_version": "2.0",
  "encoding": "agentwatch-shared-json-v1",
  "expanded_report_version": "1.1",
  "report": {"...": "rapport encode"},
  "shared": {"identifiant": "structure ou chaine partagee"}
}
~~~

Une reference a la table est un objet `{"$agentwatch_ref": "identifiant"}`.
Les identifiants sont deterministes ; les valeurs utilisateur qui ressemblent
a une reference sont echappees. La table peut contenir des objets, listes ou
chaines. Aucune reference physique, precondition ou cout n'est supprime.
Les valeurs partagees remplacent les copies repetees des memes preuves et
structures. L'encodage demande du calcul supplementaire ; ce format est un choix
explicite pour reduire le volume, pas une promesse d'acceleration.

Pour retrouver les valeurs exactes du rapport classique :

~~~python
import json
from agentwatch.reports.shared_json import expand_report

with open("rapport-refs.json", encoding="utf-8") as stream:
    rapport = expand_report(json.load(stream))
assert rapport["report_version"] == "1.1"
~~~

L'expansion rejette les versions inconnues, references absentes et cycles.
Elle reconstruit toutes les copies du format classique et peut donc demander
autant de memoire que celui-ci. `trends` conserve son format existant.

## Verification

~~~bash
python -m unittest tests.test_replacements tests.test_repeated_calls tests.test_tool_gap
python -m unittest tests.test_wait_contracts tests.test_token_coverage tests.test_shared_json
python -m unittest discover -s tests -t .
python -m agentwatch self-test
~~~

Les fixtures couvrent les contre-exemples et l'integration dans les rapports.
Elles ne mesurent pas des economies sur une session reelle.

La validation sur corpus reel a aussi revele des actions `web_search` sans
parametres exposes : G utilise leur empreinte d'entree pour les distinguer. Sans
cible, parametres comparables ni empreinte, le seul nom d'outil ne permet plus
de les presenter comme une meme sequence repetee.
