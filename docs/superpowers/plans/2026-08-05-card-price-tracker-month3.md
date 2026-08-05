# Card Price Tracker — Mois 3 : Déploiement + automatisation + portfolio — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Déployer le pipeline (Postgres + Airflow) sur la VM Oracle Cloud provisionnée au Mois 1, vérifier qu'il tourne automatiquement chaque jour sans intervention manuelle, et livrer un repo GitHub présentable pour candidater.

**Architecture:** Même stack applicative que le Mois 2 (Postgres + Airflow en Docker Compose), déployée sur la VM plutôt qu'en local. Aucun port n'est exposé publiquement au-delà de SSH : Postgres n'est joignable que depuis le réseau Docker interne, et l'UI Airflow n'est liée qu'à `127.0.0.1` sur la VM (accès via tunnel SSH). Ce choix évite d'avoir à ouvrir de nouvelles règles de firewall ce mois-ci — la surface d'exposition reste celle durcie au Mois 1.

**Tech Stack:** Identique au Mois 2, plus `docker-compose.prod.yml` et un tunnel SSH pour l'accès à l'UI Airflow.

## Global Constraints

(Identiques aux Mois 1-2. Rappel spécifique à ce mois-ci :)
- Aucune nouvelle exposition réseau publique : la VM ne doit exposer que SSH (port 22) à la fin de ce mois, comme à la fin du Mois 1.
- Le même principe de moindre privilège s'applique en prod : `pipeline_app` garde les mêmes droits qu'en local, jamais le superuser.
- `restart: unless-stopped` sur tous les services de prod — le pipeline doit survivre à un redémarrage de la VM sans intervention manuelle.

**Pré-requis :** Mois 1 (VM prête) et Mois 2 (pipeline complet + Airflow local + CI) terminés. Le code doit être poussé sur GitHub (nécessaire pour cloner sur la VM).

---

## File Structure (ajouts par rapport aux Mois 1-2)

```
card-price-tracker/
├── docker-compose.prod.yml
├── scripts/
│   └── apply_migrations.sh          # modifié : accepte le fichier compose en argument
├── infra/
│   └── oracle_vm_setup.md           # complété avec les étapes de déploiement
└── README.md                        # réécrit, portfolio-ready
```

---

### Task 1: Déployer PostgreSQL sur la VM

**Files:**
- Create: `docker-compose.prod.yml`
- Modify: `scripts/apply_migrations.sh`

**Interfaces:**
- Consomme : `migrations/*.sql` (Mois 1-2), la VM provisionnée (Mois 1, Task 5).
- Produit : Postgres accessible depuis le réseau Docker interne de la VM, avec les mêmes schémas/utilisateur applicatif qu'en local.

- [ ] **Step 1: Modifier `scripts/apply_migrations.sh` pour accepter le fichier compose en argument**

