# Prix des cartes ANGLAISES sur CardTrader (place de marché européenne), via
# son API officielle v2 (https://api.cardtrader.com/api/v2).
#
# Contexte (2026-11-17, ~/mere/rapports/cardmarket-prix-2026-11-17.md) :
# l'utilisateur veut le prix des cartes anglaises en Europe. Cardmarket ne
# l'expose par aucune voie gratuite et autorisée (API fermée, pages derrière
# Cloudflare, guide toutes langues refusé). CardTrader a une API officielle,
# gratuite avec le jeton d'un compte, dont les annonces portent la langue et
# l'état.
#
# Règles :
#   - LECTURE SEULE : ce module n'envoie que des GET. Le jeton CardTrader est
#     un jeton complet (il permet d'acheter et de vendre) : aucune méthode
#     d'écriture n'est exposée ici, et _get refuse tout autre verbe.
#   - Jeton dans .env (CARDTRADER_API_TOKEN), jamais journalisé.
#   - Quotas publiés : 200 requêtes / 10 s, 10 req/s sur
#     /marketplace/products. On reste en dessous (pauses ci-dessous).
#
# Prix retenu (décisions de l'utilisateur, 2026-11-17 et 2026-11-18) :
#   - l'offre anglaise la moins chère en Near Mint ou mieux (Mint compris),
#     sans repli sur un état inférieur : une carte sans offre NM n'a pas de
#     prix ce jour-là (décision du 2026-11-18, qui remplace « Slightly Played
#     ou mieux »). Graduation CardTrader : Mint > Near Mint > Slightly Played >
#     Moderately Played > Played > Heavily Played > Poor ;
#   - d'un vendeur d'Europe ou de Suisse (EUROPE_SELLER_COUNTRIES) ; la
#     première offre anglaise toutes origines est gardée en information ;
#   - finition dans l'ordre de FINISH_PRIORITY (même esprit que TCGplayer) ;
#   - devise : celle du compte CardTrader (CHF), sans conversion. Les prix
#     arrivent dans la devise du compte ; check_currency refuse une réponse
#     dans une autre devise plutôt que de la mélanger.
#
# Format vérifié le 2026-11-18 (mesure sur sv2 et sv4pt5) : propriétés
# pokemon_language, condition, pokemon_reverse, first_edition, signed,
# altered ; le filtre language=en avec expansion_id est respecté.
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from src.extract.pokemontcg_client import _is_retryable_error
from src.extract.tcgdex_map import normalize_number, normalize_set_name

CARDTRADER_BASE_URL = "https://api.cardtrader.com/api/v2"
_USER_AGENT = "CardPriceTracker/1.0"
_GENERAL_PAUSE_SECONDS = 0.06  # 200 / 10 s -> 50 ms mini
_MARKETPLACE_PAUSE_SECONDS = 0.15  # 10 / s -> 100 ms mini

CONDITION_ORDER = (
    "Mint",
    "Near Mint",
    "Slightly Played",
    "Moderately Played",
    "Played",
    "Heavily Played",
    "Poor",
)
DEFAULT_MIN_CONDITION = "Near Mint"

# Vendeurs retenus pour le prix « européen » (décision de l'utilisateur du
# 2026-11-18 : UE, EEE, Suisse, Royaume-Uni). Codes ISO 3166-1 alpha-2, tels
# que CardTrader les renvoie dans user.country_code.
EUROPE_SELLER_COUNTRIES = frozenset(
    {
        # Union européenne (27). La Grèce est GR en ISO (EL dans les textes UE).
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
        "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
        "SI", "ES", "SE",
        # Espace économique européen hors UE.
        "IS", "LI", "NO",
        # Suisse et Royaume-Uni (ISO : GB, pas UK).
        "CH", "GB",
    }
)  # fmt: skip


# --------------------------------------------------------------------------
# Lecture des offres (fonctions pures)
# --------------------------------------------------------------------------


def condition_rank(condition: str | None) -> int | None:
    """Rang de l'état (0 = Mint). None si l'état est inconnu : une offre dont
    on ne connaît pas l'état n'est jamais retenue."""
    if condition is None:
        return None
    try:
        return CONDITION_ORDER.index(condition)
    except ValueError:
        return None


