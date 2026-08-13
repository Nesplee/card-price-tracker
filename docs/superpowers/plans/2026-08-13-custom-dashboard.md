# Dashboard sur mesure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construire une API FastAPI (lecture seule sur `prod.*`) et un frontend React/Vite avec 3 vues (Catalogue, Détail carte, Ma collection), déployés en privé sur le VPS via tunnel SSH, en complément du dashboard Metabase existant.

**Architecture:** Deux nouveaux services Docker sur `docker-compose.prod.yml` — `dashboard-api` (FastAPI, réutilise le rôle Postgres `dashboard_reader`) et `dashboard-frontend` (build React statique servi par Nginx, proxy `/api/` vers `dashboard-api`). L'API expose 4 endpoints en lecture seule ; le frontend consomme cette API, pas Postgres directement.

**Tech Stack:** Backend : FastAPI, psycopg3, pytest + httpx (tests de contrat contre une vraie base Postgres, comme le reste du repo). Frontend : React + Vite + TypeScript, react-router-dom, Recharts (graphes).

**Spec:** `docs/superpowers/specs/2026-08-13-custom-dashboard-design.md`

## Global Constraints

- Toutes les requêtes de prix filtrent `platform_name = 'tcgplayer'` (jamais de mélange avec `cardmarket`/EUR).
- L'API se connecte à Postgres exclusivement via le rôle `dashboard_reader` (lecture seule, migration 007 déjà en place) — jamais `pipeline_app`.
- Les deux nouveaux services sont ajoutés uniquement à `docker-compose.prod.yml`, jamais à `docker-compose.yml` local, et exposés uniquement sur `127.0.0.1` (accès via tunnel SSH, pas d'exposition publique).
- Vues pré-conçues uniquement (Catalogue, Détail carte, Ma collection) — pas de constructeur de vues dynamique.
- Pas de tests frontend automatisés dans cette version — vérification manuelle uniquement.
- Les lignes `dim_owned_card` avec `average_cost_paid` NULL ou égal à `0` sont marquées `cost_unknown` et exclues de tout calcul de plus-value/valeur agrégée (jamais sommées comme un vrai `0$`).

---

## Partie 1 — API (FastAPI)

### Task 1: Dépendances et configuration `dashboard_reader`

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/common/config.py`
- Test: `tests/test_config.py` (nouveau)

**Interfaces:**
- Produces: `load_dashboard_reader_config() -> DatabaseConfig` dans `src/common/config.py`, utilisée par toutes les tâches suivantes de la Partie 1.

- [ ] **Step 1: Ajouter les dépendances API à `pyproject.toml`**

Dans `dependencies`, ajouter après `"tenacity>=8.2",` :
```toml
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
```
Dans `[project.optional-dependencies].dev`, ajouter après `"responses>=0.25",` :
```toml
    # httpx : requis par fastapi.testclient.TestClient pour simuler des
    # requêtes HTTP contre l'app FastAPI sans lancer de vrai serveur.
    "httpx>=0.27",
```

- [ ] **Step 2: Installer les dépendances**

Run: `pip install -e ".[dev]"`
Expected: installation réussie, `fastapi`, `uvicorn`, `httpx` présents dans l'environnement.

- [ ] **Step 3: Écrire le test qui échoue**

Créer `tests/test_config.py` :
```python
# Vérifie que load_dashboard_reader_config() construit bien une config
# pointant vers le rôle dashboard_reader (jamais pipeline_app), en lisant
# DASHBOARD_READER_PASSWORD plutôt que POSTGRES_APP_PASSWORD.
from __future__ import annotations

from src.common.config import load_dashboard_reader_config


def test_load_dashboard_reader_config_uses_dashboard_reader_role():
    config = load_dashboard_reader_config()
    assert config.user == "dashboard_reader"
    assert config.password  # non vide, lu depuis DASHBOARD_READER_PASSWORD
```

- [ ] **Step 4: Lancer le test, vérifier qu'il échoue**

Run: `pytest tests/test_config.py -v`
Expected: FAIL avec `ImportError: cannot import name 'load_dashboard_reader_config'`

- [ ] **Step 5: Implémenter `load_dashboard_reader_config()`**

Dans `src/common/config.py`, ajouter à la fin du fichier :
```python
def load_dashboard_reader_config() -> DatabaseConfig:
    # Même host/port/db que le pipeline (une seule base Postgres), mais un
    # utilisateur distinct : dashboard_reader (migration 007), lecture seule
    # sur prod uniquement. Ne JAMAIS réutiliser load_db_config() ici -- ce
    # serait donner à l'API du dashboard les droits d'écriture de
    # pipeline_app, qu'elle n'utilise jamais.
    return DatabaseConfig(
        host=_require_env("POSTGRES_HOST"),
        port=int(_require_env("POSTGRES_PORT")),
        dbname=_require_env("POSTGRES_DB"),
        user="dashboard_reader",
        password=_require_env("DASHBOARD_READER_PASSWORD"),
    )
```

- [ ] **Step 6: Lancer le test, vérifier qu'il passe**

Run: `pytest tests/test_config.py -v`
Expected: PASS (nécessite un `.env` local avec `DASHBOARD_READER_PASSWORD` déjà rempli — c'est déjà le cas depuis le déploiement Metabase, migration 007).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/common/config.py tests/test_config.py
git commit -m "feat: add FastAPI deps and dashboard_reader DB config"
```

---

### Task 2: Requêtes SQL (`src/api/queries.py`)

**Files:**
- Create: `src/api/__init__.py`
- Create: `src/api/db.py`
- Create: `src/api/queries.py`
- Test: `tests/test_api_queries.py`

**Interfaces:**
- Consumes: `load_dashboard_reader_config()` (Task 1).
- Produces:
  - `get_api_connection() -> Iterator[psycopg.Connection]` (`src/api/db.py`) — dépendance FastAPI, utilisée par Task 5.
  - `search_cards(conn, *, search=None, series=None, set_name=None, rarity=None, price_min=None, price_max=None, page=1, page_size=25) -> tuple[list[dict], int]`
  - `get_card_history(conn, card_id) -> tuple[dict, list[dict]] | None`
  - `get_owned_cards(conn) -> list[dict]`
  - `get_collection_value_history(conn) -> list[dict]`
  - toutes utilisées par Task 5 (`src/api/main.py`).

- [ ] **Step 1: Créer `src/api/__init__.py`** (fichier vide)

- [ ] **Step 2: Créer `src/api/db.py`**

```python
# Fournit la connexion Postgres utilisée par tous les endpoints de l'API du
# dashboard. Toujours via le rôle dashboard_reader (lecture seule, jamais
# pipeline_app) -- voir src/common/config.py:load_dashboard_reader_config().
from __future__ import annotations

from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from src.common.config import load_dashboard_reader_config


def get_api_connection() -> Iterator[psycopg.Connection]:
    # Générateur utilisé comme dépendance FastAPI (Depends(get_api_connection)) :
    # FastAPI exécute le code avant le yield à l'entrée de la requête HTTP, et
    # le code après (ici, conn.close() dans finally) une fois la réponse envoyée.
    # row_factory=dict_row : chaque ligne renvoyée par conn.execute(...).fetchall()
    # est un dict {nom_colonne: valeur} plutôt qu'un tuple positionnel -- plus
    # lisible et moins fragile si l'ordre des colonnes d'une requête change.
    cfg = load_dashboard_reader_config()
    conn = psycopg.connect(cfg.dsn, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()
```

- [ ] **Step 3: Écrire les tests qui échouent**

Créer `tests/test_api_queries.py` :
```python
# Tests contre une VRAIE connexion Postgres (rôle dashboard_reader), même
# pattern que tests/test_warehouse_loader.py : le comportement vérifié
# (filtrage, tri, dernière observation de prix) dépend de vraies requêtes
# SQL, pas juste d'un appel de fonction.
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest
from psycopg.rows import dict_row

from src.api.queries import (
    get_card_history,
    get_collection_value_history,
    get_owned_cards,
    search_cards,
)


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


def _dashboard_reader_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user=dashboard_reader "
        f"password={os.environ['DASHBOARD_READER_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    # Setup : repart d'un état vide, puis seed deux cartes + un historique de
    # prix sur 2 jours + une ligne de collection possédée (une avec coût
    # connu, une avec coût 0 = "inconnu").
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE prod.dim_owned_card, prod.fact_price_history, "
            "prod.dim_card RESTART IDENTITY CASCADE;"
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES ('base1-1', 'Alakazam', 'base1', 'Base Set', 'Rare Holo', 'Base'), "
            "('base1-2', 'Blastoise', 'base1', 'Base Set', 'Rare Holo', 'Base')"
        )
        admin_conn.execute(
            "SELECT platform_id FROM prod.dim_platform WHERE platform_name = 'tcgplayer'"
        )
        platform_id = admin_conn.execute(
            "SELECT platform_id FROM prod.dim_platform WHERE platform_name = 'tcgplayer'"
        ).fetchone()[0]
        admin_conn.execute(
            "INSERT INTO prod.fact_price_history "
            "(card_id, date_id, platform_id, average_sell_price, trend_price, low_price) "
            "VALUES "
            "('base1-1', %s, %s, 10.00, 11.00, 9.00), "
            "('base1-1', %s, %s, 12.00, 12.50, 10.00), "
            "('base1-2', %s, %s, 50.00, 52.00, 45.00)",
            (date(2026, 8, 1), platform_id, date(2026, 8, 2), platform_id, date(2026, 8, 2), platform_id),
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity, average_cost_paid) "
            "VALUES ('base1-1', 'Normal', '', 2, 8.00), ('base1-2', 'Normal', '', 1, 0.00)"
        )
        admin_conn.commit()

    with psycopg.connect(_dashboard_reader_dsn(), row_factory=dict_row) as conn:
        yield conn


def test_search_cards_filters_by_name(db_connection):
    rows, total = search_cards(db_connection, search="Blastoise")
    assert total == 1
    assert rows[0]["card_id"] == "base1-2"
    assert float(rows[0]["current_price"]) == 50.00


def test_search_cards_returns_latest_price(db_connection):
    rows, total = search_cards(db_connection, search="Alakazam")
    assert total == 1
    # 2026-08-02 est plus récent que 2026-08-01 -> 12.00, pas 10.00
    assert float(rows[0]["current_price"]) == 12.00


def test_search_cards_filters_by_price_range(db_connection):
    rows, total = search_cards(db_connection, price_min=20, price_max=100)
    assert total == 1
    assert rows[0]["card_id"] == "base1-2"


def test_get_card_history_returns_none_for_unknown_card(db_connection):
    assert get_card_history(db_connection, "does-not-exist") is None


def test_get_card_history_returns_full_series(db_connection):
    card, history = get_card_history(db_connection, "base1-1")
    assert card["name"] == "Alakazam"
    assert [float(row["average_sell_price"]) for row in history] == [10.00, 12.00]


def test_get_owned_cards_flags_zero_cost_as_unknown(db_connection):
    rows = get_owned_cards(db_connection)
    by_card = {row["card_id"]: row for row in rows}
    assert by_card["base1-1"]["cost_unknown"] is False
    assert by_card["base1-2"]["cost_unknown"] is True


def test_collection_value_history_excludes_unknown_cost_rows(db_connection):
    # base1-2 (coût 0 = inconnu) ne doit jamais entrer dans l'agrégat de
    # valeur -- seule base1-1 (coût connu, quantity=2) doit compter.
    rows = get_collection_value_history(db_connection)
    by_date = {row["date_id"]: float(row["total_value"]) for row in rows}
    assert by_date[date(2026, 8, 1)] == 20.00  # 2 * 10.00
    assert by_date[date(2026, 8, 2)] == 24.00  # 2 * 12.00
```

- [ ] **Step 4: Lancer les tests, vérifier qu'ils échouent**

Run: `pytest tests/test_api_queries.py -v`
Expected: FAIL avec `ModuleNotFoundError: No module named 'src.api.queries'`

- [ ] **Step 5: Implémenter `src/api/queries.py`**

```python
# Requêtes SQL brutes utilisées par l'API du dashboard (src/api/main.py).
# Toutes filtrent explicitement platform_name = 'tcgplayer' (voir Global
# Constraints du plan) -- jamais de mélange avec cardmarket/EUR.
from __future__ import annotations

_PLATFORM = "tcgplayer"


def _card_filters(
    search: str | None,
    series: str | None,
    set_name: str | None,
    rarity: str | None,
    price_min: float | None,
    price_max: float | None,
) -> tuple[str, dict]:
    # Construit dynamiquement la clause WHERE : seuls les filtres réellement
    # fournis par l'appelant (pas None) ajoutent une condition -- évite un
    # empilement de "colonne = %(param)s OR %(param)s IS NULL" plus lent et
    # plus difficile à lire.
    conditions = ["p.platform_name = %(platform)s"]
    params: dict = {"platform": _PLATFORM}
    if search:
        conditions.append("c.name ILIKE %(search)s")
        params["search"] = f"%{search}%"
    if series:
        conditions.append("c.series = %(series)s")
        params["series"] = series
    if set_name:
        conditions.append("c.set_name = %(set_name)s")
        params["set_name"] = set_name
    if rarity:
        conditions.append("c.rarity = %(rarity)s")
        params["rarity"] = rarity
    if price_min is not None:
        conditions.append("latest.average_sell_price >= %(price_min)s")
        params["price_min"] = price_min
    if price_max is not None:
        conditions.append("latest.average_sell_price <= %(price_max)s")
        params["price_max"] = price_max
    return " AND ".join(conditions), params


def search_cards(
    conn,
    *,
    search: str | None = None,
    series: str | None = None,
    set_name: str | None = None,
    rarity: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[dict], int]:
    # JOIN LATERAL : pour chaque carte de dim_card, va chercher SA propre
    # dernière observation de prix (ORDER BY date_id DESC LIMIT 1) -- contrairement
    # à un JOIN classique, la sous-requête est ré-évaluée pour chaque ligne de
    # dim_card, ce qui est exactement ce qu'il faut pour "le prix le plus
    # récent PAR carte" (pas un seul prix global).
    # COUNT(*) OVER() : calcule le nombre total de résultats (avant LIMIT/OFFSET)
    # dans la même requête, pour éviter un second aller-retour réseau juste
    # pour la pagination.
    where_sql, params = _card_filters(search, series, set_name, rarity, price_min, price_max)
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size
    sql = f"""
        SELECT
            c.card_id, c.name, c.series, c.set_name, c.rarity,
            latest.average_sell_price AS current_price,
            COUNT(*) OVER() AS total_count
        FROM prod.dim_card c
        JOIN LATERAL (
            SELECT fph.average_sell_price
            FROM prod.fact_price_history fph
            JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
            WHERE fph.card_id = c.card_id AND p.platform_name = %(platform)s
            ORDER BY fph.date_id DESC
            LIMIT 1
        ) latest ON true
        WHERE {where_sql}
        ORDER BY c.name
        LIMIT %(limit)s OFFSET %(offset)s
    """
    rows = conn.execute(sql, params).fetchall()
    total = rows[0]["total_count"] if rows else 0
    return rows, total


def get_card_history(conn, card_id: str) -> tuple[dict, list[dict]] | None:
    card = conn.execute(
        "SELECT card_id, name FROM prod.dim_card WHERE card_id = %(card_id)s",
        {"card_id": card_id},
    ).fetchone()
    if card is None:
        return None
    history = conn.execute(
        """
        SELECT fph.date_id, fph.average_sell_price, fph.trend_price, fph.low_price
        FROM prod.fact_price_history fph
        JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
        WHERE fph.card_id = %(card_id)s AND p.platform_name = %(platform)s
        ORDER BY fph.date_id
        """,
        {"card_id": card_id, "platform": _PLATFORM},
    ).fetchall()
    return card, history


def get_owned_cards(conn) -> list[dict]:
    # cost_unknown : average_cost_paid NULL OU littéralement 0 -- ce dernier
    # cas vient du CSV importé (coût non renseigné saisi comme "0.0000"), pas
    # d'un vrai achat gratuit. Voir la mise en garde déjà posée sur le
    # dashboard Metabase (docs/superpowers/specs/2026-08-10-interactive-dashboard-design.md).
    return conn.execute(
        """
        SELECT
            o.id, o.card_id, c.name, c.series, c.set_name, o.variance, o.grade,
            o.quantity, o.average_cost_paid,
            (o.average_cost_paid IS NULL OR o.average_cost_paid = 0) AS cost_unknown,
            latest.average_sell_price AS current_price
        FROM prod.dim_owned_card o
        JOIN prod.dim_card c ON c.card_id = o.card_id
        JOIN LATERAL (
            SELECT fph.average_sell_price
            FROM prod.fact_price_history fph
            JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
            WHERE fph.card_id = o.card_id AND p.platform_name = %(platform)s
            ORDER BY fph.date_id DESC
            LIMIT 1
        ) latest ON true
        ORDER BY c.name
        """,
        {"platform": _PLATFORM},
    ).fetchall()


def get_collection_value_history(conn) -> list[dict]:
    # Exclut les lignes cost_unknown (voir get_owned_cards ci-dessus) : la
    # spec (docs/superpowers/specs/2026-08-13-custom-dashboard-design.md)
    # demande explicitement de ne pas les inclure, pour ne pas fausser la
    # courbe de valeur avec des cartes dont le coût réel n'est simplement pas
    # connu (même biais que celui déjà identifié sur le dashboard Metabase).
    return conn.execute(
        """
        SELECT fph.date_id, SUM(o.quantity * fph.average_sell_price) AS total_value
        FROM prod.dim_owned_card o
        JOIN prod.fact_price_history fph ON fph.card_id = o.card_id
        JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
        WHERE p.platform_name = %(platform)s
          AND o.average_cost_paid IS NOT NULL AND o.average_cost_paid != 0
        GROUP BY fph.date_id
        ORDER BY fph.date_id
        """,
        {"platform": _PLATFORM},
    ).fetchall()
```

- [ ] **Step 6: Lancer les tests, vérifier qu'ils passent**

Run: `pytest tests/test_api_queries.py -v`
Expected: PASS (7 tests). Nécessite le rôle `dashboard_reader` déjà créé (migration 007, déjà appliquée en local et en prod).

- [ ] **Step 7: Commit**

```bash
git add src/api/__init__.py src/api/db.py src/api/queries.py tests/test_api_queries.py
git commit -m "feat: add read-only SQL queries for the dashboard API"
```

---

### Task 3: Schémas Pydantic

**Files:**
- Create: `src/api/schemas.py`

**Interfaces:**
- Consumes: rien (types purs).
- Produces: `CardSummary`, `CardListResponse`, `PricePoint`, `CardHistoryResponse`, `OwnedCard`, `CollectionResponse`, `CollectionValuePoint` — utilisés par Task 5 (`src/api/main.py`).

Pas de TDD ici : ce sont des déclarations de types Pydantic sans logique propre (leur seule vérification utile est celle, indirecte, faite par les tests d'API de la Task 5, qui échoueraient si un champ était mal typé).

- [ ] **Step 1: Créer `src/api/schemas.py`**

```python
# Modèles Pydantic des réponses de l'API. FastAPI s'en sert pour valider et
# sérialiser automatiquement les réponses JSON (response_model= sur chaque
# endpoint, voir src/api/main.py) -- si un endpoint renvoyait un champ
# manquant ou mal typé, FastAPI lèverait une erreur explicite plutôt que de
# laisser passer une réponse JSON incohérente vers le frontend.
from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class CardSummary(BaseModel):
    card_id: str
    name: str
    series: str | None
    set_name: str
    rarity: str | None
    current_price: float | None


class CardListResponse(BaseModel):
    items: list[CardSummary]
    total: int
    page: int
    page_size: int


class PricePoint(BaseModel):
    date_id: date
    average_sell_price: float | None
    trend_price: float | None
    low_price: float | None


class CardHistoryResponse(BaseModel):
    card_id: str
    name: str
    history: list[PricePoint]


class OwnedCard(BaseModel):
    id: int
    card_id: str
    name: str
    series: str | None
    set_name: str
    variance: str
    grade: str
    quantity: int
    average_cost_paid: float | None
    cost_unknown: bool
    current_price: float | None
    market_value: float | None
    gain_loss: float | None


class CollectionResponse(BaseModel):
    items: list[OwnedCard]


class CollectionValuePoint(BaseModel):
    date_id: date
    total_value: float
```

- [ ] **Step 2: Commit**

```bash
git add src/api/schemas.py
git commit -m "feat: add Pydantic response schemas for the dashboard API"
```

---

### Task 4: Endpoints FastAPI (`src/api/main.py`)

**Files:**
- Create: `src/api/main.py`
- Test: `tests/test_api_main.py`

**Interfaces:**
- Consumes: `get_api_connection` (Task 2), `search_cards`/`get_card_history`/`get_owned_cards`/`get_collection_value_history` (Task 2), tous les modèles de Task 3.
- Produces: `app` (instance FastAPI) — consommée par le déploiement Docker (Task 5).

- [ ] **Step 1: Écrire les tests qui échouent**

Créer `tests/test_api_main.py` :
```python
# Tests de contrat des endpoints, via TestClient (httpx interne, pas de vrai
# serveur réseau). La dépendance get_api_connection est surchargée pour
# pointer vers la même base de test que tests/test_api_queries.py -- ces
# tests vérifient le CÂBLAGE (routes, codes HTTP, sérialisation JSON), pas la
# logique SQL déjà couverte par test_api_queries.py.
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from src.api.db import get_api_connection
from src.api.main import app


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


def _dashboard_reader_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user=dashboard_reader "
        f"password={os.environ['DASHBOARD_READER_PASSWORD']}"
    )


@pytest.fixture
def client():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE prod.dim_owned_card, prod.fact_price_history, "
            "prod.dim_card RESTART IDENTITY CASCADE;"
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES ('base1-1', 'Alakazam', 'base1', 'Base Set', 'Rare Holo', 'Base')"
        )
        platform_id = admin_conn.execute(
            "SELECT platform_id FROM prod.dim_platform WHERE platform_name = 'tcgplayer'"
        ).fetchone()[0]
        admin_conn.execute(
            "INSERT INTO prod.fact_price_history "
            "(card_id, date_id, platform_id, average_sell_price, trend_price, low_price) "
            "VALUES ('base1-1', %s, %s, 10.00, 11.00, 9.00)",
            (date(2026, 8, 1), platform_id),
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity, average_cost_paid) "
            "VALUES ('base1-1', 'Normal', '', 2, 8.00)"
        )
        admin_conn.commit()

    test_conn = psycopg.connect(_dashboard_reader_dsn(), row_factory=dict_row)

    def _override_connection():
        yield test_conn

    app.dependency_overrides[get_api_connection] = _override_connection
    yield TestClient(app)
    app.dependency_overrides.clear()
    test_conn.close()


def test_health_check(client):
    response = client.get("/api/health")
    assert response.status_code == 200


def test_list_cards(client):
    response = client.get("/api/cards", params={"search": "Alakazam"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["card_id"] == "base1-1"


def test_list_cards_rejects_inverted_price_range(client):
    response = client.get("/api/cards", params={"price_min": 100, "price_max": 10})
    assert response.status_code == 422


def test_card_history_not_found(client):
    response = client.get("/api/cards/does-not-exist/history")
    assert response.status_code == 404


def test_card_history_found(client):
    response = client.get("/api/cards/base1-1/history")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Alakazam"
    assert len(body["history"]) == 1


def test_collection_computes_market_value_and_gain_loss(client):
    response = client.get("/api/collection")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["market_value"] == 20.00  # 2 * 10.00
    assert item["gain_loss"] == 4.00  # 2 * (10.00 - 8.00)


def test_collection_value_history(client):
    response = client.get("/api/collection/value-history")
    assert response.status_code == 200
    assert response.json() == [{"date_id": "2026-08-01", "total_value": 20.00}]
```

- [ ] **Step 2: Lancer les tests, vérifier qu'ils échouent**

Run: `pytest tests/test_api_main.py -v`
Expected: FAIL avec `ModuleNotFoundError: No module named 'src.api.main'`

- [ ] **Step 3: Implémenter `src/api/main.py`**

```python
# Application FastAPI du dashboard sur mesure. Chaque endpoint : (1) valide
# ses paramètres, (2) délègue la requête SQL à src/api/queries.py, (3)
# calcule les champs dérivés simples (market_value, gain_loss) et sérialise
# via les modèles de src/api/schemas.py. Aucune écriture n'est possible ici :
# la connexion (get_api_connection) utilise le rôle dashboard_reader,
# lecture seule au niveau Postgres lui-même (migration 007).
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query

from src.api import queries
from src.api.db import get_api_connection
from src.api.schemas import (
    CardHistoryResponse,
    CardListResponse,
    CardSummary,
    CollectionResponse,
    CollectionValuePoint,
    OwnedCard,
    PricePoint,
)

app = FastAPI(title="Card Price Tracker — Dashboard API")


@app.get("/api/health")
def health_check() -> dict:
    # Utilisé par le healthcheck Docker du service dashboard-api (voir
    # docker-compose.prod.yml, Task 5) -- ne touche pas la base de données :
    # un problème de connexion Postgres ne doit pas faire passer le conteneur
    # "unhealthy" alors que le processus FastAPI lui-même tourne normalement.
    return {"status": "ok"}


@app.get("/api/cards", response_model=CardListResponse)
def list_cards(
    conn=Depends(get_api_connection),
    search: str | None = None,
    series: str | None = None,
    set_name: str | None = None,
    rarity: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    page: int = Query(default=1, ge=1),
) -> CardListResponse:
    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=422, detail="price_min doit être <= price_max")
    page_size = 25
    rows, total = queries.search_cards(
        conn,
        search=search,
        series=series,
        set_name=set_name,
        rarity=rarity,
        price_min=price_min,
        price_max=price_max,
        page=page,
        page_size=page_size,
    )
    items = [
        CardSummary(**{key: value for key, value in row.items() if key != "total_count"})
        for row in rows
    ]
    return CardListResponse(items=items, total=total, page=page, page_size=page_size)


@app.get("/api/cards/{card_id}/history", response_model=CardHistoryResponse)
def card_history(card_id: str, conn=Depends(get_api_connection)) -> CardHistoryResponse:
    result = queries.get_card_history(conn, card_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Carte inconnue")
    card, history = result
    return CardHistoryResponse(
        card_id=card["card_id"],
        name=card["name"],
        history=[PricePoint(**row) for row in history],
    )


@app.get("/api/collection", response_model=CollectionResponse)
def collection(conn=Depends(get_api_connection)) -> CollectionResponse:
    rows = queries.get_owned_cards(conn)
    items = []
    for row in rows:
        current_price = row["current_price"]
        cost_unknown = row["cost_unknown"]
        market_value = row["quantity"] * current_price if current_price is not None else None
        gain_loss = (
            row["quantity"] * (current_price - row["average_cost_paid"])
            if current_price is not None and not cost_unknown
            else None
        )
        items.append(OwnedCard(**row, market_value=market_value, gain_loss=gain_loss))
    return CollectionResponse(items=items)


@app.get("/api/collection/value-history", response_model=list[CollectionValuePoint])
def collection_value_history(conn=Depends(get_api_connection)) -> list[CollectionValuePoint]:
    rows = queries.get_collection_value_history(conn)
    return [CollectionValuePoint(**row) for row in rows]
```

- [ ] **Step 4: Lancer les tests, vérifier qu'ils passent**

Run: `pytest tests/test_api_main.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Lancer toute la suite de tests du repo**

Run: `pytest -v`
Expected: PASS (tous les tests existants + les nouveaux, aucune régression).

- [ ] **Step 6: Commit**

```bash
git add src/api/main.py tests/test_api_main.py
git commit -m "feat: add FastAPI endpoints for the dashboard API"
```

---

### Task 5: Déploiement Docker de l'API

**Files:**
- Create: `Dockerfile.api`
- Modify: `docker-compose.prod.yml`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `src/api/main.py:app` (Task 4).
- Produces: service Docker `dashboard-api`, port `127.0.0.1:8000`, consommé par le frontend (Task 6-10, proxy Nginx) et par toute vérification manuelle (`curl`).

- [ ] **Step 1: Créer `Dockerfile.api`**

```dockerfile
# Image de l'API FastAPI du dashboard (src/api/). Même logique que
# Dockerfile.airflow : seules les dépendances tierces sont installées dans
# l'image, le code (src/) arrive par volume monté (docker-compose.prod.yml)
# pour éviter de reconstruire l'image à chaque modification.
FROM python:3.11-slim

COPY pyproject.toml /app/pyproject.toml
WORKDIR /app

# Guillemets obligatoires sur chaque contrainte de version -- même piège que
# Dockerfile.airflow (RUN passe par "sh -c", ">=" non quoté serait interprété
# comme une redirection shell plutôt qu'une comparaison pip).
RUN pip install --no-cache-dir \
    "fastapi>=0.111" "uvicorn[standard]>=0.30" "psycopg[binary]>=3.1" "python-dotenv>=1.0"

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Ajouter le service `dashboard-api` à `docker-compose.prod.yml`**

Ajouter avant le bloc `volumes:` final :
```yaml
  # dashboard-api : API FastAPI en lecture seule pour le dashboard sur mesure
  # (voir docs/superpowers/specs/2026-08-13-custom-dashboard-design.md).
  # Se connecte via dashboard_reader (migration 007), jamais pipeline_app.
  dashboard-api:
    build:
      context: .
      dockerfile: Dockerfile.api
    depends_on:
      db:
        condition: service_healthy
    environment:
      POSTGRES_HOST: db
      POSTGRES_PORT: 5432
    volumes:
      - ./src:/app/src
      - ./.env:/app/.env:ro
    # 127.0.0.1 uniquement -- même pattern que airflow-webserver/metabase,
    # jamais exposé sur l'interface publique du VPS.
    ports:
      - "127.0.0.1:8000:8000"
    healthcheck:
      # Pas de curl dans l'image python:3.11-slim (contrairement à
      # metabase/metabase) -- urllib.request est dans la stdlib Python, évite
      # d'installer un paquet supplémentaire juste pour le healthcheck.
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 10s
    restart: unless-stopped
```

- [ ] **Step 3: Documenter la variable d'environnement réutilisée dans `.env.example`**

Dans `.env.example`, sous le commentaire existant `# Metabase (UI d'exploration...)`, ajouter une ligne de commentaire (la variable `DASHBOARD_READER_PASSWORD` existe déjà, réutilisée telle quelle) :
```
# DASHBOARD_READER_PASSWORD est aussi utilisée par l'API du dashboard sur
# mesure (dashboard-api, voir docker-compose.prod.yml) -- même rôle Postgres
# en lecture seule que Metabase, pas de nouvelle variable nécessaire.
```

- [ ] **Step 4: Vérifier localement que l'image se construit**

Run: `docker build -f Dockerfile.api -t card-tracker-dashboard-api-test .`
Expected: build réussi, aucune erreur pip.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile.api docker-compose.prod.yml .env.example
git commit -m "feat: add dashboard-api Docker service"
```

---

## Partie 2 — Frontend (React + Vite)

Pas de TDD ici (contrainte globale du plan : pas de tests frontend automatisés dans cette version) — chaque tâche se termine par une vérification manuelle explicite (`npm run dev` + navigateur) plutôt qu'une suite de tests.

### Task 6: Scaffold du projet React

**Files:**
- Create: `frontend/` (généré par Vite, puis modifié)
- Modify: `frontend/vite.config.ts`

**Interfaces:**
- Produces: structure de projet Vite dans `frontend/`, consommée par toutes les tâches suivantes.

- [ ] **Step 1: Générer le projet**

Run (depuis la racine du repo) :
```bash
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
npm install react-router-dom recharts
```
Expected: `frontend/` créé avec `package.json`, `src/App.tsx`, etc.

- [ ] **Step 2: Configurer le proxy de dev vers l'API locale**

Remplacer le contenu de `frontend/vite.config.ts` :
```typescript
// En dev (npm run dev), Vite sert le frontend sur le port 5173 et proxifie
// toute requête vers /api/... au serveur FastAPI local (uvicorn --reload,
// port 8000, lancé séparément) -- évite les soucis de CORS en dev sans avoir
// à configurer CORS côté FastAPI (inutile en prod, où Nginx fait ce même
// proxy à l'intérieur du réseau Docker, voir Task 10).
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
```

- [ ] **Step 3: Vérifier manuellement**

Run (deux terminaux) :
```bash
# Terminal 1, depuis la racine du repo
uvicorn src.api.main:app --reload --port 8000
# Terminal 2
cd frontend && npm run dev
```
Ouvrir `http://localhost:5173` dans un navigateur.
Expected : page Vite/React par défaut affichée, aucune erreur dans la console.

- [ ] **Step 4: Commit**

```bash
git add frontend
git commit -m "feat: scaffold React/Vite frontend project"
```

---

### Task 7: Client API et routage

**Files:**
- Create: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/pages/Catalogue.tsx` (placeholder minimal, complété Task 8)
- Create: `frontend/src/pages/DetailCarte.tsx` (placeholder minimal, complété Task 9)
- Create: `frontend/src/pages/MaCollection.tsx` (placeholder minimal, complété Task 10)

**Interfaces:**
- Produces: `fetchCards`, `fetchCardHistory`, `fetchCollection`, `fetchCollectionValueHistory` (`frontend/src/api/client.ts`), types `Card`, `CardHistory`, `OwnedCard`, `CollectionValuePoint` — consommés par Tasks 8-10.

- [ ] **Step 1: Créer `frontend/src/api/client.ts`**

```typescript
// Point d'entrée unique pour tous les appels HTTP vers l'API FastAPI
// (src/api/main.py côté backend). Les pages (src/pages/*.tsx) importent ces
// fonctions plutôt que d'appeler fetch() directement -- un seul endroit à
// modifier si l'URL de base ou le format des réponses change.

export interface Card {
  card_id: string
  name: string
  series: string | null
  set_name: string
  rarity: string | null
  current_price: number | null
}

export interface CardListResponse {
  items: Card[]
  total: number
  page: number
  page_size: number
}

export interface PricePoint {
  date_id: string
  average_sell_price: number | null
  trend_price: number | null
  low_price: number | null
}

export interface CardHistory {
  card_id: string
  name: string
  history: PricePoint[]
}

export interface OwnedCard {
  id: number
  card_id: string
  name: string
  series: string | null
  set_name: string
  variance: string
  grade: string
  quantity: number
  average_cost_paid: number | null
  cost_unknown: boolean
  current_price: number | null
  market_value: number | null
  gain_loss: number | null
}

export interface CollectionValuePoint {
  date_id: string
  total_value: number
}

export interface CardFilters {
  search?: string
  series?: string
  set_name?: string
  rarity?: string
  price_min?: number
  price_max?: number
  page?: number
}

// Construit la query string en ignorant les filtres non renseignés
// (undefined) -- évite d'envoyer "search=&series=" vides à l'API, qui les
// traiterait différemment de leur absence (voir _card_filters côté backend).
function buildQuery(filters: CardFilters): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== '') {
      params.set(key, String(value))
    }
  }
  return params.toString()
}

