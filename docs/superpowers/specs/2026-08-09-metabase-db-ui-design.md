# Card Price Tracker — UI d'exploration de la base de données (Metabase) — Design

**Statut :** approuvé par l'utilisateur (2026-08-09), en attente de plan d'implémentation.

## Contexte et problème

L'utilisateur veut pouvoir explorer visuellement les données chargées en production (actuellement accessible uniquement via `psql` en ligne de commande) avant de rédiger le README du Mois 3 (Task 3). Il souhaite que ce soit un vrai morceau du projet — documenté, montrable en entretien — pas juste un outil de confort personnel.

Le design initial du projet (`docs/superpowers/specs/2026-08-05-card-price-tracker-design.md`, Task 4 du Mois 3) anticipait déjà Metabase comme option pour un dashboard stretch optionnel, avec un détail déjà écrit dans `docs/superpowers/plans/2026-08-05-card-price-tracker-month3.md:365` : un rôle Postgres dédié `dashboard_reader`, lecture seule sur `prod`, jamais `pipeline_app`.

**Décision** : plutôt que deux outils séparés (un explorateur simple type Adminer maintenant, un outil de dashboard plus tard), un seul outil couvre les deux besoins. Metabase Open Source est gratuit en auto-hébergement (aucun palier payant nécessaire), a un éditeur SQL natif pour l'exploration ad-hoc, et le même moteur sert aussi à construire des graphiques/dashboards plus tard sans reconfiguration — la Task 4 du Mois 3 devient alors une simple extension de ce qui est déployé ici, pas un nouveau projet.

## Architecture

Nouveau service `metabase` ajouté **uniquement à `docker-compose.prod.yml`** (pas au `docker-compose.yml` local) : Metabase se configure via un assistant web propre à chaque environnement (voir section Configuration initiale), et le but explicite est d'explorer les données de **production** — dupliquer l'instance en local n'apporterait rien et doublerait la configuration manuelle.

- Image officielle épinglée à une version précise (pas `latest`), cohérent avec le reste du projet (Airflow 2.9.3, Postgres 16).
- Stockage interne : base H2 embarquée (défaut Metabase), sur un volume Docker nommé pour survivre à un `docker compose down`/`up` — décision utilisateur, suffisant pour un usage personnel/portfolio (pas une vraie prod multi-utilisateurs).
- Port 3000 lié en `127.0.0.1:3000:3000` sur le VPS — identique au pattern déjà en place pour Airflow (`127.0.0.1:8080:8080`) : jamais exposé sur une interface publique, accessible uniquement via tunnel SSH depuis la machine de l'utilisateur.
- Healthcheck sur `/api/health`, cohérent avec les healthchecks déjà en place sur `db`/`airflow-*` (Mois 3 Task 2).
- Connexion au réseau Docker par défaut du projet (même réseau que le service `db`), permettant à Metabase de joindre `db:5432` en interne — même mécanisme que les conteneurs Airflow.

**Ce que Metabase stocke vs ce qu'il interroge** (point clarifié explicitement avec l'utilisateur pendant le brainstorming) : Metabase ne duplique JAMAIS les données du pipeline. Sa base H2 interne ne contient que ses propres métadonnées applicatives (comptes utilisateurs, dashboards/requêtes sauvegardés, paramètres de connexion) — un volume de données sans rapport avec celui du pipeline. Toute exploration ou dashboard exécute une requête **en direct** contre Postgres au moment de la consultation.

## Sécurité / accès aux données

Nouveau rôle Postgres `dashboard_reader`, lecture seule, scopé au seul schéma `prod` (le star schema final — `raw`/`staging` restent des détails internes du pipeline, non exposés à Metabase).

Nouvelle migration `migrations/007_create_dashboard_reader_role.sql`, même pattern que la création de `pipeline_app` (migration 001) :
- Rôle créé sans mot de passe dans le SQL versionné (`CREATE ROLE dashboard_reader LOGIN`, bloc idempotent `DO $$ ... IF NOT EXISTS ...`).
- `GRANT USAGE ON SCHEMA prod TO dashboard_reader`.
- `GRANT SELECT ON ALL TABLES IN SCHEMA prod TO dashboard_reader`.
- `ALTER DEFAULT PRIVILEGES IN SCHEMA prod GRANT SELECT ON TABLES TO dashboard_reader` — pour que toute future table ajoutée à `prod` soit automatiquement lisible, sans nouvelle migration.

Le mot de passe est synchronisé séparément par `scripts/apply_migrations.sh` (nouvelle ligne `ALTER ROLE dashboard_reader WITH PASSWORD '${DASHBOARD_READER_PASSWORD}'`, ajoutée après la ligne équivalente pour `pipeline_app` déjà présente), depuis une nouvelle variable `DASHBOARD_READER_PASSWORD` dans `.env`/`.env.example` — jamais commité en clair, pattern identique à `POSTGRES_APP_PASSWORD`.

## Configuration initiale (manuelle, non scriptable)

Contrairement à Airflow (`airflow users create` en CLI), l'édition Open Source de Metabase configure son compte admin et sa première connexion à une base de données **via un assistant web** au premier lancement — pas d'équivalent CLI. Étape manuelle, à faire une seule fois après déploiement :

1. `docker compose -f docker-compose.prod.yml up -d metabase` sur le VPS.
2. Tunnel SSH : `ssh -L 3000:localhost:3000 card-tracker-vm`.
3. Ouvrir `http://localhost:3000`, créer le compte admin Metabase.
4. Ajouter une connexion "PostgreSQL" : host `db`, port `5432`, base de données `${POSTGRES_DB}`, utilisateur `dashboard_reader`, mot de passe `${DASHBOARD_READER_PASSWORD}` (valeur du `.env`).

## Vérification

Pas de tests pytest (aucun code Python n'est ajouté par ce plan). Vérification opérationnelle :
- Le conteneur `metabase` démarre et passe `healthy`.
- Le tunnel SSH atteint l'UI Metabase.
- Une requête de test dans l'éditeur SQL de Metabase contre `prod.fact_price_history` (via `dashboard_reader`) renvoie des données.
- **Vérification sécurité explicite** : un `INSERT`/`DELETE` tenté avec les identifiants `dashboard_reader` (via `psql`, pas via Metabase) est bien refusé par Postgres — confirme que le rôle est réellement lecture seule, pas supposé l'être.

## Hors scope (rappel)

- Construction effective de graphiques/dashboards dans Metabase (Task 4 du Mois 3, stretch) — ce plan déploie l'outil et la connexion, pas les dashboards eux-mêmes. Pourra être fait plus tard dans la même instance sans reconfiguration.
- Déploiement de Metabase en local (`docker-compose.yml`) — seulement en prod, voir Architecture.
- Toute automatisation de la configuration initiale de Metabase (compte admin, connexion DB) — l'édition Open Source ne l'permet pas via CLI/API sans authentification préalable, donc reste une étape manuelle documentée.
