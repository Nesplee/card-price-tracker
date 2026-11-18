# Collecte quotidienne du prix des cartes ANGLAISES sur CardTrader
# (plateforme `cardtrader_en`, devise CHF, migration 018).
#
# Périmètre : tous les sets de la collection, c'est-à-dire les sets qui ont au
# moins une carte possédée, une carte recherchée ou qui sont marqués masterset.
# Toutes les cartes de ces sets sont relevées, possédées ou non.
#
# Par set, deux GET : la liste des blueprints de l'extension (pour relier les
# cartes par numéro) et les offres anglaises (language=en) de toute
# l'extension. Aucune réponse brute n'est gardée : seul un résumé par carte
# est écrit, dans la même transaction :
#   - prod.fact_cardtrader_price : offre européenne retenue, offre toutes
#     origines (information), nombre d'offres vues, liste tronquée ;
#   - prod.fact_price_history (plateforme cardtrader_en) : low_price = offre
#     européenne retenue, seulement si elle existe.
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from psycopg import Connection

from src.extract.cardtrader_prices import (
    DEFAULT_MIN_CONDITION,
    EUROPE_SELLER_COUNTRIES,
    CardTraderClient,
    Offer,
    check_currency,
    map_blueprints_to_cards,
    match_expansion,
    merge_marketplace,
    offer_language,
    pokemon_game_id,
    summarize_blueprint,
)
from src.extract.tcgdex_map import split_card_id

logger = logging.getLogger(__name__)

PLATFORM_NAME = "cardtrader_en"
CURRENCY = "CHF"

# Sets suivis : possédés, recherchés ou marqués masterset.
_COLLECTION_CARDS_SQL = """
    WITH sets AS (
        SELECT c.set_id FROM prod.dim_owned_card o JOIN prod.dim_card c USING (card_id)
        UNION
        SELECT c.set_id FROM prefs.wishlist_card w JOIN prod.dim_card c USING (card_id)
        UNION
        SELECT set_id FROM prefs.set_setting WHERE masterset
    )
    SELECT c.card_id, c.set_id, c.set_name, c.name
    FROM prod.dim_card c
    JOIN sets USING (set_id)
    ORDER BY c.set_id, c.card_id
"""

_UPSERT_DIM_DATE_SQL = """
    INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week)
    VALUES (%(date_id)s, %(year)s, %(month)s, %(day)s, %(day_of_week)s)
    ON CONFLICT (date_id) DO NOTHING
"""

_UPSERT_DETAIL_SQL = """
    INSERT INTO prod.fact_cardtrader_price
        (card_id, date_id, platform_id, blueprint_id,
         eu_price, eu_condition, eu_finish, eu_seller_country,
         any_price, any_condition, any_finish, any_seller_country,
         en_offers, truncated)
    VALUES
        (%(card_id)s, %(date_id)s, %(platform_id)s, %(blueprint_id)s,
         %(eu_price)s, %(eu_condition)s, %(eu_finish)s, %(eu_seller_country)s,
         %(any_price)s, %(any_condition)s, %(any_finish)s, %(any_seller_country)s,
         %(en_offers)s, %(truncated)s)
    ON CONFLICT (card_id, date_id, platform_id) DO UPDATE SET
        blueprint_id = EXCLUDED.blueprint_id,
        eu_price = EXCLUDED.eu_price, eu_condition = EXCLUDED.eu_condition,
        eu_finish = EXCLUDED.eu_finish, eu_seller_country = EXCLUDED.eu_seller_country,
        any_price = EXCLUDED.any_price, any_condition = EXCLUDED.any_condition,
        any_finish = EXCLUDED.any_finish, any_seller_country = EXCLUDED.any_seller_country,
        en_offers = EXCLUDED.en_offers, truncated = EXCLUDED.truncated,
        loaded_at = now()
"""

