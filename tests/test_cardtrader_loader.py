# Tests de src/load/cardtrader_loader.py : collecte avec un faux client (aucun
# appel réseau), puis périmètre des sets et chargement de bout en bout sur la
# base de test jetable.
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.extract.cardtrader_prices import CurrencyMismatch
from src.load.cardtrader_loader import collect_prices, collection_cards, run_cardtrader_prices


def _offer(pid, bp, cents, *, country="IT", condition="Near Mint", lang="en", currency="CHF"):
    return {
        "id": pid,
        "blueprint_id": bp,
        "quantity": 1,
        "price": {"cents": cents, "currency": currency},
        "properties_hash": {
            "condition": condition,
            "pokemon_language": lang,
            "pokemon_reverse": False,
            "first_edition": False,
            "signed": False,
            "altered": False,
        },
        "user": {"country_code": country},
        "graded": False,
        "on_vacation": False,
    }


class FakeClient:
    def __init__(self, marketplace_currency="CHF"):
        self.calls = []
        self.currency = marketplace_currency

    def games(self):
        self.calls.append("games")
        return {"array": [{"id": 1, "name": "Magic"}, {"id": 5, "name": "Pokemon"}]}

    def categories(self, game_id):
        self.calls.append(("categories", game_id))
        return [{"id": 73, "name": "Pokemon Single Card"}, {"id": 74, "name": "Booster Box"}]

    def expansions(self):
        self.calls.append("expansions")
        return [
            {"id": 3316, "game_id": 5, "code": "pal", "name": "Paldea Evolved"},
            {"id": 9, "game_id": 1, "code": "x", "name": "Paldean Fates"},  # autre jeu
        ]

    def blueprints(self, expansion_id):
        self.calls.append(("blueprints", expansion_id))
        return [
            {"id": 101, "category_id": 73, "fixed_properties": {"collector_number": "001"}},
            {"id": 102, "category_id": 73, "fixed_properties": {"collector_number": "002"}},
            {"id": 103, "category_id": 73, "fixed_properties": {"collector_number": "003"}},
            {"id": 900, "category_id": 74, "fixed_properties": {"collector_number": "004"}},
        ]

    def marketplace(self, *, expansion_id=None, blueprint_id=None, language=None):
        self.calls.append(("marketplace", expansion_id, language))
        c = self.currency
        return {
            "101": [
                _offer(1, 101, 150, country="US", currency=c),
                _offer(2, 101, 300, country="CH", currency=c),
                _offer(3, 101, 100, condition="Slightly Played", currency=c),
            ],
            "102": [_offer(4, 102, 500, country="CA", currency=c)],
            "103": [_offer(5, 103, 90, condition="Slightly Played", currency=c)],
        }


SETS = {
    "sv2": ("Paldea Evolved", [("sv2-1", "1"), ("sv2-2", "2"), ("sv2-3", "3"), ("sv2-4", "4")]),
    "sv4pt5": ("Paldean Fates", [("sv4pt5-1", "1")]),
}


def test_collect_prices_rules_and_report():
    client = FakeClient()
    prices, report = collect_prices(client, SETS)
    by_card = {p.card_id: p for p in prices}
    # sv2-1 : NM européen (CH, 3.00) retenu ; NM toutes origines (US, 1.50) en info ;
    # l'offre SP à 1.00 est ignorée (Near Mint ou mieux, sans repli).
    assert (by_card["sv2-1"].eu.price, by_card["sv2-1"].eu.seller_country) == (3.0, "CH")
    assert (by_card["sv2-1"].any.price, by_card["sv2-1"].any.seller_country) == (1.5, "US")
    assert by_card["sv2-1"].en_offers == 3
    # sv2-2 : vendeur hors Europe seulement -> pas de prix retenu, info gardée.
    assert by_card["sv2-2"].eu is None and by_card["sv2-2"].any.price == 5.0
    # sv2-3 : seulement du SP -> aucun prix ce jour-là.
    assert by_card["sv2-3"].eu is None and by_card["sv2-3"].any is None
    # sv2-4 : numéro porté par un produit scellé, pas une carte -> non relié.
    assert "sv2-4" not in by_card
    assert report.as_dict() == {
        "sets": 2,
        "sets_unmatched": ["sv4pt5"],
        "cards": 5,
        "cards_mapped": 3,
        "cards_eu": 1,
        "cards_any_only": 1,
        "cards_truncated_without_eu": 0,
        "requests": 5,
    }
    # Un seul appel d'offres par set, filtré en anglais.
    assert [c for c in client.calls if c[0] == "marketplace"] == [("marketplace", 3316, "en")]


