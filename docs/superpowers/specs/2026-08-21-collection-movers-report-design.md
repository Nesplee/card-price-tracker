# Card Price Tracker — Rapports daily/weekly (gros mouvements de collection) — Design

**Statut :** en cours de conception, pas encore implémenté.

## Contexte et problème

L'utilisateur veut un moyen de repérer rapidement, dans sa collection, les
cartes dont le prix a beaucoup bougé — à la hausse comme à la baisse — sans
avoir à parcourir carte par carte les 1000+ lignes de "Ma collection". Deux
cadences demandées :

- **Daily** : liste des cartes possédées dont le prix a varié de ±10/20/30%
  ou plus sur les 7 derniers jours.
- **Weekly** : la même chose sur une fenêtre de 30 jours, en plus riche —
  un top 5 hausses / top 5 baisses, chacun avec un mini-graphique
  d'historique de prix.

Décidé avec l'utilisateur (2026-08-21) :
- Fenêtre daily = 7 jours, fenêtre weekly = 30 jours (des fenêtres
  différentes évitent que les deux rapports fassent doublon).
- Calcul à la volée (nouvelle requête SQL à chaque ouverture de la page),
  pas de pré-calcul par une tâche Airflow ni de nouvelle table — cohérent
  avec le reste du dashboard (`docs/superpowers/specs/2026-08-13-custom-dashboard-design.md`),
  qui est déjà 100% à la volée, et le volume actuel (quelques centaines de
  cartes possédées) est largement dans les clous pour une requête
  synchrone.
- Diffusion : nouvelle page dans le dashboard existant, pas d'envoi
  email/notification (pas de nouvelle infra SMTP à gérer pour ce chantier).

**Données déjà disponibles, aucun nouvel appel API externe ni changement de
schéma nécessaire** : `prod.dim_owned_card` (cartes possédées, `quantity`),
`prod.fact_price_history` (historique quotidien par carte/plateforme,
depuis le 2026-08-07), `prod.dim_card` (nom).

## Décision

### Portée

Cette fonctionnalité concerne uniquement les cartes **possédées**
(`dim_owned_card`), pas le catalogue complet — c'est un rapport sur "ma
collection", comme demandé. `cost_unknown` (coût d'achat inconnu) n'a
aucune influence ici : le rapport compare des prix de marché entre deux
dates, jamais un coût d'achat, donc toutes les cartes possédées sont
éligibles indépendamment de ce flag.

Une carte n'apparaît dans un rapport que si elle a une observation de prix
à la fois à la date la plus récente et à la date de comparaison (N
observations en arrière) — pas d'extrapolation sur une carte dont
l'historique est trop court.

### API — un seul nouvel endpoint, paramétré par fenêtre

`GET /api/reports/movers?window=7|30`

- `window` : nombre d'observations de prix en arrière à comparer (pas un
  nombre de jours calendaires — même convention que les moyennes mobiles
  déjà implémentées côté frontend sur la fiche carte : "N lignes en
  arrière" dans la série ordonnée par date, pas "N jours calendaires en
  arrière"). Valeurs acceptées : `7` ou `30` ; toute autre valeur → 422.
