# Correspondance card_id (catalogue pokemontcg.io, prod.dim_card) -> idProduct
# Cardmarket, via l'API publique et gratuite TCGdex (https://tcgdex.dev).
#
# Contexte (2026-11-17, étude ~/mere/rapports/cardmarket-prix-2026-11-17.md) :
# les prix Cardmarket des cartes ANGLAISES seront relevés par un userscript
# dans le navigateur de l'utilisateur, sur les pages produit qu'il ouvre
# lui-même. Une page produit Cardmarket est identifiée par son idProduct ;
# il faut donc savoir à quel card_id il correspond. Le catalogue Cardmarket
# (products_singles_6.json) ne donne pas le numéro de carte, et une même carte
# existe sous des dizaines de produits (rééditions, extensions japonaises) :
# un rapprochement par nom est impossible. TCGdex, lui, expose
# pricing.cardmarket.idProduct sur chaque carte
# (ex. GET /v2/en/cards/sv03.5-199 -> 733794, vérifié dans le guide officiel).
#
# Le rapprochement se fait en deux temps :
#   1. set pokemontcg.io -> set TCGdex (les identifiants diffèrent : sv3pt5 vs
#      sv03.5) : surcharge manuelle, sinon nom de set normalisé (unique), sinon
#      identifiant identique ;
#   2. carte -> carte TCGdex par numéro normalisé (001 = 1, SV001 = SV1).
# Puis une requête par carte TCGdex pour lire son idProduct.
#
# Seul le prix agrégé de TCGdex (repris du guide Cardmarket toutes langues)
# est IGNORÉ ici : on ne lit que l'identifiant. Décision de l'utilisateur
# (août 2026, 2026-10-26, confirmée le 2026-11-17) : pas de prix « toutes
# langues ».
from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import requests
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from src.extract.pokemontcg_client import _is_retryable_error

TCGDEX_BASE_URL = "https://api.tcgdex.net/v2/en"
# Pas de règle d'usage publiée par TCGdex : on s'identifie quand même et on
# reste lent (même politesse que TCGCSV).
_HEADERS = {"User-Agent": "CardPriceTracker/1.0"}
_REQUEST_PAUSE_SECONDS = 0.2

# Sets dont le nom diffère entre pokemontcg.io et TCGdex sans que
# l'identifiant soit le même (relevé le 2026-11-17 sur les 170 sets du
# catalogue : 160 se relient par le nom, base1/hgss1-4/svp/sve par
# l'identifiant, ces deux-là seulement à la main).
SET_OVERRIDES: dict[str, str] = {
    "fut20": "fut2020",  # Pokémon Futsal Collection / Pokémon Futsal 2020
}

_NUMBER_RE = re.compile(r"^([A-Z]*)0*(\d+)([A-Z]*)$")


def normalize_set_name(name: str) -> str:
    """Nom de set comparable : sans accents, casse, ponctuation ni tirets
    (« Scarlet & Violet—151 » == « scarlet and violet 151 »)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_name.lower().replace("&", "and"))


def normalize_number(number: str) -> str:
    """Numéro de carte comparable : « 001 » == « 1 », « SV001 » == « SV1 »,
    « TG05 » == « TG5 ». Un numéro hors motif (ex. « 1a-b ») est seulement
    mis en majuscules."""
    upper = number.strip().upper()
    match = _NUMBER_RE.match(upper)
    if not match:
        return upper
    prefix, digits, suffix = match.groups()
    return f"{prefix}{int(digits)}{suffix}"


def split_card_id(card_id: str, set_id: str) -> str | None:
    """Numéro de la carte tiré de son card_id pokemontcg.io (« sv3pt5-199 »,
    set « sv3pt5 » -> « 199 »). None si le card_id ne commence pas par le
    set (cas non rencontré, mais on ne devine pas)."""
    prefix = f"{set_id}-"
    if not card_id.startswith(prefix) or len(card_id) == len(prefix):
        return None
    return card_id[len(prefix) :]


@dataclass(frozen=True)
class SetMatch:
    tcgdex_set_id: str
    rule: str  # "override", "name" ou "id"


def match_sets(
    our_sets: dict[str, str],
    tcgdex_sets: Iterable[dict],
    overrides: dict[str, str] | None = None,
) -> tuple[dict[str, SetMatch], list[str]]:
    """Relie chaque set pokemontcg.io ({set_id: set_name}) à un set TCGdex
    (liste de GET /v2/en/sets : {"id", "name", ...}).

    Ordre : surcharge manuelle, puis nom normalisé s'il est UNIQUE chez
    TCGdex, puis identifiant identique. Renvoie (correspondances, set_id
    non reliés)."""
    overrides = SET_OVERRIDES if overrides is None else overrides
    tcgdex_list = list(tcgdex_sets)
    tcgdex_ids = {s["id"] for s in tcgdex_list}
    by_name: dict[str, list[str]] = defaultdict(list)
    for s in tcgdex_list:
        by_name[normalize_set_name(s["name"])].append(s["id"])

    matches: dict[str, SetMatch] = {}
    unmatched: list[str] = []
    for set_id, set_name in sorted(our_sets.items()):
        if set_id in overrides and overrides[set_id] in tcgdex_ids:
            matches[set_id] = SetMatch(overrides[set_id], "override")
            continue
        candidates = by_name.get(normalize_set_name(set_name), [])
        if len(candidates) == 1:
            matches[set_id] = SetMatch(candidates[0], "name")
        elif set_id in tcgdex_ids:
            matches[set_id] = SetMatch(set_id, "id")
        else:
            unmatched.append(set_id)
    return matches, unmatched


def match_cards(
    cards: Iterable[tuple[str, str]],
    set_matches: dict[str, SetMatch],
    tcgdex_set_cards: dict[str, list[dict]],
) -> tuple[dict[str, str], list[str]]:
    """Relie chaque carte (card_id, set_id) à un identifiant de carte TCGdex.
    `tcgdex_set_cards` : {tcgdex_set_id: cartes de GET /v2/en/sets/{id}}
    ({"id", "localId", ...}). Un numéro présent deux fois dans le set TCGdex
    (ambigu) n'est pas relié. Renvoie (card_id -> id TCGdex, non reliés)."""
    index: dict[str, dict[str, str | None]] = {}
    for tcgdex_set_id, set_cards in tcgdex_set_cards.items():
        by_number: dict[str, str | None] = {}
        for card in set_cards:
            key = normalize_number(str(card["localId"]))
            by_number[key] = None if key in by_number else card["id"]
        index[tcgdex_set_id] = by_number

    mapped: dict[str, str] = {}
    unmatched: list[str] = []
    for card_id, set_id in cards:
        set_match = set_matches.get(set_id)
        number = split_card_id(card_id, set_id)
        by_number = index.get(set_match.tcgdex_set_id) if set_match else None
        tcgdex_id = by_number.get(normalize_number(number)) if by_number and number else None
        if tcgdex_id:
            mapped[card_id] = tcgdex_id
        else:
            unmatched.append(card_id)
    return mapped, unmatched


