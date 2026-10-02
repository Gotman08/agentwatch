# Prévision d'activité enregistrée — prototype de recherche

Ce répertoire est indépendant du collecteur, des détecteurs A–G et des verdicts
de remplacement. AgentWatch ne l'importe pas. L'entraînement et l'occlusion
s'exécutent uniquement sur demande, localement, sur un corpus normalisé figé.

## Cible et couverture

La cible est la répartition des **8 prochains appels enregistrés** entre huit
familles, à partir de 32 appels précédents du même acteur et de la même époque
de contexte. Chaque pas contient quatre appels : huit pas d'entrée et deux pas
de cible. Les cibles successives ne se chevauchent pas. Les historiques peuvent
se chevaucher ; les fenêtres ne sont donc pas des observations indépendantes.

Il s'agit d'une horloge événementielle, pas d'une prévision par minute. Une
famille à zéro signifie qu'aucun des quatre appels énumérés n'en relève. Les
silences des journaux ne sont pas transformés en zéros. La couverture continue
de collecte, les appels non enregistrés, la cadence réelle et la disponibilité
des données en ligne à l'heure indiquée ne sont pas établis par ce prototype.
La cible est conditionnelle à l'existence de huit appels enregistrés suivants.

Les groupes se séparent par session racine, acteur et époque. Une ambiguïté ou
un début absent coupe la séquence. Les frontières de blocs avec horodatages
égaux ou non ordonnés sont exclues. Les familles viennent des catégories
normalisées : lecture, recherche, liste, écriture/édition, shell, MCP,
coordination et web/autres. Cette dernière conserve les catégories inconnues.
Aucun token, résultat, paramètre, ressource ou signalement n'entre dans le modèle.

## Comparaison

Le protocole fixe les racines d'entraînement, de validation et de test avant les
résultats. Tous les acteurs d'une racine restent ensemble. Ce partage n'est pas
une séparation par projet ni une validation prospective.

- Moyenne d'entraînement, avec un poids égal par racine.
- Rythme récent : répartition des huit derniers appels.
- GRU temporel multivarié, 16 unités, toutes les familles en entrée.
- Petit STGNN : mélange spatial `[X, AX]`, embedding de famille, GRU temporel
  partagé de 16 unités, projection puis softmax donnant un total attendu de 8.
- Même STGNN avec `A=I` : même capacité et mêmes graines, sans échange entre
  familles. Ce contrôle isole l'apport du graphe de celui de l'architecture.

`A[i,j]` est une association positive au pas précédent, obtenue par similarité
cosinus des comptes entre familles sur les **entrées d'entraînement seulement**.
Les contributions des racines sont pondérées également, une boucle propre est
ajoutée, puis chaque ligne est normalisée. Ce graphe statistique ne représente
ni des dépendances établies entre outils ni des relations causales.

Trois graines fixées, une configuration, au plus 80 époques, arrêt après 12
époques sans amélioration de la MAE de validation. La perte MSE est pondérée pour
donner le même poids à chaque racine. La sélection du modèle d'occlusion utilise
uniquement la validation. Les poids, le graphe et les références sont figés et
empreints avant le calcul des métriques de test.

La MAE est calculée par famille, puis moyennée également par racine de test. Les
comparaisons de modèles moyennent leurs trois graines. Le bootstrap apparié
rééchantillonne les racines, jamais les fenêtres comme si elles étaient
indépendantes. Avec quatre racines, son intervalle reste exploratoire.

## Occlusion

Le modèle STGNN retenu reste figé. Sur les mêmes prévisions, on masque chaque
famille pendant tout l'historique, puis chacun des quatre blocs temporels de
deux pas. Seule la référence change : zéro, moyenne des entrées d'entraînement
avec poids égal par racine, ou fenêtre historique d'entraînement la plus proche
de cette moyenne en distance quadratique. Les égalités sont départagées par
l'identifiant de fenêtre. Le graphe n'est jamais recalculé après perturbation.

L'importance est la variation absolue moyenne du vecteur de prévision. Les
variations signées sont aussi conservées. Les classements sont comparés par
Spearman et recouvrement des trois premières positions, égalités comprises.
Un classement constant est déclaré non informatif, pas parfaitement stable.

Le total de quatre appels par pas impose une contrainte de composition. Une
occlusion peut la violer ; ce taux et les comptes devenus fractionnaires sont
mesurés. Une référence historique conserve cette contrainte quand elle remplace
un bloc temporel entier, sans garantir la plausibilité des raccordements.
Ces explications portent sur **ce modèle**, pas sur le raisonnement de Claude
ou Codex, la nécessité des opérations, le gaspillage ou les économies de tokens.

## Exécution

Dépendances du prototype : NumPy, PyTorch CPU, SciPy ; Matplotlib seulement pour
les figures. L'expérience initiale utilise NumPy 2.2.6, PyTorch 2.7.1+cpu,
SciPy 1.15.3 et Python 3.13.4. Aucune dépendance n'est ajoutée à AgentWatch.

Depuis la racine du dépôt, fournir des chemins vers un protocole et un manifeste
de corpus figé, puis choisir de nouveaux dossiers de sortie :

```powershell
python -m research.activity_forecast.data --manifest MANIFESTE.json --protocol PROTOCOLE.json --out DOSSIER_DONNEES
python -m unittest research.activity_forecast.test_protocol -v
python -m research.activity_forecast.experiment --dataset DOSSIER_DONNEES/dataset.npz --protocol PROTOCOLE.json --out NOUVELLE_EXPERIENCE
```

L'extraction vérifie les empreintes et lit directement les fichiers figés ; elle
n'appelle ni l'import automatique ni un outil observé dans les traces. Une fois
le NPZ produit, l'expérience de modélisation ne dépend plus du code AgentWatch.
Le programme refuse d'écraser une expérience dont `freeze.json` existe.
`freeze.json`, les poids, les prédictions, les métriques par racine et graine,
les matrices et les perturbations permettent de contrôler les conclusions.

## Sources méthodologiques

[STGCN, Yu et al., IJCAI 2018](https://www.ijcai.org/proceedings/2018/505) motive
l'étude conjointe des relations et du temps. Ce prototype GRU n'est pas une
reproduction de l'architecture entièrement convolutionnelle de cet article.

[Dynamask, Crabbé et Van der Schaar, ICML 2021](https://proceedings.mlr.press/v139/crabbe21a.html)
étudie les perturbations temporelles. Ici les masques sont fixes ; aucun masque
optimisé Dynamask n'est appris.

[Attribution baselines, Sturmfels et al., Distill 2020](https://distill.pub/2020/attribution-baselines/)
motive la comparaison de références. L'expérience actuelle emploie une occlusion
directe, pas les méthodes d'attribution par chemins de cet article.
