# Card Price Tracker — Mois 2 : Pipeline complet raw→staging→prod + Airflow local — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Étendre le pipeline du Mois 1 avec une couche de nettoyage/validation (staging), un schéma en étoile (prod), et un DAG Airflow local qui orchestre `extract >> clean >> load` de bout en bout, de façon idempotente et testée, avec une CI GitHub Actions.

**Architecture:** Deux nouveaux modules (`src/transform/`, extension de `src/load/`) suivent le même pattern que le Mois 1 : chaque fonction qui touche la base prend une connexion (`conn`) en paramètre, l'ouverture/fermeture de connexion restant la responsabilité de l'appelant (script ou tâche Airflow). Ça garde chaque fonction testable indépendamment et garantit qu'une tâche Airflow = une transaction.

**Tech Stack:** Ajoute Apache Airflow 2.9 (LocalExecutor, image Docker custom), sur la même base Python/Postgres que le Mois 1.

## Global Constraints

(Identiques au Mois 1 — voir `docs/superpowers/plans/2026-08-05-card-price-tracker-month1.md`, section Global Constraints. Rappels spécifiques à ce mois-ci :)
- Toute nouvelle table respecte les mêmes règles : schéma dédié, contraintes explicites, grants minimaux pour `pipeline_app`.
- Idempotence vérifiée par un test qui rejoue le pipeline complet deux fois (`tests/test_idempotence.py`), pas seulement à l'unité.
- CI GitHub Actions = tests + lint uniquement (pas d'orchestration, pas de déploiement).

**Pré-requis :** le plan du Mois 1 doit être terminé (raw fonctionnel, VM provisionnée).

---

## File Structure (ajouts par rapport au Mois 1)

```
card-price-tracker/
├── Dockerfile.airflow                  # image Airflow + nos dépendances Python
├── docker-compose.yml                  # étendu : airflow-db, airflow-init, airflow-webserver, airflow-scheduler
├── migrations/
│   ├── 002_create_staging_tables.sql
│   └── 003_create_star_schema.sql
├── src/
│   ├── transform/
│   │   ├── __init__.py
│   │   ├── validate.py                 # logique pure, sans DB
│   │   └── clean.py                    # orchestration raw -> staging
│   └── load/
│       ├── staging_loader.py           # écriture staging + quarantaine
│       └── warehouse_loader.py         # écriture star schema (prod)
├── dags/
│   └── card_price_pipeline_dag.py
├── tests/
│   ├── test_transform.py               # unitaires, sans DB
│   ├── test_staging_loader.py
│   ├── test_warehouse_loader.py
│   └── test_idempotence.py             # bout en bout, rejoue le pipeline 2x
└── .github/
    └── workflows/
        └── ci.yml
```

---

### Task 1: Migrations staging + star schema

**Files:**
- Create: `migrations/002_create_staging_tables.sql`
- Create: `migrations/003_create_star_schema.sql`

**Interfaces:**
- Consomme : `pipeline_app` (rôle créé au Mois 1), `scripts/apply_migrations.sh` (inchangé, boucle déjà sur tous les fichiers `migrations/*.sql`).
- Produit : tables `staging.card_prices`, `staging.card_prices_quarantine`, `prod.dim_card`, `prod.dim_date`, `prod.dim_platform`, `prod.fact_price_history` — consommées par les Tasks 2-4.

- [ ] **Step 1: Créer `migrations/002_create_staging_tables.sql`**

```sql
BEGIN;

CREATE TABLE IF NOT EXISTS staging.card_prices (
    id                    bigserial PRIMARY KEY,
    card_id               text NOT NULL,
    extracted_date        date NOT NULL,
    name                  text NOT NULL,
    set_id                text NOT NULL,
    set_name              text NOT NULL,
    rarity                text,
    average_sell_price    numeric(10, 2),
    trend_price           numeric(10, 2),
    low_price             numeric(10, 2),
    source                text NOT NULL,
    loaded_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_staging_card_prices_card_date_source UNIQUE (card_id, extracted_date, source)
);

CREATE TABLE IF NOT EXISTS staging.card_prices_quarantine (
    id                bigserial PRIMARY KEY,
    card_id           text,
    extracted_date    date NOT NULL,
    raw_payload       jsonb NOT NULL,
    rejection_reason  text NOT NULL,
    source            text NOT NULL,
    loaded_at         timestamptz NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA staging TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON staging.card_prices TO pipeline_app;
GRANT SELECT, INSERT ON staging.card_prices_quarantine TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE staging.card_prices_id_seq TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE staging.card_prices_quarantine_id_seq TO pipeline_app;

COMMIT;
```

- [ ] **Step 2: Créer `migrations/003_create_star_schema.sql`**

```sql
BEGIN;

CREATE TABLE IF NOT EXISTS prod.dim_card (
    card_id     text PRIMARY KEY,
    name        text NOT NULL,
    set_id      text NOT NULL,
    set_name    text NOT NULL,
    rarity      text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prod.dim_date (
    date_id      date PRIMARY KEY,
    year         smallint NOT NULL,
    month        smallint NOT NULL,
    day          smallint NOT NULL,
    day_of_week  smallint NOT NULL
);

CREATE TABLE IF NOT EXISTS prod.dim_platform (
    platform_id    serial PRIMARY KEY,
    platform_name  text NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS prod.fact_price_history (
    fact_id              bigserial PRIMARY KEY,
    card_id              text NOT NULL REFERENCES prod.dim_card (card_id),
    date_id              date NOT NULL REFERENCES prod.dim_date (date_id),
    platform_id          integer NOT NULL REFERENCES prod.dim_platform (platform_id),
    average_sell_price   numeric(10, 2),
    trend_price          numeric(10, 2),
    low_price            numeric(10, 2),
    loaded_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_fact_price_history_card_date_platform UNIQUE (card_id, date_id, platform_id)
);

-- Seule 'cardmarket' est utilisée pour l'instant (source unique via pokemontcg.io).
-- pipeline_app n'a que SELECT dessus : ajouter une plateforme est un acte de
-- migration/admin, pas une action que le pipeline doit pouvoir faire seul.
INSERT INTO prod.dim_platform (platform_name) VALUES ('cardmarket')
ON CONFLICT (platform_name) DO NOTHING;

GRANT USAGE ON SCHEMA prod TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON prod.dim_card TO pipeline_app;
GRANT SELECT, INSERT ON prod.dim_date TO pipeline_app;
GRANT SELECT ON prod.dim_platform TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON prod.fact_price_history TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE prod.fact_price_history_fact_id_seq TO pipeline_app;

COMMIT;
```

- [ ] **Step 3: Appliquer les migrations**

Run: `./scripts/apply_migrations.sh`
Expected : affiche "Applique : 002_create_staging_tables.sql" puis "Applique : 003_create_star_schema.sql", sans erreur.

- [ ] **Step 4: Vérification manuelle**

Run:
```bash
docker compose exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "\dt staging.*" -c "\dt prod.*"
```
Expected : les 6 tables listées existent dans les bons schémas.

- [ ] **Step 5: Commit**

```bash
git add migrations/002_create_staging_tables.sql migrations/003_create_star_schema.sql
git commit -m "feat: staging tables (with quarantine) and star schema migrations"
```

---

### Task 2: Nettoyage, validation, quarantaine (raw → staging)

**Files:**
- Create: `src/transform/validate.py`
- Create: `src/transform/clean.py`
- Create: `src/load/staging_loader.py`
- Test: `tests/test_transform.py`
- Test: `tests/test_staging_loader.py`

**Interfaces:**
- Consomme : `raw.card_prices` (Mois 1), `get_connection`/`load_db_config` (Mois 1).
- Produit : `CleanedCard` (dataclass), `ValidationResult` (dataclass, propriété `is_valid`), `validate_and_clean(payload: dict) -> ValidationResult` dans `src/transform/validate.py` ; `load_staging(conn, cleaned_cards: list[CleanedCard], extracted_date: date, source="pokemontcg.io") -> int` et `load_quarantine(conn, rejected: list[tuple[dict, str]], extracted_date: date, source="pokemontcg.io") -> int` dans `src/load/staging_loader.py` ; `clean_raw_to_staging(conn, extracted_date: date, source="pokemontcg.io") -> tuple[int, int]` dans `src/transform/clean.py` — consommés par Task 4 (DAG).

- [ ] **Step 1: Écrire les tests unitaires de validation (doivent échouer)**

`tests/test_transform.py` :
```python
from src.transform.validate import validate_and_clean


def _make_payload(**overrides) -> dict:
    payload = {
        "id": "base1-1",
        "name": "Alakazam",
        "rarity": "Rare Holo",
        "set": {"id": "base1", "name": "Base"},
        "cardmarket": {"prices": {"averageSellPrice": 12.5, "trendPrice": 13.0, "lowPrice": 8.0}},
    }
    payload.update(overrides)
    return payload


def test_validate_and_clean_accepts_valid_payload() -> None:
    result = validate_and_clean(_make_payload())

    assert result.is_valid
    assert result.cleaned.card_id == "base1-1"
    assert result.cleaned.average_sell_price == 12.5


def test_validate_and_clean_rejects_missing_set() -> None:
    result = validate_and_clean(_make_payload(set=None))

    assert not result.is_valid
    assert "set" in result.rejection_reason


def test_validate_and_clean_rejects_when_no_price_available() -> None:
    result = validate_and_clean(_make_payload(cardmarket={"prices": {}}))

    assert not result.is_valid
    assert "prix" in result.rejection_reason


def test_validate_and_clean_rejects_negative_price() -> None:
    result = validate_and_clean(_make_payload(cardmarket={"prices": {"averageSellPrice": -1.0}}))

    assert not result.is_valid
    assert "négatif" in result.rejection_reason
```

- [ ] **Step 2: Lancer pour vérifier l'échec**

Run: `pytest tests/test_transform.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.transform.validate'`

- [ ] **Step 3: Créer `src/transform/validate.py`**

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CleanedCard:
    card_id: str
    name: str
    set_id: str
    set_name: str
    rarity: str | None
    average_sell_price: float | None
    trend_price: float | None
    low_price: float | None


@dataclass(frozen=True)
class ValidationResult:
    cleaned: CleanedCard | None
    rejection_reason: str | None

    @property
    def is_valid(self) -> bool:
        return self.cleaned is not None


def validate_and_clean(payload: dict) -> ValidationResult:
    card_id = payload.get("id")
    name = payload.get("name")
    if not card_id or not name:
        return ValidationResult(cleaned=None, rejection_reason="card_id ou name manquant")

    set_info = payload.get("set") or {}
    set_id = set_info.get("id")
    set_name = set_info.get("name")
    if not set_id or not set_name:
        return ValidationResult(cleaned=None, rejection_reason="informations de set manquantes")

    cardmarket_prices = (payload.get("cardmarket") or {}).get("prices") or {}
    average_sell_price = cardmarket_prices.get("averageSellPrice")
    trend_price = cardmarket_prices.get("trendPrice")
    low_price = cardmarket_prices.get("lowPrice")

    if average_sell_price is None and trend_price is None and low_price is None:
        return ValidationResult(cleaned=None, rejection_reason="aucun prix cardmarket disponible")

    for label, value in [
        ("averageSellPrice", average_sell_price),
        ("trendPrice", trend_price),
        ("lowPrice", low_price),
    ]:
        if value is not None and value < 0:
            return ValidationResult(cleaned=None, rejection_reason=f"prix négatif ({label}={value})")

    return ValidationResult(
        cleaned=CleanedCard(
            card_id=card_id,
            name=name.strip(),
            set_id=set_id,
            set_name=set_name.strip(),
            rarity=payload.get("rarity"),
            average_sell_price=average_sell_price,
            trend_price=trend_price,
            low_price=low_price,
        ),
        rejection_reason=None,
    )
```

- [ ] **Step 4: Relancer pour vérifier le succès**

Run: `pytest tests/test_transform.py -v`
Expected: 4 PASS

- [ ] **Step 5: Écrire le test d'intégration du staging loader (doit échouer)**

`tests/test_staging_loader.py` :
```python
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.staging_loader import load_quarantine, load_staging
from src.transform.validate import CleanedCard


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE staging.card_prices, staging.card_prices_quarantine RESTART IDENTITY;"
        )
        admin_conn.commit()

    with get_connection(load_db_config()) as conn:
        yield conn