def extract_id_product(tcgdex_card: dict) -> int | None:
    """idProduct Cardmarket d'une carte TCGdex (GET /v2/en/cards/{id}), ou
    None si TCGdex n'en a pas. Rien d'autre n'est lu dans le bloc pricing."""
    cardmarket = (tcgdex_card.get("pricing") or {}).get("cardmarket") or {}
    id_product = cardmarket.get("idProduct")
    if isinstance(id_product, bool) or not isinstance(id_product, int) or id_product <= 0:
        return None
    return id_product


class TcgdexClient:
    def __init__(self, timeout_seconds: float = 30.0, max_attempts: int = 4) -> None:
        self._timeout_seconds = timeout_seconds
        self._retrying = Retrying(
            retry=retry_if_exception(_is_retryable_error),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        )

    def _get_json(self, path: str) -> object:
        def do_get() -> object:
            time.sleep(_REQUEST_PAUSE_SECONDS)
            response = requests.get(
                f"{TCGDEX_BASE_URL}{path}", headers=_HEADERS, timeout=self._timeout_seconds
            )
            response.raise_for_status()
            return response.json()

        return self._retrying(do_get)

    def sets(self) -> list[dict]:
        return self._get_json("/sets")

    def set_cards(self, tcgdex_set_id: str) -> list[dict]:
        return self._get_json(f"/sets/{tcgdex_set_id}").get("cards") or []

    def card(self, tcgdex_card_id: str) -> dict:
        return self._get_json(f"/cards/{tcgdex_card_id}")


@dataclass(frozen=True)
class ProductMapRow:
    card_id: str
    tcgdex_id: str
    id_product: int | None


def build_product_map(
    client: TcgdexClient,
    our_sets: dict[str, str],
    cards: list[tuple[str, str]],
) -> tuple[list[ProductMapRow], list[str]]:
    """Construit la correspondance pour `cards` [(card_id, set_id)] : une
    requête pour la liste des sets, une par set concerné, une par carte.
    Renvoie (lignes, card_id non reliés à une carte TCGdex). Une carte reliée
    sans idProduct chez TCGdex donne une ligne avec id_product None."""
    needed_sets = {set_id for _, set_id in cards}
    set_matches, _ = match_sets(
        {k: v for k, v in our_sets.items() if k in needed_sets}, client.sets()
    )
    tcgdex_set_cards = {
        m.tcgdex_set_id: client.set_cards(m.tcgdex_set_id) for m in set_matches.values()
    }
    mapped, unmatched = match_cards(cards, set_matches, tcgdex_set_cards)
    rows = [
        ProductMapRow(card_id, tcgdex_id, extract_id_product(client.card(tcgdex_id)))
        for card_id, tcgdex_id in sorted(mapped.items())
    ]
    return rows, unmatched
