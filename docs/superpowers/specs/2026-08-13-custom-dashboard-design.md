# Card Price Tracker — Dashboard sur mesure — Design

**Statut :** en conception, pas encore implémenté.

## Contexte et problème

Le dashboard Metabase (Mois 3 Task 4, voir [`2026-08-10-interactive-dashboard-design.md`](2026-08-10-interactive-dashboard-design.md)) est déployé et fonctionnel, mais ne satisfait pas l'utilisateur : le rendu visuel des "questions" Metabase ne correspond pas à ce qu'il attendait pour explorer sa collection, et l'outil ne permet pas les usages voulus (graphes d'évolution de prix dans le temps sur une carte donnée, recherche libre combinée à des filtres, mise en forme entièrement contrôlée).

Décision (discutée avec l'utilisateur le 2026-08-13) : construire une **application web sur mesure**, en complément de Metabase (qui reste disponible pour l'exploration SQL ad-hoc), plutôt que de continuer à pousser les capacités natives de Metabase.

**Données déjà disponibles, aucun nouvel appel API ni changement de schéma nécessaire** :
- `prod.dim_card` (`name`, `set_name`, `series`, `rarity`) — 19 545 cartes.
- `prod.fact_price_history` (`date_id`, `platform_id`, `average_sell_price`, `trend_price`, `low_price`) — historique quotidien depuis le 2026-08-07.
- `prod.dim_owned_card` (`quantity`, `average_cost_paid`) — 562 cartes de la collection personnelle de l'utilisateur.
- Rôle Postgres `dashboard_reader` (migration 007) — lecture seule, scopé au schéma `prod`, déjà utilisé par Metabase, réutilisé tel quel ici.

## Décision

### Architecture

Deux nouveaux services Docker, ajoutés à `docker-compose.prod.yml` uniquement (jamais à `docker-compose.yml` local — même convention que Metabase) :

- **`dashboard-api`** (FastAPI, Python) : lit `prod.*` en lecture seule via `dashboard_reader`. Choix FastAPI plutôt que Node/Express pour rester dans l'écosystème Python déjà utilisé par tout le reste du repo (pas de second runtime serveur à maintenir pour un bénéfice marginal ici).
- **`dashboard-frontend`** (React + Vite, build statique servi par Nginx, proxy vers `dashboard-api` en interne au réseau Docker) : choix React plutôt que HTML/JS simple ou Streamlit — stack la plus demandée côté marché de l'emploi (signal recruteur), écosystème de composants de graphs riche (Recharts), et démontre une compétence frontend en plus du backend/data déjà couvert par le reste du projet.

Les deux conteneurs exposés uniquement sur `127.0.0.1` (même pattern qu'Airflow/Metabase) — accès via `ssh -L <port>:localhost:<port> card-tracker-vm`. Pas d'exposition publique dans cette version.

### Vues (pages pré-conçues, pas de constructeur de vues dynamique)

Trois pages statiques dans leur structure, dynamiques dans leur contenu :

1. **Catalogue** — recherche texte sur `dim_card.name` + filtres combinables (Bloc = `series`, Série = `set_name`, Rareté = `rarity`, plage de prix) + tableau paginé triable par prix. Chaque ligne renvoie vers le détail de la carte.
2. **Détail carte** — fiche carte (nom, bloc, série, rareté) + graphe d'évolution des prix dans le temps (`average_sell_price`, `trend_price`, `low_price`), zoomable sur une période.
3. **Ma collection** — valeur totale de la collection dans le temps + tableau des cartes possédées avec plus/moins-value individuelle (`quantity * (prix_actuel - average_cost_paid)`).

Toutes les vues filtrent explicitement `platform_name = 'tcgplayer'` (même décision que le dashboard Metabase : scope USD uniquement, pas de mélange de devises entre TCGPlayer et CardMarket).

**Correction d'un biais déjà identifié sur le dashboard Metabase** : les lignes de `dim_owned_card` avec `average_cost_paid = 0.0000` (coût non renseigné dans le CSV importé, traité à tort comme un vrai 0$ par une somme brute) sont marquées par un flag `cost_unknown` côté API et affichées séparément dans la vue "Ma collection", plutôt que sommées aveuglément dans la plus-value totale.

### API — endpoints

- `GET /api/cards?search=&series=&set_name=&rarity=&price_min=&price_max=&page=` : liste paginée. Jointure `dim_card` + dernier prix connu par carte (sous-requête `MAX(date_id)` par `card_id`, `platform_name='tcgplayer'`).
- `GET /api/cards/{card_id}/history` : série temporelle complète (`date_id`, `average_sell_price`, `trend_price`, `low_price`) pour le graphe de la vue Détail carte. 404 si `card_id` inconnu.
- `GET /api/collection` : cartes possédées jointes à leur dernier prix + `average_cost_paid` + flag `cost_unknown`.
- `GET /api/collection/value-history` : valeur totale agrégée par jour, calculée uniquement sur les lignes avec un coût connu (`cost_unknown = false`).

### Gestion d'erreurs

- Validation des paramètres de requête via les modèles Pydantic de FastAPI (ex : `price_min > price_max` → 422 automatique).
- `card_id` inconnu sur `/api/cards/{card_id}/history` → 404 explicite.
- `dashboard_reader` étant strictement lecture seule au niveau Postgres, aucune écriture n'est possible depuis l'API — pas de risque de corruption des données, seulement des erreurs de requête à valider côté client.

## Vérification prévue

- Tests de contrat pytest sur chaque endpoint (cas nominal, filtre vide, carte inconnue), dans `tests/`, suivant le pattern déjà établi par le reste du repo.
- Vérification manuelle de chaque vue en dev avant déploiement (pas de tests frontend automatisés dans cette version — hors scope pour un portfolio solo, cf. section suivante).
- Une fois déployé sur le VPS : vérification via tunnel SSH que les 3 vues chargent et que les filtres/recherche produisent des résultats cohérents avec `psql`.

## Hors scope (cette version)

- Constructeur de vues dynamique façon Metabase/Grafana (l'utilisateur choisirait lui-même les champs/graphs affichés et sauvegarderait sa propre vue) — décision explicite de rester sur des vues pré-conçues, scope trop large sinon.
- Exposition publique (HTTPS, nom de domaine) — accès privé via tunnel SSH uniquement pour l'instant ; pourra être traité comme un chantier séparé si le besoin de partager un lien à un recruteur se confirme.
- Tests frontend automatisés (composants React) — vérification manuelle suffisante à ce stade.
- Retrait ou dépréciation du dashboard Metabase existant — reste disponible en parallèle pour l'exploration SQL ad-hoc.
- Filtre plateforme CardMarket/EUR — TCGPlayer/USD uniquement, comme pour le dashboard Metabase.
