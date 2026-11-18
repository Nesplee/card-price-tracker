# Taux BCE en base (migration 020) : règle du taux d'un jour, chargeur
# idempotent, droits, garde-fous des migrations 021 et 022. Base de test
# jetable (voir conftest) ; la parité USD = CHF est remise après chaque test
# (fixture `rates`).
from __future__ import annotations

import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from src.extract.ecb_rates import EcbRate
from src.load.fx_loader import run_fx_rates, write_rates

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def _dsn(user: str, password: str) -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={user} password={password}"
    )


def _admin() -> psycopg.Connection:
    return psycopg.connect(
        _dsn(os.environ["POSTGRES_ADMIN_USER"], os.environ["POSTGRES_ADMIN_PASSWORD"])
    )


def _pipeline() -> psycopg.Connection:
    return psycopg.connect(
        _dsn(os.environ["POSTGRES_APP_USER"], os.environ["POSTGRES_APP_PASSWORD"])
    )


def _rate_of(conn, d: date):
    return conn.execute(
        "SELECT ecb_date, usd_chf FROM prod.mv_usd_chf WHERE date_id = %s", (d,)
    ).fetchone()


# Semaine de Pâques 2026 : vendredi saint 03/04 et lundi de Pâques 06/04,
# jours fériés de la BCE (aucun cours).
EASTER = {
    date(2026, 3, 30): (1.10, 0.88),
    date(2026, 3, 31): (1.10, 0.90),
    date(2026, 4, 1): (1.10, 0.92),
    date(2026, 4, 2): (1.10, 0.935),
    date(2026, 4, 7): (1.10, 0.946),
}


def test_rate_of_a_day_is_the_last_ecb_rate_strictly_before(rates):
    with _admin() as conn:
        rates(conn, EASTER)
        # Mardi 31/03 : cours du lundi 30/03, pas celui du 31 (publié vers
        # 15:00 UTC, après le relevé des prix de 07:00).
        assert _rate_of(conn, date(2026, 3, 31)) == (date(2026, 3, 30), Decimal("0.8"))
        # Week-end et jours fériés : dernier jour ouvré. Mardi 07/04 : cours
        # du jeudi 02/04, 5 jours avant (le plus long trou du calendrier BCE).
        for day in (date(2026, 4, 3), date(2026, 4, 5), date(2026, 4, 6), date(2026, 4, 7)):
            assert _rate_of(conn, day) == (date(2026, 4, 2), Decimal("0.85"))
        assert _rate_of(conn, date(2026, 4, 8))[0] == date(2026, 4, 7)
        # Cours de plus de 5 jours : aucun taux, donc aucun prix converti.
        assert _rate_of(conn, date(2026, 4, 12)) == (date(2026, 4, 7), Decimal("0.86"))
        assert _rate_of(conn, date(2026, 4, 13)) is None
        assert _rate_of(conn, date(2026, 3, 30)) is None  # rien avant le premier cours


def test_tcgplayer_view_converts_each_day_at_its_rate_and_nulls_without_rate(rates):
    with _admin() as conn:
        rates(conn, EASTER)
        conn.execute("TRUNCATE TABLE prod.fact_price_history, prod.dim_card CASCADE")
        conn.execute(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "SELECT d, 2026, EXTRACT(MONTH FROM d)::int, EXTRACT(DAY FROM d)::int, 1 "
            "FROM generate_series('2026-03-31'::date, '2026-04-13'::date, interval '1 day') d "
            "ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name) "
            "VALUES ('x-1', 'X', 'x', 'X')"
        )
        for d in (date(2026, 3, 31), date(2026, 4, 7), date(2026, 4, 13)):
            conn.execute(
                "INSERT INTO prod.fact_price_history (card_id, date_id, platform_id, "
                "average_sell_price, low_price) SELECT 'x-1', %s, platform_id, 10.00, 8.00 "
                "FROM prod.dim_platform WHERE platform_name = 'tcgplayer'",
                (d,),
            )
        rows = conn.execute(
            "SELECT date_id, market_chf, low_chf FROM prod.v_tcgplayer_chf ORDER BY date_id"
        ).fetchall()
    assert rows == [
        (date(2026, 3, 31), Decimal("8.00000000"), Decimal("6.40000000")),
        (date(2026, 4, 7), Decimal("8.50000000"), Decimal("6.80000000")),
        (date(2026, 4, 13), None, None),
    ]


