# Recalcule les opportunités (src/signals/opportunities.py) sur des jours déjà
# chargés, pour remplir prod.fact_opportunity rétroactivement. Sert de
# backtest : les vues prod.v_opportunity_outcome et
# prod.v_opportunity_signal_performance mesurent tout de suite si les signaux
# passés avaient raison, sans attendre 30 jours. Idempotent (upsert), une
# transaction par jour.
#
# Usage : python -m scripts.backfill_opportunities 2026-09-06 2026-11-01
from __future__ import annotations

import argparse
import logging
from datetime import date

from src.common.config import load_db_config
from src.common.db import get_connection
from src.signals.opportunities import compute_opportunities

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("start", type=date.fromisoformat)
    parser.add_argument("end", type=date.fromisoformat)
    args = parser.parse_args()

    # Seulement les jours réellement chargés (il y a des trous dans l'historique).
    with get_connection(load_db_config()) as conn:
        days = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT date_id FROM prod.fact_price_history "
                "WHERE date_id BETWEEN %s AND %s ORDER BY date_id",
                (args.start, args.end),
            )
        ]
    totals = {"buy": 0, "sell": 0}
    for day in days:
        with get_connection(load_db_config()) as conn:
            counts = compute_opportunities(conn, day)
        totals["buy"] += counts["buy"]
        totals["sell"] += counts["sell"]
    logger.info("Backfill terminé : %d jours, %s", len(days), totals)


if __name__ == "__main__":
    main()