def offer_language(properties: dict[str, Any]) -> str | None:
    """Langue d'une offre : la propriété « language » ou « *_language »
    (pokemon_language, mtg_language…), en minuscules."""
    for key, value in properties.items():
        if (key == "language" or key.endswith("_language")) and isinstance(value, str):
            return value.lower()
    return None


def _flag(properties: dict[str, Any], needle: str) -> bool:
    return any(needle in key and value is True for key, value in properties.items())


def offer_finish(properties: dict[str, Any]) -> str:
    """Finition d'une offre : « first_edition », « reverse » ou « standard ».
    Pour Pokémon, l'holo est une propriété de la carte (blueprint) ; le
    reverse et la 1re édition sont des propriétés de l'exemplaire."""
    if _flag(properties, "first_edition"):
        return "first_edition"
    if _flag(properties, "reverse"):
        return "reverse"
    return "standard"


# Même esprit que _VARIANT_PRIORITY (src/transform/validate.py) :
# normal/holofoil d'abord, puis reverse, puis 1re édition.
FINISH_PRIORITY = ("standard", "reverse", "first_edition")


@dataclass(frozen=True)
class Offer:
    product_id: int
    blueprint_id: int
    price: float
    currency: str
    quantity: int
    condition: str
    language: str | None
    finish: str
    seller_country: str | None


def parse_offer(raw: dict[str, Any], currency: str = "EUR") -> Offer | None:
    """Une annonce /marketplace/products -> Offer, ou None si elle ne doit
    pas compter : autre devise que `currency`, prix absent ou nul, gradée,
    vendeur en vacances, lot (bundle_size > 1), signée ou altérée, état
    inconnu.

    L'API renvoie les prix dans la devise choisie dans le compte CardTrader
    (CHF pour un compte suisse) : on n'en mélange jamais deux."""
    price = raw.get("price") or {}
    cents = price.get("cents")
    if price.get("currency") != currency or not isinstance(cents, int) or cents <= 0:
        return None
    if raw.get("graded") or raw.get("on_vacation"):
        return None
    if (raw.get("bundle_size") or 1) != 1:
        return None
    props = raw.get("properties_hash") or {}
    if props.get("signed") is True or props.get("altered") is True:
        return None
    condition = props.get("condition")
    if condition_rank(condition) is None:
        return None
    return Offer(
        product_id=raw["id"],
        blueprint_id=raw["blueprint_id"],
        price=cents / 100,
        currency=currency,
        quantity=int(raw.get("quantity") or 0),
        condition=condition,
        language=offer_language(props),
        finish=offer_finish(props),
        seller_country=(raw.get("user") or {}).get("country_code"),
    )


@dataclass
class BlueprintPrices:
    """Résumé des offres d'un blueprint (une carte CardTrader).

    en_best : offre anglaise la moins chère au seuil d'état, dans la
    meilleure finition disponible (FINISH_PRIORITY), parmi les vendeurs de
    `seller_countries` (tous si None).
    en_best_any : même règle, vendeurs de tous pays (information).
    cheapest_by_language : {langue: offre la moins chère au seuil}, toutes
    finitions confondues (repli « la moins chère par langue »).
    truncated : l'API ne renvoie que les 25 offres les moins chères par
    blueprint ; si la liste est pleine, une langue absente peut exister
    plus loin (d'où l'appel filtré language=en pour l'anglais)."""

    blueprint_id: int
    en_best: Offer | None
    en_best_any: Offer | None = None
    cheapest_by_language: dict[str, Offer] = field(default_factory=dict)
    offers_seen: int = 0
    truncated: bool = False


MARKETPLACE_PAGE_SIZE = 25


def _best_by_finish(offers: list[Offer]) -> Offer | None:
    """L'offre la moins chère dans la première finition de FINISH_PRIORITY
    qui en a une."""
    for finish in FINISH_PRIORITY:
        candidates = [o for o in offers if o.finish == finish]
        if candidates:
            return min(candidates, key=lambda o: o.price)
    return None