- Requête SQL (nouvelle fonction dans `src/api/queries.py`, style cohérent
  avec l'existant) : un CTE `ROW_NUMBER() OVER (PARTITION BY card_id ORDER
  BY date_id DESC)` sur `fact_price_history` (filtré
  `platform_name = 'tcgplayer'`), puis deux LEFT JOIN sur ce CTE — un pour
  le prix le plus récent (rang 1), un pour le prix de comparaison (rang
  `1 + window`) — joints à `dim_owned_card` + `dim_card`.
- Calcul de la variation en pourcentage fait côté Python (dans
  `src/api/main.py`), comme `market_value`/`gain_loss` le sont déjà pour
  `/api/collection` : `pct_change = (current - past) / past * 100`. Lignes
  exclues si `current` ou `past` est `None`, ou si `past == 0` (division par
  zéro).
- Réponse : liste triée par `abs(pct_change)` décroissant, ne retenant que
  les cartes avec `abs(pct_change) >= 10`. Chaque élément inclut
  `card_id`, `name`, `quantity`, `current_price`, `past_price`,
  `pct_change`, et `threshold` (30, 20 ou 10 — le plus grand palier
  franchi, pour permettre au frontend d'afficher un badge de sévérité sans
  recalculer de logique de seuil côté client).

Le top 5 / flop 5 de la vue weekly ne nécessite **aucun endpoint
supplémentaire** : c'est simplement les 5 premières et 5 dernières lignes
de la réponse `window=30` (déjà triée par `abs(pct_change)` décroissant —
il suffit de re-trier par signe pour séparer hausses/baisses). Les
mini-graphiques réutilisent l'endpoint existant `GET
/api/cards/{card_id}/history`, appelé une fois par carte du top/flop (10
appels max, négligeable).

### Frontend — nouvelle page `Rapports.tsx`

- Nouvelle route `/rapports` + lien de navigation "Rapports" dans
  `App.tsx`, à côté de "Catalogue" et "Ma collection".
- Deux sections sur la même page (pas deux pages séparées, pour rester
  simple) :
  1. **Daily** (`window=7`) : tableau des cartes ±10/20/30%, triées par
     ampleur, avec un badge coloré par palier (ex : rouge/orange/jaune
     selon la sévérité, vert/rouge selon le sens hausse/baisse — détail
     visuel laissé à l'implémentation, cohérent avec le design déjà en
     place — points de rareté, palette existante).
  2. **Weekly** (`window=30`) : même tableau, **plus** un bloc "Top 5
     hausses" / "Top 5 baisses" juste au-dessus, chaque carte du top/flop
     affichant un mini-graphique (`LineChart` Recharts, taille réduite,
     pas de sélecteur de plage ni de moyennes mobiles — c'est un aperçu,
     pas la fiche carte complète) construit à partir de
     `fetchCardHistory(card_id)`.
- Chaque ligne des deux tableaux renvoie vers `/cartes/:cardId` (même
  pattern que Catalogue et Ma collection) pour consulter l'historique
  complet si besoin.
- Aucune nouvelle dépendance (Recharts et react-router-dom déjà utilisés).

### Gestion d'erreurs

- `window` hors de `{7, 30}` → 422 (validation Pydantic/FastAPI, même
  pattern que `price_min > price_max` sur les endpoints existants).
- Aucune carte ne dépasse le seuil de 10% → liste vide, le frontend
  affiche un message neutre ("Aucun mouvement notable sur cette période"),
  pas une erreur.
- Les 10 appels `fetchCardHistory` du bloc top/flop weekly sont
  indépendants : l'échec d'un seul (carte sans historique complet, 404
  déjà géré par l'endpoint existant) ne doit pas casser l'affichage des 9
  autres.

## Vérification prévue

- Tests pytest sur la nouvelle fonction `queries.py` (fixture avec au
  moins 3 cartes : une hausse >30%, une baisse >20%, une carte stable
  <10% qui ne doit pas apparaître) et sur le contrat de l'endpoint
  (`window` invalide → 422, tri par ampleur décroissante, exclusion des
  cartes sans prix aux deux dates).
- Build TypeScript propre (`tsc -b && vite build`) sans erreur.
- Vérification manuelle de la page en dev (ou via le VPS si le navigateur
  n'est pas utilisable dans l'environnement d'implémentation, comme déjà
  rencontré sur ce projet — voir
  `docs/superpowers/specs/2026-08-13-custom-dashboard-design.md`).

## Hors scope (cette version)

- Envoi automatique (email, Slack, notification push) — page consultable
  à la demande uniquement.
- Pré-calcul/historisation des rapports générés (une nouvelle table
  "snapshot de rapport quotidien" par exemple) — recalcul à la volée à
  chaque consultation, aucun historique des rapports eux-mêmes conservé.
- Seuils configurables par l'utilisateur (les paliers ±10/20/30% sont en
  dur) — pourra être revisité si le besoin se confirme.
- Prise en compte du grade/variance dans le calcul (cf. limitation déjà
  identifiée : le prix stocké est celui de la carte brute, pas de la carte
  gradée réellement possédée) — hors scope de ce chantier, sujet séparé.