def test_load_staging_is_idempotent_for_same_day(db_connection) -> None:
    card = CleanedCard(
        card_id="base1-1", name="Alakazam", set_id="base1", set_name="Base",
        rarity="Rare Holo", average_sell_price=12.5, trend_price=13.0, low_price=8.0,
    )

    load_staging(db_connection, [card], extracted_date=date(2026, 9, 1))
    load_staging(db_connection, [card], extracted_date=date(2026, 9, 1))

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM staging.card_prices WHERE card_id = 'base1-1';")
        (count,) = cur.fetchone()
    assert count == 1


def test_load_quarantine_records_rejected_rows(db_connection) -> None:
    rejected = [({"id": "base1-2", "name": "Blastoise"}, "prix négatif (averageSellPrice=-1)")]

    inserted = load_quarantine(db_connection, rejected, extracted_date=date(2026, 9, 1))

    assert inserted == 1
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT rejection_reason FROM staging.card_prices_quarantine WHERE card_id = 'base1-2';"
        )
        (reason,) = cur.fetchone()
    assert reason == "prix négatif (averageSellPrice=-1)"
```

- [ ] **Step 6: Lancer pour vérifier l'échec**

Run: `pytest tests/test_staging_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.load.staging_loader'`

- [ ] **Step 7: Créer `src/load/staging_loader.py`**

