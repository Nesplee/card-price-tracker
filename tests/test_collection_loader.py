# Tests d'intégration du chargement de la collection -- comme
# tests/test_staging_loader.py, ces tests touchent une vraie base Postgres
# (nécessite `docker compose up -d db`), car ils vérifient un comportement
# SQL réel (UPSERT, contraintes UNIQUE) qu'un mock ne peut pas garantir.
from __future__ import annotations

import os

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.collection_loader import load_match_quarantine, load_owned_cards, load_raw_import
from src.transform.collection_match import CollectionRow, MatchResult


def _admin_dsn() -> str:
    # Même pattern que tests/test_idempotence.py : DSN admin (pas
    # pipeline_app) nécessaire pour TRUNCATE ... CASCADE ci-dessous, une
    # opération que le rôle applicatif least-privilege n'a pas le droit
    # d'exécuter.
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE collection.raw_import, collection.match_quarantine, "
            "prod.dim_owned_card, prod.dim_card RESTART IDENTITY CASCADE;"
        )
        admin_conn.commit()
        # dim_card a besoin d'au moins une carte connue pour tester un
        # rapprochement réussi ci-dessous.
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity) "
            "VALUES ('swsh6-132', 'Caitlin', 'swsh6', 'Chilling Reign', 'Ultra Rare');"
        )
        admin_conn.commit()

    with get_connection(load_db_config()) as conn:
        yield conn


def _row(**overrides) -> CollectionRow:
    defaults = dict(
        set_name="Chilling Reign",
        card_number="132/198",
        product_name="Caitlin",
        variance="Holofoil",
        grade="Ungraded",
        rarity="Ultra Rare",
        quantity=1,
        average_cost_paid=5.0,
        market_price_at_export=6.0,
    )
    defaults.update(overrides)
    return CollectionRow(**defaults)


def test_load_raw_import_is_idempotent(db_connection) -> None:
    row = _row()

    ids_first = load_raw_import(db_connection, [row])
    # Rejoue avec une quantité différente : même clé naturelle (set/numéro/
    # produit/variante/grade inchangés) -> doit mettre à jour la même ligne,
    # pas en créer une deuxième.
    ids_second = load_raw_import(db_connection, [_row(quantity=3)])

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM collection.raw_import;")
        (count,) = cur.fetchone()
        cur.execute("SELECT quantity FROM collection.raw_import WHERE id = %s;", (ids_first[0],))
        (quantity,) = cur.fetchone()

    assert count == 1
    assert ids_first == ids_second
    assert quantity == 3


def test_load_owned_cards_inserts_matched_row(db_connection) -> None:
    row = _row()
    (raw_id,) = load_raw_import(db_connection, [row])
    match = MatchResult(row=row, card_id="swsh6-132", rejection_reason=None)

    loaded = load_owned_cards(db_connection, [(raw_id, match)])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT card_id, quantity, average_cost_paid FROM prod.dim_owned_card "
            "WHERE card_id = %s;",
            ("swsh6-132",),
        )
        result = cur.fetchone()

    assert loaded == 1
    assert result == ("swsh6-132", 1, 5.0)


def test_load_owned_cards_is_idempotent(db_connection) -> None:
    row = _row()
    (raw_id,) = load_raw_import(db_connection, [row])
    match = MatchResult(row=row, card_id="swsh6-132", rejection_reason=None)

    load_owned_cards(db_connection, [(raw_id, match)])
    load_owned_cards(
        db_connection,
        [(raw_id, MatchResult(row=_row(quantity=4), card_id="swsh6-132", rejection_reason=None))],
    )

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM prod.dim_owned_card;")
        (count,) = cur.fetchone()
        cur.execute("SELECT quantity FROM prod.dim_owned_card WHERE card_id = %s;", ("swsh6-132",))
        (quantity,) = cur.fetchone()

    assert count == 1
    assert quantity == 4


def test_load_match_quarantine_records_unmatched_row(db_connection) -> None:
    row = _row(set_name="Set Inconnu")
    (raw_id,) = load_raw_import(db_connection, [row])
    match = MatchResult(row=row, card_id=None, rejection_reason="set inconnu : 'Set Inconnu'")

    loaded = load_match_quarantine(db_connection, [(raw_id, match)])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT rejection_reason FROM collection.match_quarantine WHERE raw_import_id = %s;",
            (raw_id,),
        )
        (reason,) = cur.fetchone()

    assert loaded == 1
    assert reason == "set inconnu : 'Set Inconnu'"