class GalleryClient(FakeClient):
    def expansions(self):
        self.calls.append("expansions")
        return [{"id": 3007, "game_id": 5, "code": "astr", "name": "Astral Radiance"}]

    def blueprints(self, expansion_id):
        self.calls.append(("blueprints", expansion_id))
        return [
            {"id": 101, "category_id": 73, "fixed_properties": {"collector_number": "001"}},
            {"id": 102, "category_id": 73, "fixed_properties": {"collector_number": "TG01"}},
        ]


def test_gallery_subset_shares_its_parent_expansion():
    client = GalleryClient()
    sets = {
        "swsh10": ("Astral Radiance", [("swsh10-1", "1", "A")]),
        "swsh10tg": ("Astral Radiance Trainer Gallery", [("swsh10tg-TG01", "TG01", "B")]),
    }
    prices, report = collect_prices(client, sets)
    assert {p.card_id: p.blueprint_id for p in prices} == {"swsh10-1": 101, "swsh10tg-TG01": 102}
    assert report.sets_unmatched == []
    # L'extension mère n'est lue qu'une fois pour les deux sets.
    assert [c for c in client.calls if c[0] in ("blueprints", "marketplace")] == [
        ("blueprints", 3007),
        ("marketplace", 3007, "en"),
    ]


def test_collect_prices_stops_on_another_currency():
    with pytest.raises(CurrencyMismatch):
        collect_prices(FakeClient(marketplace_currency="EUR"), SETS)


def _admin():
    return psycopg.connect(
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


def _pipeline():
    return psycopg.connect(
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_APP_USER']} "
        f"password={os.environ['POSTGRES_APP_PASSWORD']}"
    )


@pytest.fixture
def catalog():
    cards = [
        ("sv2-1", "sv2", "Paldea Evolved"),
        ("sv2-2", "sv2", "Paldea Evolved"),
        ("sv2-3", "sv2", "Paldea Evolved"),
        ("sv3-7", "sv3", "Obsidian Flames"),  # possédée
        ("sv5-9", "sv5", "Temporal Forces"),  # recherchée
        ("sv6-1", "sv6", "Twilight Masquerade"),  # hors collection
    ]
    with _admin() as conn:
        conn.execute(
            "TRUNCATE TABLE prod.fact_opportunity, prod.fact_cardtrader_price, "
            "prod.dim_owned_card, prod.fact_price_history, prod.dim_card, "
            "prefs.card_setting, prefs.wishlist_card, prefs.set_setting RESTART IDENTITY CASCADE"
        )
        conn.cursor().executemany(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name) VALUES (%s, 'x', %s, %s)",
            cards,
        )
        conn.execute("INSERT INTO prefs.set_setting (set_id, masterset) VALUES ('sv2', true)")
        conn.execute(
            "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity) "
            "VALUES ('sv3-7', 'Normal', '', 1)"
        )
        conn.execute(
            "INSERT INTO prefs.wishlist_card (list_id, card_id) "
            "SELECT list_id, 'sv5-9' FROM prefs.wishlist WHERE name = 'Recherchées'"
        )
    yield
    with _admin() as conn:
        conn.execute("TRUNCATE TABLE prefs.set_setting")
        conn.execute(
            "INSERT INTO prefs.set_setting (set_id, masterset) "
            "VALUES ('sv2', true), ('sv4pt5', true)"
        )


def test_collection_cards_scope(catalog):
    with _pipeline() as conn:
        sets = collection_cards(conn)
    assert sorted(sets) == ["sv2", "sv3", "sv5"]
    assert sets["sv2"] == (
        "Paldea Evolved",
        [("sv2-1", "1", "x"), ("sv2-2", "2", "x"), ("sv2-3", "3", "x")],
    )


def test_run_cardtrader_prices_end_to_end(catalog):
    day = date(2026, 11, 18)
    with _pipeline() as conn:
        summary = run_cardtrader_prices(conn, FakeClient(), day)
    assert summary["detail_rows"] == 3 and summary["fact_rows"] == 1
    assert summary["sets_unmatched"] == ["sv3", "sv5"]
    with _admin() as conn:
        facts = conn.execute(
            "SELECT f.card_id, f.low_price::text, f.average_sell_price, p.currency "
            "FROM prod.fact_price_history f JOIN prod.dim_platform p USING (platform_id)"
        ).fetchall()
    assert facts == [("sv2-1", "3.00", None, "CHF")]