```python
from __future__ import annotations

import json
import logging
from datetime import date

from psycopg import Connection

from src.transform.validate import CleanedCard

logger = logging.getLogger(__name__)

_UPSERT_STAGING_SQL = """
    INSERT INTO staging.card_prices
        (card_id, extracted_date, name, set_id, set_name, rarity,
         average_sell_price, trend_price, low_price, source)
    VALUES
        (%(card_id)s, %(extracted_date)s, %(name)s, %(set_id)s, %(set_name)s, %(rarity)s,
         %(average_sell_price)s, %(trend_price)s, %(low_price)s, %(source)s)
    ON CONFLICT (card_id, extracted_date, source)
    DO UPDATE SET
        name = EXCLUDED.name,
        set_id = EXCLUDED.set_id,
        set_name = EXCLUDED.set_name,
        rarity = EXCLUDED.rarity,
        average_sell_price = EXCLUDED.average_sell_price,
        trend_price = EXCLUDED.trend_price,
        low_price = EXCLUDED.low_price,
        loaded_at = now()
"""

_INSERT_QUARANTINE_SQL = """
    INSERT INTO staging.card_prices_quarantine
        (card_id, extracted_date, raw_payload, rejection_reason, source)
    VALUES (%(card_id)s, %(extracted_date)s, %(raw_payload)s, %(rejection_reason)s, %(source)s)
"""


def load_staging(
    conn: Connection,
    cleaned_cards: list[CleanedCard],
    extracted_date: date,
    source: str = "pokemontcg.io",
) -> int:
    rows = [
        {
            "card_id": c.card_id,
            "extracted_date": extracted_date,
            "name": c.name,
            "set_id": c.set_id,
            "set_name": c.set_name,
            "rarity": c.rarity,
            "average_sell_price": c.average_sell_price,
            "trend_price": c.trend_price,
            "low_price": c.low_price,
            "source": source,
        }
        for c in cleaned_cards
    ]
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_STAGING_SQL, rows)
    logger.info("Staging : %d cartes chargées (date=%s)", len(rows), extracted_date)
    return len(rows)


def load_quarantine(
    conn: Connection,
    rejected: list[tuple[dict, str]],
    extracted_date: date,
    source: str = "pokemontcg.io",
) -> int:
    rows = [
        {
            "card_id": payload.get("id"),
            "extracted_date": extracted_date,
            "raw_payload": json.dumps(payload),
            "rejection_reason": reason,
            "source": source,
        }
        for payload, reason in rejected
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_INSERT_QUARANTINE_SQL, rows)
    logger.warning("Quarantaine : %d cartes rejetées (date=%s)", len(rows), extracted_date)
    return len(rows)
```

