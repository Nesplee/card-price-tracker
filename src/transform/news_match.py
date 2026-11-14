# Rattachement d'une news aux cartes du catalogue (ajouté le 2026-11-06).
# Le LLM local (src/signals/news_llm.py) ne fait qu'EXTRAIRE les noms de sets
# et de cartes cités : un modèle 4B n'est pas fiable pour deviner un card_id.
# Ce module, pur et testé sans base, résout ces noms contre prod.dim_card :
#   - carte + set cités       -> les cartes de ce nom dans ce set, poids 1 ;
#   - carte seule             -> toutes ses impressions, poids 0,5 ;
#   - set seul                -> toutes les cartes du set, poids 0,3.
# Un nom de carte trop vague (plus de MAX_CARDS_PER_NAME impressions, ex.
# "Pikachu") est ignoré plutôt que d'arroser des dizaines de cartes.
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

MAX_CARDS_PER_NAME = 15

WEIGHT_CARD_AND_SET = 1.0
WEIGHT_CARD_ONLY = 0.5
WEIGHT_SET_ONLY = 0.3

# Mots de rareté / finition que le LLM ajoute parfois au nom de la carte.
_RARITY_WORDS = re.compile(
    r"\b(alt(ernate)? art|special illustration rare|illustration rare|sir|sar|ir|full art|"
    r"secret rare|hyper rare|gold|rainbow|promo|1st edition|holo|reverse holo|shadowless)\b"
)


def normalize(name: str) -> str:
    text = name.lower().replace("’", "'").replace("é", "e")
    text = re.sub(r"\b(pokemon|tcg)\b", " ", text)
    return re.sub(r"[^a-z0-9']+", " ", text).strip()


def _strip_rarity(name: str) -> str:
    return re.sub(r"\s+", " ", _RARITY_WORDS.sub(" ", name)).strip()


@dataclass(frozen=True)
class CatalogCard:
    card_id: str
    name: str
    set_name: str


@dataclass
class CatalogIndex:
    by_name: dict[str, list[CatalogCard]] = field(default_factory=lambda: defaultdict(list))
    by_set: dict[str, list[CatalogCard]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def build(cls, cards: list[CatalogCard]) -> CatalogIndex:
        index = cls()
        for card in cards:
            index.by_name[normalize(card.name)].append(card)
            index.by_set[normalize(card.set_name)].append(card)
        return index

    def resolve_set(self, raw: str) -> str | None:
        """Nom de set du LLM -> clé de by_set. Exact d'abord, sinon le plus
        long set connu contenu dans le texte ("Scarlet & Violet—Prismatic
        Evolutions" -> "prismatic evolutions")."""
        name = normalize(raw)
        if name in self.by_set:
            return name
        contained = [s for s in self.by_set if len(s) >= 5 and f" {s} " in f" {name} "]
        return max(contained, key=len) if contained else None

    def resolve_card(self, raw: str) -> str | None:
        name = normalize(raw)
        for candidate in (name, _strip_rarity(name)):
            if candidate in self.by_name:
                return candidate
        return None


def match_cards(index: CatalogIndex, sets: list[str], cards: list[str]) -> dict[str, float]:
    """card_id -> poids, pour les sets et cartes cités par une news. Une
    carte atteinte par plusieurs chemins garde le poids le plus fort."""
    set_keys = {k for s in sets if (k := index.resolve_set(s))}
    weights: dict[str, float] = {}

    def add(card_id: str, weight: float) -> None:
        weights[card_id] = max(weights.get(card_id, 0.0), weight)

    sets_with_named_card = set()
    for raw in cards:
        key = index.resolve_card(raw)
        if key is None:
            continue
        printings = index.by_name[key]
        in_cited_sets = [c for c in printings if normalize(c.set_name) in set_keys]
        if in_cited_sets:
            for card in in_cited_sets:
                add(card.card_id, WEIGHT_CARD_AND_SET)
                sets_with_named_card.add(normalize(card.set_name))
        elif len(printings) <= MAX_CARDS_PER_NAME:
            for card in printings:
                add(card.card_id, WEIGHT_CARD_ONLY)

    # Un set cité SANS carte nommée dedans : la news parle du set entier
    # (réimpression, sortie...). S'il y a une carte nommée, c'est elle le sujet.
    for key in set_keys - sets_with_named_card:
        for card in index.by_set[key]:
            add(card.card_id, WEIGHT_SET_ONLY)
    return weights