# average_sell_price et trend_price restent NULL : CardTrader donne des
# offres, pas des ventes. Une carte sans offre européenne n'a pas de ligne
# ce jour-là ; si un premier passage du même jour en avait écrit une, son
# low_price est remis à NULL (_CLEAR_FACT_SQL) pour rester aligné sur le
# détail (UPDATE : pipeline_app n'a pas DELETE).
_UPSERT_FACT_SQL = """
    INSERT INTO prod.fact_price_history
        (card_id, date_id, platform_id, average_sell_price, trend_price, low_price)
    VALUES (%(card_id)s, %(date_id)s, %(platform_id)s, NULL, NULL, %(eu_price)s)
    ON CONFLICT (card_id, date_id, platform_id) DO UPDATE SET
        low_price = EXCLUDED.low_price
"""

_CLEAR_FACT_SQL = """
    UPDATE prod.fact_price_history SET low_price = NULL
    WHERE card_id = %(card_id)s AND date_id = %(date_id)s AND platform_id = %(platform_id)s
      AND low_price IS NOT NULL
"""


@dataclass(frozen=True)
class CardPrice:
    card_id: str
    blueprint_id: int
    eu: Offer | None
    any: Offer | None
    en_offers: int
    truncated: bool


@dataclass
class CollectReport:
    sets: int = 0
    sets_unmatched: list[str] = field(default_factory=list)
    cards: int = 0
    cards_mapped: int = 0
    cards_eu: int = 0
    cards_any_only: int = 0
    # Pas d'offre européenne dans une liste tronquée à 25 : il peut en
    # exister une plus loin.
    cards_truncated_without_eu: int = 0
    requests: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sets": self.sets,
            "sets_unmatched": self.sets_unmatched,
            "cards": self.cards,
            "cards_mapped": self.cards_mapped,
            "cards_eu": self.cards_eu,
            "cards_any_only": self.cards_any_only,
            "cards_truncated_without_eu": self.cards_truncated_without_eu,
            "requests": self.requests,
        }


def collection_cards(conn: Connection) -> dict[str, tuple[str, list[tuple[str, str, str]]]]:
    """{set_id: (set_name, [(card_id, numéro, nom)])} pour les sets suivis."""
    sets: dict[str, tuple[str, list[tuple[str, str, str]]]] = {}
    for card_id, set_id, set_name, name in conn.execute(_COLLECTION_CARDS_SQL):
        number = split_card_id(card_id, set_id)
        if number is None:
            continue
        sets.setdefault(set_id, (set_name, []))[1].append((card_id, number, name))
    return sets


def _singles_category_ids(categories: Any) -> set[int] | None:
    items = categories.get("array", []) if isinstance(categories, dict) else categories
    ids = {c["id"] for c in items if "single" in str(c.get("name", "")).lower()}
    return ids or None


def collect_prices(
    client: CardTraderClient,
    sets: dict[str, tuple[str, list[tuple[str, ...]]]],
    *,
    currency: str = CURRENCY,
    min_condition: str = DEFAULT_MIN_CONDITION,
    seller_countries: frozenset[str] = EUROPE_SELLER_COUNTRIES,
) -> tuple[list[CardPrice], CollectReport]:
    """Relève les offres anglaises de chaque set (GET seulement)."""
    report = CollectReport(sets=len(sets))
    games = client.games()
    game_id = pokemon_game_id(games)
    if game_id is None:
        raise RuntimeError("jeu Pokémon introuvable dans GET /games")
    singles = _singles_category_ids(client.categories(game_id))
    expansions = client.expansions()
    report.requests += 3

    # Regroupe les sets par extension : un sous-set Gallery partage
    # l'extension de son set mère, lue une seule fois.
    by_expansion: dict[int, tuple[dict[str, Any], list[str]]] = {}
    for set_id, (set_name, cards) in sets.items():
        report.cards += len(cards)
        expansion = match_expansion(set_name, expansions, game_id, set_id)
        if expansion is None:
            report.sets_unmatched.append(set_id)
            continue
        by_expansion.setdefault(expansion["id"], (expansion, []))[1].append(set_id)

    prices: list[CardPrice] = []
    for expansion_id, (expansion, set_ids) in by_expansion.items():
        blueprints = client.blueprints(expansion_id)
        mapped_by_set = {
            set_id: map_blueprints_to_cards(blueprints, sets[set_id][1], singles)[0]
            for set_id in set_ids
        }
        del blueprints
        english = client.marketplace(expansion_id=expansion_id, language="en")
        report.requests += 2
        check_currency(english, currency)
        offers = merge_marketplace(english)
        del english
        for set_id, mapped in mapped_by_set.items():
            for card_id, blueprint_id in mapped.items():
                raw_offers = offers.get(blueprint_id, [])
                summary = summarize_blueprint(
                    blueprint_id, raw_offers, min_condition, currency, seller_countries
                )
                prices.append(
                    CardPrice(
                        card_id=card_id,
                        blueprint_id=blueprint_id,
                        eu=summary.en_best,
                        any=summary.en_best_any,
                        en_offers=sum(1 for o in raw_offers if _is_english(o)),
                        truncated=summary.truncated,
                    )
                )
            report.cards_mapped += len(mapped)
            logger.info(
                "CardTrader %s (%s) : %d/%d cartes reliées",
                set_id,
                expansion.get("code"),
                len(mapped),
                len(sets[set_id][1]),
            )

    for price in prices:
        if price.eu is not None:
            report.cards_eu += 1
        elif price.any is not None:
            report.cards_any_only += 1
        if price.eu is None and price.truncated:
            report.cards_truncated_without_eu += 1
    return prices, report