- [ ] **Step 8: Relancer pour vérifier le succès**

Run: `pytest tests/test_staging_loader.py -v`
Expected: 2 PASS

- [ ] **Step 9: Créer `src/transform/clean.py` (orchestration raw → staging)**

```python
from __future__ import annotations

import logging
from datetime import date

from psycopg import Connection

from src.load.staging_loader import load_quarantine, load_staging
from src.transform.validate import validate_and_clean

logger = logging.getLogger(__name__)


def clean_raw_to_staging(
    conn: Connection, extracted_date: date, source: str = "pokemontcg.io"
) -> tuple[int, int]:
    """Lit raw.card_prices pour extracted_date, valide/nettoie chaque carte,
    et route vers staging.card_prices ou la quarantaine. Retourne (valides, rejetées)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM raw.card_prices WHERE extracted_date = %s AND source = %s",
            (extracted_date, source),
        )
        payloads = [row[0] for row in cur.fetchall()]

    cleaned = []
    rejected = []
    for payload in payloads:
        result = validate_and_clean(payload)
        if result.is_valid:
            cleaned.append(result.cleaned)
        else:
            rejected.append((payload, result.rejection_reason))

    load_staging(conn, cleaned, extracted_date=extracted_date, source=source)
    load_quarantine(conn, rejected, extracted_date=extracted_date, source=source)

    logger.info(
        "Nettoyage terminé (date=%s) : %d valides, %d en quarantaine",
        extracted_date, len(cleaned), len(rejected),
    )
    return len(cleaned), len(rejected)
```

