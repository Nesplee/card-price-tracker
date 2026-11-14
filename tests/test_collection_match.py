# Tests unitaires de la lecture CSV et du rapprochement (logique pure, sans
# DB ni appel API) -- src/transform/collection_match.py. Même philosophie que
# tests/test_transform.py : fonctions pures, testables en mémoire.
from __future__ import annotations

import csv
import tempfile
from pathlib import Path

from src.transform.collection_match import (
    CollectionRow,
    match_row,
    normalize_card_number,
    read_collection_csv,
)

_CSV_HEADER = [
    "Portfolio Name",
    "Category",
    "Set",
    "Product Name",
    "Card Number",
    "Rarity",
    "Variance",
    "Grade",
    "Card Condition",
    "Average Cost Paid",
    "Quantity",
    "Market Price (As of 2026-08-07)",
    "Price Override",
    "Watchlist",
    "Date Added",
    "Notes",
]


def _write_csv(tmp_path: Path, rows: list[list[str]]) -> str:
    path = tmp_path / "export.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        writer.writerows(rows)
    return str(path)


def test_normalize_card_number_strips_total_and_leading_zeros() -> None:
    assert normalize_card_number("011/193") == "11"
    assert normalize_card_number("132/193") == "132"
    assert normalize_card_number("001/025") == "1"


def test_normalize_card_number_handles_no_total() -> None:
    # Certaines cartes promo n'ont pas de "/total" dans leur numéro.
    assert normalize_card_number("SWSH001") == "SWSH001"


def test_read_collection_csv_filters_category_portfolio_and_rarity(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        [
            # Gardée : Pokemon, Main, Rare.
            [
                "Main",
                "Pokemon",
                "Paldea Evolved",
                "Baxcalibur",
                "060/193",
                "Rare",
                "Holofoil",
                "Ungraded",
                "Near Mint",
                "0.50",
                "1",
                "0.09",
                "0",
                "false",
                "2025-10-01",
                "",
            ],
            # Exclue : Category=One Piece.
            [
                "Main",
                "One Piece",
                "500 Years in the Future",
                "Ain",
                "OP07-002",
                "R",
                "Foil",
                "Ungraded",
                "Near Mint",
                "0",
                "1",
                "0.21",
                "0",
                "false",
                "2025-05-22",
                "",
            ],
            # Exclue : Portfolio Name != Main.
            [
                "MS - Paldea Evolved",
                "Pokemon",
                "Paldea Evolved",
                "Baxcalibur",
                "060/193",
                "Rare",
                "Holofoil",
                "Ungraded",
                "Near Mint",
                "0.50",
                "1",
                "0.09",
                "0",
                "false",
                "2025-10-01",
                "",
            ],
            # Exclue : Rarity=Common.
            [
                "Main",
                "Pokemon",
                "Jungle",
                "Caterpie",
                "45/64",
                "Common",
                "",
                "Ungraded",
                "Near Mint",
                "0.10",
                "2",
                "0.15",
                "0",
                "false",
                "2025-01-01",
                "",
            ],
            # Exclue : Rarity=Uncommon.
            [
                "Main",
                "Pokemon",
                "Jungle",
                "Metapod",
                "46/64",
                "Uncommon",
                "",
                "Ungraded",
                "Near Mint",
                "0.10",
                "1",
                "0.20",
                "0",
                "false",
                "2025-01-01",
                "",
            ],
        ],
    )

    rows = read_collection_csv(csv_path)

    assert len(rows) == 1
    assert rows[0] == CollectionRow(
        set_name="Paldea Evolved",
        card_number="060/193",
        product_name="Baxcalibur",
        variance="Holofoil",
        grade="Ungraded",
        rarity="Rare",
        quantity=1,
        average_cost_paid=0.50,
        market_price_at_export=0.09,
    )


def test_read_collection_csv_finds_market_price_column_regardless_of_embedded_date(
    tmp_path: Path,
) -> None:
    # Le nom de la colonne "Market Price (As of ...)" change à chaque export
    # (la date est intégrée au nom de colonne) -- doit être retrouvée par
    # préfixe, pas par un nom de colonne figé.
    header = [
        h if not h.startswith("Market Price") else "Market Price (As of 2099-01-01)"
        for h in _CSV_HEADER
    ]
    path = Path(tempfile.mkdtemp()) / "export.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerow(
            [
                "Main",
                "Pokemon",
                "Jungle",
                "Clefable",
                "1/64",
                "Rare",
                "Holofoil",
                "Ungraded",
                "Near Mint",
                "1.00",
                "1",
                "42.00",
                "0",
                "false",
                "2025-01-01",
                "",
            ]
        )

    rows = read_collection_csv(str(path))

    assert rows[0].market_price_at_export == 42.00


