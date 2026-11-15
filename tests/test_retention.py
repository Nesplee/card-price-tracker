# Tests d'intégration de la rétention glissante (migration 016,
# src/maintenance/retention.py) contre un vrai Postgres jetable.
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.maintenance.retention import (
    RETENTION_TABLES,
    purge_old_days,
    purge_table,
    quarantine_reasons,
)

LATEST = date(2026, 11, 15)


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


def _seed(days: int, latest: date = LATEST) -> None:
    """Deux cartes par jour dans raw et staging, une rejetée par jour (plus une
    sans card_id) en quarantaine, sur `days` jours consécutifs finissant à `latest`."""
    with psycopg.connect(_admin_dsn()) as admin:
        for offset in range(days):
            d = latest - timedelta(days=offset)
            for card in ("base1-1", "base1-2"):
                admin.execute(
                    "INSERT INTO raw.card_prices (card_id, extracted_date, source, payload) "
                    "VALUES (%s, %s, 'pokemontcg.io', %s::jsonb)",
                    (card, d, json.dumps({"id": card})),
                )
                admin.execute(
                    "INSERT INTO staging.card_prices (card_id, extracted_date, name, set_id, "
                    "set_name, source) VALUES (%s, %s, 'x', 'base1', 'Base', 'pokemontcg.io')",
                    (card, d),
                )
            admin.execute(
                "INSERT INTO staging.card_prices_quarantine (card_id, extracted_date, raw_payload, "
                "rejection_reason, source) VALUES "
                "('base1-3', %s, '{}', 'aucun prix tcgplayer disponible', 'pokemontcg.io'), "
                "(NULL, %s, '{}', 'payload sans id', 'pokemontcg.io')",
                (d, d),
            )
        admin.commit()


def _dates(table: str) -> list[date]:
    with psycopg.connect(_admin_dsn()) as admin:
        rows = admin.execute(f"SELECT DISTINCT extracted_date FROM {table} ORDER BY 1").fetchall()
    return [r[0] for r in rows]


@pytest.fixture
def conn():
    with psycopg.connect(_admin_dsn()) as admin:
        admin.execute(
            "TRUNCATE TABLE raw.card_prices, staging.card_prices, "
            "staging.card_prices_quarantine RESTART IDENTITY;"
        )
        admin.commit()
    # Connexion pipeline_app : la rétention doit fonctionner avec le rôle du
    # pipeline, sans aucun droit DELETE direct.
    with get_connection(load_db_config()) as c:
        yield c


def test_keeps_exactly_the_window_in_every_table(conn) -> None:
    _seed(days=40)

    report = purge_old_days(conn, keep_days=30)

    expected_first = LATEST - timedelta(days=29)
    for table in RETENTION_TABLES:
        kept = _dates(table)
        assert kept[0] == expected_first
        assert kept[-1] == LATEST
        assert len(kept) == 30
        purged_days = [d for d, _ in report[table]]
        assert purged_days == [LATEST - timedelta(days=offset) for offset in range(39, 29, -1)]
    assert all(rows == 2 for _, rows in report["raw.card_prices"])
    # La ligne de quarantaine sans card_id part avec son jour.
    assert all(rows == 2 for _, rows in report["staging.card_prices_quarantine"])


def test_second_run_deletes_nothing(conn) -> None:
    _seed(days=35)
    purge_old_days(conn, keep_days=30)

    report = purge_old_days(conn, keep_days=30)

    assert report == {table: [] for table in RETENTION_TABLES}
    assert len(_dates("raw.card_prices")) == 30


def test_window_counts_from_latest_raw_day_not_today(conn) -> None:
    # Pipeline arrêté depuis longtemps : le dernier jour est loin dans le
    # passé, mais rien de la fenêtre ne doit disparaître pour autant.
    old_latest = date(2025, 1, 31)
    _seed(days=30, latest=old_latest)

    assert purge_table(conn, "raw.card_prices", keep_days=30) == []
    assert len(_dates("raw.card_prices")) == 30


def test_latest_raw_day_survives_even_alone(conn) -> None:
    _seed(days=1)
    purge_old_days(conn, keep_days=30)
    assert _dates("raw.card_prices") == [LATEST]


def test_window_below_30_days_is_refused(conn) -> None:
    _seed(days=40)
    with pytest.raises(psycopg.errors.RaiseException, match="minimum 30"):
        purge_table(conn, "raw.card_prices", keep_days=29)


def test_unknown_table_is_refused(conn) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="non autorisée"):
        purge_table(conn, "prod.fact_price_history", keep_days=60)


@pytest.mark.parametrize("table", RETENTION_TABLES)
def test_pipeline_app_still_cannot_delete_directly(conn, table) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(f"DELETE FROM {table} WHERE extracted_date = '1900-01-01'")


def test_quarantine_reasons_of_latest_day(conn) -> None:
    _seed(days=3)
    rows = quarantine_reasons(conn)
    assert rows == [
        (LATEST, "pokemontcg.io", "aucun prix tcgplayer disponible", 1),
        (LATEST, "pokemontcg.io", "payload sans id", 1),
    ]
    assert quarantine_reasons(conn, LATEST - timedelta(days=2))[0][0] == LATEST - timedelta(days=2)