Ce test n'a pas de fichier dédié : il est couvert indirectement par `tests/test_idempotence.py` (Task 4), qui exerce `clean_raw_to_staging` de bout en bout.

- [ ] **Step 10: Lint, format, commit**

```bash
ruff check . && black .
git add src/transform src/load/staging_loader.py tests/test_transform.py tests/test_staging_loader.py
git commit -m "feat: validation, cleaning and quarantine logic for raw-to-staging"
```

---

### Task 3: Chargement staging → prod (star schema)

**Files:**
- Create: `src/load/warehouse_loader.py`
- Test: `tests/test_warehouse_loader.py`

**Interfaces:**
- Consomme : `staging.card_prices` (Task 2), `prod.*` (Task 1).
- Produit : `load_staging_to_warehouse(conn, extracted_date: date, source="pokemontcg.io", platform_name="cardmarket") -> int` dans `src/load/warehouse_loader.py` — consommé par Task 4 (DAG) et `tests/test_idempotence.py`.

- [ ] **Step 1: Écrire les tests (doivent échouer)**

`tests/test_warehouse_loader.py` :
```python
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.staging_loader import load_staging
from src.load.warehouse_loader import load_staging_to_warehouse
from src.transform.validate import CleanedCard


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE staging.card_prices, prod.fact_price_history, prod.dim_card "
            "RESTART IDENTITY CASCADE;"
        )
        admin_conn.commit()

    with get_connection(load_db_config()) as conn:
        yield conn


def _seed_card() -> CleanedCard:
    return CleanedCard(
        card_id="base1-1", name="Alakazam", set_id="base1", set_name="Base",
        rarity="Rare Holo", average_sell_price=12.5, trend_price=13.0, low_price=8.0,
    )


def test_load_staging_to_warehouse_inserts_fact(db_connection) -> None:
    load_staging(db_connection, [_seed_card()], extracted_date=date(2026, 9, 1))

    inserted = load_staging_to_warehouse(db_connection, extracted_date=date(2026, 9, 1))

    assert inserted == 1
    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM prod.dim_card WHERE card_id = 'base1-1';")
        (dim_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM prod.fact_price_history WHERE card_id = 'base1-1';")
        (fact_count,) = cur.fetchone()
    assert dim_count == 1
    assert fact_count == 1


def test_load_staging_to_warehouse_is_idempotent(db_connection) -> None:
    load_staging(db_connection, [_seed_card()], extracted_date=date(2026, 9, 1))

    load_staging_to_warehouse(db_connection, extracted_date=date(2026, 9, 1))
    load_staging_to_warehouse(db_connection, extracted_date=date(2026, 9, 1))

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM prod.fact_price_history WHERE card_id = 'base1-1';")
        (count,) = cur.fetchone()
    assert count == 1
```

- [ ] **Step 2: Lancer pour vérifier l'échec**

Run: `pytest tests/test_warehouse_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.load.warehouse_loader'`

- [ ] **Step 3: Créer `src/load/warehouse_loader.py`**

