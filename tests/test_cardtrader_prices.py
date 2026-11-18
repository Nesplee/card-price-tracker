# Tests de src/extract/cardtrader_prices.py : fonctions pures sur des
# fixtures au format de l'API CardTrader v2, et client HTTP avec une fausse
# session (aucun appel réseau, aucune base).
from __future__ import annotations

import pytest

from src.extract.cardtrader_prices import (
    DEFAULT_MIN_CONDITION,
    EUROPE_SELLER_COUNTRIES,
    CardTraderClient,
    CurrencyMismatch,
    check_currency,
    condition_rank,
    map_blueprints_to_cards,
    match_expansion,
    merge_marketplace,
    offer_finish,
    offer_language,
    parse_offer,
    pokemon_game_id,
    summarize_blueprint,
)


def offer(
    pid,
    cents,
    *,
    lang="en",
    condition="Near Mint",
    bp=100,
    currency="EUR",
    graded=False,
    vacation=False,
    bundle=1,
    reverse=None,
    first_ed=None,
    signed=False,
):
    props = {"condition": condition, "signed": signed, "altered": False}
    if lang is not None:
        props["pokemon_language"] = lang
    if reverse is not None:
        props["pokemon_reverse"] = reverse
    if first_ed is not None:
        props["first_edition"] = first_ed
    return {
        "id": pid,
        "blueprint_id": bp,
        "quantity": 2,
        "price": {"cents": cents, "currency": currency},
        "properties_hash": props,
        "user": {"country_code": "IT"},
        "graded": graded,
        "on_vacation": vacation,
        "bundle_size": bundle,
    }


def test_condition_rank():
    assert condition_rank("Mint") == 0
    assert condition_rank("Slightly Played") == 2
    assert condition_rank("Poor") == 6
    assert condition_rank("Excellent") is None
    assert condition_rank(None) is None


@pytest.mark.parametrize(
    ("props", "expected"),
    [
        ({"pokemon_language": "EN"}, "en"),
        ({"mtg_language": "it"}, "it"),
        ({"language": "fr"}, "fr"),
        ({"condition": "Near Mint"}, None),
        ({"pokemon_language": None}, None),
    ],
)
def test_offer_language(props, expected):
    assert offer_language(props) == expected


@pytest.mark.parametrize(
    ("props", "expected"),
    [
        ({}, "standard"),
        ({"pokemon_reverse": False}, "standard"),
        ({"pokemon_reverse": True}, "reverse"),
        ({"first_edition": True, "pokemon_reverse": True}, "first_edition"),
        ({"pokemon_reverse": "true"}, "standard"),  # seul le booléen compte
    ],
)
def test_offer_finish(props, expected):
    assert offer_finish(props) == expected


def test_parse_offer_account_currency():
    chf = offer(1, 1250, currency="CHF")
    assert parse_offer(chf) is None  # EUR attendu par défaut
    parsed = parse_offer(chf, currency="CHF")
    assert parsed.price == 12.5 and parsed.currency == "CHF"
    assert summarize_blueprint(100, [chf], currency="CHF").en_best.product_id == 1


def test_parse_offer_keeps_a_normal_offer():
    parsed = parse_offer(offer(1, 1250))
    assert parsed is not None
    assert parsed.price == 12.5
    assert parsed.currency == "EUR"
    assert parsed.language == "en"
    assert parsed.condition == "Near Mint"
    assert parsed.finish == "standard"
    assert parsed.seller_country == "IT"


@pytest.mark.parametrize(
    "raw",
    [
        offer(1, 1250, currency="USD"),
        offer(1, 0),
        offer(1, 1250, graded=True),
        offer(1, 1250, vacation=True),
        offer(1, 1250, bundle=4),
        offer(1, 1250, signed=True),
        offer(1, 1250, condition="Excellent"),
        {**offer(1, 1250), "price": {"cents": "1250", "currency": "EUR"}},
    ],
)
def test_parse_offer_rejects(raw):
    assert parse_offer(raw) is None


def test_summarize_blueprint_en_best_and_per_language():
    raw = [
        offer(1, 300, lang="it"),
        offer(2, 450, lang="de", condition="Moderately Played"),  # sous le seuil
        offer(3, 500, lang="en", condition="Played"),  # sous le seuil
        offer(4, 900, lang="en", condition="Slightly Played"),
        offer(5, 700, lang="en", reverse=True),  # reverse : moins prioritaire
        offer(6, 1100, lang="en", condition="Mint"),
        offer(7, 650, lang="fr"),
        offer(8, 200, lang="en", graded=True),  # écartée
    ]
    summary = summarize_blueprint(100, raw, min_condition="Slightly Played")
    assert summary.en_best.product_id == 4
    assert summary.en_best.price == 9.0
    assert {k: v.product_id for k, v in summary.cheapest_by_language.items()} == {
        "it": 1,
        "fr": 7,
        "en": 5,  # toutes finitions confondues
    }
    assert summary.offers_seen == 8
    assert summary.truncated is False


