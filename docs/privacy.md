# Confidentialite

## Principes

- Tout reste local : aucun export reseau, aucun appel a un service.
- Liste de champs autorises : seuls les parametres enumeres par outil sont conserves en
  clair (voir `docs/events.md`). Les autres cles ne sont gardees que sous forme
  `nom + empreinte de la valeur`.
- Jamais conserves par defaut : prompts (seule la longueur), reponses du modele, contenus
  de fichiers lus ou ecrits (seules longueur et empreinte), sorties de commandes, variables
  d'environnement, `transcript_path`.
- Les extraits de sortie (`detailed_excerpts`) sont bornes (`detailed_excerpt_chars`) et
  desactives par defaut ; ils passent aussi par le masquage.

## Masquage des secrets

Applique avant toute ecriture, y compris dans les diagnostics, en une seule passe (un
remplacement n'est jamais re-analyse). Motifs : cles AWS, jetons GitHub, cles `sk-*`,
jetons Slack, JWT, cles Google, blocs PEM, `user:motdepasse@` dans les URL, valeurs
suivant `api_key`, `token`, `password`, `authorization`, `Bearer`, etc. Les URL de
WebFetch perdent leur requete et leur fragment.

Une valeur masquee devient `<secret:xxxxxxxxxx>` ou `xxxxxxxxxx` est le debut de son
empreinte HMAC : deux secrets differents donnent deux marqueurs differents, donc les
comparaisons (repetition de commande) restent possibles sans fabriquer de doublons.

Le masquage est heuristique : un secret d'une forme inconnue peut passer. Verifiez les
rapports avant de les partager.

## Empreintes HMAC

- Cle de 32 octets tiree au premier evenement, stockee dans `<home>/keys/hmac.key`
  (droits 0600 sur POSIX ; sous Windows, protection par le dossier utilisateur).
- Utilisee pour : empreintes d'entree et de resultat, contenus d'edition, valeurs de
  parametres non autorises, marqueurs de secrets.
- Comparaison : deux empreintes egales signifient des contenus identiques au moment des
  appels (pas une equivalence semantique). Deux dossiers de donnees ont des cles
  differentes : leurs empreintes ne sont pas comparables entre elles.
- Limite : quiconque possede la cle et une hypothese de contenu peut verifier si elle
  correspond a une empreinte. La cle ne doit jamais etre exportee avec les rapports.

## Dossier utilisateur

`mask_home_dir` (defaut : vrai) remplace le prefixe du dossier utilisateur par `~` dans
les chemins conserves. Les chemins relatifs au projet sont stockes relatifs.

## Donnees collectees = donnees non fiables

Tout ce qui vient des hooks (noms d'outils, commandes, erreurs) est traite comme des
donnees : jamais evalue, jamais execute, jamais interprete comme une instruction. Le
rejeu (`replay`) reanalyse des fichiers ; il n'execute aucune commande contenue.

## Installation reversible

`configure` montre un diff avant d'ecrire, cree une sauvegarde, ne modifie que les
entrees AgentWatch et laisse intactes les autres. `uninstall` ne retire que ces entrees.
Les permissions des clients ne sont jamais modifiees ; les hooks ne renvoient jamais de
decision (allow/deny) ni de contexte au modele.
