# Point critique de la migration 018 : ajouter la plateforme cardtrader_en
# (prix en CHF) dans prod.fact_price_history ne doit RIEN changer à ce qui
# lit les prix TCGplayer (USD).
#
# Méthode : on sème un historique TCGplayer, on calcule les signaux et on
# photographie tout ce qui lit fact_price_history (chaque endpoint GET de
# l'API et du board, la fraîcheur, les jours du rattrapage, les vues
# d'opportunités). Puis on charge des prix CardTrader très différents (x37,
# sur les mêmes cartes, et sur cinq jours APRÈS le dernier jour TCGplayer)
# avec le vrai chargeur, on recalcule les signaux, et on reprend la photo :
# elle doit être identique.
from __future__ import annotations

import os
import random
from dataclasses import asdict
from datetime import date, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from scripts.backfill_opportunities import loaded_days
from src.api.db import get_api_connection
from src.api.main import app
from src.extract.cardtrader_prices import Offer
from src.load.cardtrader_loader import CardPrice, write_prices
from src.monitor.price_freshness import check_price_freshness
from src.signals.opportunities import compute_opportunities

FIRST = date(2026, 8, 1)
LAST_TCG = date(2026, 9, 15)  # dernier jour TCGplayer
LAST_CT = date(2026, 9, 20)  # CardTrader continue après
SIGNAL_DAYS = [LAST_TCG - timedelta(days=i) for i in range(14, -1, -1)]
CARDS = [f"sv2-{i}" for i in range(1, 9)]

ENDPOINTS = [
    "/api/cards",
    "/api/cards?price_min=1&price_max=60",
    "/api/cards?search=Card",
    "/api/cards/sv2-1/history",
    "/api/cards/sv2-5/history",
    "/api/collection",
    "/api/collection/value-history",
    "/api/reports/movers?window=7",
    "/api/reports/movers?window=30",
    "/api/opportunities",
    "/api/opportunities?side=buy",
    "/api/opportunities?side=sell",
    "/api/opportunities/performance",
    "/api/opportunities/trends",
    "/api/board/collection",
    "/api/board/master-sets",
    "/api/board/signals?days=30",
    "/api/prefs/wishlists",
]


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


def _tcg_prices() -> dict[tuple[str, date], float]:
    # Marche aléatoire déterministe ; sv2-3 chute puis rebondit, sv2-6 monte
    # fort (de quoi produire des signaux) ; le dernier jour est figé (même
    # prix que la veille) pour que la fraîcheur signale une source figée.
    rng = random.Random(42)
    prices: dict[tuple[str, date], float] = {}
    for n, card in enumerate(CARDS):
        price = 20.0 + 5 * n
        d = FIRST
        while d < LAST_TCG:
            price *= 1 + rng.uniform(-0.03, 0.03)
            if card == "sv2-3" and date(2026, 9, 1) <= d <= date(2026, 9, 8):
                price *= 0.93
            if card == "sv2-6" and d >= date(2026, 9, 8):
                price *= 1.06
            prices[(card, d)] = round(price, 2)
            d += timedelta(days=1)
        prices[(card, LAST_TCG)] = prices[(card, LAST_TCG - timedelta(days=1))]
    return prices


@pytest.fixture
def seeded():
    with _admin() as conn:
        conn.execute(
            "TRUNCATE TABLE prod.fact_opportunity, prod.fact_cardtrader_price, "
            "prod.dim_owned_card, prod.fact_price_history, prod.dim_card, "
            "prefs.card_setting, prefs.wishlist_card RESTART IDENTITY CASCADE"
        )
        conn.execute(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "SELECT d, EXTRACT(YEAR FROM d)::int, EXTRACT(MONTH FROM d)::int, "
            "EXTRACT(DAY FROM d)::int, EXTRACT(ISODOW FROM d)::int "
            "FROM generate_series(%s::date, %s::date, interval '1 day') AS d "
            "ON CONFLICT (date_id) DO NOTHING",
            (FIRST, LAST_TCG),
        )
        for card in CARDS:
            conn.execute(
                "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
                "VALUES (%s, %s, 'sv2', 'Paldea Evolved', 'Special Illustration Rare', "
                "'Scarlet & Violet')",
                (card, f"Card {card}"),
            )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO prod.fact_price_history "
                "(card_id, date_id, platform_id, average_sell_price, trend_price, low_price) "
                "SELECT %s, %s, platform_id, %s, %s, %s FROM prod.dim_platform "
                "WHERE platform_name = 'tcgplayer'",
                [
                    (card, d, p, round(p * 1.01, 2), round(p * 0.85, 2))
                    for (card, d), p in _tcg_prices().items()
                ],
            )
        conn.execute(
            "INSERT INTO prod.dim_owned_card "
            "(card_id, variance, grade, quantity, average_cost_paid) "
            "VALUES ('sv2-1', 'Normal', '', 2, 10.00), ('sv2-2', 'Holofoil', '', 1, 50.00), "
            "('sv2-6', 'Normal', '', 1, 20.00)"
        )
        conn.execute(
            "INSERT INTO prefs.wishlist_card (list_id, card_id, target_price) "
            "SELECT list_id, 'sv2-4', 40.00 FROM prefs.wishlist WHERE name = 'Recherchées'"
        )
    _compute_signals()
    yield