class CurrencyMismatch(ValueError):
    """Une réponse /marketplace/products n'est pas dans la devise attendue
    (devise du compte CardTrader changée ?)."""


def check_currency(response: dict[str, list[dict[str, Any]]], currency: str) -> None:
    """Refuse une réponse dont une offre n'est pas en `currency` : la devise
    vient du compte CardTrader, pas de la requête ; si elle change, il faut
    s'arrêter plutôt que de stocker des prix dans une autre unité."""
    seen = {
        (offer.get("price") or {}).get("currency")
        for offers in response.values()
        for offer in offers
    }
    others = seen - {currency}
    if others:
        raise CurrencyMismatch(f"devise attendue {currency}, reçue {sorted(map(str, others))}")


def summarize_blueprint(
    blueprint_id: int,
    raw_offers: list[dict[str, Any]],
    min_condition: str = DEFAULT_MIN_CONDITION,
    currency: str = "EUR",
    seller_countries: frozenset[str] | None = None,
) -> BlueprintPrices:
    max_rank = condition_rank(min_condition)
    if max_rank is None:
        raise ValueError(f"état inconnu : {min_condition!r}")
    offers = [o for o in (parse_offer(r, currency) for r in raw_offers) if o is not None]
    eligible = [o for o in offers if condition_rank(o.condition) <= max_rank]

    by_language: dict[str, Offer] = {}
    for offer in sorted(eligible, key=lambda o: o.price):
        if offer.language and offer.language not in by_language:
            by_language[offer.language] = offer

    english = [o for o in eligible if o.language == "en"]
    local = (
        english
        if seller_countries is None
        else [o for o in english if o.seller_country in seller_countries]
    )

    return BlueprintPrices(
        blueprint_id=blueprint_id,
        en_best=_best_by_finish(local),
        en_best_any=_best_by_finish(english),
        cheapest_by_language=by_language,
        offers_seen=len(raw_offers),
        truncated=len(raw_offers) >= MARKETPLACE_PAGE_SIZE,
    )


def merge_marketplace(*responses: dict[str, list[dict]]) -> dict[int, list[dict]]:
    """Fusionne plusieurs réponses /marketplace/products ({blueprint_id:
    [offres]}, clés en texte dans le JSON), sans doublon d'offre. Sert à
    combiner l'appel non filtré (toutes langues) et l'appel language=en."""
    merged: dict[int, dict[int, dict]] = {}
    for response in responses:
        for key, offers in response.items():
            bucket = merged.setdefault(int(key), {})
            for offer in offers:
                bucket.setdefault(offer["id"], offer)
    return {bp: list(offers.values()) for bp, offers in merged.items()}


# --------------------------------------------------------------------------
# Rapprochement avec le catalogue (fonctions pures)
# --------------------------------------------------------------------------


def pokemon_game_id(games: Any) -> int | None:
    """Identifiant du jeu Pokémon dans GET /games (liste, ou objet
    {"array": [...]} selon les versions de l'API)."""
    items = games.get("array", []) if isinstance(games, dict) else games
    for game in items:
        names = f"{game.get('name', '')} {game.get('display_name', '')}".lower()
        if "pokemon" in names or "pokémon" in names:
            return game["id"]
    return None


# Sets dont le nom ne suffit pas (vérifié le 2026-11-18) : set_id
# pokemontcg.io -> code de l'extension CardTrader. Les sous-sets « Trainer
# Gallery » / « Galarian Gallery » sont rangés dans leur extension mère, avec
# leurs numéros TG01… / GG01… ; les promos SV sont dans « SV Black Star
# Promos » (svpromo), où les numéros se répètent entre gammes (départagés par
# le nom, voir map_blueprints_to_cards).
EXPANSION_CODE_OVERRIDES = {
    "svp": "svpromo",
    "swsh9tg": "brs",
    "swsh10tg": "astr",
    "swsh12pt5gg": "crz",
}


