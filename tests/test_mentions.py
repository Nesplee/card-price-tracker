# Comptage des mentions de Pokémon dans les posts (src/api/mentions.py) : pur
# Python, aucune base.
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.api.mentions import SpeciesIndex, build_index, count_mentions

POKEMON = {
    "Charizard",
    "Charizard ex",
    "Mew",
    "Mew ex",
    "Mewtwo",
    "Team Rocket's Mewtwo ex",
    "Mr. Mime",
    "Galarian Mr. Mime",
    "Tapu Koko",
    "Ho-Oh",
    "Ethan's Ho-Oh ex",
    "Espeon",
    "Dark Espeon",
    "Rayquaza",
    "Ditto",
}
TODAY = date(2026, 11, 14)


@pytest.fixture
def index() -> SpeciesIndex:
    return SpeciesIndex(POKEMON)


@pytest.mark.parametrize(
    ("card", "species"),
    [
        ("Mega Charizard X ex", "Charizard"),
        ("Team Rocket's Mewtwo ex", "Mewtwo"),
        ("Ethan's Ho-Oh ex", "Ho-Oh"),
        ("Galarian Mr. Mime", "Mr. Mime"),
        ("Tapu Koko", "Tapu Koko"),
        ("Dark Espeon", "Espeon"),
        ("Rayquaza δ", "Rayquaza"),
        ("Lillie's Determination", None),
    ],
)
def test_species_of(index, card, species):
    assert index.species_of(card) == species


def test_find_matches_whole_words_only(index):
    assert index.find("Pulled a Mew ex and a Mewtwo!") == {"Mew", "Mewtwo"}
    assert index.find("Mewtwo's best art") == {"Mewtwo"}
    assert index.find("ho-oh and mr. mime pulls") == {"Ho-Oh", "Mr. Mime"}
    # « ditto » en minuscules est un mot anglais, pas le Pokémon.
    assert index.find("ditto, same here") == set()
    assert index.find("Ditto alt art") == {"Ditto"}


def test_build_index_adds_recent_cards_with_pokemon_suffix():
    idx = build_index(["Iron Valiant ex", "Boss's Orders"])
    assert idx.species_of("Iron Valiant ex") == "Iron Valiant"
    assert idx.species_of("Boss's Orders") is None


def test_count_mentions_per_day_week_and_sources(index):
    def post(days_ago, title, source="reddit", direction=None):
        return {
            "published_on": TODAY - timedelta(days=days_ago),
            "title": title,
            "summary": "",
            "source": source,
            "url": "https://x",
            "direction": direction,
        }

    posts = [
        post(0, "Mew ex is climbing", direction="hausse"),
        post(1, "Mew and Charizard", source="google_news"),
        post(9, "Old Mew post"),
        post(20, "Too old Mew"),
    ]
    out = count_mentions(posts, index, TODAY)
    mew = out["Mew"]
    assert mew["daily"][-1] == 1 and mew["daily"][-2] == 1 and len(mew["daily"]) == 14
    assert mew["week"] == 2 and mew["prev_week"] == 1
    assert mew["sources"] == {"reddit": 1, "presse": 1} and mew["tone"] == {"hausse": 1}
    assert [p["title"] for p in mew["posts"]][0] == "Mew ex is climbing"
    assert out["Charizard"]["week"] == 1 and "Mewtwo" not in out