def test_default_condition_is_near_mint_without_fallback():
    raw = [
        offer(1, 300, condition="Slightly Played"),
        offer(2, 900, condition="Near Mint"),
        offer(3, 800, condition="Mint"),
    ]
    assert DEFAULT_MIN_CONDITION == "Near Mint"
    assert summarize_blueprint(100, raw).en_best.product_id == 3  # Mint compris
    only_sp = summarize_blueprint(100, [offer(1, 300, condition="Slightly Played")])
    assert only_sp.en_best is None and only_sp.en_best_any is None  # pas de repli


def eu_offer(pid, cents, country, **kw):
    raw = offer(pid, cents, **kw)
    raw["user"] = {"country_code": country}
    return raw


def test_seller_countries_filter_keeps_any_origin_as_information():
    raw = [
        eu_offer(1, 300, "US"),
        eu_offer(2, 450, "CA"),
        eu_offer(3, 700, "CH"),
        eu_offer(4, 650, "JP", lang="it"),
        {**offer(5, 100), "user": {}},  # pays inconnu : jamais européen
    ]
    summary = summarize_blueprint(100, raw, seller_countries=EUROPE_SELLER_COUNTRIES)
    assert summary.en_best.product_id == 3
    assert summary.en_best_any.product_id == 5
    only_far = summarize_blueprint(
        100, [eu_offer(1, 300, "US")], seller_countries=EUROPE_SELLER_COUNTRIES
    )
    assert only_far.en_best is None and only_far.en_best_any.product_id == 1


def test_europe_seller_countries():
    assert len(EUROPE_SELLER_COUNTRIES) == 27 + 3 + 2
    assert {"CH", "GB", "NO", "IS", "LI", "GR", "IT", "DE"} <= EUROPE_SELLER_COUNTRIES
    assert not {"US", "CA", "JP", "UK", "EL"} & EUROPE_SELLER_COUNTRIES


def test_check_currency():
    check_currency({"1": [offer(1, 100, currency="CHF")], "2": []}, "CHF")
    with pytest.raises(CurrencyMismatch):
        check_currency({"1": [offer(1, 100, currency="CHF"), offer(2, 1, currency="EUR")]}, "CHF")


def test_summarize_blueprint_falls_back_to_reverse_then_none():
    only_reverse = summarize_blueprint(100, [offer(1, 700, reverse=True)])
    assert only_reverse.en_best.finish == "reverse"
    no_english = summarize_blueprint(100, [offer(1, 700, lang="it")])
    assert no_english.en_best is None


def test_summarize_blueprint_stricter_condition_and_truncation():
    raw = [offer(i, 100 + i, condition="Slightly Played") for i in range(25)]
    summary = summarize_blueprint(100, raw, min_condition="Near Mint")
    assert summary.en_best is None
    assert summary.truncated is True
    with pytest.raises(ValueError):
        summarize_blueprint(100, raw, min_condition="EX")


def test_merge_marketplace_dedupes_and_casts_keys():
    unfiltered = {"100": [offer(1, 300, lang="it"), offer(2, 900)], "101": []}
    english = {"100": [offer(2, 900), offer(3, 1200)]}
    merged = merge_marketplace(unfiltered, english)
    assert sorted(merged) == [100, 101]
    assert [o["id"] for o in merged[100]] == [1, 2, 3]


def test_pokemon_game_id_list_and_wrapped():
    games = [{"id": 1, "name": "Magic"}, {"id": 5, "name": "Pokemon", "display_name": "Pokémon"}]
    assert pokemon_game_id(games) == 5
    assert pokemon_game_id({"array": games}) == 5
    assert pokemon_game_id([{"id": 1, "name": "Magic"}]) is None


def test_match_expansion_by_normalized_name_and_game():
    expansions = [
        {"id": 10, "game_id": 5, "code": "pal", "name": "Paldea Evolved"},
        {"id": 11, "game_id": 1, "code": "x", "name": "151"},  # autre jeu
        {"id": 12, "game_id": 5, "code": "mew", "name": "151"},
        {"id": 13, "game_id": 5, "code": "d1", "name": "Dup"},
        {"id": 14, "game_id": 5, "code": "d2", "name": "Dup"},
    ]
    assert match_expansion("Paldea Evolved", expansions, 5)["id"] == 10
    assert match_expansion("151", expansions, 5)["id"] == 12
    assert match_expansion("Dup", expansions, 5) is None
    assert match_expansion("Unknown", expansions, 5) is None