export async function fetchCards(filters: CardFilters): Promise<CardListResponse> {
  const response = await fetch(`/api/cards?${buildQuery(filters)}`)
  if (!response.ok) {
    throw new Error(`Erreur API /api/cards : ${response.status}`)
  }
  return response.json()
}

export async function fetchCardHistory(cardId: string): Promise<CardHistory> {
  const response = await fetch(`/api/cards/${encodeURIComponent(cardId)}/history`)
  if (!response.ok) {
    throw new Error(`Erreur API /api/cards/${cardId}/history : ${response.status}`)
  }
  return response.json()
}

export async function fetchCollection(): Promise<{ items: OwnedCard[] }> {
  const response = await fetch('/api/collection')
  if (!response.ok) {
    throw new Error(`Erreur API /api/collection : ${response.status}`)
  }
  return response.json()
}

export async function fetchCollectionValueHistory(): Promise<CollectionValuePoint[]> {
  const response = await fetch('/api/collection/value-history')
  if (!response.ok) {
    throw new Error(`Erreur API /api/collection/value-history : ${response.status}`)
  }
  return response.json()
}
```

- [ ] **Step 2: Créer les 3 pages en placeholder minimal**

`frontend/src/pages/Catalogue.tsx` :
```tsx
export function Catalogue() {
  return <h1>Catalogue</h1>
}
```

`frontend/src/pages/DetailCarte.tsx` :
```tsx
export function DetailCarte() {
  return <h1>Détail carte</h1>
}
```

`frontend/src/pages/MaCollection.tsx` :
```tsx
export function MaCollection() {
  return <h1>Ma collection</h1>
}
```

- [ ] **Step 3: Câbler le routage dans `frontend/src/App.tsx`**

```tsx
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import { Catalogue } from './pages/Catalogue'
import { DetailCarte } from './pages/DetailCarte'
import { MaCollection } from './pages/MaCollection'

