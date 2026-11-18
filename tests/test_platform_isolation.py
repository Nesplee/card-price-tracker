# Deux plateformes, deux mesures, jamais mélangées (migrations 018, 020-022).
#
# Depuis le passage du dashboard en CHF (2026-11-18), le dashboard lit AUSSI
# CardTrader (cardtrader_en, CHF), mais seulement pour le prix du jour d'une
# carte (champ `eu`, total « au prix Europe ») et pour les seuils de
# l'utilisateur (cible d'une carte recherchée, prix minimum de vente). Toute la
# TENDANCE (courbes, variations, valeur de la collection et des sets, signaux
# de prix, backtest, fraîcheur, rattrapage) reste sur TCGplayer converti au
# taux BCE du jour.
#
# Méthode : on sème un historique TCGplayer et des taux BCE réels (pas la
# parité), on calcule les signaux et on photographie tout ce qui lit des prix
# (chaque endpoint GET de l'API et du board, la fraîcheur, les jours du
# rattrapage, les vues d'opportunités). Puis on charge des prix CardTrader
# différents (1,2 à 1,8 fois le prix en dollars, soit 1,5 à 2,3 fois le prix
# converti : sous le seuil d'offre aberrante de la migration 023), sur les
# mêmes cartes et sur cinq jours APRÈS le dernier jour TCGplayer, avec le vrai
# chargeur, et on recalcule les signaux :
#   - la partie tendance de la photo doit rester identique ;
#   - les champs CardTrader doivent refléter exactement les lignes chargées,
#     en CHF, sans conversion ;
#   - aucune somme ne mélange les deux ;
#   - des lignes cardmarket (EUR) ne sont jamais lues.
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
from tests.fx import weekdays

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


# Taux BCE fictifs mais réalistes, qui bougent chaque jour ouvré (1 USD entre
# 0,79 et 0,82 CHF) : une conversion oubliée ou faite au mauvais jour se voit.
RATES = {
    d: (round(1.15 + 0.001 * (i % 17), 4), round(0.93 - 0.0005 * (i % 11), 4))
    for i, d in enumerate(weekdays(date(2026, 7, 27), date(2026, 9, 30)))
}


def _usd_chf(d: date) -> float:
    """Règle de la migration 020 : dernier cours BCE daté avant d, taux croisé
    arrondi à 6 décimales comme dans prod.mv_usd_chf."""
    ecb = max(e for e in RATES if e < d)
    usd, chf = RATES[ecb]
    return round(chf / usd, 6)


@pytest.fixture
def seeded(rates):
    with _admin() as conn:
        rates(conn, RATES)
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
            "SELECT list_id, 'sv2-4', 5.00 FROM prefs.wishlist WHERE name = 'Recherchées'"
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


# Champs qui viennent de CardTrader, par construction (prix du jour, total au
# prix Europe, comparaison à la cible) : retirés de la photo « tendance ».
CT_FIELDS = {"eu", "eu_total", "eu_price", "ref_price", "to_target", "remaining_value", "n_approx"}


def _trend_only(value):
    if isinstance(value, dict):
        return {k: _trend_only(v) for k, v in value.items() if k not in CT_FIELDS}
    if isinstance(value, list):
        # Fiche carte : les jours CardTrader seuls (sans prix TCGplayer) sont
        # la courbe CardTrader, pas la tendance.
        return [
            _trend_only(v)
            for v in value
            if not (isinstance(v, dict) and "eu_price" in v and v.get("average_sell_price") is None)
        ]
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
            base = tcg.get((card, min(d, LAST_TCG)), 30.0) * rng.uniform(1.2, 1.8)
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


def test_cardtrader_rows_change_nothing_in_the_trend(seeded):
    before = _snapshot()
    assert before["fact_opportunity"], "le jeu de données doit produire des signaux"
    assert before[f"freshness {LAST_TCG}"]["changed"] == 0  # source TCGplayer figée
    # Sans CardTrader, aucune carte n'a de prix Europe.
    assert before["/api/board/collection"]["eu_total"]["cards"] == 0

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
    # Et le dashboard les lit (prix du jour) : la photo complète a changé...
    assert after["/api/board/collection"]["eu_total"]["cards"] == 3
    assert after != before

    # ... mais pas la tendance.
    assert after.keys() == before.keys()
    differing = sorted(key for key in before if _trend_only(after[key]) != _trend_only(before[key]))
    assert differing == []