def _compute_signals() -> None:
    with _admin() as conn:
        conn.execute("TRUNCATE TABLE prod.fact_opportunity RESTART IDENTITY")
    with _pipeline() as conn:
        for d in SIGNAL_DAYS:
            compute_opportunities(conn, d)


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != "computed_at"}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _snapshot() -> dict:
    snap: dict = {}
    reader = psycopg.connect(
        _dsn("dashboard_reader", os.environ["DASHBOARD_READER_PASSWORD"]), row_factory=dict_row
    )
    app.dependency_overrides[get_api_connection] = lambda: reader
    try:
        client = TestClient(app)
        for url in ENDPOINTS:
            response = client.get(url)
            assert response.status_code == 200, (url, response.text)
            snap[url] = _strip(response.json())
    finally:
        app.dependency_overrides.clear()
        reader.close()
    with _admin() as conn:
        for d in (LAST_TCG, LAST_TCG + timedelta(days=1), LAST_CT):
            snap[f"freshness {d}"] = asdict(check_price_freshness(conn, d))
        snap["backfill days"] = loaded_days(conn, FIRST, LAST_CT + timedelta(days=10))
        for view in ("v_opportunity_outcome", "v_opportunity_signal_performance"):
            rows = conn.execute(f"SELECT row_to_json(v)::text FROM prod.{view} v").fetchall()
            snap[view] = sorted(r[0] for r in rows)
        snap["fact_opportunity"] = conn.execute(
            "SELECT date_id, card_id, side, score, signals, reason, market_price, low_price, "
            "ma30, change_7d, change_30d, volatility FROM prod.fact_opportunity "
            "ORDER BY date_id, card_id, side"
        ).fetchall()
    return snap


def _load_cardtrader() -> dict[str, int]:
    rng = random.Random(7)
    totals = {"detail_rows": 0, "fact_rows": 0, "fact_rows_cleared": 0}
    tcg = _tcg_prices()
    d = FIRST
    while d <= LAST_CT:
        prices = []
        for card in CARDS:
            base = tcg.get((card, min(d, LAST_TCG)), 30.0) * 37 * rng.uniform(0.5, 1.5)
            eu = Offer(1, 1, round(base, 2), "CHF", 1, "Near Mint", "en", "standard", "IT")
            # Une carte sans offre européenne : détail seulement, aucun fait.
            if card == "sv2-8":
                eu = None
            far = Offer(2, 1, round(base * 0.9, 2), "CHF", 1, "Mint", "en", "standard", "US")
            prices.append(CardPrice(card, 1000 + CARDS.index(card), eu, far, 3, False))
        with _pipeline() as conn:
            written = write_prices(conn, d, prices)
        for k in totals:
            totals[k] += written[k]
        d += timedelta(days=1)
    return totals