def test_match_row_succeeds_when_set_and_card_id_known() -> None:
    row = CollectionRow(
        set_name="Chilling Reign",
        card_number="132/198",
        product_name="Caitlin",
        variance="Holofoil",
        grade="Ungraded",
        rarity="Ultra Rare",
        quantity=1,
        average_cost_paid=None,
        market_price_at_export=None,
    )

    result = match_row(
        row, set_name_to_id={"Chilling Reign": "swsh6"}, known_card_ids={"swsh6-132"}
    )

    assert result.card_id == "swsh6-132"
    assert result.rejection_reason is None


def test_match_row_rejects_unknown_set() -> None:
    row = CollectionRow(
        set_name="SV: 151",
        card_number="1/165",
        product_name="Bulbasaur",
        variance="Holofoil",
        grade="Ungraded",
        rarity="Rare",
        quantity=1,
        average_cost_paid=None,
        market_price_at_export=None,
    )

    result = match_row(
        row, set_name_to_id={"Chilling Reign": "swsh6"}, known_card_ids={"swsh6-132"}
    )

    assert result.card_id is None
    assert "SV: 151" in result.rejection_reason


def test_match_row_rejects_unknown_card_id_within_known_set() -> None:
    row = CollectionRow(
        set_name="Chilling Reign",
        card_number="999/198",
        product_name="Carte inexistante",
        variance="Holofoil",
        grade="Ungraded",
        rarity="Rare",
        quantity=1,
        average_cost_paid=None,
        market_price_at_export=None,
    )

    result = match_row(
        row, set_name_to_id={"Chilling Reign": "swsh6"}, known_card_ids={"swsh6-132"}
    )

    assert result.card_id is None
    assert "swsh6-999" in result.rejection_reason


def _row(**kw) -> CollectionRow:
    base = dict(
        set_name="SV: 151",
        card_number="183/165",
        product_name="Charizard ex",
        variance="Holofoil",
        grade="",
        rarity="Ultra Rare",
        quantity=1,
        average_cost_paid=None,
        market_price_at_export=None,
    )
    return CollectionRow(**{**base, **kw})


def test_match_row_uses_set_aliases() -> None:
    result = match_row(_row(), {"151": "sv3pt5"}, {"sv3pt5-183"})
    assert result.card_id == "sv3pt5-183"


def test_match_row_tries_number_without_inner_zeros() -> None:
    row = _row(set_name="Crown Zenith: Galarian Gallery", card_number="GG05/GG70")
    result = match_row(row, {"Crown Zenith Galarian Gallery": "swsh12pt5gg"}, {"swsh12pt5gg-GG5"})
    assert result.card_id == "swsh12pt5gg-GG5"


def test_match_row_falls_back_on_unique_name_in_set() -> None:
    row = _row(set_name="Paldean Fates", card_number="", product_name="Iono 237/091")
    names = {"sv4pt5": {"iono": ["sv4pt5-237"]}}
    result = match_row(row, {"Paldean Fates": "sv4pt5"}, {"sv4pt5-237"}, names)
    assert result.card_id == "sv4pt5-237"
    # Deux cartes du même nom dans le set : pas de choix arbitraire.
    names = {"sv4pt5": {"iono": ["sv4pt5-237", "sv4pt5-80"]}}
    result = match_row(row, {"Paldean Fates": "sv4pt5"}, set(), names)
    assert result.card_id is None and "introuvable" in result.rejection_reason


def test_read_collection_csv_options_merge_portfolios_and_parse_thousands(tmp_path: Path) -> None:
    def line(portfolio, name, number, rarity, cost, qty, market):
        # Ordre de _CSV_HEADER.
        return [
            portfolio,
            "Pokemon",
            "SV: 151",
            name,
            number,
            rarity,
            "Holofoil",
            "",
            "",
            cost,
            qty,
            market,
            "0",
            "false",
            "",
            "",
        ]

    path = _write_csv(
        tmp_path,
        [
            line("Main", "Charizard ex", "183/165", "Ultra Rare", "10.00", "2", "1,234.50"),
            line("MS - 151", "Charizard ex", "183/165", "Ultra Rare", "40.00", "1", "1,234.50"),
            line("Main", "Pidgey", "016/165", "Common", "", "3", "0.10"),
        ],
    )

    assert [r.quantity for r in read_collection_csv(path)] == [2]
    merged = read_collection_csv(path, portfolios=None, include_commons=True)
    char = next(r for r in merged if r.product_name == "Charizard ex")
    assert char.quantity == 3 and char.average_cost_paid == 20.0
    assert char.market_price_at_export == 1234.5
    assert {r.product_name for r in merged} == {"Charizard ex", "Pidgey"}
