# Tests de la conversion TCGCSV -> format pokemontcg.io et de la résolution
# du productId (HTTP mocké avec responses, pas de base de données).
from __future__ import annotations

import responses

from src.extract.tcgcsv_prices import TcgCsvClient, to_pokemontcg_prices, variant_key


def test_variant_key_matches_pokemontcg_names():
    assert variant_key("Normal") == "normal"
    assert variant_key("Reverse Holofoil") == "reverseHolofoil"
    assert variant_key("1st Edition Holofoil") == "1stEditionHolofoil"


def test_to_pokemontcg_prices_groups_variants():
    rows = [
        {
            "subTypeName": "Normal",
            "lowPrice": 0.01,
            "midPrice": 0.2,
            "highPrice": 19.72,
            "marketPrice": 0.16,
            "directLowPrice": 0.05,
        },
        {
            "subTypeName": "Reverse Holofoil",
            "lowPrice": 0.08,
            "midPrice": 0.28,
            "highPrice": 2.49,
            "marketPrice": 0.28,
            "directLowPrice": None,
        },
    ]
    assert to_pokemontcg_prices(rows) == {
        "normal": {"low": 0.01, "mid": 0.2, "high": 19.72, "market": 0.16, "directLow": 0.05},
        "reverseHolofoil": {
            "low": 0.08,
            "mid": 0.28,
            "high": 2.49,
            "market": 0.28,
            "directLow": None,
        },
    }


@responses.activate
def test_resolve_product_id_reads_redirect_target():
    responses.head(
        "https://prices.pokemontcg.io/tcgplayer/sv8-1",
        status=302,
        headers={
            "location": "https://tcgplayer.pxf.io/scrydex?u=https://tcgplayer.com/product/589939"
        },
    )
    assert (
        TcgCsvClient().resolve_product_id("https://prices.pokemontcg.io/tcgplayer/sv8-1") == 589939
    )


@responses.activate
def test_resolve_product_id_returns_none_without_product():
    responses.head("https://prices.pokemontcg.io/tcgplayer/x-1", status=404)
    assert TcgCsvClient().resolve_product_id("https://prices.pokemontcg.io/tcgplayer/x-1") is None


@responses.activate
def test_fetch_all_prices_indexes_by_product():
    base = "https://tcgcsv.com/tcgplayer/3"
    responses.get(f"{base}/groups", json={"results": [{"groupId": 1}, {"groupId": 2}]})
    responses.get(
        f"{base}/1/prices",
        json={
            "results": [
                {"productId": 10, "subTypeName": "Normal", "marketPrice": 1.0},
            ]
        },
    )
    responses.get(
        f"{base}/2/prices",
        json={
            "results": [
                {"productId": 20, "subTypeName": "Holofoil", "marketPrice": 2.0},
            ]
        },
    )
    prices = TcgCsvClient().fetch_all_prices()
    assert prices[10]["normal"]["market"] == 1.0
    assert prices[20]["holofoil"]["market"] == 2.0
