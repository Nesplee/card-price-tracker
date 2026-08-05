# Card Price Tracker — Mois 1 : Fondations Python/SQL + VM prête — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produire un script Python qui extrait les cartes Pokémon (avec leurs prix CardMarket) depuis l'API pokemontcg.io et les charge de façon idempotente dans une base PostgreSQL locale (Docker), avec secrets externalisés, logging structuré et tests — plus une VM Oracle Cloud provisionnée et durcie, prête pour le déploiement du mois 3.

**Architecture:** Trois modules Python isolés (`extract`, `load`, `common`) reliés par un script d'orchestration fin. La base Postgres locale expose trois schémas (`raw`/`staging`/`prod`) dès la première migration, même si seul `raw` est peuplé ce mois-ci. Un utilisateur applicatif à droits minimaux (`pipeline_app`) est créé dès le départ et utilisé pour toutes les écritures du pipeline.

**Tech Stack:** Python 3.11+, `psycopg` 3 (driver Postgres), `requests` + `tenacity` (appels API avec retry/backoff), `python-dotenv` (config), `pytest` + `responses` (tests), `ruff` + `black` (lint/format), Docker Compose (Postgres local), Oracle Cloud Free Tier (VM ARM Always Free).

## Global Constraints

- Schémas Postgres séparés `raw`/`staging`/`prod` — de vrais `CREATE SCHEMA`, pas une convention de nommage de table.
- Utilisateur applicatif dédié à droits minimaux (`pipeline_app`) pour toute écriture du pipeline — jamais le superuser Postgres.
- Contraintes explicites en base : clés primaires, `NOT NULL`, contraintes `UNIQUE` pour l'idempotence.
- Aucun secret en dur dans le code ou dans les fichiers versionnés (y compris les migrations SQL) : `.env` local git-ignoré + `.env.example` committé.
- Idempotence par `UPSERT` (`ON CONFLICT DO UPDATE`), jamais par delete-and-reload aveugle.
- Transactions explicites : une exécution réussit ou échoue en entier.
- Logging structuré (module `logging`, niveaux INFO/WARNING/ERROR) — zéro `print()` en code de prod.
- Retries avec backoff sur les appels API externes, échec explicite (exception dédiée) après épuisement des tentatives.
- Tests unitaires sur la logique métier + tests d'intégration contre la vraie base Postgres locale pour la persistance.
- Lint (`ruff`) et formatage (`black`) appliqués dès le premier script, vérifiés avant chaque commit.
- Migrations SQL numérotées, jamais modifiées après merge (une correction = une nouvelle migration).

**Note de scope :** ce plan couvre uniquement le Mois 1. Les Mois 2 (schéma en étoile, Airflow local) et Mois 3 (déploiement, automatisation) feront l'objet de plans séparés une fois ce mois-ci terminé, car leurs détails dépendront de ce qui aura été appris ici.

---

## File Structure

```
card-price-tracker/
├── pyproject.toml
├── .env.example
├── .gitignore
├── docker-compose.yml
├── migrations/
│   └── 001_create_schemas_and_raw.sql
├── scripts/
│   ├── __init__.py
│   ├── apply_migrations.sh
│   └── run_extract_load.py
├── src/
│   ├── __init__.py
│   ├── common/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── db.py
│   ├── extract/
│   │   ├── __init__.py
│   │   └── pokemontcg_client.py
│   └── load/
│       ├── __init__.py
│       └── raw_loader.py
├── tests/
│   ├── test_db_setup.py
│   ├── test_extract.py
│   └── test_raw_loader.py
└── infra/
    └── oracle_vm_setup.md
```

- `src/common/` : configuration et connexion DB, partagées par tout le reste.
- `src/extract/` : un seul responsable, parler à l'API pokemontcg.io.
- `src/load/` : un seul responsable, écrire dans `raw`.
- `scripts/` : points d'entrée exécutables qui composent les modules ci-dessus ; ne contiennent pas de logique métier propre.
- `infra/` : documentation d'infrastructure (runbook), pas de code applicatif.