def test_match_expansion_code_overrides():
    expansions = [
        {"id": 3221, "game_id": 5, "code": "svpromo", "name": "SV Black Star Promos"},
        {"id": 3268, "game_id": 5, "code": "promosv", "name": "Scarlet & Violet Promos"},
        {"id": 3171, "game_id": 5, "code": "crz", "name": "Crown Zenith"},
    ]
    name = "Scarlet & Violet Black Star Promos"
    assert match_expansion(name, expansions, 5, "svp")["id"] == 3221
    assert match_expansion(name, expansions, 5) is None  # le nom seul ne suffit pas
    gallery = match_expansion("Crown Zenith Galarian Gallery", expansions, 5, "swsh12pt5gg")
    assert gallery["id"] == 3171


def test_map_blueprints_number_formats():
    blueprints = [
        {"id": 1, "name": "Victini", "fixed_properties": {"collector_number": "025/189"}},
        {"id": 2, "name": "Charizard V", "fixed_properties": {"collector_number": "050"}},
        {"id": 3, "name": "Pikachu", "fixed_properties": {"collector_number": "SWSH075"}},
        {"id": 4, "name": "Alakazam", "fixed_properties": {"collector_number": "1"}},
    ]
    cards = [
        ("swsh3-25", "25", "Victini"),  # « 025/189 »
        ("swshp-SWSH050", "SWSH050", "Charizard V"),  # « 050 », même nom
        ("swshp-SWSH075", "SWSH075", "Pikachu"),
        ("swsh10tg-TG01", "TG01", "Sylveon"),  # « 1 » mais autre nom : refusé
        ("swshp-SWSH001", "SWSH001", None),  # pas de nom : pas de repli
    ]
    mapped, unmatched = map_blueprints_to_cards(blueprints, cards)
    assert mapped == {"swsh3-25": 1, "swshp-SWSH050": 2, "swshp-SWSH075": 3}
    assert unmatched == ["swsh10tg-TG01", "swshp-SWSH001"]


def test_map_blueprints_disambiguates_repeated_numbers_by_name():
    blueprints = [
        {"id": 1, "name": "Sprigatito", "fixed_properties": {"collector_number": "001"}},
        {"id": 2, "name": "Snivy", "fixed_properties": {"collector_number": "001"}},
        {"id": 3, "name": "Orthworm", "fixed_properties": {"collector_number": "151"}},
        {"id": 4, "name": "Orthworm", "fixed_properties": {"collector_number": "151"}},
    ]
    cards = [("svp-1", "1", "Sprigatito"), ("sv2-151", "151", "Orthworm"), ("x-1", "1")]
    mapped, unmatched = map_blueprints_to_cards(blueprints, cards)
    assert mapped == {"svp-1": 1}
    assert unmatched == ["sv2-151", "x-1"]  # même nom deux fois ; pas de nom


def test_map_blueprints_to_cards():
    blueprints = [
        {"id": 1, "category_id": 73, "fixed_properties": {"collector_number": "001"}},
        {"id": 2, "category_id": 73, "fixed_properties": {"collector_number": "199"}},
        {"id": 3, "category_id": 73, "fixed_properties": {"collector_number": "050"}},
        {"id": 4, "category_id": 73, "fixed_properties": {"collector_number": "50"}},  # ambigu
        {"id": 5, "category_id": 99, "fixed_properties": {"collector_number": "7"}},  # scellé
        {"id": 6, "category_id": 73, "fixed_properties": {}},
    ]
    cards = [("s-1", "1"), ("s-199", "199"), ("s-50", "50"), ("s-7", "7"), ("s-8", "8")]
    mapped, unmatched = map_blueprints_to_cards(blueprints, cards, singles_category_ids={73})
    assert mapped == {"s-1": 1, "s-199": 2}
    assert unmatched == ["s-50", "s-7", "s-8"]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, timeout=None):
        self.calls.append((method, url, params))
        return FakeResponse({"ok": True})


def test_client_only_sends_get_with_bearer(monkeypatch):
    monkeypatch.setattr("src.extract.cardtrader_prices.time.sleep", lambda _s: None)
    session = FakeSession()
    client = CardTraderClient("secret-token", session=session)
    client.marketplace(expansion_id=10, language="en")
    client.blueprints(10)
    assert session.headers["Authorization"] == "Bearer secret-token"
    assert [c[0] for c in session.calls] == ["GET", "GET"]
    assert session.calls[0][1].endswith("/marketplace/products")
    assert session.calls[0][2] == {"expansion_id": 10, "language": "en"}
    assert not any(hasattr(client, name) for name in ("post", "put", "delete", "purchase"))


def test_client_requires_token_and_one_marketplace_key():
    with pytest.raises(ValueError):
        CardTraderClient("")
    client = CardTraderClient("t", session=FakeSession())
    with pytest.raises(ValueError):
        client.marketplace()
    with pytest.raises(ValueError):
        client.marketplace(expansion_id=1, blueprint_id=2)
