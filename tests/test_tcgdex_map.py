# Tests de src/extract/tcgdex_map.py : fonctions pures + build_product_map
# avec un faux client (aucun appel réseau, aucune base).
from __future__ import annotations

import pytest

from src.extract.tcgdex_map import (
    ProductMapRow,
    SetMatch,
    build_product_map,
    extract_id_product,
    match_cards,
    match_sets,
    normalize_number,
    normalize_set_name,
    split_card_id,
)

TCGDEX_SETS = [
    {"id": "sv03.5", "name": "151"},
    {"id": "sv02", "name": "Paldea Evolved"},
    {"id": "base1", "name": "Base Set"},
    {"id": "hgss2", "name": "Unleashed"},
    {"id": "fut2020", "name": "Pokémon Futsal 2020"},
    {"id": "dup-a", "name": "Same Name"},
    {"id": "dup-b", "name": "Same Name"},
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Scarlet & Violet—151", "scarletandviolet151"),
        ("Pokémon Futsal 2020", "pokemonfutsal2020"),
        ("HS—Unleashed", "hsunleashed"),
    ],
)
def test_normalize_set_name(raw, expected):
    assert normalize_set_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("001", "1"),
        ("199", "199"),
        ("SV001", "SV1"),
        ("sv001", "SV1"),
        ("TG05", "TG5"),
        ("RC10", "RC10"),
        ("12a", "12A"),
        ("1a-b", "1A-B"),
    ],
)
def test_normalize_number(raw, expected):
    assert normalize_number(raw) == expected


def test_split_card_id():
    assert split_card_id("sv3pt5-199", "sv3pt5") == "199"
    assert split_card_id("swsh45sv-SV001", "swsh45sv") == "SV001"
    # Le set lui-même contient un tiret : seul le préfixe complet compte.
    assert split_card_id("tk-ex-p-3", "tk-ex-p") == "3"
    assert split_card_id("sv3pt5-199", "sv2") is None
    assert split_card_id("sv2-", "sv2") is None


def test_match_sets_rules_and_order():
    ours = {
        "sv3pt5": "151",  # nom
        "sv2": "Paldea Evolved",  # nom
        "base1": "Base",  # nom différent, identifiant identique
        "hgss2": "HS—Unleashed",  # idem
        "fut20": "Pokémon Futsal Collection",  # surcharge
        "xx1": "Same Name",  # nom ambigu chez TCGdex, pas d'id identique
        "zz9": "Unknown",  # rien
    }
    matches, unmatched = match_sets(ours, TCGDEX_SETS, overrides={"fut20": "fut2020"})
    assert matches == {
        "sv3pt5": SetMatch("sv03.5", "name"),
        "sv2": SetMatch("sv02", "name"),
        "base1": SetMatch("base1", "id"),
        "hgss2": SetMatch("hgss2", "id"),
        "fut20": SetMatch("fut2020", "override"),
    }
    assert unmatched == ["xx1", "zz9"]


def test_match_sets_ignores_override_to_unknown_set():
    matches, unmatched = match_sets({"a1": "Nothing"}, TCGDEX_SETS, overrides={"a1": "gone"})
    assert matches == {}
    assert unmatched == ["a1"]


def test_match_cards_by_normalized_number():
    set_matches = {"sv3pt5": SetMatch("sv03.5", "name"), "sv2": SetMatch("sv02", "name")}
    tcgdex_set_cards = {
        "sv03.5": [
            {"id": "sv03.5-001", "localId": "001"},
            {"id": "sv03.5-199", "localId": "199"},
        ],
        "sv02": [
            {"id": "sv02-005", "localId": "005"},
            {"id": "sv02-005b", "localId": "5"},  # même numéro normalisé : ambigu
        ],
    }
    cards = [
        ("sv3pt5-1", "sv3pt5"),
        ("sv3pt5-199", "sv3pt5"),
        ("sv3pt5-300", "sv3pt5"),  # absent chez TCGdex
        ("sv2-5", "sv2"),  # ambigu
        ("me9-1", "me9"),  # set non relié
    ]
    mapped, unmatched = match_cards(cards, set_matches, tcgdex_set_cards)
    assert mapped == {"sv3pt5-1": "sv03.5-001", "sv3pt5-199": "sv03.5-199"}
    assert unmatched == ["sv3pt5-300", "sv2-5", "me9-1"]


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        ({"pricing": {"cardmarket": {"idProduct": 733794, "low": 199.99}}}, 733794),
        ({"pricing": {"cardmarket": None}}, None),
        ({"pricing": None}, None),
        ({}, None),
        ({"pricing": {"cardmarket": {"idProduct": "733794"}}}, None),
        ({"pricing": {"cardmarket": {"idProduct": 0}}}, None),
        ({"pricing": {"cardmarket": {"idProduct": True}}}, None),
    ],
)
def test_extract_id_product(card, expected):
    assert extract_id_product(card) == expected


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def sets(self):
        self.calls.append("sets")
        return TCGDEX_SETS

    def set_cards(self, tcgdex_set_id):
        self.calls.append(f"set:{tcgdex_set_id}")
        return {
            "sv03.5": [
                {"id": "sv03.5-199", "localId": "199"},
                {"id": "sv03.5-200", "localId": "200"},
            ]
        }.get(tcgdex_set_id, [])

    def card(self, tcgdex_card_id):
        self.calls.append(f"card:{tcgdex_card_id}")
        return {
            "sv03.5-199": {"pricing": {"cardmarket": {"idProduct": 733794}}},
            "sv03.5-200": {"pricing": {}},
        }[tcgdex_card_id]


def test_build_product_map_only_fetches_needed_sets_and_cards():
    client = FakeClient()
    rows, unmatched = build_product_map(
        client,
        our_sets={"sv3pt5": "151", "sv2": "Paldea Evolved"},
        cards=[("sv3pt5-199", "sv3pt5"), ("sv3pt5-200", "sv3pt5"), ("sv3pt5-999", "sv3pt5")],
    )
    assert rows == [
        ProductMapRow("sv3pt5-199", "sv03.5-199", 733794),
        ProductMapRow("sv3pt5-200", "sv03.5-200", None),
    ]
    assert unmatched == ["sv3pt5-999"]
    # sv2 n'a aucune carte demandée : son set n'est pas téléchargé.
    assert client.calls == ["sets", "set:sv03.5", "card:sv03.5-199", "card:sv03.5-200"]