```python
from __future__ import annotations

import logging
from datetime import date

from psycopg import Connection

logger = logging.getLogger(__name__)

_UPSERT_DIM_CARD_SQL = """
    INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity)
    VALUES (%(card_id)s, %(name)s, %(set_id)s, %(set_name)s, %(rarity)s)
    ON CONFLICT (card_id) DO UPDATE SET
        name = EXCLUDED.name, set_id = EXCLUDED.set_id,
        set_name = EXCLUDED.set_name, rarity = EXCLUDED.rarity,
        updated_at = now()
"""

_UPSERT_DIM_DATE_SQL = """
    INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week)
    VALUES (%(date_id)s, %(year)s, %(month)s, %(day)s, %(day_of_week)s)
    ON CONFLICT (date_id) DO NOTHING
"""

_UPSERT_FACT_SQL = """
    INSERT INTO prod.fact_price_history
        (card_id, date_id, platform_id, average_sell_price, trend_price, low_price)
    SELECT %(card_id)s, %(date_id)s, platform_id, %(average_sell_price)s, %(trend_price)s, %(low_price)s
    FROM prod.dim_platform WHERE platform_name = %(platform_name)s
    ON CONFLICT (card_id, date_id, platform_id) DO UPDATE SET
        average_sell_price = EXCLUDED.average_sell_price,
        trend_price = EXCLUDED.trend_price,
        low_price = EXCLUDED.low_price,
        loaded_at = now()
"""


def load_staging_to_warehouse(
    conn: Connection,
    extracted_date: date,
    source: str = "pokemontcg.io",
    platform_name: str = "cardmarket",
) -> int:
    """Charge staging.card_prices vers le star schema. Les dimensions
    (dim_card, dim_date) sont upsertées avant le fait pour respecter les
    contraintes de clé étrangère de fact_price_history."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT card_id, name, set_id, set_name, rarity,
                   average_sell_price, trend_price, low_price
            FROM staging.card_prices
            WHERE extracted_date = %s AND source = %s
            """,
            (extracted_date, source),
        )
        rows = cur.fetchall()

        cur.execute(
            _UPSERT_DIM_DATE_SQL,
            {
                "date_id": extracted_date,
                "year": extracted_date.year,
                "month": extracted_date.month,
                "day": extracted_date.day,
                "day_of_week": extracted_date.isoweekday(),
            },
        )

        for (card_id, name, set_id, set_name, rarity,
             average_sell_price, trend_price, low_price) in rows:
            cur.execute(
                _UPSERT_DIM_CARD_SQL,
                {"card_id": card_id, "name": name, "set_id": set_id,
                 "set_name": set_name, "rarity": rarity},
            )
            cur.execute(
                _UPSERT_FACT_SQL,
                {
                    "card_id": card_id,
                    "date_id": extracted_date,
                    "average_sell_price": average_sell_price,
                    "trend_price": trend_price,
                    "low_price": low_price,
                    "platform_name": platform_name,
                },
            )

    logger.info("Warehouse : %d faits chargés (date=%s)", len(rows), extracted_date)
    return len(rows)
```

- [ ] **Step 4: Relancer pour vérifier le succès**

Run: `pytest tests/test_warehouse_loader.py -v`
Expected: 2 PASS

- [ ] **Step 5: Lint, format, commit**

```bash
ruff check . && black .
git add src/load/warehouse_loader.py tests/test_warehouse_loader.py
git commit -m "feat: idempotent staging-to-warehouse loader for the star schema"
```

---

### Task 4: DAG Airflow local + test d'idempotence bout en bout + CI

**Files:**
- Create: `Dockerfile.airflow`
- Modify: `docker-compose.yml` (ajout des services Airflow)
- Modify: `.env.example` (ajout des variables Airflow)
- Create: `dags/card_price_pipeline_dag.py`
- Test: `tests/test_idempotence.py`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consomme : `load_cards` (Mois 1), `clean_raw_to_staging` (Task 2), `load_staging_to_warehouse` (Task 3), `get_connection`/`load_db_config`/`load_pokemontcg_config` (Mois 1).
- Produit : DAG Airflow `card_price_pipeline` (aucun autre module ne le consomme, c'est un point d'entrée).

- [ ] **Step 1: Créer `Dockerfile.airflow`**

```dockerfile
FROM apache/airflow:2.9.3-python3.11
COPY pyproject.toml /opt/airflow/pyproject.toml
RUN pip install --no-cache-dir requests>=2.31 "psycopg[binary]>=3.1" python-dotenv>=1.0 tenacity>=8.2
```