---

### Task 1: Scaffolding du projet et outillage (lint/format/tests)

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `src/__init__.py`, `src/common/__init__.py`, `src/extract/__init__.py`, `src/load/__init__.py`
- Create: `scripts/__init__.py`

**Interfaces:**
- Consomme : rien (première tâche).
- Produit : environnement de dev fonctionnel (venv + dépendances installées), config `ruff`/`black`/`pytest` prête à être utilisée par toutes les tâches suivantes.

- [ ] **Step 1: Créer l'arborescence de packages Python**

```bash
mkdir -p src/common src/extract src/load scripts tests migrations infra
touch src/__init__.py src/common/__init__.py src/extract/__init__.py src/load/__init__.py scripts/__init__.py
```

- [ ] **Step 2: Créer `pyproject.toml`**

```toml
[project]
name = "card-price-tracker"
version = "0.1.0"
description = "Pipeline de suivi des prix de cartes Pokemon (raw -> staging -> prod)"
requires-python = ">=3.11"
dependencies = [
    "requests>=2.31",
    "psycopg[binary]>=3.1",
    "python-dotenv>=1.0",
    "tenacity>=8.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "responses>=0.25",
    "ruff>=0.4",
    "black>=24.0",
]

[tool.pytest.ini_options]
pythonpath = ["."]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP"]

[tool.black]
line-length = 100
target-version = ["py311"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 3: Créer `.env.example`**

```
# Copier ce fichier vers .env et remplir les valeurs réelles. .env ne doit JAMAIS être commité.

# Base de données Postgres (locale, via docker-compose)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=card_tracker
POSTGRES_APP_USER=pipeline_app
POSTGRES_APP_PASSWORD=changeme
POSTGRES_ADMIN_USER=postgres
POSTGRES_ADMIN_PASSWORD=changeme_admin

# API pokemontcg.io (clé gratuite : https://dev.pokemontcg.io)
POKEMONTCG_API_KEY=changeme
```

- [ ] **Step 4: Créer `.gitignore`**

```
.env
__pycache__/
*.pyc
.venv/
.pytest_cache/
*.egg-info/
```

- [ ] **Step 5: Créer le venv et installer les dépendances**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

- [ ] **Step 6: Vérifier que l'outillage tourne**

Run: `ruff check . && black --check . && pytest`
Expected: `ruff` et `black` ne signalent rien (aucun fichier Python encore écrit), `pytest` affiche "no tests ran".

- [ ] **Step 7: Commit**

```bash
cp .env.example .env  # puis éditer .env localement (jamais commité)
git add pyproject.toml .env.example .gitignore src scripts
git commit -m "chore: scaffold project structure and tooling (ruff/black/pytest)"
```

---

### Task 2: Postgres local, schémas raw/staging/prod, utilisateur applicatif

**Files:**
- Create: `docker-compose.yml`
- Create: `migrations/001_create_schemas_and_raw.sql`
- Create: `scripts/apply_migrations.sh`
- Create: `src/common/config.py`
- Create: `src/common/db.py`
- Test: `tests/test_db_setup.py`

**Interfaces:**
- Consomme : variables d'environnement du `.env` (Task 1).
- Produit : `DatabaseConfig` (dataclass), `load_db_config() -> DatabaseConfig`, `get_connection(config: DatabaseConfig | None = None) -> ContextManager[psycopg.Connection]` dans `src/common/db.py` et `src/common/config.py` — utilisés par toutes les tâches suivantes qui touchent la DB.

- [ ] **Step 1: Écrire le test qui doit échouer**

`tests/test_db_setup.py` :
```python
from src.common.config import load_db_config
from src.common.db import get_connection


