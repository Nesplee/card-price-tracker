# Card Price Tracker — Dashboard interactif Metabase — Design

**Statut :** implémenté et déployé en production (2026-08-12). Le schéma (`series`) et les 3 panneaux + 4 filtres du dashboard sont en place et vérifiés. Voir "Mise à jour post-implémentation" en fin de document pour l'écart avec la construction manuelle initialement prévue.

## Contexte et problème

Mois 3 Task 4 (stretch, optionnel) : construire un dashboard exploitant les données déjà en place (schéma en étoile `prod.*`, alimenté quotidiennement par le pipeline, plus `prod.dim_owned_card` — la collection personnelle de l'utilisateur, 562 cartes reliées au catalogue, importée le 2026-08-09).

L'utilisateur veut une vraie **page navigable** dans Metabase (déjà déployé et fonctionnel), pas seulement 2-3 graphiques statiques comme l'envisageait a minima le plan initial du Mois 3 : les plus grosses hausses/baisses de prix, un tri par bloc, par série, un filtre sur sa collection possédée, et une recherche par nom de carte.

**Point technique clé, vérifié avant de concevoir** : le concept de "bloc" que demande l'utilisateur correspond exactement au champ `series` de l'API pokemontcg.io (ex : le set "Paldea Evolved" appartient à la série "Scarlet & Violet"). Ce champ **existe déjà** dans les payloads bruts stockés depuis le Mois 1 (`raw.card_prices.payload->'set'->'series'`) mais n'a jamais été propagé jusqu'à `prod.dim_card` — aucun nouvel appel API n'est nécessaire, juste un ajout de colonne + une propagation dans le pipeline existant + un backfill depuis les données déjà en base.

**Fenêtre historique** : le pipeline ne collecte des prix TCGPlayer que depuis le 2026-08-07 — quelques jours d'historique au moment de la conception. Décision utilisateur explicite : construire le dashboard maintenant avec ce qui est disponible ; il devient naturellement plus riche chaque jour que le pipeline tourne, sans rien reconstruire.

## Décision

### Schéma : ajout de `series`

Propagé de bout en bout dans le pipeline existant, pas juste ajouté à `prod.dim_card` isolément (sinon la colonne cesserait d'être à jour dès la prochaine extraction) :

1. `src/transform/validate.py` : `CleanedCard` gagne un champ `series` (extrait de `payload["set"]["series"]`, `str | None` — comme `rarity`, son absence n'est pas assez grave pour rejeter la carte).
2. `migrations/009_add_series_to_schema.sql` : ajoute `series text` à `staging.card_prices` ET `prod.dim_card`. **Backfille immédiatement** `prod.dim_card.series` pour toutes les cartes déjà connues, depuis les payloads déjà stockés dans `raw.card_prices` (une seule requête SQL, jointure sur `card_id`, aucun appel API).
3. `src/load/staging_loader.py` : `_UPSERT_STAGING_SQL` propage `series`.
4. `src/load/warehouse_loader.py` : `_UPSERT_DIM_CARD_SQL` et la lecture de `staging.card_prices` propagent `series`.

### Dashboard Metabase

**Filtres globaux** (widgets de filtre Metabase, liés à toutes les questions concernées, recalcul en direct) :
- **Bloc** (`dim_card.series`)
- **Série** (`dim_card.set_name`)
- **Recherche** (texte, "contient", sur `dim_card.name`)
- **Collection uniquement** (bascule qui restreint aux cartes présentes dans `prod.dim_owned_card`)

Toutes les requêtes filtrent explicitement `platform_name = 'tcgplayer'` (décision utilisateur : scope USD uniquement, pas de mélange de devises — voir la bascule TCGPlayer déjà actée et la colonne `currency` de `dim_platform`).

**Panneaux (questions Metabase, une requête SQL chacune)** :

1. **Plus fortes hausses / baisses** : pour chaque carte, variation entre le prix le plus ancien et le plus récent disponibles dans la fenêtre filtrée (`average_sell_price`), en valeur et en pourcentage. Triable croissant/décroissant.
2. **Tableau des cartes** : nom, bloc, série, prix actuel — triable par n'importe quelle colonne, base pour naviguer/explorer le catalogue filtré.
3. **Valeur de la collection** : `SUM(quantity * prix_actuel)` (valeur totale actuelle) et `SUM(quantity * (prix_actuel - average_cost_paid))` (plus/moins-value), calculés sur `prod.dim_owned_card` jointe à la dernière observation de prix disponible par carte.

### Construction dans Metabase

Metabase se configure principalement via son interface web (assistants "New Question"/"New Dashboard"), pas par du code versionné — même nature que la configuration initiale du compte admin (plan Metabase, hors scope de l'automatisation). Le plan d'implémentation couvre : (a) le changement de schéma comme tâches de développement classiques (migration + code + tests, suivant le pattern déjà établi), et (b) un guide pas à pas avec le SQL exact de chaque panneau, pour assemblage manuel dans l'UI Metabase une fois le schéma déployé.

*(Voir "Mise à jour post-implémentation" : ce point (b) a finalement été automatisé via l'API REST de Metabase plutôt qu'assemblé manuellement, à la demande explicite de l'utilisateur.)*

## Vérification prévue

- Tests unitaires sur `validate_and_clean()` : `series` correctement extrait/absent (fonction pure, même pattern que les tests existants de `tests/test_transform.py`).
- Test d'intégration : `load_staging_to_warehouse` propage bien `series` jusqu'à `prod.dim_card`.
- Vérification du backfill : compter les cartes de `prod.dim_card` avec `series` renseigné après la migration (doit correspondre au nombre total de cartes, puisque tous les payloads déjà stockés ont ce champ).
- Chaque panneau Metabase vérifié manuellement contre des valeurs connues avant de le considérer terminé (ex : la valeur totale de la collection recalculée à la main sur un échantillon).

## Hors scope (rappel)

- Historique de prix plus profond que ce qui existe déjà (2026-08-07 et après) — pas de reconstruction rétroactive, impossible (l'API ne renvoie que les prix courants).
- Filtre plateforme CardMarket/EUR dans le dashboard — TCGPlayer/USD uniquement pour cette version.
- ~~Toute automatisation de la construction Metabase elle-même (comme pour son compte admin) — reste un assemblage manuel guidé, pas un artefact versionné.~~ Revu en cours d'implémentation, voir ci-dessous.

## Mise à jour post-implémentation (2026-08-12)

Le point "hors scope" sur l'automatisation de la construction Metabase a été revu en cours de route : l'utilisateur a explicitement demandé ("Tu ne peux pas gérer toi même les dashboards ?") que la construction soit automatisée plutôt que documentée comme un guide manuel pour assemblage dans l'UI. Ce point n'avait pas été anticipé au moment du design — arbitré directement avec l'utilisateur au moment où il s'est posé, pas décidé unilatéralement.

**Ce qui a réellement été fait** (au lieu du guide pas-à-pas prévu au point (b) de la section "Construction dans Metabase") : construction via l'API REST de Metabase (clé API fournie par l'utilisateur, stockée dans `.env` non commité), depuis l'intérieur du conteneur `airflow-scheduler` (seul point du réseau Docker avec accès à la fois à `metabase:3000` et à `curl`). Dashboard id=2 "Card Price Tracker — Dashboard" : 3 cartes (questions Metabase) correspondant exactement aux 3 panneaux du design, 4 filtres croisés correspondant exactement aux filtres globaux prévus. Chaque carte vérifiée individuellement contre `psql` avant assemblage, puis le dashboard assemblé vérifié end-to-end (propagation réelle des filtres depuis le dashboard vers les cartes, pas seulement testée carte par carte).

**Écart technique découvert en cours de route, non anticipé par le design** : le filtre booléen "Collection uniquement" ne peut pas être implémenté par un tag de paramètre Metabase placé dans un commentaire SQL (`--{{tag}}`) — rejeté explicitement par le moteur de requête Metabase. Contournement : utiliser le tag comme un vrai prédicat SQL toujours-vrai-si-présent (`AND {{collection_uniquement}} IS NOT NULL`), vérifié empiriquement par comparaison de `row_count` avec/sans filtre.

Cette configuration Metabase (cartes + dashboard) n'est pas versionnée dans le repo (nature déclarative, vit dans la base H2 interne de Metabase, comme prévu au design initial) — seul le schéma SQL sous-jacent (migration 009) et le code de propagation (`validate.py`, `staging_loader.py`, `warehouse_loader.py`) le sont.