- [ ] **Step 2: Étendre `docker-compose.yml` avec les services Airflow**

Ajouter à la fin du fichier existant (avant la section `volumes:` finale, en fusionnant avec le volume déjà déclaré) :
```yaml
  airflow-db:
    image: postgres:16
    environment:
      POSTGRES_USER: airflow
      POSTGRES_PASSWORD: ${AIRFLOW_DB_PASSWORD}
      POSTGRES_DB: airflow
    volumes:
      - airflow_pg_data:/var/lib/postgresql/data

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
      - "8080:8080"
    volumes:
      - ./dags:/opt/airflow/dags
      - ./src:/opt/airflow/src
      - ./.env:/opt/airflow/.env:ro

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
```

Et ajouter `airflow_pg_data:` à la section `volumes:` déjà présente en bas du fichier.

- [ ] **Step 3: Ajouter les variables Airflow à `.env.example` (et `.env`)**

Ajouter à la fin de `.env.example` :
```
# Airflow (local, LocalExecutor, métadonnées dans un Postgres dédié)
AIRFLOW_DB_PASSWORD=changeme_airflow
AIRFLOW_ADMIN_PASSWORD=changeme_admin_airflow
```

- [ ] **Step 4: Créer `dags/card_price_pipeline_dag.py`**

```python
from __future__ import annotations

from datetime import date, datetime, timezone

from airflow.decorators import dag, task

from src.common.config import load_db_config, load_pokemontcg_config
from src.common.db import get_connection
from src.extract.pokemontcg_client import PokemonTcgClient
from src.load.raw_loader import load_cards
from src.load.warehouse_loader import load_staging_to_warehouse
from src.transform.clean import clean_raw_to_staging


@dag(
    schedule="@daily",
    start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
    catchup=False,
    default_args={"retries": 2},
)
def card_price_pipeline():
    @task
    def extract_and_load_raw() -> str:
        extracted_date = datetime.now(timezone.utc).date()
        client = PokemonTcgClient(load_pokemontcg_config())
        page = 1
        with get_connection(load_db_config()) as conn:
            while True:
                cards = client.fetch_cards_page(page=page)
                if not cards:
                    break
                load_cards(conn, cards, extracted_date=extracted_date)
                page += 1
        return extracted_date.isoformat()

    @task
    def clean_to_staging(extracted_date_iso: str) -> str:
        extracted_date = date.fromisoformat(extracted_date_iso)
        with get_connection(load_db_config()) as conn:
            clean_raw_to_staging(conn, extracted_date)
        return extracted_date_iso

    @task
    def load_to_warehouse(extracted_date_iso: str) -> None:
        extracted_date = date.fromisoformat(extracted_date_iso)
        with get_connection(load_db_config()) as conn:
            load_staging_to_warehouse(conn, extracted_date)

    extracted_date_iso = extract_and_load_raw()
    cleaned_date_iso = clean_to_staging(extracted_date_iso)
    load_to_warehouse(cleaned_date_iso)


card_price_pipeline()
```

- [ ] **Step 5: Démarrer Airflow en local et vérifier le DAG**

Run:
```bash
docker compose up -d airflow-db airflow-init
docker compose up -d airflow-webserver airflow-scheduler
```
Puis ouvrir `http://localhost:8080` (admin / `${AIRFLOW_ADMIN_PASSWORD}`), vérifier que `card_price_pipeline` apparaît sans erreur d'import, le déclencher manuellement une fois, et vérifier que les 3 tâches passent au vert dans l'ordre `extract_and_load_raw >> clean_to_staging >> load_to_warehouse`.

- [ ] **Step 6: Écrire le test d'idempotence bout en bout (doit échouer)**

