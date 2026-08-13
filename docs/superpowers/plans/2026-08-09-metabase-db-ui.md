# Card Price Tracker — UI d'exploration de la base de données (Metabase) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Déployer Metabase (édition Open Source, gratuite en auto-hébergement) sur le VPS de production comme UI d'exploration de la base de données, accessible uniquement via tunnel SSH — sert aussi de fondation pour la Task 4 (dashboard, stretch) du Mois 3.

**Architecture:** Un nouveau rôle Postgres `dashboard_reader` (lecture seule, scopé au schéma `prod`) donne à Metabase un accès minimal aux données. Metabase lui-même est un nouveau service dans `docker-compose.prod.yml` uniquement, avec son propre stockage interne (H2, sur volume Docker persistant) totalement séparé des données du pipeline. La configuration initiale de Metabase (compte admin, connexion à la base) se fait via son assistant web au premier lancement — aucun équivalent CLI dans l'édition gratuite, donc cette étape reste manuelle et n'est pas encodée comme une tâche automatisable de ce plan.

**Tech Stack:** Docker Compose (existant), PostgreSQL 16 (existant), Metabase Open Source `v0.50.32` (nouveau).

## Global Constraints

- Aucune image `latest` : Metabase épinglé à une version précise (`v0.50.32`), cohérent avec Airflow (2.9.3) et Postgres (16) déjà épinglés dans ce projet.
- Port Metabase (3000) lié à `127.0.0.1:3000:3000` sur le VPS — jamais exposé sur une interface publique, accessible uniquement via tunnel SSH (même pattern que le port 8080 d'Airflow).
- `dashboard_reader` : lecture seule (`SELECT` uniquement), scopé au seul schéma `prod` — jamais `raw`/`staging`, jamais de droits d'écriture.
- Mots de passe jamais commités : `DASHBOARD_READER_PASSWORD` suit exactement le pattern déjà en place pour `POSTGRES_APP_PASSWORD` (rôle créé sans mot de passe dans la migration SQL, synchronisé séparément par `scripts/apply_migrations.sh` depuis `.env`).
- Aucun ajout à `docker-compose.yml` (local) — Metabase vit uniquement dans `docker-compose.prod.yml`, décision actée dans le spec.
- Configuration initiale de Metabase (compte admin + connexion DB) : manuelle, non scriptable dans l'édition gratuite — ne pas essayer de l'automatiser.

**Référence :** `docs/superpowers/specs/2026-08-09-metabase-db-ui-design.md` (spec approuvée).

---

### Task 1 : Rôle Postgres `dashboard_reader` (lecture seule, schéma `prod`)

**Files:**
- Create: `migrations/007_create_dashboard_reader_role.sql`
- Modify: `scripts/apply_migrations.sh`
- Modify: `.env.example`
- Modify: `.env` (fichier local non versionné — ajouter une valeur réelle pour tester)

**Interfaces:**
- Consomme : `prod.dim_card`, `prod.dim_date`, `prod.dim_platform`, `prod.fact_price_history` (schéma déjà créé, migration 003).
- Produit : un rôle Postgres `dashboard_reader` avec `SELECT` sur toutes les tables actuelles et futures de `prod`, sans droit d'écriture. Consommé par Task 3 (connexion Metabase).

- [ ] **Step 1 : Créer `migrations/007_create_dashboard_reader_role.sql`**

```sql
-- Migration 007 : crée le rôle Postgres dashboard_reader, en lecture seule,
-- scopé au schéma prod -- destiné à être utilisé par un outil d'exploration
-- de données externe (Metabase), jamais par le pipeline lui-même (qui garde
-- pipeline_app). Voir docs/superpowers/specs/2026-08-09-metabase-db-ui-design.md.
--
-- Pourquoi un rôle séparé plutôt que de réutiliser pipeline_app : least
-- privilege -- un outil de visualisation externe n'a besoin QUE de lire les
-- données finales (prod), jamais d'écrire, et surtout jamais d'accéder à
-- raw/staging (détails internes du pipeline, pas destinés à une consultation
-- externe). Réutiliser pipeline_app donnerait à Metabase des droits
-- d'écriture dont il n'a aucun besoin -- un risque inutile si les
-- identifiants Metabase fuitaient un jour.

BEGIN;

-- Bloc DO $$ ... END $$ idempotent : même pattern que la création de
-- pipeline_app (migration 001) -- CREATE ROLE n'a pas de IF NOT EXISTS
-- natif, ce bloc procédural PL/pgSQL vérifie l'existence avant de créer.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader') THEN
        CREATE ROLE dashboard_reader LOGIN;
    END IF;
END
$$;

-- GRANT USAGE : nécessaire pour accéder au schéma prod, même avec des droits
-- sur les tables (sans USAGE, l'accès au schéma est refusé).
GRANT USAGE ON SCHEMA prod TO dashboard_reader;

-- SELECT sur toutes les tables ACTUELLES de prod.
GRANT SELECT ON ALL TABLES IN SCHEMA prod TO dashboard_reader;

-- ALTER DEFAULT PRIVILEGES : s'applique aux tables créées À L'AVENIR dans
-- prod PAR LE RÔLE QUI EXÉCUTE CETTE COMMANDE (ici POSTGRES_ADMIN_USER, voir
-- scripts/apply_migrations.sh -- toutes les migrations sont appliquées par ce
-- rôle admin, y compris les CREATE TABLE de prod) -- tant que les futures
-- migrations continuent d'être appliquées par ce même rôle admin,
-- dashboard_reader lira automatiquement toute nouvelle table de prod, sans
-- migration supplémentaire.
ALTER DEFAULT PRIVILEGES IN SCHEMA prod GRANT SELECT ON TABLES TO dashboard_reader;

COMMIT;
```

- [ ] **Step 2 : Ajouter la synchronisation du mot de passe dans `scripts/apply_migrations.sh`**

À la toute fin du fichier, après la ligne existante `echo "Mot de passe de pipeline_app synchronisé avec .env"` :
```bash
$ADMIN_PSQL -c "ALTER ROLE dashboard_reader WITH PASSWORD '${DASHBOARD_READER_PASSWORD}';"
echo "Mot de passe de dashboard_reader synchronisé avec .env"
```

- [ ] **Step 3 : Ajouter la variable dans `.env.example`**

Après le bloc `# API pokemontcg.io ...` existant, ajouter :
```
# Metabase (UI d'exploration de la base de données, Mois 3) : rôle Postgres
# dédié en lecture seule sur le schéma prod, jamais pipeline_app.
DASHBOARD_READER_PASSWORD=changeme_dashboard_reader
```

- [ ] **Step 4 : Ajouter une vraie valeur dans `.env` local (non versionné)**

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"
```
Copier la valeur générée, l'ajouter dans `.env` (pas `.env.example`) :
```
DASHBOARD_READER_PASSWORD=<valeur générée>
```

- [ ] **Step 5 : Appliquer la migration en local**

```bash
./scripts/apply_migrations.sh
```
Expected : `Applique : 007_create_dashboard_reader_role.sql` puis `Mot de passe de dashboard_reader synchronisé avec .env`, sans erreur.

- [ ] **Step 6 : Vérifier que le rôle peut lire `prod`**

```bash
docker compose exec db psql -U dashboard_reader -d "$POSTGRES_DB" -c "SELECT count(*) FROM prod.fact_price_history;"
```
Expected : un nombre renvoyé (le compte de lignes existantes), sans erreur de permission.

- [ ] **Step 7 : Vérifier que le rôle NE PEUT PAS écrire (sécurité)**

```bash
docker compose exec db psql -U dashboard_reader -d "$POSTGRES_DB" -c "DELETE FROM prod.fact_price_history WHERE 1=0;"
```
Expected : `ERROR: permission denied for table fact_price_history` (le `WHERE 1=0` est une double sécurité : même si la permission passait par erreur, aucune ligne ne serait supprimée).

```bash
docker compose exec db psql -U dashboard_reader -d "$POSTGRES_DB" -c "INSERT INTO prod.dim_platform (platform_name, currency) VALUES ('test_should_fail', 'USD');"
```
Expected : `ERROR: permission denied for table dim_platform`.

- [ ] **Step 8 : Commit**

```bash
git add migrations/007_create_dashboard_reader_role.sql scripts/apply_migrations.sh .env.example
git commit -m "feat: add read-only dashboard_reader Postgres role for Metabase"
```
(`.env` local n'est jamais commité — vérifier avec `git status` qu'il n'apparaît pas dans les fichiers stagés.)

---

### Task 2 : Service Metabase dans `docker-compose.prod.yml`

**Files:**
- Modify: `docker-compose.prod.yml`

**Interfaces:**
- Consomme : rien du pipeline directement (Metabase se connecte à `db` seulement après la configuration manuelle du Task 3/étape finale, pas via docker-compose).
- Produit : un service `metabase` démarrable indépendamment (`docker compose -f docker-compose.prod.yml up -d metabase`), consommé par Task 3 (déploiement prod).

- [ ] **Step 1 : Ajouter le service `metabase`**

Dans `docker-compose.prod.yml`, ajouter ce bloc après le service `airflow-scheduler` (avant la section `volumes:` finale) :
```yaml
  # metabase : UI d'exploration de la base de données (Mois 3, voir
  # docs/superpowers/specs/2026-08-09-metabase-db-ui-design.md). Stockage
  # interne H2 (métadonnées Metabase -- comptes, dashboards sauvegardés --
  # PAS une copie des données du pipeline) sur un volume nommé persistant.
  # Aucun depends_on : Metabase démarre et devient "healthy" de façon
  # autonome (serveur Jetty + assistant de configuration), la connexion à
  # "db" se fait plus tard, via son assistant web, pas au démarrage du
  # conteneur.
  metabase:
    image: metabase/metabase:v0.50.32
    environment:
      # MB_DB_FILE : sans cette variable, Metabase stocke son fichier H2
      # dans le système de fichiers éphémère du conteneur (perdu à chaque
      # recréation). En la pointant vers un chemin sous le volume monté
      # ci-dessous, les comptes/dashboards Metabase survivent à un
      # `docker compose down`/`up` -- piège documenté de Metabase en Docker,
      # vérifié explicitement pour ne pas le reproduire ici.
      MB_DB_FILE: /metabase-data/metabase.db
    volumes:
      - metabase_data:/metabase-data
    # 127.0.0.1 uniquement : jamais exposé sur l'interface publique du VPS,
    # identique au pattern déjà en place pour airflow-webserver (port 8080).
    # Accès uniquement via tunnel SSH (voir Task 3 pour la commande exacte).
    ports:
      - "127.0.0.1:3000:3000"
    healthcheck:
      # curl confirmé présent dans l'image officielle metabase/metabase
      # (Alpine, vérifié directement avant d'écrire ce plan). /api/health
      # répond dès que le serveur Jetty interne est prêt -- PAS besoin
      # qu'une connexion à une base externe soit configurée au préalable
      # (ça, c'est l'étape manuelle du Task 3).
      test: ["CMD", "curl", "--fail", "http://localhost:3000/api/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      # start_period plus long que les autres services (60s pour Airflow) :
      # Metabase est une application JVM, son démarrage à froid est
      # nettement plus lent qu'un serveur Python/Airflow.
      start_period: 120s
    restart: unless-stopped
```

- [ ] **Step 2 : Ajouter le volume nommé**

Dans la section `volumes:` en bas du fichier, ajouter une ligne :
```yaml
volumes:
  card_tracker_pg_data:
  airflow_pg_data:
  metabase_data:
```

- [ ] **Step 3 : Valider la syntaxe du fichier compose**

```bash
docker compose -f docker-compose.prod.yml config --quiet
```
Expected : aucune sortie, code de sortie 0 (fichier syntaxiquement valide).

- [ ] **Step 4 : Test local isolé du service (sans toucher au reste de la stack locale)**

```bash
docker compose -f docker-compose.prod.yml up -d metabase
```
Expected : `Container ... Started`, sans dépendance créée (pas de `db`/`airflow-*` lancés — `metabase` n'a pas de `depends_on`).

- [ ] **Step 5 : Attendre et vérifier l'état `healthy`**

```bash
sleep 30 && docker compose -f docker-compose.prod.yml ps metabase
```
Expected : colonne `STATUS` affichant `Up ... (healthy)`. Si `(health: starting)` persiste après plusieurs vérifications, attendre encore (JVM lente à froid) avant de conclure à un problème.

- [ ] **Step 6 : Nettoyer le test local**

```bash
docker compose -f docker-compose.prod.yml down
```
Expected : conteneur et réseau supprimés. Le volume `metabase_data` persiste (comportement normal de `down` sans `-v` — pas grave ici, il sera de toute façon vide, aucune config n'a été faite dessus).

- [ ] **Step 7 : Commit**

```bash
git add docker-compose.prod.yml
git commit -m "feat: add Metabase service to docker-compose.prod.yml"
```

---

### Task 3 : Déployer sur le VPS et vérifier l'accès via tunnel SSH

**Files:**
- (Aucun nouveau fichier — déploiement des Tasks 1-2 sur le VPS déjà en production.)

**Interfaces:**
- Consomme : le rôle `dashboard_reader` (Task 1), le service `metabase` (Task 2).
- Produit : Metabase accessible depuis la machine de l'utilisateur via tunnel SSH, prêt pour la configuration manuelle finale (hors scope de ce plan, voir Hors Scope du spec).

- [ ] **Step 1 : Pousser les commits des Tasks 1-2**

```bash
git push
```

- [ ] **Step 2 : Récupérer le code sur la VM**

```bash
ssh card-tracker-vm
cd ~/card-price-tracker
git pull
```

- [ ] **Step 3 : Ajouter `DASHBOARD_READER_PASSWORD` au `.env` du VM**

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"
```
Ajouter la ligne suivante au `.env` du VM (via `nano .env` ou équivalent — PAS via un heredoc qui écraserait le fichier existant) :
```
DASHBOARD_READER_PASSWORD=<valeur générée>
```

- [ ] **Step 4 : Appliquer la migration 007 en prod**

```bash
./scripts/apply_migrations.sh docker-compose.prod.yml
```
Expected : `Applique : 007_create_dashboard_reader_role.sql` puis `Mot de passe de dashboard_reader synchronisé avec .env`, sans erreur.

- [ ] **Step 5 : Vérifier le rôle en prod (mêmes contrôles qu'en local, Task 1 Steps 6-7)**

```bash
docker compose -f docker-compose.prod.yml exec db psql -U dashboard_reader -d "$POSTGRES_DB" -c "SELECT count(*) FROM prod.fact_price_history;"
docker compose -f docker-compose.prod.yml exec db psql -U dashboard_reader -d "$POSTGRES_DB" -c "DELETE FROM prod.fact_price_history WHERE 1=0;"
```
Expected : la première commande renvoie un nombre, la seconde échoue avec `ERROR: permission denied`.

- [ ] **Step 6 : Démarrer Metabase en prod**

```bash
docker compose -f docker-compose.prod.yml up -d metabase
```
Expected : `Container ... Started`.

- [ ] **Step 7 : Attendre et vérifier l'état `healthy` en prod**

```bash
sleep 30 && docker compose -f docker-compose.prod.yml ps metabase
```
Expected : `Up ... (healthy)`. Revérifier après un délai supplémentaire si encore `starting`.

- [ ] **Step 8 : Vérifier l'accès de bout en bout via un vrai tunnel SSH**

Depuis la machine locale (PAS sur la VM) :
```bash
ssh -f -N -L 3000:localhost:3000 card-tracker-vm
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:3000/api/health
```
Expected : `HTTP 200`. Puis fermer le tunnel :
```bash
pkill -f "3000:localhost:3000"
```

---

## Self-Review Notes

- **Couverture du spec** : rôle `dashboard_reader` lecture seule sur `prod` ✓ (Task 1), service Metabase avec stockage H2 persistant + port `127.0.0.1` uniquement ✓ (Task 2), déploiement + vérification tunnel SSH ✓ (Task 3). Configuration manuelle (compte admin + connexion DB) explicitement non incluse dans les tâches, conformément au spec (section Hors Scope) — sera fournie directement par le contrôleur à l'utilisateur après ce plan, avec les valeurs exactes à saisir (host `db`, port `5432`, base `${POSTGRES_DB}`, utilisateur `dashboard_reader`, mot de passe = valeur du `.env` du VM).
- **Cohérence des types/valeurs** : `DASHBOARD_READER_PASSWORD` référencé de façon identique dans `.env.example` (Task 1), `.env` local (Task 1) et `.env` du VM (Task 3) — même nom de variable partout, aucune divergence.
- **Ordre des tasks** : Task 3 dépend de Task 1 (le rôle doit exister avant que Metabase puisse s'y connecter, même si cette connexion elle-même reste hors scope de ce plan) et de Task 2 (le service doit être défini avant de pouvoir être démarré). Task 1 et Task 2 sont indépendantes entre elles.
- **Sécurité vérifiée à deux niveaux** : le rôle `dashboard_reader` est testé en écriture-refusée en LOCAL (Task 1) ET en PROD (Task 3) — pas seulement supposé identique entre les deux environnements.