export default function App() {
  return (
    <BrowserRouter>
      <nav>
        <Link to="/">Catalogue</Link>
        {' | '}
        <Link to="/collection">Ma collection</Link>
      </nav>
      <Routes>
        <Route path="/" element={<Catalogue />} />
        <Route path="/cartes/:cardId" element={<DetailCarte />} />
        <Route path="/collection" element={<MaCollection />} />
      </Routes>
    </BrowserRouter>
  )
}
```

- [ ] **Step 4: Vérifier manuellement**

Avec les deux serveurs de dev toujours lancés (Task 6, Step 3), naviguer entre `http://localhost:5173/` et `http://localhost:5173/collection` via les liens de la barre de navigation.
Expected : les 2 titres placeholder s'affichent, l'URL change, aucune erreur console.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat: add API client and page routing"
```

---

### Task 8: Vue Catalogue

**Files:**
- Modify: `frontend/src/pages/Catalogue.tsx`

**Interfaces:**
- Consumes: `fetchCards`, `Card`, `CardFilters` (Task 7).

- [ ] **Step 1: Implémenter la recherche, les filtres et le tableau**

```tsx
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, CardFilters, fetchCards } from '../api/client'

export function Catalogue() {
  const [filters, setFilters] = useState<CardFilters>({ page: 1 })
  const [cards, setCards] = useState<Card[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)

  // Se redéclenche à chaque changement de filtre ou de page -- pas de
  // debounce sur la recherche texte dans cette première version (YAGNI :
  // 19 545 cartes, une requête filtrée reste rapide, à revisiter seulement
  // si un vrai ralentissement est observé).
  useEffect(() => {
    fetchCards(filters)
      .then((response) => {
        setCards(response.items)
        setTotal(response.total)
        setError(null)
      })
      .catch((err) => setError(String(err)))
  }, [filters])

  function updateFilter<K extends keyof CardFilters>(key: K, value: CardFilters[K]) {
    setFilters((prev) => ({ ...prev, [key]: value, page: 1 }))
  }

  return (
    <div>
      <h1>Catalogue</h1>
      <input
        placeholder="Rechercher une carte..."
        onChange={(e) => updateFilter('search', e.target.value)}
      />
      <input
        placeholder="Bloc"
        onChange={(e) => updateFilter('series', e.target.value)}
      />
      <input
        placeholder="Série"
        onChange={(e) => updateFilter('set_name', e.target.value)}
      />
      <input
        placeholder="Rareté"
        onChange={(e) => updateFilter('rarity', e.target.value)}
      />
      <input
        type="number"
        placeholder="Prix min"
        onChange={(e) => updateFilter('price_min', e.target.value ? Number(e.target.value) : undefined)}
      />
      <input
        type="number"
        placeholder="Prix max"
        onChange={(e) => updateFilter('price_max', e.target.value ? Number(e.target.value) : undefined)}
      />

      {error && <p role="alert">{error}</p>}

      <table>
        <thead>
          <tr>
            <th>Nom</th>
            <th>Bloc</th>
            <th>Série</th>
            <th>Rareté</th>
            <th>Prix</th>
          </tr>
        </thead>
        <tbody>
          {cards.map((card) => (
            <tr key={card.card_id}>
              <td>
                <Link to={`/cartes/${card.card_id}`}>{card.name}</Link>
              </td>
              <td>{card.series ?? '—'}</td>
              <td>{card.set_name}</td>
              <td>{card.rarity ?? '—'}</td>
              <td>{card.current_price !== null ? `$${card.current_price.toFixed(2)}` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <p>{total} résultat(s)</p>
      <button disabled={(filters.page ?? 1) <= 1} onClick={() => updateFilter('page', (filters.page ?? 1) - 1)}>
        Précédent
      </button>
      <button onClick={() => updateFilter('page', (filters.page ?? 1) + 1)}>Suivant</button>
    </div>
  )
}
```

- [ ] **Step 2: Vérifier manuellement**

Avec les serveurs de dev lancés (backend rempli d'au moins quelques cartes réelles — pointer `POSTGRES_HOST`/`DASHBOARD_READER_PASSWORD` du `.env` local vers la base de dev déjà peuplée) : ouvrir `http://localhost:5173/`, taper un nom de carte connu dans la recherche, vérifier que le tableau se filtre ; tester un filtre de prix ; cliquer sur une carte et vérifier la navigation vers `/cartes/<id>`.
Expected : recherche et filtres renvoient des résultats cohérents avec `psql`, pagination fonctionnelle.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/Catalogue.tsx
git commit -m "feat: implement Catalogue view (search, filters, pagination)"
```

---

### Task 9: Vue Détail carte

**Files:**
- Modify: `frontend/src/pages/DetailCarte.tsx`

**Interfaces:**
- Consumes: `fetchCardHistory`, `CardHistory` (Task 7).

- [ ] **Step 1: Implémenter la fiche + le graphe d'évolution**

```tsx
import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { CardHistory, fetchCardHistory } from '../api/client'

export function DetailCarte() {
  const { cardId } = useParams<{ cardId: string }>()
  const [history, setHistory] = useState<CardHistory | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!cardId) return
    fetchCardHistory(cardId)
      .then((data) => {
        setHistory(data)
        setError(null)
      })
      .catch((err) => setError(String(err)))
  }, [cardId])

  if (error) return <p role="alert">{error}</p>
  if (!history) return <p>Chargement...</p>

  return (
    <div>
      <h1>{history.name}</h1>
      <ResponsiveContainer width="100%" height={400}>
        <LineChart data={history.history}>
          <XAxis dataKey="date_id" />
          <YAxis />
          <Tooltip />
          <Line type="monotone" dataKey="average_sell_price" name="Prix moyen" stroke="#2563eb" />
          <Line type="monotone" dataKey="trend_price" name="Tendance" stroke="#16a34a" />
          <Line type="monotone" dataKey="low_price" name="Prix bas" stroke="#dc2626" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
```

- [ ] **Step 2: Vérifier manuellement**

Depuis la vue Catalogue (Task 8), cliquer sur une carte ayant plusieurs jours d'historique en base.
Expected : le graphe affiche 3 courbes (prix moyen/tendance/bas), l'axe X montre les dates, le survol affiche les valeurs (tooltip Recharts).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/DetailCarte.tsx
git commit -m "feat: implement card detail view with price history chart"
```

---

### Task 10: Vue Ma collection

**Files:**
- Modify: `frontend/src/pages/MaCollection.tsx`

**Interfaces:**
- Consumes: `fetchCollection`, `fetchCollectionValueHistory`, `OwnedCard`, `CollectionValuePoint` (Task 7).

- [ ] **Step 1: Implémenter le tableau + le graphe de valeur**

```tsx
import { useEffect, useState } from 'react'
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { CollectionValuePoint, OwnedCard, fetchCollection, fetchCollectionValueHistory } from '../api/client'

export function MaCollection() {
  const [items, setItems] = useState<OwnedCard[]>([])
  const [valueHistory, setValueHistory] = useState<CollectionValuePoint[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([fetchCollection(), fetchCollectionValueHistory()])
      .then(([collection, history]) => {
        setItems(collection.items)
        setValueHistory(history)
        setError(null)
      })
      .catch((err) => setError(String(err)))
  }, [])

  if (error) return <p role="alert">{error}</p>

  // Valeur totale actuelle : uniquement les cartes au coût connu (voir
  // cost_unknown) pour rester cohérent avec le graphe de value-history
  // (qui applique déjà exactement ce filtre côté API).
  const knownCostItems = items.filter((item) => !item.cost_unknown)
  const totalValue = knownCostItems.reduce((sum, item) => sum + (item.market_value ?? 0), 0)
  const totalGainLoss = knownCostItems.reduce((sum, item) => sum + (item.gain_loss ?? 0), 0)
  const unknownCostCount = items.length - knownCostItems.length

  return (
    <div>
      <h1>Ma collection</h1>
      <p>
        Valeur totale : ${totalValue.toFixed(2)} (plus/moins-value : ${totalGainLoss.toFixed(2)})
      </p>
      {unknownCostCount > 0 && (
        <p role="note">
          {unknownCostCount} carte(s) au coût d'achat inconnu, exclue(s) de ce calcul.
        </p>
      )}

      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={valueHistory}>
          <XAxis dataKey="date_id" />
          <YAxis />
          <Tooltip />
          <Line type="monotone" dataKey="total_value" name="Valeur totale" stroke="#2563eb" />
        </LineChart>
      </ResponsiveContainer>

      <table>
        <thead>
          <tr>
            <th>Nom</th>
            <th>Quantité</th>
            <th>Coût moyen</th>
            <th>Prix actuel</th>
            <th>Valeur</th>
            <th>Plus/moins-value</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td>{item.name}</td>
              <td>{item.quantity}</td>
              <td>{item.cost_unknown ? 'inconnu' : `$${item.average_cost_paid?.toFixed(2)}`}</td>
              <td>{item.current_price !== null ? `$${item.current_price.toFixed(2)}` : '—'}</td>
              <td>{item.market_value !== null ? `$${item.market_value.toFixed(2)}` : '—'}</td>
              <td>{item.gain_loss !== null ? `$${item.gain_loss.toFixed(2)}` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 2: Vérifier manuellement**

Ouvrir `http://localhost:5173/collection`.
Expected : le tableau liste les cartes possédées (562 en prod), les lignes à coût inconnu affichent "inconnu" et sont comptées dans le message d'avertissement, le graphe de valeur affiche une courbe croissante avec l'historique de prix, la valeur totale affichée correspond à un calcul manuel sur un échantillon (`psql`).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/MaCollection.tsx
git commit -m "feat: implement Ma collection view (value chart + owned cards table)"
```

---

### Task 11: Déploiement Docker du frontend

**Files:**
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Create: `frontend/.dockerignore`
- Modify: `docker-compose.prod.yml`

**Interfaces:**
- Consumes: `frontend/` buildable (Tasks 6-10), service `dashboard-api` (Task 5, résolu par son nom de service Docker `dashboard-api` dans le réseau interne Compose).
- Produces: service Docker `dashboard-frontend`, port `127.0.0.1:5173`.

- [ ] **Step 1: Créer `frontend/.dockerignore`**

```
node_modules
dist
```

- [ ] **Step 2: Créer `frontend/nginx.conf`**

```nginx
# Sert le build statique React (dist/) et proxifie /api/ vers le conteneur
# dashboard-api -- "dashboard-api" est résolu par le DNS interne de Docker
# Compose (nom du service), pas besoin d'IP en dur ni de variable d'env ici.
server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;

    location /api/ {
        proxy_pass http://dashboard-api:8000/api/;
    }

    # try_files ... /index.html : nécessaire pour un routeur côté client
    # (react-router-dom, Task 7) -- sans ça, un accès direct à /collection ou
    # /cartes/xyz renverrait un 404 Nginx (le fichier collection/xyz n'existe
    # pas sur le disque, seul index.html + JS sait router ces chemins).
    location / {
        try_files $uri /index.html;
    }
}
```

- [ ] **Step 3: Créer `frontend/Dockerfile`**

```dockerfile
# Build multi-étapes : la première étape (node) compile le code
# TypeScript/React en fichiers statiques ; la seconde (nginx) ne contient QUE
# ce résultat compilé -- l'image finale ne contient ni Node.js ni les
# dépendances npm, seulement du HTML/JS/CSS servi par Nginx.
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
```

- [ ] **Step 4: Ajouter le service `dashboard-frontend` à `docker-compose.prod.yml`**

Ajouter juste après le service `dashboard-api` (avant le bloc `volumes:` final) :
```yaml
  # dashboard-frontend : build React statique servi par Nginx, proxy /api/
  # vers dashboard-api (voir frontend/nginx.conf). Port 5173 choisi pour ne
  # pas entrer en conflit avec le port de dev Vite (aussi 5173, mais jamais
  # utilisé en même temps que ce conteneur sur la même machine).
  dashboard-frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    depends_on:
      - dashboard-api
    ports:
      - "127.0.0.1:5173:80"
    restart: unless-stopped
```

- [ ] **Step 5: Vérifier localement que l'image se construit**

Run: `docker build -t card-tracker-dashboard-frontend-test ./frontend`
Expected : build réussi (étape `npm run build` sans erreur TypeScript, image Nginx finale créée).

- [ ] **Step 6: Commit**

```bash
git add frontend/Dockerfile frontend/nginx.conf frontend/.dockerignore docker-compose.prod.yml
git commit -m "feat: add dashboard-frontend Docker service"
```

---

### Task 12: Déploiement et vérification en production

**Files:** aucun (déploiement, pas de code)

**Interfaces:**
- Consumes : services `dashboard-api` et `dashboard-frontend` (Tasks 5, 11), déjà validés localement.

- [ ] **Step 1: Pousser la branche et déployer sur le VPS**

Depuis la machine locale :
```bash
git push origin custom-dashboard
```
Sur le VPS (`ssh card-tracker-vm`), dans `/home/ubuntu/card-price-tracker` :
```bash
git fetch origin
git checkout custom-dashboard
git pull origin custom-dashboard
docker compose -f docker-compose.prod.yml up -d --build dashboard-api dashboard-frontend
```
Expected : les deux nouveaux conteneurs démarrent, `docker compose -f docker-compose.prod.yml ps` les montre `healthy`/`running`.

- [ ] **Step 2: Vérifier l'API en prod**

Sur le VPS :
```bash
curl -s http://localhost:8000/api/health
curl -s "http://localhost:8000/api/cards?search=Pikachu" | head -c 500
```
Expected : `{"status":"ok"}`, puis une liste JSON de cartes cohérente avec `psql`.

- [ ] **Step 3: Vérifier le frontend via tunnel SSH**

Depuis la machine locale :
```bash
ssh -L 5173:localhost:5173 card-tracker-vm
```
Puis ouvrir `http://localhost:5173` dans un navigateur.
Expected : les 3 vues (Catalogue, Détail carte, Ma collection) s'affichent avec les vraies données de prod, recherche/filtres fonctionnels, graphes rendus.

- [ ] **Step 4: Merger dans `main`**

Une fois la vérification en prod concluante :
```bash
git push origin custom-dashboard
gh pr create --base main --head custom-dashboard \
  --title "Dashboard sur mesure (API FastAPI + frontend React)" \
  --body "Voir docs/superpowers/specs/2026-08-13-custom-dashboard-design.md. Vues Catalogue, Détail carte, Ma collection, vérifiées en prod via tunnel SSH."
```
Attendre la revue/décision de l'utilisateur avant tout `gh pr merge` (ne pas merger automatiquement cette PR-ci — contrairement à la PR #2, l'utilisateur n'a pas encore donné cette autorisation pour ce travail).