def test_raw_schema_and_table_exist() -> None:
    with get_connection(load_db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'raw' AND table_name = 'card_prices'
                );
                """
            )
            (exists,) = cur.fetchone()
    assert exists is True
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `pytest tests/test_db_setup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.common.config'` (rien n'existe encore).

- [ ] **Step 3: Créer `src/common/config.py`**

```python
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    @property
    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Variable d'environnement manquante : {name}")
    return value


def load_db_config() -> DatabaseConfig:
    return DatabaseConfig(
        host=_require_env("POSTGRES_HOST"),
        port=int(_require_env("POSTGRES_PORT")),
        dbname=_require_env("POSTGRES_DB"),
        user=_require_env("POSTGRES_APP_USER"),
        password=_require_env("POSTGRES_APP_PASSWORD"),
    )
```

- [ ] **Step 4: Créer `src/common/db.py`**

```python
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection

from src.common.config import DatabaseConfig, load_db_config


@contextmanager
def get_connection(config: DatabaseConfig | None = None) -> Iterator[Connection]:
    """Ouvre une connexion Postgres. COMMIT si le bloc réussit, ROLLBACK sinon —
    garantit qu'une exécution du pipeline n'écrit jamais un état partiel."""
    cfg = config or load_db_config()
    conn = psycopg.connect(cfg.dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

- [ ] **Step 5: Créer `docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: ${POSTGRES_ADMIN_USER}
      POSTGRES_PASSWORD: ${POSTGRES_ADMIN_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports:
      - "${POSTGRES_PORT}:5432"
    volumes:
      - card_tracker_pg_data:/var/lib/postgresql/data
      - ./migrations:/migrations:ro

volumes:
  card_tracker_pg_data:
```

- [ ] **Step 6: Créer `migrations/001_create_schemas_and_raw.sql`**

```sql
-- Crée les trois zones du pipeline comme schémas Postgres séparés.
-- staging et prod restent vides jusqu'au mois 2 ; seul raw est peuplé ce mois-ci.

BEGIN;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS prod;

-- Clé (card_id, extracted_date, source) plutôt que card_id seul : rejouer
-- l'extraction du même jour met à jour la ligne du jour (idempotence) sans
-- écraser l'historique des jours précédents, indispensable pour suivre
-- l'évolution des prix dans le temps.
CREATE TABLE IF NOT EXISTS raw.card_prices (
    id              bigserial PRIMARY KEY,
    card_id         text NOT NULL,
    extracted_date  date NOT NULL,
    source          text NOT NULL,
    payload         jsonb NOT NULL,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_card_prices_card_date_source UNIQUE (card_id, extracted_date, source)
);

-- Utilisateur applicatif à droits minimaux : jamais le superuser pour les
-- écritures du pipeline. Le mot de passe est fixé séparément (voir
-- scripts/apply_migrations.sh) pour ne jamais committer de secret ici.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pipeline_app') THEN
        CREATE ROLE pipeline_app LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA raw TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON raw.card_prices TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE raw.card_prices_id_seq TO pipeline_app;

COMMIT;
```

- [ ] **Step 7: Créer `scripts/apply_migrations.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Applique migrations/*.sql non encore appliquées, dans l'ordre, chacune dans
# sa propre transaction. Garde une trace dans public.schema_migrations pour ne
# jamais rejouer un fichier déjà appliqué. Le mot de passe applicatif est fixé
# ici (hors fichier versionné) pour respecter la règle "aucun secret en dur".

source .env

ADMIN_PSQL="docker compose exec -T db psql -v ON_ERROR_STOP=1 -U ${POSTGRES_ADMIN_USER} -d ${POSTGRES_DB}"

$ADMIN_PSQL -c "CREATE TABLE IF NOT EXISTS public.schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());"

for filepath in migrations/*.sql; do
    filename=$(basename "$filepath")
    already_applied=$($ADMIN_PSQL -tAc "SELECT 1 FROM public.schema_migrations WHERE filename = '${filename}';")
    if [ "$already_applied" = "1" ]; then
        echo "Skip (déjà appliquée) : ${filename}"
        continue
    fi
    echo "Applique : ${filename}"
    docker compose exec -T db psql -v ON_ERROR_STOP=1 -U "${POSTGRES_ADMIN_USER}" -d "${POSTGRES_DB}" -f "/migrations/${filename}"
    $ADMIN_PSQL -c "INSERT INTO public.schema_migrations (filename) VALUES ('${filename}');"
done

$ADMIN_PSQL -c "ALTER ROLE pipeline_app WITH PASSWORD '${POSTGRES_APP_PASSWORD}';"
echo "Mot de passe de pipeline_app synchronisé avec .env"
```

```bash
chmod +x scripts/apply_migrations.sh
```

- [ ] **Step 8: Démarrer Postgres et appliquer les migrations**

Run:
```bash
docker compose up -d db
sleep 3
./scripts/apply_migrations.sh
```
Expected: le script affiche "Applique : 001_create_schemas_and_raw.sql" puis "Mot de passe de pipeline_app synchronisé avec .env", sans erreur.

- [ ] **Step 9: Relancer le test pour vérifier qu'il passe**

Run: `pytest tests/test_db_setup.py -v`
Expected: PASS

- [ ] **Step 10: Lint, format, commit**

```bash
ruff check . && black .
git add docker-compose.yml migrations scripts/apply_migrations.sh src/common tests/test_db_setup.py
git commit -m "feat: local Postgres with raw/staging/prod schemas and least-privilege app user"
```

---

### Task 3: Client API pokemontcg.io (retry/backoff, logging)

**Files:**
- Modify: `src/common/config.py` (ajout de `PokemonTcgConfig`)
- Create: `src/extract/pokemontcg_client.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consomme : rien de nouveau (config pattern de Task 2).
- Produit : `PokemonTcgConfig` (dataclass : `api_key`, `base_url="https://api.pokemontcg.io/v2"`), `load_pokemontcg_config() -> PokemonTcgConfig`, `PokemonTcgApiError` (exception), `PokemonTcgClient(config, timeout_seconds=10.0, max_attempts=4, wait_min_seconds=1.0, wait_max_seconds=10.0)` avec méthode `fetch_cards_page(page: int, page_size: int = 250) -> list[dict]` — consommé par Task 4.

- [ ] **Step 1: Ajouter `PokemonTcgConfig` à `src/common/config.py`**

Ajouter à la fin du fichier :
```python
@dataclass(frozen=True)
class PokemonTcgConfig:
    api_key: str
    base_url: str = "https://api.pokemontcg.io/v2"


def load_pokemontcg_config() -> PokemonTcgConfig:
    return PokemonTcgConfig(api_key=_require_env("POKEMONTCG_API_KEY"))
```

- [ ] **Step 2: Écrire les tests qui doivent échouer**

`tests/test_extract.py` :
```python
import pytest
import responses

from src.common.config import PokemonTcgConfig
from src.extract.pokemontcg_client import PokemonTcgApiError, PokemonTcgClient


@pytest.fixture
def client() -> PokemonTcgClient:
    config = PokemonTcgConfig(api_key="test-key")
    return PokemonTcgClient(config, wait_min_seconds=0, wait_max_seconds=0)


@responses.activate
def test_fetch_cards_page_returns_data(client: PokemonTcgClient) -> None:
    responses.add(
        responses.GET,
        "https://api.pokemontcg.io/v2/cards",
        json={"data": [{"id": "base1-1", "name": "Alakazam"}]},
        status=200,
    )

    cards = client.fetch_cards_page(page=1)

    assert cards == [{"id": "base1-1", "name": "Alakazam"}]


@responses.activate
def test_fetch_cards_page_retries_then_succeeds(client: PokemonTcgClient) -> None:
    responses.add(responses.GET, "https://api.pokemontcg.io/v2/cards", status=500)
    responses.add(responses.GET, "https://api.pokemontcg.io/v2/cards", status=500)
    responses.add(
        responses.GET,
        "https://api.pokemontcg.io/v2/cards",
        json={"data": [{"id": "base1-2", "name": "Blastoise"}]},
        status=200,
    )

    cards = client.fetch_cards_page(page=1)

    assert cards == [{"id": "base1-2", "name": "Blastoise"}]
    assert len(responses.calls) == 3


@responses.activate
def test_fetch_cards_page_raises_after_exhausting_retries(client: PokemonTcgClient) -> None:
    for _ in range(4):
        responses.add(responses.GET, "https://api.pokemontcg.io/v2/cards", status=500)

    with pytest.raises(PokemonTcgApiError):
        client.fetch_cards_page(page=1)

    assert len(responses.calls) == 4
```

- [ ] **Step 3: Lancer les tests pour vérifier qu'ils échouent**

Run: `pytest tests/test_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.extract.pokemontcg_client'`

- [ ] **Step 4: Créer `src/extract/pokemontcg_client.py`**

```python
from __future__ import annotations

import logging

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.common.config import PokemonTcgConfig

logger = logging.getLogger(__name__)


class PokemonTcgApiError(Exception):
    """Levée quand l'API pokemontcg.io reste en échec après épuisement des tentatives."""


