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

# Import from src.common.config triggers load_dotenv() at module import time,
# which populates os.environ with .env variables before tests try to access them.
from src.common.config import load_dashboard_reader_config  # noqa: F401

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
        # Insert test dates into dim_date if they don't already exist
        # dim_date has columns: date_id, year, month, day, day_of_week
        date_1 = date(2026, 8, 1)
        date_2 = date(2026, 8, 2)
        admin_conn.execute(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s) "
            "ON CONFLICT (date_id) DO NOTHING",
            (date_1, 2026, 8, 1, date_1.weekday(),
             date_2, 2026, 8, 2, date_2.weekday()),
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


def test_get_owned_cards_includes_rarity(db_connection):
    # Nécessaire pour afficher le point de couleur par rareté (signature
    # visuelle du dashboard) sur la vue "Ma collection", comme sur le
    # Catalogue.
    rows = get_owned_cards(db_connection)
    by_card = {row["card_id"]: row for row in rows}
    assert by_card["base1-1"]["rarity"] == "Rare Holo"


def test_collection_value_history_excludes_unknown_cost_rows(db_connection):
    # base1-2 (coût 0 = inconnu) ne doit jamais entrer dans l'agrégat de
    # valeur -- seule base1-1 (coût connu, quantity=2) doit compter.
    rows = get_collection_value_history(db_connection)
    by_date = {row["date_id"]: float(row["total_value"]) for row in rows}
    assert by_date[date(2026, 8, 1)] == 20.00  # 2 * 10.00
    assert by_date[date(2026, 8, 2)] == 24.00  # 2 * 12.00


def test_search_cards_includes_priceless_cards(db_connection):
    # Une nouvelle carte (base1-3) sans aucune observation de prix tcgplayer
    # doit quand même apparaître dans les résultats de recherche, avec
    # current_price = None. Cela teste la correction LEFT JOIN LATERAL.
    with psycopg.connect(_admin_dsn()) as admin_conn:
        # Ajouter une carte sans prix
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES ('base1-3', 'Charizard', 'base1', 'Base Set', 'Rare Holo', 'Base')"
        )
        admin_conn.commit()

    rows, total = search_cards(db_connection)
    assert total == 3  # Alakazam, Blastoise, Charizard
    by_card = {row["card_id"]: row for row in rows}
    assert "base1-3" in by_card
    assert by_card["base1-3"]["current_price"] is None


def test_get_owned_cards_includes_priceless_owned_card(db_connection):
    # Une carte possédée sans observation de prix tcgplayer doit quand même
    # apparaître dans "Ma Collection", avec current_price = None.
    # Cela teste la correction LEFT JOIN LATERAL dans get_owned_cards().
    with psycopg.connect(_admin_dsn()) as admin_conn:
        # Ajouter une carte et l'ajouter à la collection, mais sans prix
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES ('base1-4', 'Venusaur', 'base1', 'Base Set', 'Rare Holo', 'Base')"
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity, average_cost_paid) "
            "VALUES ('base1-4', 'Normal', '', 1, 15.00)"
        )
        admin_conn.commit()

    rows = get_owned_cards(db_connection)
    by_card = {row["card_id"]: row for row in rows}
    assert "base1-4" in by_card
    assert by_card["base1-4"]["current_price"] is None
    assert by_card["base1-4"]["average_cost_paid"] == 15.00


def test_search_cards_rarity_filter_matches_partially(db_connection):
    # Retour utilisateur : chercher "Illustration" doit trouver une carte de
    # rareté "Illustration Rare" -- une correspondance exacte (c.rarity =
    # %(rarity)s) ne le permettait pas. Même logique que le filtre "search"
    # sur le nom, déjà en ILIKE '%...%'.
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES ('sv1-1', 'Sprigatito', 'sv1', 'Scarlet & Violet', "
            "'Illustration Rare', 'Scarlet & Violet')"
        )
        admin_conn.commit()

    rows, total = search_cards(db_connection, rarity="Illustration")
    assert total == 1
    assert rows[0]["card_id"] == "sv1-1"


def test_search_cards_series_and_set_name_filters_match_partially(db_connection):
    # Même logique que le filtre rareté ci-dessus : "Base" doit trouver
    # "Base Set" (série ou nom de set), pas seulement une égalité stricte.
    rows, total = search_cards(db_connection, series="Bas", set_name="Base S")
    assert total == 2  # Alakazam et Blastoise, tous deux série "Base"/set "Base Set"


def test_get_owned_cards_filters_by_search_and_rarity(db_connection):
    # "Ma collection" doit pouvoir se filtrer comme le Catalogue -- même
    # comportement de correspondance partielle sur rarity/series/set_name.
    rows = get_owned_cards(db_connection, search="Alakazam")
    assert [row["card_id"] for row in rows] == ["base1-1"]

    rows = get_owned_cards(db_connection, rarity="Rare Holo")
    assert {row["card_id"] for row in rows} == {"base1-1", "base1-2"}

    rows = get_owned_cards(db_connection, price_min=40)
    assert [row["card_id"] for row in rows] == ["base1-2"]


def test_search_cards_pagination_total_correct_on_out_of_range_page(db_connection):
    # Avec 3 résultats totaux et page_size=2, il y a 2 pages.
    # Demander page=5 doit quand même retourner total=3 (pas 0), même
    # si rows est vide. Cela teste la correction : deux requêtes séparées
    # pour count et résultats paginés.
    with psycopg.connect(_admin_dsn()) as admin_conn:
        # Ajouter 2 cartes supplémentaires pour avoir 4 au total
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES ('base1-3', 'Charizard', 'base1', 'Base Set', 'Rare Holo', 'Base'), "
            "('base1-4', 'Venusaur', 'base1', 'Base Set', 'Rare Holo', 'Base')"
        )
        admin_conn.commit()

    # Page 1 avec page_size=2
    rows_page1, total = search_cards(db_connection, page=1, page_size=2)
    assert len(rows_page1) == 2
    assert total == 4  # Alakazam, Blastoise, Charizard, Venusaur

    # Page 3 avec page_size=2 (hors limites: offset=4, il n'y a que 4 cartes)
    rows_page3, total = search_cards(db_connection, page=3, page_size=2)
    assert len(rows_page3) == 0  # Pas de résultat à cette page
    assert total == 4  # Mais le total doit quand même être correct
