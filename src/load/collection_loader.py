# Chargement en base des données de collection personnelle déjà lues/validées
# par src/transform/collection_match.py -- aucune règle métier ici, seulement
# des INSERT/UPSERT SQL, exactement le même partage de responsabilité que
# src/load/staging_loader.py vis-à-vis de src/transform/validate.py.
from __future__ import annotations

import logging

from psycopg import Connection

from src.transform.collection_match import CollectionRow, MatchResult

logger = logging.getLogger(__name__)

# RETURNING id : renvoie l'id de la ligne insérée OU mise à jour (ON
# CONFLICT DO UPDATE renvoie aussi la ligne via RETURNING, contrairement à
# DO NOTHING qui ne renverrait rien pour une ligne déjà existante inchangée)
# -- nécessaire pour que l'appelant (scripts/import_collection.py) puisse
# ensuite référencer cette ligne comme raw_import_id dans
# collection.match_quarantine ou pour corréler avec prod.dim_owned_card.
_UPSERT_RAW_IMPORT_SQL = """
    INSERT INTO collection.raw_import
        (set_name, card_number, product_name, variance, grade, rarity,
         quantity, average_cost_paid, market_price_at_export)
    VALUES
        (%(set_name)s, %(card_number)s, %(product_name)s, %(variance)s, %(grade)s,
         %(rarity)s, %(quantity)s, %(average_cost_paid)s, %(market_price_at_export)s)
    ON CONFLICT (set_name, card_number, product_name, variance, grade)
    DO UPDATE SET
        rarity = EXCLUDED.rarity,
        quantity = EXCLUDED.quantity,
        average_cost_paid = EXCLUDED.average_cost_paid,
        market_price_at_export = EXCLUDED.market_price_at_export,
        imported_at = now()
    RETURNING id
"""

_UPSERT_OWNED_CARD_SQL = """
    INSERT INTO prod.dim_owned_card
        (card_id, variance, grade, quantity, average_cost_paid)
    VALUES
        (%(card_id)s, %(variance)s, %(grade)s, %(quantity)s, %(average_cost_paid)s)
    ON CONFLICT (card_id, variance, grade)
    DO UPDATE SET
        quantity = EXCLUDED.quantity,
        average_cost_paid = EXCLUDED.average_cost_paid,
        imported_at = now()
"""

_UPSERT_MATCH_QUARANTINE_SQL = """
    INSERT INTO collection.match_quarantine (raw_import_id, rejection_reason)
    VALUES (%(raw_import_id)s, %(rejection_reason)s)
    ON CONFLICT (raw_import_id)
    DO UPDATE SET
        rejection_reason = EXCLUDED.rejection_reason,
        imported_at = now()
"""


def load_raw_import(conn: Connection, rows: list[CollectionRow]) -> list[int]:
    """Charge une liste de CollectionRow dans collection.raw_import.
    Idempotent sur (set_name, card_number, product_name, variance, grade) :
    rejouer met à jour la ligne existante (ex: quantité modifiée) au lieu de
    la dupliquer. Renvoie la liste des id (insérés ou mis à jour), dans le
    MÊME ORDRE que `rows` -- l'appelant peut donc faire
    zip(rows, load_raw_import(conn, rows)) pour retrouver l'id de chaque ligne."""
    ids: list[int] = []
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                _UPSERT_RAW_IMPORT_SQL,
                {
                    "set_name": row.set_name,
                    "card_number": row.card_number,
                    "product_name": row.product_name,
                    "variance": row.variance,
                    "grade": row.grade,
                    "rarity": row.rarity,
                    "quantity": row.quantity,
                    "average_cost_paid": row.average_cost_paid,
                    "market_price_at_export": row.market_price_at_export,
                },
            )
            (new_id,) = cur.fetchone()
            ids.append(new_id)
    logger.info("collection.raw_import : %d lignes chargées", len(ids))
    return ids


def load_owned_cards(conn: Connection, matched: list[tuple[int, MatchResult]]) -> int:
    """Charge les MatchResult RÉUSSIS (card_id renseigné) dans
    prod.dim_owned_card. `matched` est une liste de (raw_import_id,
    MatchResult) -- raw_import_id n'est pas utilisé dans cette table (elle
    référence directement card_id, pas raw_import), gardé uniquement pour
    une signature d'appel cohérente avec load_match_quarantine. Idempotent
    sur (card_id, variance, grade)."""
    rows = [
        {
            "card_id": match.card_id,
            "variance": match.row.variance,
            "grade": match.row.grade,
            "quantity": match.row.quantity,
            "average_cost_paid": match.row.average_cost_paid,
        }
        for _, match in matched
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_OWNED_CARD_SQL, rows)
    logger.info("prod.dim_owned_card : %d cartes chargées", len(rows))
    return len(rows)


def load_match_quarantine(conn: Connection, unmatched: list[tuple[int, MatchResult]]) -> int:
    """Charge les MatchResult ÉCHOUÉS (card_id=None) dans
    collection.match_quarantine. `unmatched` est une liste de
    (raw_import_id, MatchResult). Idempotent sur raw_import_id."""
    rows = [
        {"raw_import_id": raw_id, "rejection_reason": match.rejection_reason}
        for raw_id, match in unmatched
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_MATCH_QUARANTINE_SQL, rows)
    logger.warning("collection.match_quarantine : %d cartes non reliées", len(rows))
    return len(rows)