def test_loader_is_idempotent_and_refreshes_the_daily_rates(rates):
    batch = [
        EcbRate(date(2026, 11, 16), Decimal("1.1014"), Decimal("0.9244")),
        EcbRate(date(2026, 11, 17), Decimal("1.0989"), Decimal("0.9233")),
    ]
    with _admin() as conn:
        rates(conn, {})
    with _pipeline() as conn:
        assert write_rates(conn, batch) == 2
    with _pipeline() as conn:
        assert write_rates(conn, batch) == 0  # même lot : rien ne change
        assert _rate_of(conn, date(2026, 11, 18)) == (date(2026, 11, 17), Decimal("0.840204"))
    # Correction d'un cours (publication rectifiée) : réécrit.
    fixed = [EcbRate(date(2026, 11, 17), Decimal("1.1000"), Decimal("0.9240"))]
    with _pipeline() as conn:
        assert write_rates(conn, fixed) == 1
        assert _rate_of(conn, date(2026, 11, 18))[1] == Decimal("0.840000")


class FakeClient:
    def __init__(self):
        self.calls = []

    def history(self, since):
        self.calls.append(("history", since))
        return [EcbRate(date(2026, 8, 3), Decimal("1.1535"), Decimal("0.932"))]

    def last_90_days(self):
        self.calls.append(("90d", None))
        return [EcbRate(date(2026, 11, 17), Decimal("1.0989"), Decimal("0.9233"))]


def test_run_reads_full_history_only_when_the_table_is_empty(rates):
    with _admin() as conn:
        rates(conn, {})
        conn.execute("TRUNCATE TABLE prod.fact_price_history, prod.dim_card CASCADE")
    client = FakeClient()
    with _pipeline() as conn:
        assert run_fx_rates(conn, client)["source"] == "hist.zip"
    with _pipeline() as conn:
        assert run_fx_rates(conn, client)["source"] == "hist-90d.xml"
    assert client.calls == [("history", None), ("90d", None)]


def test_dashboard_reader_reads_rates_but_cannot_write_or_refresh():
    reader = psycopg.connect(_dsn("dashboard_reader", os.environ["DASHBOARD_READER_PASSWORD"]))
    with reader:
        reader.execute("SELECT count(*) FROM prod.mv_usd_chf").fetchone()
        reader.execute("SELECT count(*) FROM prod.v_tcgplayer_chf").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("SELECT prod.refresh_usd_chf()")
    reader = psycopg.connect(_dsn("dashboard_reader", os.environ["DASHBOARD_READER_PASSWORD"]))
    with reader, pytest.raises(psycopg.errors.InsufficientPrivilege):
        reader.execute(
            "INSERT INTO prod.fact_fx_rate (ecb_date, usd_per_eur, chf_per_eur) "
            "VALUES ('2020-01-01', 1, 1)"
        )


def _do_block(migration: str) -> str:
    text = (MIGRATIONS / migration).read_text()
    return re.search(r"DO \$\$.*?END \$\$;", text, re.S).group(0)


def test_migration_021_refuses_to_convert_usd_thresholds():
    # État d'avant 021 rejoué dans une transaction annulée : un seuil en USD.
    with _admin() as conn:
        conn.execute("ALTER TABLE prefs.card_setting DROP CONSTRAINT card_setting_currency_check")
        conn.execute("TRUNCATE TABLE prefs.card_setting")
        conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name) "
            "VALUES ('g-1', 'G', 'g', 'G') ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO prefs.card_setting (card_id, min_sell_price, currency) "
            "VALUES ('g-1', 12.00, 'USD')"
        )
        with pytest.raises(psycopg.errors.RaiseException, match="1 prix minimum de vente"):
            conn.execute(_do_block("021_prefs_chf.sql"))
        conn.rollback()
    # Seuils sans prix (réglages simples) : aucune objection.
    with _admin() as conn:
        conn.execute("ALTER TABLE prefs.card_setting DROP CONSTRAINT card_setting_currency_check")
        conn.execute("UPDATE prefs.card_setting SET currency = 'USD', min_sell_price = NULL")
        conn.execute(_do_block("021_prefs_chf.sql"))
        conn.rollback()


