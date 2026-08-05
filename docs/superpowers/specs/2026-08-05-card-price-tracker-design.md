# Card Price Tracker — Design

**Date** : 2026-08-05
**Statut** : Validé par l'utilisateur, en attente de revue finale avant passage au plan d'implémentation.

## Contexte

Projet portfolio pour décrocher un poste de Data Engineer junior en Suisse romande d'ici environ 3 mois (cible : début novembre 2026). L'auteur est en reconversion (ex-restauration, étudiant à l'École 42), actuellement en stage Data/Digital (n8n, SQL, automatisation). Temps disponible : **3 à 5 heures par semaine** en dehors du stage. Priorité explicite : un projet fonctionnel et démontrable, traité avec la rigueur d'un vrai environnement professionnel — l'auteur n'a pas encore les automatismes du métier et compte sur ce projet (et sur les choix techniques qui le sous-tendent) pour les acquérir. Pas d'approximation : chaque décision doit refléter une bonne pratique réelle, expliquée, pas un raccourci de tutoriel.

Ce document remplace et affine le plan initial (`plan_projet_card_tracker.md`, à la racine du repo), qui reposait sur l'API CardMarket directe et un scope Pokémon + One Piece. Ce plan initial est conservé tel quel comme trace historique ; il contient des éléments aujourd'hui obsolètes (voir section Pivot).

## Objectif du projet

Construire un pipeline de données de bout en bout qui suit l'évolution des prix de cartes Pokémon, structuré en trois zones (raw / staging / production), orchestré par Airflow, et automatisé en continu — pour démontrer les compétences clés d'un Data Engineer junior dans un repo GitHub présentable.

## Pivot par rapport au plan initial

L'API CardMarket n'accepte actuellement plus de nouvelles demandes d'accès ("we are not accepting applications for API access at this time" — confirmé par capture d'écran du dashboard développeur CardMarket). Le web scraping direct a été écarté : il viole généralement les conditions d'utilisation, expose à un blocage d'IP imprévisible, et introduit une fragilité incompatible avec les principes de traçabilité et de non-insertion de données silencieusement fausses que ce projet doit démontrer.

**Décision** : la source de données devient l'API **pokemontcg.io** (gratuite, accès instantané via clé API, sans processus d'approbation). Chaque carte y expose un objet `cardmarket` avec des prix réels sourcés de CardMarket (`averageSellPrice`, `trendPrice`, `lowPrice`, etc.), mis à jour quotidiennement. L'intention originale (suivre les prix CardMarket) est donc préservée, via un intermédiaire fiable.

**Conséquence sur le scope** : le suivi des cartes One Piece est abandonné, faute de source de données aussi mature et fiable que pokemontcg.io pour ce jeu. Le projet se concentre sur Pokémon uniquement.

## Stack technique

| Composant | Choix | Où |
|---|---|---|
| Langage | Python | — |
| Source de données | API `pokemontcg.io` | — |
| Base de données | PostgreSQL (Docker) | Local (mois 1-2), puis VM Oracle Cloud (mois 3) |
| Orchestration | Apache Airflow (Docker) | Local (mois 2), puis VM Oracle Cloud (mois 3) |
| Hébergement de production | VM Oracle Cloud Free Tier (Always Free, ARM) | Provisionnée tôt (mois 1), utilisée en déploiement au mois 3 |
| CI | GitHub Actions (tests + lint) | Rôle limité à la CI, pas d'orchestration ni de déploiement continu pour l'instant |
| Modélisation | Schéma en étoile | `fact_price_history`, `dim_card`, `dim_date`, `dim_platform` |
| Versioning | Git / GitHub, migrations SQL numérotées | — |
| Visualisation (stretch, mois 3) | Metabase ou notebook Python | Optionnel, ne conditionne pas la réussite du projet |

**Exclu du scope** : dbt, BigQuery/Snowflake, certification AWS Cloud Practitioner, Kubernetes, cartes One Piece.

## Architecture des données

Trois zones strictement séparées, en tant que **schémas Postgres distincts** (`raw`, `staging`, `prod` — pas une simple convention de nommage de table) :