def test_cardtrader_rows_change_nothing_for_tcgplayer(seeded):
    before = _snapshot()
    assert before["fact_opportunity"], "le jeu de données doit produire des signaux"
    assert before[f"freshness {LAST_TCG}"]["changed"] == 0  # source TCGplayer figée

    totals = _load_cardtrader()
    n_days = (LAST_CT - FIRST).days + 1
    assert totals == {"detail_rows": 8 * n_days, "fact_rows": 7 * n_days, "fact_rows_cleared": 0}
    _compute_signals()
    after = _snapshot()

    with _admin() as conn:
        by_platform = dict(
            conn.execute(
                "SELECT p.platform_name, count(*) FROM prod.fact_price_history f "
                "JOIN prod.dim_platform p USING (platform_id) GROUP BY 1"
            ).fetchall()
        )
        (max_day,) = conn.execute("SELECT max(date_id) FROM prod.fact_price_history").fetchone()
    # Témoin : les lignes CardTrader sont bien là, et plus récentes.
    assert by_platform["cardtrader_en"] == 7 * n_days
    assert max_day == LAST_CT

    assert after.keys() == before.keys()
    differing = sorted(key for key in before if after[key] != before[key])
    assert differing == []


def test_write_prices_is_idempotent_and_keeps_currency(seeded):
    offer = Offer(1, 1, 12.5, "CHF", 1, "Near Mint", "en", "standard", "CH")
    price = CardPrice("sv2-1", 1001, offer, offer, 2, False)
    with _pipeline() as conn:
        write_prices(conn, LAST_CT, [price])
        write_prices(conn, LAST_CT, [price])
    with _admin() as conn:
        rows = conn.execute(
            "SELECT p.currency, f.average_sell_price, f.trend_price, f.low_price "
            "FROM prod.fact_price_history f JOIN prod.dim_platform p USING (platform_id) "
            "WHERE p.platform_name = 'cardtrader_en'"
        ).fetchall()
        detail = conn.execute(
            "SELECT eu_price, eu_seller_country, any_price, en_offers "
            "FROM prod.fact_cardtrader_price"
        ).fetchall()
    assert [tuple(map(str, r)) for r in rows] == [("CHF", "None", "None", "12.50")]
    assert [tuple(map(str, r)) for r in detail] == [("12.50", "CH", "12.50", "2")]


def test_replay_without_european_offer_clears_the_fact(seeded):
    # 1er passage : offre européenne ; retry du même jour : plus d'offre
    # européenne. Le fait et le détail doivent dire la même chose.
    eu = Offer(1, 1, 12.5, "CHF", 1, "Near Mint", "en", "standard", "IT")
    far = Offer(2, 1, 11.0, "CHF", 1, "Near Mint", "en", "standard", "US")
    with _pipeline() as conn:
        write_prices(conn, LAST_CT, [CardPrice("sv2-1", 1001, eu, far, 2, False)])
    with _pipeline() as conn:
        written = write_prices(conn, LAST_CT, [CardPrice("sv2-1", 1001, None, far, 1, False)])
    assert written == {"detail_rows": 1, "fact_rows": 0, "fact_rows_cleared": 1}
    with _admin() as conn:
        fact = conn.execute(
            "SELECT f.low_price FROM prod.fact_price_history f "
            "JOIN prod.dim_platform p USING (platform_id) "
            "WHERE p.platform_name = 'cardtrader_en' AND f.card_id = 'sv2-1'"
        ).fetchall()
        detail = conn.execute(
            "SELECT eu_price, any_price::text FROM prod.fact_cardtrader_price"
        ).fetchall()
    assert fact == [(None,)]
    assert detail == [(None, "11.00")]
    # Une carte jamais vue avec une offre européenne ne crée pas de fait vide.
    with _pipeline() as conn:
        written = write_prices(conn, LAST_CT, [CardPrice("sv2-2", 1002, None, far, 1, False)])
    assert written["fact_rows_cleared"] == 0


def test_write_prices_refuses_another_currency(seeded):
    with _pipeline() as conn, pytest.raises(RuntimeError, match="CHF"):
        write_prices(conn, LAST_CT, [], currency="EUR")


def test_dashboard_reader_cannot_write_cardtrader_detail(seeded):
    reader = psycopg.connect(_dsn("dashboard_reader", os.environ["DASHBOARD_READER_PASSWORD"]))
    with reader, pytest.raises(psycopg.errors.InsufficientPrivilege):
        reader.execute("SELECT count(*) FROM prod.fact_cardtrader_price").fetchone()
        reader.execute(
            "INSERT INTO prod.fact_cardtrader_price (card_id, date_id, platform_id, "
            "blueprint_id, en_offers, truncated) VALUES ('sv2-1', %s, 1, 1, 0, false)",
            (LAST_TCG,),
        )