Remplacer la première ligne utile du script (après `source .env`) :
```bash
source .env

COMPOSE_FILE="${1:-docker-compose.yml}"
ADMIN_PSQL="docker compose -f ${COMPOSE_FILE} exec -T db psql -v ON_ERROR_STOP=1 -U ${POSTGRES_ADMIN_USER} -d ${POSTGRES_DB}"
```
Et dans la boucle, remplacer `docker compose exec -T db psql ...` par `docker compose -f ${COMPOSE_FILE} exec -T db psql ...` (2 occurrences : dans la boucle et pour l'`ALTER ROLE` final).

- [ ] **Step 2: Vérifier que l'usage local (sans argument) fonctionne toujours**

Run: `./scripts/apply_migrations.sh`
Expected : comportement identique à avant (utilise `docker-compose.yml` par défaut), toutes les migrations déjà appliquées → uniquement des lignes "Skip".

- [ ] **Step 3: Créer `docker-compose.prod.yml`**

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: ${POSTGRES_ADMIN_USER}
      POSTGRES_PASSWORD: ${POSTGRES_ADMIN_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - card_tracker_pg_data:/var/lib/postgresql/data
      - ./migrations:/migrations:ro
    restart: unless-stopped

  airflow-db:
    image: postgres:16
    environment:
      POSTGRES_USER: airflow
      POSTGRES_PASSWORD: ${AIRFLOW_DB_PASSWORD}
      POSTGRES_DB: airflow
    volumes:
      - airflow_pg_data:/var/lib/postgresql/data
    restart: unless-stopped

  airflow-init:
    build:
      context: .
      dockerfile: Dockerfile.airflow
    depends_on:
      - airflow-db
    environment:
      AIRFLOW__CORE__EXECUTOR: LocalExecutor
      AIRFLOW__DATABASE__SQL_ALCHEMY_CONN: postgresql+psycopg2://airflow:${AIRFLOW_DB_PASSWORD}@airflow-db/airflow
      AIRFLOW__CORE__LOAD_EXAMPLES: "false"
      PYTHONPATH: /opt/airflow
    entrypoint: /bin/bash
    command: >
      -c "airflow db migrate &&
          airflow users create --username admin --password ${AIRFLOW_ADMIN_PASSWORD}
          --firstname Admin --lastname User --role Admin --email admin@example.com"
    volumes:
      - ./dags:/opt/airflow/dags
      - ./src:/opt/airflow/src
      - ./.env:/opt/airflow/.env:ro

  airflow-webserver:
    build:
      context: .
      dockerfile: Dockerfile.airflow
    depends_on:
      - airflow-init
    environment:
      AIRFLOW__CORE__EXECUTOR: LocalExecutor
      AIRFLOW__DATABASE__SQL_ALCHEMY_CONN: postgresql+psycopg2://airflow:${AIRFLOW_DB_PASSWORD}@airflow-db/airflow
      AIRFLOW__CORE__LOAD_EXAMPLES: "false"
      PYTHONPATH: /opt/airflow
    command: webserver
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - ./dags:/opt/airflow/dags
      - ./src:/opt/airflow/src
      - ./.env:/opt/airflow/.env:ro
    restart: unless-stopped

  airflow-scheduler:
    build:
      context: .
      dockerfile: Dockerfile.airflow
    depends_on:
      - airflow-init
    environment:
      AIRFLOW__CORE__EXECUTOR: LocalExecutor
      AIRFLOW__DATABASE__SQL_ALCHEMY_CONN: postgresql+psycopg2://airflow:${AIRFLOW_DB_PASSWORD}@airflow-db/airflow
      AIRFLOW__CORE__LOAD_EXAMPLES: "false"
      PYTHONPATH: /opt/airflow
    command: scheduler
    volumes:
      - ./dags:/opt/airflow/dags
      - ./src:/opt/airflow/src
      - ./.env:/opt/airflow/.env:ro
    restart: unless-stopped

volumes:
  card_tracker_pg_data:
  airflow_pg_data:
```

Note : contrairement à `docker-compose.yml` (dev local), `db` et `airflow-db` n'ont **aucun port publié** — ni Postgres ni les métadonnées Airflow ne sont joignables depuis l'extérieur de la VM, uniquement via le réseau Docker interne entre conteneurs. `airflow-webserver` n'est lié qu'à `127.0.0.1:8080`, donc invisible depuis l'extérieur de la VM elle-même — accès uniquement via tunnel SSH (Task 2).

- [ ] **Step 4: Cloner le repo sur la VM et préparer `.env`**

Sur la VM :
```bash
ssh -i /path/to/key.pem ubuntu@<IP_PUBLIQUE>
git clone https://github.com/<ton-user>/card-price-tracker.git
cd card-price-tracker
cp .env.example .env
# éditer .env avec vim/nano : mots de passe de prod (différents du local), clé API pokemontcg.io
```

- [ ] **Step 5: Démarrer Postgres et appliquer les migrations**

Sur la VM :
```bash
docker compose -f docker-compose.prod.yml up -d db
sleep 3
./scripts/apply_migrations.sh docker-compose.prod.yml
```
Expected : chaque migration s'affiche comme "Applique : ..." (aucune n'a encore été jouée sur cette base neuve), sans erreur.

- [ ] **Step 6: Vérifier depuis la VM**

Run (sur la VM) :
```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "\dt raw.*" -c "\dt staging.*" -c "\dt prod.*"
```
Expected : les 6 tables des trois schémas sont listées.

- [ ] **Step 7: Commit (en local, pas sur la VM)**

```bash
git add docker-compose.prod.yml scripts/apply_migrations.sh
git commit -m "feat: production docker-compose for Postgres, no ports exposed beyond Docker network"
git push
```
Puis sur la VM : `git pull` pour récupérer ce commit.

---

### Task 2: Déployer Airflow sur la VM et vérifier l'exécution automatique

**Files:**
- (Aucun nouveau fichier — utilise `docker-compose.prod.yml` de Task 1 et `dags/card_price_pipeline_dag.py` du Mois 2.)

**Interfaces:**
- Consomme : `dags/card_price_pipeline_dag.py` (Mois 2), `docker-compose.prod.yml` (Task 1).
- Produit : pipeline tournant automatiquement en continu sur la VM, sans trigger manuel après la vérification initiale.

- [ ] **Step 1: Démarrer les services Airflow sur la VM**

Sur la VM :
```bash
docker compose -f docker-compose.prod.yml up -d airflow-db airflow-init
docker compose -f docker-compose.prod.yml up -d airflow-webserver airflow-scheduler
```

- [ ] **Step 2: Ouvrir un tunnel SSH pour accéder à l'UI Airflow**

Depuis ta machine locale :
```bash
ssh -i /path/to/key.pem -L 8080:127.0.0.1:8080 ubuntu@<IP_PUBLIQUE>
```
Puis ouvrir `http://localhost:8080` dans le navigateur local — le trafic passe par le tunnel chiffré, aucun port n'est ouvert publiquement sur la VM.

- [ ] **Step 3: Vérifier le DAG et déclencher une première exécution manuelle**

Dans l'UI : vérifier que `card_price_pipeline` apparaît sans erreur d'import, le déclencher une fois manuellement, confirmer que les 3 tâches passent au vert.

- [ ] **Step 4: Vérifier l'exécution automatique sur plusieurs jours**

Sans aucune autre action manuelle, revenir sur `http://localhost:8080` (via un nouveau tunnel SSH) 2-3 jours plus tard.
Expected : de nouvelles exécutions du DAG apparaissent aux dates suivantes, déclenchées par le scheduler seul (`schedule="@daily"`), sans qu'aucun trigger manuel n'ait eu lieu. Vérifier aussi côté base :
```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "SELECT extracted_date, count(*) FROM raw.card_prices GROUP BY extracted_date ORDER BY extracted_date;"
```
Expected : une ligne par jour écoulé depuis le déploiement.

- [ ] **Step 5: Vérifier la résilience au redémarrage**

Run (sur la VM) :
```bash
sudo reboot
```
Attendre ~1 minute, se reconnecter en SSH, puis :
```bash
docker compose -f docker-compose.prod.yml ps
```
Expected : tous les services (`db`, `airflow-db`, `airflow-webserver`, `airflow-scheduler`) sont repartis automatiquement (`restart: unless-stopped`), sans intervention.

---

### Task 3: README portfolio-ready + nettoyage du repo

**Files:**
- Create/Rewrite: `README.md`
- Modify: `infra/oracle_vm_setup.md` (compléter avec les étapes de déploiement)

**Interfaces:**
- Consomme : rien (documentation).
- Produit : repo présentable, reproductible depuis un clone frais.

- [ ] **Step 1: Écrire `README.md`**

```markdown
# Card Price Tracker

Pipeline de données de bout en bout qui suit l'évolution quotidienne des prix
de cartes Pokémon (source CardMarket, via l'API pokemontcg.io), avec une
architecture raw → staging → production en schéma en étoile, orchestrée par
Airflow et tournant automatiquement chaque jour sur une VM Oracle Cloud.

## Architecture

```
pokemontcg.io API
      │
      ▼
 raw.card_prices        (copie brute, traçabilité loaded_at/source)
      │  clean_raw_to_staging()
      ▼
 staging.card_prices     (typé, nettoyé)
 staging.card_prices_quarantine  (lignes rejetées + raison)
      │  load_staging_to_warehouse()
      ▼
 prod.fact_price_history (schéma en étoile)
 prod.dim_card / dim_date / dim_platform
```

Orchestré par un DAG Airflow quotidien (`extract >> clean >> load`), chaque
tâche = une transaction Postgres. Idempotent : rejouer un jour déjà traité
met à jour les lignes de ce jour sans dupliquer ni affecter l'historique.

## Stack

Python 3.11, PostgreSQL 16, Apache Airflow 2.9 (LocalExecutor), Docker Compose,
GitHub Actions (tests + lint), déployé sur une VM Oracle Cloud Free Tier.

## Lancer en local

```bash
cp .env.example .env   # remplir les valeurs
docker compose up -d db
./scripts/apply_migrations.sh
pip install -e ".[dev]"
python -m scripts.run_extract_load   # extraction manuelle ponctuelle
docker compose up -d airflow-db airflow-init
docker compose up -d airflow-webserver airflow-scheduler
# UI Airflow : http://localhost:8080
```

## Tests

```bash
pytest        # nécessite Postgres démarré (docker compose up -d db)
ruff check .
black --check .
```

## Déploiement

Voir `infra/oracle_vm_setup.md` pour le provisioning de la VM et le
déploiement de la stack en production (`docker-compose.prod.yml`).
```

- [ ] **Step 2: Compléter `infra/oracle_vm_setup.md`**

Ajouter une section "Déploiement (Mois 3)" reprenant les commandes des Tasks 1-2 de ce plan (clone, `.env`, `docker compose -f docker-compose.prod.yml up -d`, tunnel SSH pour l'UI), pour que la procédure soit reproductible sans redevoir consulter ce plan d'implémentation.

- [ ] **Step 3: Vérifier la reproductibilité depuis un clone frais**

```bash
cd /tmp && git clone <url-du-repo> card-price-tracker-verif
cd card-price-tracker-verif
cp .env.example .env  # remplir avec des valeurs de test
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d db
./scripts/apply_migrations.sh
pytest
```
Expected : tout s'exécute sans erreur ni étape manquante non documentée.

- [ ] **Step 4: Commit**

```bash
git add README.md infra/oracle_vm_setup.md
git commit -m "docs: portfolio-ready README and completed deployment runbook"
git push
```

---

### Task 4 (Stretch — optionnel) : Dashboard

À n'entreprendre que si les Tasks 1-3 sont terminées avec de la marge dans le budget de 3-5h/semaine. Deux options, à trancher au moment venu (décision explicitement différée du spec, voir "Hors scope" du document de design) :

- **Notebook Python** : un notebook (`notebooks/price_trends.ipynb`) qui interroge `prod.fact_price_history` via `pandas.read_sql` et affiche 2-3 graphiques (évolution de prix par carte, top variations). Plus rapide à mettre en place, moins "démontrable" en un coup d'œil.
- **Metabase** : conteneur additionnel dans `docker-compose.prod.yml`, connecté à `prod.*` en lecture seule (créer un rôle `dashboard_reader` avec uniquement `SELECT` sur le schéma `prod` — jamais réutiliser `pipeline_app` pour ça). Plus impressionnant visuellement pour un portfolio, plus long à configurer.

Si le temps manque, cette tâche est explicitement la première à sacrifier (voir la section "Gestion du risque" du document de design) — le pipeline automatisé et le repo propre restent le livrable qui démontre la compétence recherchée.

---

## Self-Review Notes

- **Couverture du spec (Mois 3)** : déploiement Postgres ✓ (Task 1), déploiement Airflow + vérification automatique + résilience au reboot ✓ (Task 2), README + reproductibilité ✓ (Task 3), dashboard stretch ✓ (Task 4, volontairement moins détaillé car optionnel).
- **Cohérence avec les Mois 1-2** : `docker-compose.prod.yml` reprend exactement les services définis au Mois 2, sans changement de code applicatif — seule la configuration réseau (ports non publiés) diffère, cohérent avec le principe "même utilisateur/mêmes droits qu'en local" du spec.
- **Point de rigueur clé** : aucune nouvelle règle de firewall n'est nécessaire ce mois-ci — la décision (Task 1) de ne publier aucun port Postgres et de lier Airflow à `127.0.0.1` uniquement signifie que la surface d'exposition de la VM reste identique à la fin du Mois 1 (seul SSH). C'est un choix de conception, pas un oubli — à mentionner tel quel si un recruteur pose la question en entretien.