`tests/test_idempotence.py` :
```python
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.raw_loader import load_cards
from src.load.warehouse_loader import load_staging_to_warehouse
from src.transform.clean import clean_raw_to_staging


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE raw.card_prices, staging.card_prices, "
            "staging.card_prices_quarantine, prod.fact_price_history, prod.dim_card "
            "RESTART IDENTITY CASCADE;"
        )
        admin_conn.commit()

    with get_connection(load_db_config()) as conn:
        yield conn


def test_full_pipeline_is_idempotent_when_replayed(db_connection) -> None:
    extracted_date = date(2026, 9, 5)
    cards = [
        {
            "id": "base1-1",
            "name": "Alakazam",
            "rarity": "Rare Holo",
            "set": {"id": "base1", "name": "Base"},
            "cardmarket": {
                "prices": {"averageSellPrice": 12.5, "trendPrice": 13.0, "lowPrice": 8.0}
            },
        }
    ]

    for _ in range(2):
        load_cards(db_connection, cards, extracted_date=extracted_date)
        clean_raw_to_staging(db_connection, extracted_date)
        load_staging_to_warehouse(db_connection, extracted_date)

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.card_prices;")
        (raw_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM staging.card_prices;")
        (staging_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM prod.fact_price_history;")
        (fact_count,) = cur.fetchone()

    assert raw_count == 1
    assert staging_count == 1
    assert fact_count == 1
```

- [ ] **Step 7: Lancer pour vérifier l'échec, puis le succès**

Run: `pytest tests/test_idempotence.py -v`
Expected (avant Task 2/3, si lancé isolément) : passe déjà si Tasks 2 et 3 sont faites — sinon `ModuleNotFoundError`. À ce stade du plan (Tasks 1-3 complètes), attendu : PASS directement.

- [ ] **Step 8: Créer `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      db:
        image: postgres:16
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: card_tracker
        ports:
          - 5432:5432
        options: >-
          --health-cmd="pg_isready -U postgres"
          --health-interval=5s
          --health-timeout=5s
          --health-retries=10
    env:
      POSTGRES_HOST: localhost
      POSTGRES_PORT: 5432
      POSTGRES_DB: card_tracker
      POSTGRES_ADMIN_USER: postgres
      POSTGRES_ADMIN_PASSWORD: postgres
      POSTGRES_APP_USER: pipeline_app
      POSTGRES_APP_PASSWORD: pipeline_app_password
      POKEMONTCG_API_KEY: unused-in-ci
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: sudo apt-get update && sudo apt-get install -y postgresql-client
      - run: pip install -e ".[dev]"
      - name: Apply migrations
        run: |
          for f in migrations/*.sql; do
            PGPASSWORD=$POSTGRES_ADMIN_PASSWORD psql -h localhost -U $POSTGRES_ADMIN_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -f "$f"
          done
          PGPASSWORD=$POSTGRES_ADMIN_PASSWORD psql -h localhost -U $POSTGRES_ADMIN_USER -d $POSTGRES_DB -c "ALTER ROLE pipeline_app WITH PASSWORD '${POSTGRES_APP_PASSWORD}';"
      - run: ruff check .
      - run: black --check .
      - run: pytest
```

- [ ] **Step 9: Pousser sur GitHub et vérifier que la CI passe**

Run: `git push` (après avoir créé le repo distant si ce n'est pas déjà fait)
Expected : le workflow "CI" apparaît dans l'onglet Actions du repo et se termine en vert.

- [ ] **Step 10: Lint, format, commit final**

```bash
ruff check . && black .
git add Dockerfile.airflow docker-compose.yml .env.example dags tests/test_idempotence.py .github
git commit -m "feat: local Airflow orchestration (extract >> clean >> load) and CI pipeline"
```

---

## Self-Review Notes

- **Couverture du spec (Mois 2)** : modélisation étoile + migrations ✓ (Task 1), nettoyage/validation/quarantaine ✓ (Task 2), chargement staging→prod ✓ (Task 3), DAG Airflow + tests idempotence + CI ✓ (Task 4).
- **Cohérence des types/signatures** : toutes les fonctions de `src/load/*` et `src/transform/clean.py` prennent `conn` en premier paramètre — même convention que `load_cards` du Mois 1. Le DAG (Task 4) est le seul endroit qui ouvre `get_connection` à ce niveau, une fois par tâche, ce qui borne chaque tâche Airflow à sa propre transaction.
- **Piège évité** : une première version de `load_staging_to_warehouse` ouvrait sa propre connexion en interne — incompatible avec les tests, qui doivent partager la même transaction que le seed de données (sinon les lignes "stagées" ne sont pas visibles tant qu'elles ne sont pas committées). Corrigé en alignant sa signature sur le pattern `conn`-en-paramètre.