class PokemonTcgClient:
    def __init__(
        self,
        config: PokemonTcgConfig,
        timeout_seconds: float = 10.0,
        max_attempts: int = 4,
        wait_min_seconds: float = 1.0,
        wait_max_seconds: float = 10.0,
    ) -> None:
        self._config = config
        self._timeout_seconds = timeout_seconds
        self._retrying = Retrying(
            retry=retry_if_exception_type(requests.RequestException),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=wait_min_seconds, max=wait_max_seconds),
            reraise=True,
        )

    def fetch_cards_page(self, page: int, page_size: int = 250) -> list[dict]:
        """Récupère une page de cartes. Lève PokemonTcgApiError si l'API reste
        indisponible après épuisement des tentatives — jamais d'échec silencieux."""
        try:
            response = self._retrying(self._do_get, "/cards", {"page": page, "pageSize": page_size})
        except requests.RequestException as exc:
            logger.error("Échec définitif de l'appel API après retries: %s", exc)
            raise PokemonTcgApiError(f"Impossible de récupérer la page {page}") from exc
        return response.json()["data"]

    def _do_get(self, path: str, params: dict) -> requests.Response:
        url = f"{self._config.base_url}{path}"
        logger.info("Appel API pokemontcg.io: %s params=%s", url, params)
        response = requests.get(
            url,
            params=params,
            headers={"X-Api-Key": self._config.api_key},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return response
```

- [ ] **Step 5: Relancer les tests pour vérifier qu'ils passent**

Run: `pytest tests/test_extract.py -v`
Expected: 3 PASS

- [ ] **Step 6: Lint, format, commit**

```bash
ruff check . && black .
git add src/common/config.py src/extract tests/test_extract.py
git commit -m "feat: pokemontcg.io client with retry/backoff and structured logging"
```

---

### Task 4: Chargement idempotent en raw + script d'orchestration

**Files:**
- Create: `src/load/raw_loader.py`
- Create: `scripts/run_extract_load.py`
- Test: `tests/test_raw_loader.py`

**Interfaces:**
- Consomme : `get_connection`/`load_db_config` (Task 2), `PokemonTcgClient.fetch_cards_page`/`load_pokemontcg_config` (Task 3).
- Produit : `load_cards(conn, cards: list[dict], extracted_date: date, source: str = "pokemontcg.io") -> int` dans `src/load/raw_loader.py` ; `scripts/run_extract_load.py` comme point d'entrée exécutable (aucun autre module n'en dépend).

- [ ] **Step 1: Écrire les tests qui doivent échouer**

`tests/test_raw_loader.py` :
```python
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.raw_loader import load_cards


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute("TRUNCATE TABLE raw.card_prices RESTART IDENTITY;")
        admin_conn.commit()

    with get_connection(load_db_config()) as conn:
        yield conn


def test_load_cards_inserts_new_rows(db_connection) -> None:
    cards = [{"id": "base1-1", "name": "Alakazam"}]

    inserted = load_cards(db_connection, cards, extracted_date=date(2026, 8, 20))

    assert inserted == 1
    with db_connection.cursor() as cur:
        cur.execute("SELECT card_id, extracted_date FROM raw.card_prices;")
        rows = cur.fetchall()
    assert rows == [("base1-1", date(2026, 8, 20))]


def test_load_cards_is_idempotent_for_same_day(db_connection) -> None:
    cards = [{"id": "base1-1", "name": "Alakazam", "price": 1.0}]
    updated_cards = [{"id": "base1-1", "name": "Alakazam", "price": 2.0}]

    load_cards(db_connection, cards, extracted_date=date(2026, 8, 20))
    load_cards(db_connection, updated_cards, extracted_date=date(2026, 8, 20))

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.card_prices WHERE card_id = 'base1-1';")
        (count,) = cur.fetchone()
    assert count == 1


def test_load_cards_keeps_history_across_different_days(db_connection) -> None:
    cards = [{"id": "base1-1", "name": "Alakazam"}]

    load_cards(db_connection, cards, extracted_date=date(2026, 8, 20))
    load_cards(db_connection, cards, extracted_date=date(2026, 8, 21))

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.card_prices WHERE card_id = 'base1-1';")
        (count,) = cur.fetchone()
    assert count == 2
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `pytest tests/test_raw_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.load.raw_loader'`

- [ ] **Step 3: Créer `src/load/raw_loader.py`**

```python
from __future__ import annotations

import json
import logging
from datetime import date

from psycopg import Connection

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
    INSERT INTO raw.card_prices (card_id, extracted_date, source, payload)
    VALUES (%(card_id)s, %(extracted_date)s, %(source)s, %(payload)s)
    ON CONFLICT (card_id, extracted_date, source)
    DO UPDATE SET payload = EXCLUDED.payload, loaded_at = now()
"""


def load_cards(
    conn: Connection,
    cards: list[dict],
    extracted_date: date,
    source: str = "pokemontcg.io",
) -> int:
    """Charge les cartes dans raw.card_prices. Idempotent pour un même
    (card_id, extracted_date, source) : rejouer met à jour la ligne du jour
    au lieu de la dupliquer, sans toucher aux jours précédents."""
    rows = [
        {
            "card_id": card["id"],
            "extracted_date": extracted_date,
            "source": source,
            "payload": json.dumps(card),
        }
        for card in cards
    ]
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, rows)
    logger.info("Chargé %d cartes (date=%s, source=%s)", len(rows), extracted_date, source)
    return len(rows)
```

- [ ] **Step 4: Relancer les tests pour vérifier qu'ils passent**

Run: `pytest tests/test_raw_loader.py -v`
Expected: 3 PASS — le 3e test valide explicitement que l'historique inter-jours est préservé, pas seulement l'idempotence intra-jour.

- [ ] **Step 5: Créer le script d'orchestration `scripts/run_extract_load.py`**

```python
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.common.config import load_db_config, load_pokemontcg_config
from src.common.db import get_connection
from src.extract.pokemontcg_client import PokemonTcgClient
from src.load.raw_loader import load_cards

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    extracted_date = datetime.now(timezone.utc).date()
    client = PokemonTcgClient(load_pokemontcg_config())

    page = 1
    total_loaded = 0
    with get_connection(load_db_config()) as conn:
        while True:
            cards = client.fetch_cards_page(page=page)
            if not cards:
                break
            total_loaded += load_cards(conn, cards, extracted_date=extracted_date)
            page += 1

    logger.info("Extraction terminée : %d cartes chargées pour le %s", total_loaded, extracted_date)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Vérification manuelle bout en bout**

Run: `python -m scripts.run_extract_load`
Expected : logs INFO affichant chaque appel API et le total chargé, sans exception. Puis vérifier en base :
```bash
docker compose exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM raw.card_prices;"
```
Expected : un nombre de lignes proche du nombre total de cartes Pokémon disponibles sur pokemontcg.io.

- [ ] **Step 7: Lint, format, commit**

```bash
ruff check . && black .
git add src/load scripts/run_extract_load.py tests/test_raw_loader.py
git commit -m "feat: idempotent raw loader with day-scoped upsert, plus extract-load entrypoint"
```

---

### Task 5: Provisioning de la VM Oracle Cloud (runbook, tâche isolée)

**Files:**
- Create: `infra/oracle_vm_setup.md`

**Interfaces:**
- Consomme : rien (infrastructure indépendante du code applicatif ce mois-ci).
- Produit : une VM joignable en SSH, avec Docker + Docker Compose installés, qui servira de cible de déploiement au Mois 3. Aucune interface de code.

- [ ] **Step 1: Créer le compte Oracle Cloud et activer l'Always Free Tier**

Aller sur `https://www.oracle.com/cloud/free/`, créer un compte, choisir une "Home Region" (le choix est définitif). Activer l'offre Always Free.

- [ ] **Step 2: Créer l'instance Compute Always Free (Ampere A1, Ubuntu)**

Dans la console OCI : Compute → Instances → Create Instance. Choisir l'image "Canonical Ubuntu 22.04", la forme "VM.Standard.A1.Flex" (1 OCPU / 6 Go RAM suffisent pour ce mois-ci — le budget Always Free permet jusqu'à 4 OCPU / 24 Go au total, à ajuster au mois 3 si besoin pour Airflow). Générer une paire de clés SSH lors de la création, télécharger la clé privée.

