# DAG de rétention : supprime chaque jour les jours de raw.card_prices,
# staging.card_prices et staging.card_prices_quarantine sortis de la fenêtre
# glissante (KEEP_DAYS, voir src/maintenance/retention.py), puis journalise les
# motifs de quarantaine du dernier jour.
#
# 09:30 UTC : après le pipeline de prix (07:00) et hors des créneaux chargés de
# la machine (01:30 sauvegarde, 03:00 notation second_brain, 12:00 et 20:00
# news). Aucune alerte ntfy ici : un échec reste visible dans Airflow, où le
# veilleur le relève.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from airflow.decorators import dag, task

from src.common.config import load_db_config
from src.common.db import get_connection
from src.maintenance.retention import KEEP_DAYS, purge_old_days, quarantine_reasons


@dag(
    schedule="30 9 * * *",
    start_date=datetime(2024, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=1),
)
def card_raw_retention():
    @task(retries=2, retry_delay=timedelta(minutes=10))
    def log_quarantine_reasons() -> None:
        with get_connection(load_db_config()) as conn:
            quarantine_reasons(conn)

    @task(retries=2, retry_delay=timedelta(minutes=10))
    def purge() -> None:
        with get_connection(load_db_config()) as conn:
            purge_old_days(conn, KEEP_DAYS)

    log_quarantine_reasons() >> purge()


card_raw_retention()
