# Tests du rattachement news -> cartes (src/transform/news_match.py), sans base.
from __future__ import annotations

from src.transform.news_match import (
    WEIGHT_CARD_AND_SET,
    WEIGHT_CARD_ONLY,
    WEIGHT_SET_ONLY,
    CatalogCard,
    CatalogIndex,
    match_cards,
    normalize,
)

INDEX = CatalogIndex.build(
    [
        CatalogCard("swsh7-215", "Umbreon VMAX", "Evolving Skies"),
        CatalogCard("swsh7-189", "Leafeon V", "Evolving Skies"),
        CatalogCard("swsh45sv-SV122", "Umbreon VMAX", "Shining Fates"),
        CatalogCard("sv8pt5-161", "Umbreon ex", "Prismatic Evolutions"),
        CatalogCard("sv1-1", "Pineco", "Scarlet & Violet"),
    ]
    + [CatalogCard(f"x-{i}", "Pikachu", f"Set {i}") for i in range(20)]
)


def test_normalize_drops_pokemon_and_punctuation():
    assert normalize("Pokémon TCG: Scarlet & Violet—Prismatic Evolutions") == (
        "scarlet violet prismatic evolutions"
    )


def test_card_and_set_cited_targets_that_printing():
    assert match_cards(INDEX, ["Evolving Skies"], ["Umbreon VMAX"]) == {
        "swsh7-215": WEIGHT_CARD_AND_SET
    }


def test_card_alone_targets_all_printings():
    assert match_cards(INDEX, [], ["Umbreon VMAX"]) == {
        "swsh7-215": WEIGHT_CARD_ONLY,
        "swsh45sv-SV122": WEIGHT_CARD_ONLY,
    }


def test_rarity_words_are_ignored_in_card_name():
    assert match_cards(INDEX, [], ["Umbreon ex Special Illustration Rare"]) == {
        "sv8pt5-161": WEIGHT_CARD_ONLY
    }


def test_set_alone_targets_the_whole_set():
    assert match_cards(INDEX, ["Evolving Skies"], []) == {
        "swsh7-215": WEIGHT_SET_ONLY,
        "swsh7-189": WEIGHT_SET_ONLY,
    }


def test_set_name_inside_longer_title_prefers_longest_known_set():
    # "Scarlet & Violet" est aussi un set : le plus long nom contenu gagne.
    assert match_cards(INDEX, ["Scarlet & Violet—Prismatic Evolutions"], []) == {
        "sv8pt5-161": WEIGHT_SET_ONLY
    }


def test_too_common_card_name_is_ignored():
    assert match_cards(INDEX, [], ["Pikachu"]) == {}


def test_unknown_names_match_nothing():
    assert match_cards(INDEX, ["Imaginary Set"], ["Missingno"]) == {}
