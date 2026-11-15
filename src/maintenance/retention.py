# Rétention glissante des tables de rejeu (raw.card_prices, staging.card_prices,
# staging.card_prices_quarantine). L'historique des prix reste entier dans
# prod.fact_price_history : seules les copies brutes et intermédiaires, qui ne
# servent qu'à rejouer le pipeline, sont limitées à KEEP_DAYS jours.
#
# La suppression elle-même est faite en base par raw.purge_oldest_day()
# (migration 016), une fonction SECURITY DEFINER qui borne ce qui peut être
# supprimé (trois tables, fenêtre d'au moins 30 jours comptée depuis le dernier
# jour de raw). pipeline_app n'a toujours aucun droit DELETE direct.
from __future__ import annotations

import logging
from datetime import date

from psycopg import Connection

logger = logging.getLogger(__name__)

# Fenêtre gardée, en jours calendaires comptés depuis le dernier jour présent
# dans raw.card_prices (décision du 2026-11-15). Le plancher de 30 jours est
# imposé par la fonction SQL, pas ici.
KEEP_DAYS = 60

# Ordre : les tables dérivées d'abord, raw en dernier. Sans clé étrangère entre
# elles, l'ordre ne change rien au résultat ; il évite seulement qu'une purge
# interrompue laisse un jour en staging dont la source raw aurait disparu.
RETENTION_TABLES = (
    "staging.card_prices_quarantine",
    "staging.card_prices",
    "raw.card_prices",
)


def purge_table(conn: Connection, table: str, keep_days: int = KEEP_DAYS) -> list[tuple[date, int]]:
    """Supprime, un jour à la fois, les jours de `table` sortis de la fenêtre.
    COMMIT après chaque jour : une purge interrompue garde le travail fait.
    Retourne [(jour supprimé, lignes supprimées), ...] dans l'ordre."""
    purged: list[tuple[date, int]] = []
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT purged_date, deleted_rows FROM raw.purge_oldest_day(%s, %s)",
                (table, keep_days),
            )
            row = cur.fetchone()
        conn.commit()
        if row is None:
            return purged
        if purged and row[0] <= purged[-1][0]:
            # Ne devrait jamais arriver (chaque appel vide entièrement le plus
            # vieux jour) : on s'arrête plutôt que de boucler sans fin.
            raise RuntimeError(f"Purge de {table} bloquée sur le jour {row[0]}")
        purged.append((row[0], int(row[1])))


def purge_old_days(
    conn: Connection, keep_days: int = KEEP_DAYS
) -> dict[str, list[tuple[date, int]]]:
    """Applique la rétention aux trois tables et journalise le bilan."""
    report = {}
    for table in RETENTION_TABLES:
        purged = purge_table(conn, table, keep_days)
        report[table] = purged
        if purged:
            logger.info(
                "Rétention %s : %d jours supprimés (%s → %s), %d lignes",
                table,
                len(purged),
                purged[0][0],
                purged[-1][0],
                sum(rows for _, rows in purged),
            )
        else:
            logger.info("Rétention %s : rien à supprimer (fenêtre %d jours)", table, keep_days)
    return report


def quarantine_reasons(
    conn: Connection, day: date | None = None
) -> list[tuple[date, str, str, int]]:
    """Motifs de quarantaine du jour `day` (par défaut le dernier jour présent),
    lus dans la vue staging.v_quarantine_reasons_daily (migration 016). Le
    veilleur lit la même vue ; ici on se contente de les journaliser."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT extracted_date, source, rejection_reason, rejected
            FROM staging.v_quarantine_reasons_daily
            WHERE extracted_date = coalesce(
                %s, (SELECT max(extracted_date) FROM staging.card_prices_quarantine)
            )
            ORDER BY rejected DESC, rejection_reason
            """,
            (day,),
        )
        rows = [(d, source, reason, int(n)) for d, source, reason, n in cur.fetchall()]
    for d, source, reason, n in rows:
        logger.info("Quarantaine %s (%s) : %d × « %s »", d, source, n, reason)
    return rows