def match_expansion(
    set_name: str,
    expansions: list[dict[str, Any]],
    game_id: int,
    set_id: str | None = None,
) -> dict[str, Any] | None:
    """Extension CardTrader du jeu `game_id` : par code si le set est dans
    EXPANSION_CODE_OVERRIDES, sinon celle dont le nom normalisé est exactement
    celui du set (unique). None sinon."""
    code = EXPANSION_CODE_OVERRIDES.get(set_id or "")
    if code is not None:
        found = [e for e in expansions if e.get("game_id") == game_id and e.get("code") == code]
        return found[0] if len(found) == 1 else None
    target = normalize_set_name(set_name)
    found = [
        e
        for e in expansions
        if e.get("game_id") == game_id and normalize_set_name(e.get("name", "")) == target
    ]
    return found[0] if len(found) == 1 else None


def blueprint_number(blueprint: dict[str, Any]) -> str | None:
    number = (blueprint.get("fixed_properties") or {}).get("collector_number")
    return normalize_number(str(number)) if number not in (None, "") else None


def map_blueprints_to_cards(
    blueprints: list[dict[str, Any]],
    cards: list[tuple[str, ...]],
    singles_category_ids: set[int] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Relie les cartes [(card_id, numéro[, nom])] d'UN set aux blueprints de
    l'extension correspondante, par numéro de collection normalisé. Un numéro
    porté par plusieurs blueprints (ex. promos de plusieurs gammes) n'est relié
    que si un seul de ces blueprints porte le nom de la carte ; sinon la carte
    reste non reliée. `singles_category_ids` limite aux cartes à l'unité."""
    by_number: dict[str, list[dict[str, Any]]] = {}
    for bp in blueprints:
        if singles_category_ids is not None and bp.get("category_id") not in singles_category_ids:
            continue
        number = blueprint_number(bp)
        if number is not None:
            by_number.setdefault(number, []).append(bp)

    mapped: dict[str, int] = {}
    unmatched: list[str] = []
    for card_id, number, *rest in cards:
        candidates = by_number.get(normalize_number(number), [])
        if len(candidates) > 1 and rest and rest[0]:
            name = normalize_set_name(rest[0])
            candidates = [b for b in candidates if normalize_set_name(b.get("name", "")) == name]
        if len(candidates) == 1:
            mapped[card_id] = candidates[0]["id"]
        else:
            unmatched.append(card_id)
    return mapped, unmatched


# --------------------------------------------------------------------------
# Client HTTP (GET uniquement)
# --------------------------------------------------------------------------


class CardTraderClient:
    def __init__(
        self,
        token: str,
        timeout_seconds: float = 60.0,
        max_attempts: int = 4,
        session: requests.Session | None = None,
    ) -> None:
        if not token:
            raise ValueError("CARDTRADER_API_TOKEN manquant")
        self._session = session or requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {token}", "User-Agent": _USER_AGENT}
        )
        self._timeout_seconds = timeout_seconds
        self._retrying = Retrying(
            retry=retry_if_exception(_is_retryable_error),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            reraise=True,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None, pause: float = 0.0) -> Any:
        def do_get() -> Any:
            time.sleep(pause or _GENERAL_PAUSE_SECONDS)
            response = self._session.request(
                "GET",
                f"{CARDTRADER_BASE_URL}{path}",
                params=params,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            return response.json()

        return self._retrying(do_get)

    def info(self) -> Any:
        return self._get("/info")

    def games(self) -> Any:
        return self._get("/games")

    def categories(self, game_id: int) -> Any:
        return self._get("/categories", {"game_id": game_id})

    def expansions(self) -> list[dict[str, Any]]:
        return self._get("/expansions")

    def blueprints(self, expansion_id: int) -> list[dict[str, Any]]:
        return self._get("/blueprints/export", {"expansion_id": expansion_id})

    def marketplace(
        self,
        *,
        expansion_id: int | None = None,
        blueprint_id: int | None = None,
        language: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if (expansion_id is None) == (blueprint_id is None):
            raise ValueError("expansion_id OU blueprint_id")
        params: dict[str, Any] = {}
        if expansion_id is not None:
            params["expansion_id"] = expansion_id
        if blueprint_id is not None:
            params["blueprint_id"] = blueprint_id
        if language:
            params["language"] = language
        return self._get("/marketplace/products", params, pause=_MARKETPLACE_PAUSE_SECONDS)
