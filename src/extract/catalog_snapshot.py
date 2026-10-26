# Catalogue du jour sans appel à pokemontcg.io (ajouté le 2026-10-26).
# Depuis que les prix viennent de TCGCSV (voir src/extract/tcgcsv_prices.py),
# pokemontcg.io ne sert plus qu'au catalogue (noms, sets, raretés), qui ne
# change qu'à la sortie d'une extension. Le retélécharger chaque matin (~80
# pages, sur une API instable qui demandait parfois des dizaines de retries)
# n'apportait plus rien : le DAG quotidien copie donc le dernier catalogue
# connu, et le DAG hebdomadaire card_catalog_refresh le rafraîchit.
from __future__ import annotations

import logging
from datetime import date

from psycopg import Connection

logger = logging.getLogger(__name__)

SOURCE = "pokemontcg.io"


def copy_previous_catalog(conn: Connection, extracted_date: date) -> int:
    """Copie dans extracted_date les cartes du dernier jour chargé AVANT lui.
    ON CONFLICT DO NOTHING : les cartes déjà présentes pour extracted_date
    (rafraîchissement hebdomadaire du jour, même partiel, ou relance de la
    tâche) ne sont pas écrasées -- seules les manquantes sont complétées.
    Le payload copié contient encore les prix de la veille : ils sont
    réécrits juste après par refresh_tcgplayer_prices. Retourne le nombre de
    cartes ajoutées."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(extracted_date) FROM raw.card_prices "
            "WHERE extracted_date < %s AND source = %s",
            (extracted_date, SOURCE),
        )
        previous = cur.fetchone()[0]
        if previous is None:
            raise RuntimeError(
                f"Aucun catalogue antérieur au {extracted_date} : lancer d'abord le DAG "
                "card_catalog_refresh"
            )
        cur.execute(
            """
            INSERT INTO raw.card_prices (card_id, extracted_date, source, payload)
            SELECT card_id, %s, source, payload
            FROM raw.card_prices
            WHERE extracted_date = %s AND source = %s
            ON CONFLICT (card_id, extracted_date, source) DO NOTHING
            """,
            (extracted_date, previous, SOURCE),
        )
        inserted = cur.rowcount
    logger.info(
        "Catalogue du %s copié vers le %s : %d cartes ajoutées", previous, extracted_date, inserted
    )
    return inserted