def _is_english(raw: dict[str, Any]) -> bool:
    return offer_language(raw.get("properties_hash") or {}) == "en"


def platform_id(conn: Connection, currency: str = CURRENCY) -> int:
    """platform_id de cardtrader_en, après avoir vérifié que la devise
    déclarée en base est celle des prix relevés."""
    row = conn.execute(
        "SELECT platform_id, currency FROM prod.dim_platform WHERE platform_name = %s",
        (PLATFORM_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"{PLATFORM_NAME} absente de prod.dim_platform (migration 018)")
    if row[1] != currency:
        raise RuntimeError(f"{PLATFORM_NAME} est déclarée en {row[1]}, les prix sont en {currency}")
    return row[0]


def _offer_columns(prefix: str, offer: Offer | None) -> dict[str, Any]:
    return {
        f"{prefix}_price": offer.price if offer else None,
        f"{prefix}_condition": offer.condition if offer else None,
        f"{prefix}_finish": offer.finish if offer else None,
        f"{prefix}_seller_country": offer.seller_country if offer else None,
    }


def write_prices(
    conn: Connection, date_id: date, prices: Iterable[CardPrice], currency: str = CURRENCY
) -> dict[str, int]:
    """Écrit le résumé du jour (idempotent : rejouer le même jour met à jour
    les mêmes lignes). Ne commite pas : get_connection s'en charge."""
    pid = platform_id(conn, currency)
    conn.execute(
        _UPSERT_DIM_DATE_SQL,
        {
            "date_id": date_id,
            "year": date_id.year,
            "month": date_id.month,
            "day": date_id.day,
            "day_of_week": date_id.isoweekday(),
        },
    )
    detail = facts = cleared = 0
    with conn.cursor() as cur:
        for price in prices:
            params = {
                "card_id": price.card_id,
                "date_id": date_id,
                "platform_id": pid,
                "blueprint_id": price.blueprint_id,
                "en_offers": price.en_offers,
                "truncated": price.truncated,
                **_offer_columns("eu", price.eu),
                **_offer_columns("any", price.any),
            }
            cur.execute(_UPSERT_DETAIL_SQL, params)
            detail += 1
            if price.eu is not None:
                cur.execute(_UPSERT_FACT_SQL, params)
                facts += 1
            else:
                cur.execute(_CLEAR_FACT_SQL, params)
                cleared += cur.rowcount
    return {"detail_rows": detail, "fact_rows": facts, "fact_rows_cleared": cleared}


def run_cardtrader_prices(
    conn: Connection, client: CardTraderClient, date_id: date
) -> dict[str, Any]:
    sets = collection_cards(conn)
    prices, report = collect_prices(client, sets)
    written = write_prices(conn, date_id, prices)
    summary = {**report.as_dict(), **written}
    logger.info("CardTrader %s : %s", date_id, summary)
    return summary