def test_currency_is_chf_and_usd_is_refused():
    with _admin() as conn:
        conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name) "
            "VALUES ('g-1', 'G', 'g', 'G') ON CONFLICT DO NOTHING"
        )
        conn.execute("TRUNCATE TABLE prefs.card_setting")
        conn.execute("INSERT INTO prefs.card_setting (card_id, min_sell_price) VALUES ('g-1', 5)")
        assert conn.execute("SELECT currency FROM prefs.card_setting").fetchone() == ("CHF",)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE prefs.card_setting SET currency = 'USD'")
        conn.rollback()


def test_migration_022_converts_existing_signals_at_their_day_rate(rates):
    day = date(2026, 4, 1)
    with _admin() as conn:
        rates(conn, EASTER)
        conn.execute("TRUNCATE TABLE prod.fact_opportunity RESTART IDENTITY")
        conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name) "
            "VALUES ('g-1', 'G', 'g', 'G') ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "VALUES (%s, 2026, 4, 1, 3) ON CONFLICT DO NOTHING",
            (day,),
        )
        # État d'avant 022 (sans ses colonnes), une ligne calculée en USD.
        conn.execute("ALTER TABLE prod.fact_opportunity DROP COLUMN eu_price, DROP COLUMN usd_chf")
        conn.execute(
            "INSERT INTO prod.fact_opportunity (date_id, card_id, side, score, signals, reason, "
            "market_price, low_price, ma30, change_7d) "
            "VALUES (%s, 'g-1', 'buy', 50, '{rebond}', 'r', 100.00, 50.00, 120.00, 0.1)",
            (day,),
        )
        conn.execute(_do_block("022_opportunity_chf.sql"))
        row = conn.execute(
            "SELECT market_price, low_price, ma30, change_7d, usd_chf, eu_price "
            "FROM prod.fact_opportunity"
        ).fetchone()
        # Taux du 01/04 : cours du 31/03, 0,90 / 1,10.
        assert row == (
            Decimal("81.82"),
            Decimal("40.91"),
            Decimal("98.18"),
            Decimal("0.1000"),
            Decimal("0.818182"),
            None,
        )
        # Second passage : rien n'est reconverti.
        conn.execute(_do_block("022_opportunity_chf.sql"))
        assert conn.execute("SELECT market_price FROM prod.fact_opportunity").fetchone() == (
            Decimal("81.82"),
        )
        conn.rollback()


def test_migration_022_refuses_a_signal_without_rate(rates):
    with _admin() as conn:
        rates(conn, EASTER)
        conn.execute("TRUNCATE TABLE prod.fact_opportunity RESTART IDENTITY")
        conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name) "
            "VALUES ('g-1', 'G', 'g', 'G') ON CONFLICT DO NOTHING"
        )
        day = date(2026, 6, 1)  # aucun cours BCE autour
        conn.execute(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "VALUES (%s, 2026, 6, 1, 1) ON CONFLICT DO NOTHING",
            (day,),
        )
        conn.execute("ALTER TABLE prod.fact_opportunity DROP COLUMN eu_price, DROP COLUMN usd_chf")
        conn.execute(
            "INSERT INTO prod.fact_opportunity (date_id, card_id, side, score, signals, reason, "
            "market_price) VALUES (%s, 'g-1', 'buy', 50, '{rebond}', 'r', 100.00)",
            (day,),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="1 signaux sans taux"):
            conn.execute(_do_block("022_opportunity_chf.sql"))
        conn.rollback()