def test_trend_is_tcgplayer_converted_and_today_price_is_cardtrader_unconverted(seeded):
    ct = {}
    rng = random.Random(7)
    tcg = _tcg_prices()
    d = FIRST
    while d <= LAST_CT:  # mêmes tirages que _load_cardtrader
        for card in CARDS:
            base = tcg.get((card, min(d, LAST_TCG)), 30.0) * rng.uniform(1.2, 1.8)
            if card != "sv2-8":
                ct[(card, d)] = round(base, 2)
        d += timedelta(days=1)
    _load_cardtrader()
    with _admin() as conn:
        # sv2-8 n'a jamais d'offre NM en Europe : repli sur le market converti.
        conn.execute(
            "INSERT INTO prefs.wishlist_card (list_id, card_id, target_price) "
            "SELECT list_id, 'sv2-8', 1000.00 FROM prefs.wishlist WHERE name = 'Recherchées'"
        )

    reader = psycopg.connect(
        _dsn("dashboard_reader", os.environ["DASHBOARD_READER_PASSWORD"]), row_factory=dict_row
    )
    app.dependency_overrides[get_api_connection] = lambda: reader
    try:
        client = TestClient(app)
        board = client.get("/api/board/collection").json()
        hist = client.get("/api/cards/sv2-1/history").json()
        cards = client.get("/api/cards?search=sv2-1").json()
        wish = client.get("/api/prefs/wishlists").json()
    finally:
        app.dependency_overrides.clear()
        reader.close()

    # Tendance : TCGplayer (USD) x taux du jour, à la date de chaque prix.
    rate = _usd_chf(LAST_TCG)
    owned = {"sv2-1": 2, "sv2-2": 1, "sv2-6": 1}
    expected_total = sum(q * tcg[(c, LAST_TCG)] * rate for c, q in owned.items())
    assert board["currency"] == "CHF" and board["as_of"] == LAST_TCG.isoformat()
    assert board["total_value"] == pytest.approx(expected_total, abs=1e-6)
    assert board["fx"]["usd_chf"] == pytest.approx(rate, abs=1e-6)
    assert board["fx"]["missing_since"] is None
    one = next(c for c in board["cards"] if c["card_id"] == "sv2-1")
    assert one["price"] == pytest.approx(tcg[("sv2-1", LAST_TCG)] * rate, abs=1e-6)
    assert cards["items"][0]["current_price"] == pytest.approx(one["price"], abs=1e-6)
    # Prix du jour : la dernière offre NM Europe, en CHF telle quelle.
    assert one["eu"]["price"] == ct[("sv2-1", LAST_CT)]
    assert one["eu"]["date"] == LAST_CT.isoformat() and one["eu"]["country"] == "IT"
    # Total au prix Europe : à part, jamais additionné à la tendance.
    assert board["eu_total"]["value"] == pytest.approx(
        sum(q * ct[(c, LAST_CT)] for c, q in owned.items()), abs=0.01
    )
    assert board["eu_total"]["value"] != pytest.approx(board["total_value"])

    # Fiche carte : deux séries, chacune dans sa mesure, jamais raccordées.
    points = {p["date_id"]: p for p in hist["history"]}
    for day in (FIRST + timedelta(days=3), LAST_TCG):
        p = points[day.isoformat()]
        assert p["average_sell_price"] == pytest.approx(tcg[("sv2-1", day)] * _usd_chf(day))
        assert p["eu_price"] == ct[("sv2-1", day)]
    after_tcg = points[LAST_CT.isoformat()]
    assert after_tcg["average_sell_price"] is None
    assert after_tcg["eu_price"] == ct[("sv2-1", LAST_CT)]

    # Cartes recherchées : prix du jour = offre NM Europe, à défaut market
    # converti (compté « ≈ ») ; l'écart à la cible suit le même prix.
    w = {c["card_id"]: c for c in wish["cards"]}
    four, eight = w["sv2-4"], w["sv2-8"]
    assert four["ref_price"] == ct[("sv2-4", LAST_CT)] and four["eu"]["price"] == four["ref_price"]
    assert four["price"] == pytest.approx(tcg[("sv2-4", LAST_TCG)] * rate, abs=1e-6)
    assert four["to_target"] == pytest.approx(four["ref_price"] / 5 - 1)
    assert eight["eu"] is None and eight["ref_price"] == eight["price"]
    assert eight["price"] == pytest.approx(tcg[("sv2-8", LAST_TCG)] * rate, abs=1e-6)
    assert wish["total"]["n_approx"] == 1
    assert wish["total"]["remaining_value"] == pytest.approx(
        four["ref_price"] + eight["ref_price"], abs=0.01
    )