1. **`raw`** : copie brute de la réponse de l'API pokemontcg.io, aucune transformation, avec métadonnées `source` et `loaded_at`.
2. **`staging`** : données nettoyées et typées (prix en `numeric`, chaînes normalisées), avec validation avant insertion (types, valeurs manquantes, valeurs aberrantes) et **table de quarantaine** pour les lignes rejetées.
3. **`prod`** (Data Warehouse) : schéma en étoile —
   - Fait : `fact_price_history` (card_id, date_id, platform_id, price, quantity_available)
   - Dimensions : `dim_card`, `dim_date`, `dim_platform`

## Orchestration & infrastructure

- **Airflow** est l'unique ordonnanceur de production. Un DAG (`extract >> clean >> load`) tourne en continu, sans déclenchement manuel, une fois déployé sur la VM.
- **GitHub Actions** ne joue aucun rôle d'orchestration ou de déploiement pour l'instant : il exécute uniquement les tests et le lint à chaque push/PR. La question d'un déploiement continu (CD) vers la VM est explicitement reportée à une décision ultérieure, hors scope de ce spec.
- **Séquencement hybride pour la VM** (décision clé) : la VM Oracle Cloud est provisionnée **tôt** (fin mois 1), comme tâche isolée — création de l'instance, durcissement SSH (authentification par clé uniquement), configuration du firewall, installation de Docker/Docker Compose, vérification via un conteneur de test. Le **déploiement réel** du pipeline (Postgres + Airflow + DAG) n'a lieu qu'au **mois 3**, une fois le pipeline stabilisé en local.
  - Raison : découpler l'apprentissage de l'administration d'un serveur (compétence ponctuelle, mieux acquise tôt et isolément) de l'itération rapide sur la logique du pipeline (qui doit rester locale tant que la logique change encore, pour éviter la latence du débogage à distance et le risque d'exposer prématurément un serveur mal sécurisé).

## Standards professionnels non négociables

Ces règles s'appliquent dès le mois 1, à chaque script et chaque décision — pas de rattrapage en fin de projet :

- **Base de données** : schémas séparés `raw`/`staging`/`prod` ; utilisateur applicatif dédié à droits minimaux (jamais le superuser Postgres) pour toute écriture venant du pipeline ; contraintes explicites en base (clés primaires/étrangères, `NOT NULL`, `UNIQUE`).
- **Secrets & configuration** : aucun secret en dur dans le code ; `.env` local git-ignoré + `.env.example` committé ; GitHub Actions Secrets pour la CI ; variables d'environnement sur la VM pour la prod.
- **ETL** : idempotence par `UPSERT` (`ON CONFLICT DO UPDATE`), jamais par delete-and-reload aveugle ; transactions explicites (une exécution réussit ou échoue en entier) ; logging structuré (module `logging`, niveaux INFO/WARNING/ERROR), zéro `print()` en code de prod ; retries avec backoff sur les appels API externes, échec explicite après épuisement des tentatives.
- **Qualité & tests** : tests unitaires sur la logique de transformation/validation ; test d'intégration qui rejoue le pipeline deux fois et vérifie l'absence de doublons ; lint (`ruff`) et formattage (`black`) appliqués dès le premier script.
- **Git** : migrations SQL numérotées jamais modifiées après merge (une correction = une nouvelle migration) ; commits atomiques avec messages expliquant le pourquoi.

## Structure du repo

```
card-price-tracker/
├── README.md
├── docker-compose.yml              # Postgres + Airflow — dev local
├── docker-compose.prod.yml         # même stack — VM Oracle Cloud
├── .env.example
├── .gitignore
├── pyproject.toml
│
├── migrations/
│   ├── 001_create_raw_tables.sql
│   ├── 002_create_staging_tables.sql
│   └── 003_create_star_schema.sql
│
├── src/
│   ├── extract/
│   │   └── pokemontcg_client.py
│   ├── transform/
│   │   ├── clean.py
│   │   └── validate.py
│   ├── load/
│   │   ├── raw_loader.py
│   │   ├── staging_loader.py
│   │   └── warehouse_loader.py
│   └── common/
│       ├── db.py
│       └── config.py
│
├── dags/
│   └── card_price_pipeline_dag.py
│
├── tests/
│   ├── test_extract.py
│   ├── test_transform.py
│   └── test_idempotence.py
│
├── infra/
│   └── oracle_vm_setup.md
│
└── .github/
    └── workflows/
        └── ci.yml
```

## Plan en 3 mois

Budget : ~3-5h/semaine, soit ~13-20h/mois.

### Mois 1 (≈ 5 août → 5 septembre 2026) — Fondations + VM prête
| Semaine | Tâche | Rigueur intégrée |
|---|---|---|
| 1 | Setup env (venv, `docker-compose.yml` Postgres local), schémas vides `raw`/`staging`/`prod` (migration 001) | Schémas séparés dès le départ |
| 2 | Client Python `pokemontcg.io` (clé API, retry/backoff, logging structuré) | Secrets en `.env`, pas de `print()` |
| 3 | Script de chargement en table `raw` (UPSERT, `loaded_at`/`source`) | Idempotence vérifiée manuellement |
| 4 | Provisioning VM Oracle Cloud (instance Always Free ARM, SSH par clé, firewall, Docker + Compose, conteneur test) | VM durcie avant tout déploiement sensible |

**Livrable** : extraction → chargement fonctionnel en local, idempotent, secrets externalisés ; VM prête et testée (vide).

### Mois 2 (≈ 5 septembre → 5 octobre 2026) — Pipeline complet + Airflow local
| Semaine | Tâche | Rigueur intégrée |
|---|---|---|
| 1 | Modélisation schéma étoile + migrations numérotées | PK/FK/`NOT NULL` explicites |
| 2 | Nettoyage + validation (staging) + table de quarantaine | Transactions explicites, échec propre si source indisponible |
| 3 | Chargement staging → prod (UPSERT vers le star schema) | Utilisateur DB à droits minimaux |
| 4 | DAG Airflow local (`extract >> clean >> load`) + tests d'idempotence + CI GitHub Actions (tests + lint) | Rejeu 2x → zéro doublon, vérifié automatiquement |

**Livrable** : pipeline raw→staging→prod complet, orchestré par Airflow en local, idempotent, testé, CI verte.

### Mois 3 (≈ 5 octobre → 5 novembre 2026) — Déploiement + portfolio
| Semaine | Tâche | Rigueur intégrée |
|---|---|---|
| 1 | Déployer Postgres (`docker-compose.prod.yml`) sur la VM, rejouer les migrations | Même utilisateur à droits minimaux qu'en local |
| 2 | Déployer Airflow sur la VM, transférer le DAG, vérifier l'exécution automatique planifiée sur plusieurs jours | Logs consultables à distance, alerting minimal sur échec |
| 3 | README complet (schéma d'architecture, instructions), nettoyage repo, vérification clone frais → ça tourne | Reproductibilité comme critère de base |
| 4 | Stretch (si le temps le permet) : dashboard Metabase ou notebook | — |

**Livrable** : pipeline tournant automatiquement en continu sur la VM Oracle Cloud, repo GitHub présentable, prêt pour candidature.

## Gestion du risque (temps limité)

Avec ~65h au total sur 3 mois pour ce périmètre, la marge est réelle mais pas confortable, en particulier parce que provisionner une VM et déployer Airflow pour la première fois comporte toujours des inconnues. En cas de dérapage du calendrier, l'ordre de priorité est :

1. **Non négociable** : pipeline raw→staging→prod idempotent, testé, avec les standards professionnels listés plus haut — c'est ce qui démontre la compétence recherchée.
2. **Important mais sacrifiable en dernier recours** : déploiement automatisé sur la VM (peut légèrement déborder sur novembre si nécessaire).
3. **Stretch, coupé en premier si le temps manque** : dashboard Metabase/notebook.

## Hors scope de ce document

- Décision sur un éventuel déploiement continu (CD) via GitHub Actions vers la VM — à trancher plus tard, une fois la CI de base en place.
- Choix définitif entre Metabase et notebook pour le dashboard stretch du mois 3.
