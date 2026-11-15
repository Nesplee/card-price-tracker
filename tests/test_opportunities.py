# Tests des signaux d'opportunité (src/signals/opportunities.py). Les règles
# (evaluate) sont pures et testées sans base ; le calcul des indicateurs et
# l'upsert sont testés contre une vraie base Postgres, comme
# tests/test_warehouse_loader.py, parce que les fenêtres de dates, les trous
# de données et la contrainte UNIQUE sont des garanties SQL.
from __future__ import annotations

import os
from dataclasses import replace
from datetime import date, timedelta

import psycopg
import pytest

from src.common.config import load_dashboard_reader_config, load_db_config
from src.common.db import get_connection
from src.signals.opportunities import (
    CardFeatures,
    _with_context,
    compute_opportunities,
    evaluate,
)


def _features(**overrides) -> CardFeatures:
    # Carte "calme" : éligible, aucun signal ne se déclenche.
    base = CardFeatures(
        card_id="sv3pt5-199",
        in_universe=True,
        owned_quantity=0,
        avg_cost_paid=None,
        market=100.0,
        low=80.0,
        ma30=100.0,
        n30=25,
        price_7d=100.0,
        price_30d=100.0,
        volatility=0.02,
        max_jump_7d=0.05,
        low_ratio_median=0.8,
        n_low=20,
    )
    return replace(base, **overrides)


def _codes(f: CardFeatures, side: str) -> list[str]:
    return [s.code for o in evaluate(f) if o.side == side for s in o.signals]


def test_quiet_card_has_no_opportunity():
    assert evaluate(_features()) == []


def test_cheap_card_is_ignored():
    assert evaluate(_features(market=4.0, low=1.0)) == []


def test_card_with_too_few_days_is_ignored():
    assert evaluate(_features(n30=10, low=40.0)) == []


def test_daily_jump_means_unstable_data():
    # x2.5 en un jour : changement de variante probable, pas un signal.
    assert evaluate(_features(max_jump_7d=0.9, low=40.0)) == []


def test_listing_far_below_usual_ratio_is_a_buy():
    assert _codes(_features(low=40.0), "buy") == ["annonce_sous_cote"]


def test_listing_on_illiquid_card_is_ignored():
    # D'habitude l'annonce la moins chère est 2x market : market n'est pas fiable.
    assert _codes(_features(low=90.0, low_ratio_median=2.0), "buy") == []


def test_rebound_after_drop_is_a_buy():
    f = _features(market=80.0, low=64.0, ma30=100.0, price_7d=78.0, price_30d=100.0)
    assert _codes(f, "buy") == ["rebond"]


def test_drop_still_falling_is_not_a_rebound():
    f = _features(market=80.0, low=64.0, ma30=100.0, price_7d=90.0)
    assert "rebond" not in _codes(f, "buy")


def test_unusual_rise_is_momentum():
    # +15 % en 7 j avec 2 %/jour de volatilité : z ~ 2,6.
    f = _features(market=115.0, low=92.0, ma30=105.0, price_7d=100.0, price_30d=100.0)
    assert _codes(f, "buy") == ["momentum"]


def test_rise_within_usual_volatility_is_not_momentum():
    f = _features(market=115.0, low=92.0, ma30=105.0, price_7d=100.0, volatility=0.06)
    assert _codes(f, "buy") == []


def test_sell_signals_only_for_owned_cards():
    f = _features(market=140.0, low=112.0, ma30=100.0)
    assert _codes(f, "sell") == []
    assert _codes(replace(f, owned_quantity=1), "sell") == ["pic"]


def test_sell_signals_apply_outside_buy_universe():
    f = _features(in_universe=False, owned_quantity=1, market=140.0, low=112.0, ma30=100.0)
    assert [o.side for o in evaluate(f)] == ["sell"]


def test_gain_turning_down_is_a_sell():
    f = _features(owned_quantity=2, avg_cost_paid=50.0, price_7d=105.0)
    assert _codes(f, "sell") == ["plus_value"]


def test_gain_without_unknown_cost_is_not_a_sell():
    f = _features(owned_quantity=2, avg_cost_paid=None, price_7d=105.0)
    assert _codes(f, "sell") == []


def test_continuing_drop_is_a_sell():
    f = _features(owned_quantity=1, market=70.0, low=56.0, ma30=80.0, price_7d=78.0)
    assert "chute" in _codes(f, "sell")