def test_user_thresholds_use_cardtrader_first(seeded):
    _load_cardtrader()
    with _admin() as conn:
        (eu,) = conn.execute(
            "SELECT eu_price FROM prod.fact_cardtrader_price "
            "WHERE card_id = 'sv2-4' AND date_id = %s",
            (LAST_TCG,),
        ).fetchone()
        # Cible juste au-dessus de l'offre NM Europe, bien au-dessus du market.
        conn.execute("UPDATE prefs.wishlist_card SET target_price = %s", (eu + 1,))
    _compute_signals()
    with _admin() as conn:
        row = conn.execute(
            "SELECT signals, reason, eu_price, market_price FROM prod.fact_opportunity "
            "WHERE card_id = 'sv2-4' AND side = 'buy' AND date_id = %s",
            (LAST_TCG,),
        ).fetchone()
    assert row is not None and "cible_atteinte" in row[0]
    assert "offre NM Europe" in row[1] and row[2] == eu

    # Prix minimum de vente : comparé à l'offre NM Europe du jour du signal,
    # au calcul comme à la lecture (RULES_FILTER).
    with _admin() as conn:
        sell = conn.execute(
            "SELECT card_id, market_price, eu_price FROM prod.fact_opportunity "
            "WHERE side = 'sell' AND date_id = %s ORDER BY card_id LIMIT 1",
            (LAST_TCG,),
        ).fetchone()
    assert sell is not None, "le jeu de données doit produire un signal de vente"
    card, market, eu_sell = sell
    assert eu_sell is not None and eu_sell > market

    def sells_shown(min_price) -> set[str]:
        with _admin() as conn:
            conn.execute(
                "INSERT INTO prefs.card_setting (card_id, min_sell_price) VALUES (%s, %s) "
                "ON CONFLICT (card_id) DO UPDATE SET min_sell_price = EXCLUDED.min_sell_price",
                (card, min_price),
            )
        reader = psycopg.connect(
            _dsn("dashboard_reader", os.environ["DASHBOARD_READER_PASSWORD"]),
            row_factory=dict_row,
        )
        app.dependency_overrides[get_api_connection] = lambda: reader
        try:
            body = TestClient(app).get("/api/opportunities?side=sell&selection=false").json()
        finally:
            app.dependency_overrides.clear()
            reader.close()
        return {i["card_id"] for i in body["items"]}

    # Seuil entre market et l'offre Europe : visible (l'offre Europe le dépasse).
    assert card in sells_shown((market + eu_sell) / 2)
    # Seuil au-dessus de l'offre Europe : masqué.
    assert card not in sells_shown(eu_sell + 1)


def test_cardmarket_rows_are_never_read(seeded):
    _load_cardtrader()
    _compute_signals()
    before = _snapshot()
    with _admin() as conn:
        conn.execute(
            "INSERT INTO prod.fact_price_history "
            "(card_id, date_id, platform_id, average_sell_price, trend_price, low_price) "
            "SELECT f.card_id, f.date_id, p.platform_id, f.average_sell_price * 50, "
            "f.trend_price * 50, f.low_price * 50 "
            "FROM prod.fact_price_history f "
            "JOIN prod.dim_platform t ON t.platform_id = f.platform_id "
            "AND t.platform_name = 'tcgplayer' "
            "CROSS JOIN prod.dim_platform p WHERE p.platform_name = 'cardmarket'"
        )
    _compute_signals()
    after = _snapshot()
    differing = sorted(key for key in before if after[key] != before[key])
    assert differing == []


def test_aberrant_european_offer_is_ignored_everywhere(seeded):
    # Offre « pas à vendre » (9 494,94 CHF, vue le 18/11/2026) sur sv2-1 le
    # dernier jour : ni prix du jour, ni seuil, ni courbe ; la carte est « ≈ ».
    _load_cardtrader()
    with _admin() as conn:
        conn.execute(
            "UPDATE prod.fact_cardtrader_price SET eu_price = 9494.94, any_price = 20.00 "
            "WHERE card_id = 'sv2-1' AND date_id = %s",
            (LAST_CT,),
        )
        (plausible,) = conn.execute(
            "SELECT plausible FROM prod.v_cardtrader_eu WHERE card_id = 'sv2-1' AND date_id = %s",
            (LAST_CT,),
        ).fetchone()
    assert plausible is False
    reader = psycopg.connect(
        _dsn("dashboard_reader", os.environ["DASHBOARD_READER_PASSWORD"]), row_factory=dict_row
    )
    app.dependency_overrides[get_api_connection] = lambda: reader
    try:
        client = TestClient(app)
        board = client.get("/api/board/collection").json()
        hist = client.get("/api/cards/sv2-1/history").json()
    finally:
        app.dependency_overrides.clear()
        reader.close()
    one = next(c for c in board["cards"] if c["card_id"] == "sv2-1")
    # La dernière offre est aberrante : pas de repli sur une offre plus ancienne,
    # mais elle reste visible, pour information.
    assert one["eu"] is None and one["eu_ignored"]["price"] == 9494.94
    assert board["eu_total"]["cards"] == 2 and board["eu_total"]["ignored"] == 1
    assert LAST_CT.isoformat() not in {p["date_id"] for p in hist["history"] if p["eu_price"]}
    # Signaux : le seuil de vente se compare à la vente moyenne convertie.
    with _admin() as conn:
        rows = conn.execute(
            "SELECT card_id, eu_price FROM prod.v_cardtrader_eu WHERE plausible "
            "AND eu_price > 1000"
        ).fetchall()
    assert rows == []
    from src.signals.opportunities import _EU_PRICE_SQL

    with _pipeline() as conn:
        eu = dict(conn.execute(_EU_PRICE_SQL, {"d": LAST_CT, "max_age": 3}).fetchall())
    assert "sv2-1" not in eu and "sv2-2" in eu


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
