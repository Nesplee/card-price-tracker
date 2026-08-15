# Card Price Tracker

Pipeline de données de bout en bout qui suit l'évolution quotidienne des prix
de cartes Pokémon (source [pokemontcg.io](https://pokemontcg.io), qui relaie
les prix TCGPlayer et CardMarket), avec une architecture **raw → staging →
production** en schéma en étoile, orchestrée par Apache Airflow et tournant
automatiquement chaque jour en production sur un VPS auto-hébergé.

Projet portfolio réalisé pour démontrer des compétences de Data Engineering :
extraction robuste face à une API tierce instable, idempotence de bout en
bout, moindre privilège sur les accès base de données, et une orchestration
qui survit réellement à des pannes en conditions réelles — pas seulement en
théorie (voir [Fiabilité en production](#fiabilité-en-production) ci-dessous,
qui documente un vrai incident et sa correction).

## Architecture

```
pokemontcg.io API (TCGPlayer + CardMarket)
      │  extract_and_load_raw (checkpoint par page, reprend après échec)
      ▼
raw.card_prices                  copie brute (payload JSON complet, traçable)
      │  clean_to_staging (validation + nettoyage)
      ▼
staging.card_prices              cartes valides, typées
staging.card_prices_quarantine   cartes rejetées + raison explicite
      │  load_to_warehouse
      ▼
prod.fact_price_history          schéma en étoile, prix par carte/jour/plateforme
prod.dim_card / dim_date / dim_platform (avec devise : EUR pour CardMarket, USD pour TCGPlayer)
```

Orchestré par un DAG Airflow quotidien (`extract >> clean >> load`), chaque
tâche = une transaction Postgres. **Idempotent de bout en bout** : rejouer un
jour déjà traité met à jour les lignes de ce jour sans dupliquer ni affecter
l'historique — vérifié par un test d'intégration qui rejoue le pipeline
complet deux fois de suite (`tests/test_idempotence.py`).

**Deux plateformes de prix coexistent délibérément** : les données
historiques CardMarket ne sont jamais supprimées (le schéma interdit tout
`DELETE` sur `fact_price_history`), TCGPlayer est simplement devenu la source
par défaut après un changement de conception documenté ci-dessous. La colonne
`currency` sur `dim_platform` évite de mélanger silencieusement des euros et
des dollars dans les mêmes agrégats.

## Stack

Python 3.11 · PostgreSQL 16 · Apache Airflow 2.9.3 (LocalExecutor) · Docker
Compose · Metabase (exploration de données) · GitHub Actions (tests + lint) ·
déployé sur un VPS OVH.

## Fiabilité en production

Ce pipeline appelle une API tierce mesurée à ~37% d'échecs transitoires
(5xx/timeouts) sur une extraction complète (~80 pages, ~20 000 cartes). Trois
mécanismes, empilés, rendent ça gérable :

1. **Checkpoint par page** (`src/extract/pipeline.py`) : chaque page réussie
   est commitée immédiatement. Une panne en cours de route ne perd jamais le
   travail déjà fait — la reprise continue exactement où elle s'est arrêtée.
2. **Retries généreux, peu coûteux** (`retries=60`, `retry_delay=30s` sur la
   tâche d'extraction) : comme chaque retry reprend via le checkpoint plutôt
   que de repartir de zéro, un budget élevé ne coûte quasiment rien.
3. **Plafond de durée cumulée** (`dagrun_timeout=4h`) : distinct d'un simple
   timeout par tentative (qui ne borne rien du cumul des 60 retries) — un run
   anormalement bloqué finit par échouer visiblement plutôt que de tourner
   indéfiniment sans jamais apparaître en rouge.

**Testé en conditions réelles, pas juste en théorie** : le run automatique du
2026-08-07 a épuisé son budget de retries (alors fixé à 20) pendant une panne
`pokemontcg.io` prolongée. Diagnostiqué à partir des logs de production,
corrigé (retries, plafond de durée, et un bug de calcul de date qui aurait pu
corrompre le checkpoint si une séquence de retries traversait minuit UTC),
puis validé en rejouant ce run précis jusqu'à `success` en production. Détail
complet : `docs/superpowers/specs/2026-08-08-dag-reliability-design.md`.

## Sécurité

- Accès base de données par moindre privilège : `pipeline_app` (lecture/écriture,
  scopé aux schémas dont le pipeline a besoin) pour l'orchestration,
  `dashboard_reader` (lecture seule, scopé au seul schéma `prod`) pour
  l'exploration de données — jamais le même rôle pour les deux usages.
- VPS de production : SSH par clé uniquement, firewall restreint au seul port
  22. Airflow (8080), Metabase (3000), l'API (8000) et le frontend (5173) du
  dashboard sur mesure sont **jamais exposés sur l'interface publique** —
  accessibles en HTTPS via le tailnet Tailscale (certificat Let's Encrypt
  émis pour le nom MagicDNS du VPS), qui joue le même rôle d'accès restreint
  qu'un tunnel SSH sans nécessiter de tunnel actif. Voir
  `docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md`.
  PostgreSQL n'est jamais publié du tout.
- Aucun secret commité : mots de passe générés localement, synchronisés en
  base séparément du SQL versionné (`scripts/apply_migrations.sh`).

## Explorer les données (Metabase)

Un rôle Postgres dédié en lecture seule (`dashboard_reader`) alimente une
instance Metabase auto-hébergée, accessible en HTTPS via le tailnet
Tailscale (certificat Let's Encrypt émis pour le nom MagicDNS du VPS, pas
de tunnel SSH à ouvrir) :

```
https://annonces-vps.tail094416.ts.net:3000
```

Accessible uniquement depuis une machine membre du même tailnet. Détail de
la conception : `docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md`.

## Dashboard sur mesure

Une seconde interface, en lecture seule elle aussi, complète Metabase :
une petite application web (API FastAPI + frontend React) dédiée à trois
usages précis plutôt qu'à l'exploration libre — catalogue de cartes avec
recherche et filtres, historique de prix détaillé par carte, et valeur de
la collection personnelle. Même rôle Postgres en lecture seule
(`dashboard_reader`) que Metabase, même politique d'accès : jamais exposée
publiquement, accessible en HTTPS via le tailnet Tailscale :

```
https://annonces-vps.tail094416.ts.net:5173
```

Détail de la conception : `docs/superpowers/specs/2026-08-13-custom-dashboard-design.md`
et `docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md`.

## Lancer en local

```bash
cp .env.example .env               # remplir les valeurs (voir les commentaires du fichier)
python3 -m venv .venv && source .venv/bin/activate   # nécessite Python >= 3.11
pip install -e ".[dev]"

docker compose up -d db
./scripts/apply_migrations.sh

python -m scripts.run_extract_load  # extraction manuelle ponctuelle

docker compose up -d airflow-db airflow-init
docker compose up -d airflow-webserver airflow-scheduler
# UI Airflow : http://localhost:8080
```

## Tests

```bash
docker compose up -d db      # les tests d'intégration ont besoin de Postgres
pytest
ruff check .
black --check .
```

31 tests couvrant : extraction avec retry (`tests/test_extract.py`), reprise
par checkpoint (`tests/test_run_extract_load.py`), validation/nettoyage
(`tests/test_transform.py`), chargement idempotent à chaque étage
(`tests/test_raw_loader.py`, `test_staging_loader.py`,
`test_warehouse_loader.py`), et un test d'intégration bout-en-bout
(`tests/test_idempotence.py`). CI (`.github/workflows/ci.yml`) : reconstruit
un Postgres jetable et rejoue la suite complète à chaque push/PR sur `main`.

## Déploiement

Voir `infra/ovh_vps_setup.md` pour le provisioning du VPS (durcissement SSH,
firewall, Docker) et le déploiement de la stack de production
(`docker-compose.prod.yml`).

## Structure du repo

```
src/
  extract/    appel API pokemontcg.io (retry, backoff, checkpoint de pagination)
  transform/  validation et nettoyage (fonctions pures, testées sans DB)
  load/       chargement idempotent (raw, staging, quarantaine, entrepôt)
  common/     configuration et connexion base de données
dags/         DAG Airflow (extract >> clean >> load)
migrations/   SQL numéroté, jamais modifié après merge
scripts/      points d'entrée manuels (extraction ponctuelle, migrations)
tests/        suite pytest (31 tests, unitaires + intégration)
infra/        runbook de provisioning et déploiement du VPS
docs/superpowers/  specs et plans d'implémentation de chaque évolution majeure
```
