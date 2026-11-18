# DAG quotidien : cours de référence de la BCE (USD et CHF contre l'euro),
# pour afficher le dashboard en francs suisses (migration 020, voir
# src/extract/ecb_rates.py et src/load/fx_loader.py).
#
# 06:15 UTC : avant la collecte CardTrader (06:30) et le pipeline TCGplayer
# (07:00). La BCE publie le cours du jour D vers 15:00 UTC ; le taux appliqué
# aux prix du jour D est le dernier cours daté avant D, déjà publié la veille.
# Hors des créneaux chargés de la machine (01:30 sauvegarde, 03:00 notation
# second_brain, 07:00 prix, 12:00 et 20:00 news). Deux petits GET publics,
# sans jeton.
#
# Créé EN PAUSE (is_paused_upon_creation) : activé à la main dans Airflow,
# après accord de l'utilisateur et application de la migration 020.
# Aucune alerte ntfy : un échec est enregistré par record_failure
# (logs/failures.jsonl), relevé par le veilleur. Les cours de la migration 020
# et la tolérance de 5 jours laissent le temps de réparer : au-delà, le board
# affiche un bandeau au lieu de prix faux.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from airflow.decorators import dag, task

from src.common.config import load_db_config
from src.common.db import get_connection
from src.extract.ecb_rates import EcbClient
from src.load.fx_loader import run_fx_rates
from src.monitor.failure_callback import record_failure


@dag(
    schedule="15 6 * * *",
    start_date=datetime(2024, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    dagrun_timeout=timedelta(minutes=30),
    on_failure_callback=record_failure,
    default_args={"on_failure_callback": record_failure},
)
def card_fx_rates():
    @task(retries=3, retry_delay=timedelta(minutes=5), execution_timeout=timedelta(minutes=10))
    def load_ecb_rates() -> dict:
        with get_connection(load_db_config()) as conn:
            return run_fx_rates(conn, EcbClient())

    load_ecb_rates()


card_fx_rates()