def test_score_combines_signals_without_exceeding_100():
    # pic (+40 % sur la moyenne, force 0,67) + plus_value (x2, force 0,5).
    f = _features(owned_quantity=1, avg_cost_paid=70.0, market=140.0, low=112.0, price_7d=150.0)
    (sell,) = [o for o in evaluate(f) if o.side == "sell"]
    assert [s.code for s in sell.signals] == ["pic", "plus_value"]
    assert sell.score == 83
    assert " ; " in sell.reason


def test_news_buy_signal_needs_no_price_history():
    # Carte d'un set qui vient de sortir : 3 jours de prix seulement.
    f = _features(n30=3, news_up=0.6, news_up_title="Umbreon ex SIR is the chase card")
    assert _codes(f, "buy") == ["actu_hausse"]


def test_price_signals_still_need_history():
    assert _codes(_features(n30=3, low=40.0), "buy") == []


def test_weak_news_is_ignored():
    assert _codes(_features(news_up=0.1), "buy") == []


def test_negative_news_is_a_sell_for_owned_cards_only():
    f = _features(news_down=0.5, news_down_title="Evolving Skies reprint")
    assert _codes(f, "sell") == []
    assert _codes(replace(f, owned_quantity=1), "sell") == ["actu_baisse"]


def test_rising_meta_card_is_a_buy_in_standard_only():
    f = _features(series="Scarlet & Violet", meta_share=0.35, meta_share_prev=0.05)
    assert _codes(f, "buy") == ["meta_hausse"]
    assert _codes(replace(f, series="Sword & Shield"), "buy") == []


def test_card_leaving_meta_is_a_sell():
    f = _features(owned_quantity=1, series="Mega Evolution", meta_share=0.05, meta_share_prev=0.4)
    assert _codes(f, "sell") == ["meta_baisse"]


def test_contradictory_news_cancel_out():
    news = {("sv3pt5-199", "hausse"): (0.6, "up"), ("sv3pt5-199", "baisse"): (0.5, "down")}
    f = _with_context(_features(), news, {})
    assert (round(f.news_up, 2), f.news_down) == (0.1, 0.0)
    assert _codes(f, "buy") == []


# --- Intégration : indicateurs SQL + upsert -----------------------------------

D = date(2026, 10, 31)


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


def _seed(admin_conn: psycopg.Connection) -> None:
    cur = admin_conn.cursor()
    cards = [
        # (card_id, set_name, series, rarity) -- base1-4 : rétro, dans le périmètre.
        ("base1-4", "Base", "Base", "Rare Holo"),
        # sv1-50 : Scarlet & Violet commune, hors périmètre mais possédée.
        ("sv1-50", "Scarlet & Violet", "Scarlet & Violet", "Common"),
    ]
    cur.executemany(
        "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, series, rarity) "
        "VALUES (%s, 'x', split_part(%s, '-', 1), %s, %s, %s)",
        [(c, c, s, se, r) for c, s, se, r in cards],
    )
    days = [D - timedelta(days=i) for i in range(36)]
    cur.executemany(
        "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
        "VALUES (%s, 0, 0, 0, 0) ON CONFLICT DO NOTHING",
        [(d,) for d in days],
    )
    rows = []
    for d in days:
        if date(2026, 10, 16) <= d <= date(2026, 10, 25):
            continue  # trou de données, comme en production
        # base1-4 : 100 $ stable, annonce habituelle à 80 $, à 30 $ le jour J.
        rows.append(("base1-4", d, 100.0, 30.0 if d == D else 80.0))
        # sv1-50 : 100 $ puis 150 $ les 3 derniers jours (pic pour un propriétaire).
        rows.append(("sv1-50", d, 150.0 if d > D - timedelta(days=3) else 100.0, 90.0))
    cur.executemany(
        "INSERT INTO prod.fact_price_history "
        "(card_id, date_id, platform_id, average_sell_price, trend_price, low_price) "
        "SELECT %s, %s, platform_id, %s, NULL, %s FROM prod.dim_platform "
        "WHERE platform_name = 'tcgplayer'",
        rows,
    )
    cur.execute(
        "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity, average_cost_paid) "
        "VALUES ('sv1-50', 'Holofoil', 'Ungraded', 1, 40.0)"
    )


@pytest.fixture
def db_connection():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE prod.fact_opportunity, prod.fact_news_event, raw.news_item, "
            "raw.meta_card_usage, raw.meta_tournament, prod.dim_owned_card, "
            "prod.fact_price_history, prod.dim_card RESTART IDENTITY CASCADE;"
        )
        _seed(admin_conn)
        admin_conn.commit()
    with get_connection(load_db_config()) as conn:
        yield conn


