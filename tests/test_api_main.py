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
        admin_conn.execute(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (date_id) DO NOTHING",
            (date(2026, 8, 1), 2026, 8, 1, date(2026, 8, 1).weekday()),
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
            "INSERT INTO prod.dim_owned_card "
            "(card_id, variance, grade, quantity, average_cost_paid) "
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


def test_collection_accepts_filters(client):
    # "Ma collection" doit se filtrer comme le Catalogue -- une carte qui ne
    # matche pas le filtre disparaît de la réponse.
    response = client.get("/api/collection", params={"search": "Alakazam"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1

    response = client.get("/api/collection", params={"search": "Charizard"})
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_collection_value_history(client):
    response = client.get("/api/collection/value-history")
    assert response.status_code == 200
    assert response.json() == [{"date_id": "2026-08-01", "total_value": 20.00}]


def test_reports_movers_rejects_invalid_window(client):
    response = client.get("/api/reports/movers", params={"window": 14})
    assert response.status_code == 422


def test_reports_movers_filters_sorts_and_flags_threshold(client):
    # Setup dédié : 3 cartes possédées sur 8 jours (2026-08-01 .. 2026-08-08).
    # - base1-1 : 10.00 -> 15.00 sur les 8 jours = +50% (franchit le palier 30)
    # - base1-2 : seulement 3 observations (06,07,08) -> pas de rang 8,
    #   donc past_price=None pour window=7 -- doit être EXCLUE de la réponse.
    # - base1-3 : 10.00 -> 10.30 = +3% -- sous le seuil de 10%, doit être EXCLUE.
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "SELECT d, EXTRACT(YEAR FROM d)::int, EXTRACT(MONTH FROM d)::int, "
            "EXTRACT(DAY FROM d)::int, EXTRACT(DOW FROM d)::int "
            "FROM generate_series(date '2026-08-01', date '2026-08-08', interval '1 day') AS d "
            "ON CONFLICT (date_id) DO NOTHING"
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES "
            "('base1-2', 'Blastoise', 'base1', 'Base Set', 'Rare Holo', 'Base'), "
            "('base1-3', 'Charizard', 'base1', 'Base Set', 'Rare Holo', 'Base')"
        )
        platform_id = admin_conn.execute(
            "SELECT platform_id FROM prod.dim_platform WHERE platform_name = 'tcgplayer'"
        ).fetchone()[0]
        admin_conn.execute(
            "INSERT INTO prod.fact_price_history "
            "(card_id, date_id, platform_id, average_sell_price, trend_price, low_price) "
            "SELECT 'base1-1', d, %(platform_id)s, "
            "  CASE WHEN d = date '2026-08-08' THEN 15.00 ELSE 10.00 END, NULL::numeric, NULL::numeric "
            "FROM generate_series(date '2026-08-02', date '2026-08-08', interval '1 day') AS d "
            "UNION ALL "
            "SELECT 'base1-2', d, %(platform_id)s, 20.00, NULL::numeric, NULL::numeric "
            "FROM generate_series(date '2026-08-06', date '2026-08-08', interval '1 day') AS d "
            "UNION ALL "
            "SELECT 'base1-3', d, %(platform_id)s, "
            "  CASE WHEN d = date '2026-08-08' THEN 10.30 ELSE 10.00 END, NULL::numeric, NULL::numeric "
            "FROM generate_series(date '2026-08-01', date '2026-08-08', interval '1 day') AS d",
            {"platform_id": platform_id},
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity, average_cost_paid) "
            "VALUES "
            "('base1-2', 'Normal', '', 1, 5.00), "
            "('base1-3', 'Normal', '', 1, 10.00)"
        )
        admin_conn.commit()

    response = client.get("/api/reports/movers", params={"window": 7})
    assert response.status_code == 200
    movers = response.json()

    card_ids = [m["card_id"] for m in movers]
    assert card_ids == ["base1-1"]  # base1-2 (past manquant) et base1-3 (<10%) exclues

    mover = movers[0]
    assert mover["current_price"] == 15.00
    assert mover["past_price"] == 10.00
    assert mover["pct_change"] == 50.0
    assert mover["threshold"] == 30
    assert mover["quantity"] == 2