- [ ] **Step 3: Durcir l'accès SSH**

Sur la VM :
```bash
ssh -i /path/to/downloaded_key.pem ubuntu@<IP_PUBLIQUE>
```
Puis, dans `/etc/ssh/sshd_config`, vérifier/forcer :
```
PasswordAuthentication no
PermitRootLogin no
```
```bash
sudo systemctl restart sshd
```

- [ ] **Step 4: Configurer le firewall (n'ouvrir que SSH pour l'instant)**

Dans la console OCI, sur la Security List du VCN par défaut : ne laisser que la règle Ingress TCP port 22 (0.0.0.0/0) et retirer toute autre règle ouverte par défaut. Sur la VM elle-même :
```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```
Expected : seul le port 22 est listé comme autorisé. Les ports Postgres (5432) et Airflow (8080) seront ouverts explicitement au mois 3, au moment du déploiement — pas avant.

- [ ] **Step 5: Installer Docker et Docker Compose**

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
newgrp docker
docker compose version
```

- [ ] **Step 6: Vérifier avec un conteneur de test**

Run: `docker run --rm hello-world`
Expected : message "Hello from Docker!" affiché sans erreur de permission.

- [ ] **Step 7: Documenter et committer**

Créer `infra/oracle_vm_setup.md` reprenant les étapes ci-dessus (shape de l'instance choisie, région, décisions de firewall) pour que le déploiement du mois 3 puisse s'appuyer dessus sans deviner les choix faits.

```bash
git add infra/oracle_vm_setup.md
git commit -m "docs: Oracle Cloud VM provisioned and hardened, ready for month 3 deployment"
```

---

## Self-Review Notes

- **Couverture du spec (Mois 1)** : setup env ✓ (Task 1), client API + gestion d'erreurs ✓ (Task 3), script d'insertion SQL en table raw ✓ (Task 4), provisioning VM ✓ (Task 5). Schémas raw/staging/prod dès la migration 001 ✓ (Task 2, au-delà du minimum demandé mais nécessaire pour respecter le standard "schémas séparés dès le départ").
- **Cohérence des types** : `DatabaseConfig`/`load_db_config` (Task 2) réutilisés tels quels par Task 4 et le script d'orchestration ; `PokemonTcgClient.fetch_cards_page` (Task 3) a la même signature partout où il est appelé (Task 4, script).
- **Point de rigueur explicitement testé** : le test `test_load_cards_keeps_history_across_different_days` (Task 4) vérifie que la clé d'idempotence (`card_id, extracted_date, source`) préserve l'historique inter-jours — une erreur fréquente serait de faire l'UPSERT sur `card_id` seul, ce qui écraserait silencieusement l'historique des prix.