def test_compute_opportunities_writes_buy_and_sell(db_connection) -> None:
    counts = compute_opportunities(db_connection, D)
    assert counts == {"cards": 2, "buy": 1, "sell": 1}
    rows = db_connection.execute(
        "SELECT card_id, side, signals, market_price FROM prod.fact_opportunity ORDER BY card_id"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("base1-4", "buy", ["annonce_sous_cote"]),
        ("sv1-50", "sell", ["pic"]),
    ]


def test_compute_opportunities_is_idempotent(db_connection) -> None:
    compute_opportunities(db_connection, D)
    compute_opportunities(db_connection, D)
    assert db_connection.execute("SELECT count(*) FROM prod.fact_opportunity").fetchone()[0] == 2


def test_outcome_view_measures_return(db_connection) -> None:
    # Achat fictif de base1-4 à 90 $ sept jours avant J : à J, elle vaut 100 $.
    db_connection.execute(
        "INSERT INTO prod.fact_opportunity (date_id, card_id, side, score, signals, reason, "
        "market_price) VALUES (%s, 'base1-4', 'buy', 50, ARRAY['rebond'], 'test', 90)",
        (D - timedelta(days=7),),
    )
    db_connection.commit()
    # Les vues de suivi sont lues par le dashboard (dashboard_reader),
    # pas par le pipeline : on vérifie aussi ces droits.
    with psycopg.connect(load_dashboard_reader_config().dsn) as reader:
        outcome = reader.execute(
            "SELECT return_7d, hit_7d, return_30d FROM prod.v_opportunity_outcome"
        ).fetchone()
        perf = reader.execute(
            "SELECT side, signal, n, hit_rate_7d FROM prod.v_opportunity_signal_performance"
        ).fetchone()
    assert (float(outcome[0]), outcome[1], outcome[2]) == (pytest.approx(0.1111), True, None)
    assert perf[:3] == ("buy", "rebond", 1) and perf[3] == 1


def test_recent_news_is_attached_to_the_card(db_connection) -> None:
    news_id = db_connection.execute(
        "INSERT INTO raw.news_item (source, external_id, url, title, published_at, status) "
        "VALUES ('google_news', 'n1', 'https://x', 'Base Set reprint rumours', %s, 'analyzed') "
        "RETURNING news_id",
        (D,),
    ).fetchone()[0]
    db_connection.execute(
        "INSERT INTO prod.fact_news_event VALUES (%s, 'sv1-50', %s, 'reimpression', 'baisse', "
        "0.8, 1)",
        (news_id, D - timedelta(days=1)),
    )
    compute_opportunities(db_connection, D)
    signals, reason = db_connection.execute(
        "SELECT signals, reason FROM prod.fact_opportunity WHERE card_id = 'sv1-50'"
    ).fetchone()
    # Publiée la veille : 0,8 x 1 x (1 - 1/7) ~ 0,69.
    assert signals == ["pic", "actu_baisse"]
    assert "Base Set reprint rumours" in reason


def test_rising_meta_card_from_limitless_usage(db_connection) -> None:
    with psycopg.connect(_admin_dsn()) as admin_conn:
        cur = admin_conn.cursor()
        # Carte SV du périmètre, prix stable, absente des top 8 avant J-7 puis
        # jouée dans 6 decklists sur 16 la dernière semaine.
        cur.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, series, rarity) "
            "VALUES ('sv6-210', 'Dragapult ex', 'sv6', 'Twilight Masquerade', "
            "'Scarlet & Violet', 'Special Illustration Rare')"
        )
        cur.execute(
            "INSERT INTO prod.fact_price_history "
            "(card_id, date_id, platform_id, average_sell_price, low_price) "
            "SELECT 'sv6-210', %s, platform_id, 50, 40 FROM prod.dim_platform "
            "WHERE platform_name = 'tcgplayer'",
            (D,),
        )
        cur.executemany(
            "INSERT INTO raw.meta_tournament VALUES (%s, 'cup', %s, 'STANDARD', 128, 8)",
            [
                ("old1", D - timedelta(days=12)),
                ("old2", D - timedelta(days=10)),
                ("new1", D - timedelta(days=2)),
                ("new2", D - timedelta(days=1)),
            ],
        )
        cur.executemany(
            "INSERT INTO raw.meta_card_usage VALUES (%s, %s, %s)",
            [
                ("old1", "Ralts", 8),
                ("old2", "Ralts", 8),
                ("new1", "Dragapult ex", 2),
                ("new2", "Dragapult ex", 4),
            ],
        )
    compute_opportunities(db_connection, D)
    signals, reason = db_connection.execute(
        "SELECT signals, reason FROM prod.fact_opportunity WHERE card_id = 'sv6-210'"
    ).fetchone()
    assert signals == ["meta_hausse"]
    assert "38 % des top 8" in reason
