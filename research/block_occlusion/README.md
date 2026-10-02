# Occlusions par blocs historiques — prolongement exploratoire

Cette étude reprend le STGNN figé de la première expérience et les 120 mêmes
prévisions. Elle compare uniquement des remplacements de deux pas consécutifs,
toutes les familles ensemble. Chaque pas contient quatre appels enregistrés.
Elle ne réentraîne aucun modèle et n'est pas un nouveau test indépendant.
AgentWatch n'importe pas ce module ; collecteur, A–G et verdicts sont inchangés.

## Références

Treize règles sont appliquées aux mêmes quatre masques :

- La fenêtre historique fixe de la première expérience.
- Six fenêtres fixes d'entraînement : une par racine, choisie par le plus petit
  identifiant de fenêtre, sans consulter la prévision ou la cible.
- Six références contextuelles : pour chaque prévision et chaque masque, la
  fenêtre la plus proche parmi celles de chaque racine d'entraînement.

La distance contextuelle utilise uniquement les six pas visibles, dans la
requête comme dans le candidat. Le bloc masqué ne participe jamais au choix.
Les pas visibles situés après le bloc sont encore dans l'historique précédant
la prévision ; les huit appels futurs ne sont jamais utilisés. Les égalités
sont départagées par l'identifiant. Une racine avec une seule fenêtre ne fournit
qu'un seul candidat, même dans la sélection contextuelle.

Le bloc donneur est extrait aux mêmes positions que le bloc remplacé. Aucune
famille n'est renormalisée ou redistribuée. Aucune moyenne de comptes n'est
injectée dans le réseau. La moyenne d'un panneau de références se calcule sur
les six vecteurs d'importance, après six évaluations distinctes du modèle.

## Mesures et portée

L'importance est la variation absolue moyenne du vecteur prédit. Les quatre
blocs sont classés, puis comparés avec Spearman et l'ensemble des premières
places, égalités comprises. Les classements constants sont non informatifs.
Les comparaisons sont moyennées par racine évaluée, puis entre racines.

Chaque remplacement vérifie les comptes entiers non négatifs, le total de
quatre, l'appartenance du donneur à l'entraînement et la conservation exacte
des pas visibles. Les remplacements identiques à l'original sont recensés :
une importance nulle dans ce cas constate une absence de transformation.

La validité de ces contraintes ne garantit pas un historique plausible.
Le choix de référence, la composition et l'amplitude du remplacement restent
liés. Une référence mieux appariée au contexte n'est pas nécessairement une
explication plus fidèle. Les mêmes sessions et le même modèle étant réutilisés,
les résultats sont descriptifs et exploratoires. Le partage original n'est pas
chronologique ; cette étude ne qualifie ni déploiement prospectif, ni économie
de tokens, ni nécessité des opérations, ni raisonnement de Claude ou Codex.

## Exécution locale

Python, NumPy, PyTorch CPU et SciPy déjà employés dans la première expérience
suffisent. La commande lit une livraison originale figée, le nouveau protocole
et exige un dossier de résultats qui n'existe pas encore :

```powershell
python -B -m unittest research.block_occlusion.test_study -v
python -B -m research.block_occlusion.study --original PREMIERE_LIVRAISON --protocol NOUVEAU_PROTOCOLE.json --out NOUVELLE_ANALYSE
```

Les tests vérifient notamment l'absence d'accès aux cibles, l'invariance du choix
quand les valeurs masquées sont modifiées, l'effet du contexte visible, les
égalités et les perturbations sans changement.

Le programme vérifie les empreintes de la première livraison et du code de
modélisation effectivement importé. Il écrit le choix de tous les donneurs et
son empreinte avant les nouvelles inférences, conserve chaque perturbation et
refuse d'écraser une analyse existante. Il vérifie à nouveau l'intégrité après
le calcul. Les figures et le rapport accompagnent séparément les résultats.
