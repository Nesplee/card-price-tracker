# DAG quotidien : prix des cartes ANGLAISES sur le marché européen, lus sur
# l'API officielle CardTrader (plateforme cardtrader_en, CHF, migration 018).
# Voir src/load/cardtrader_loader.py et ~/mere/rapports/cardmarket-prix-2026-11-17.md.
#
# 06:30 UTC (07:30 jusqu'au 2026-11-18) : AVANT le pipeline TCGplayer (07:00),
# dont les signaux lisent le prix CardTrader du jour (cible atteinte, prix
# minimum de vente : dashboard en CHF, migration 022). Une collecte qui
# déborde après 07:00 n'est pas grave : les signaux prennent alors le dernier
# prix CardTrader de moins de 3 jours. Hors des créneaux chargés de la machine
# (01:30 sauvegarde, 03:00 notation second_brain, 07:00 prix, 12:00 et 20:00
# news). Indépendant du DAG TCGplayer : il ne lit que le catalogue
# (prod.dim_card) et la collection, et n'écrit que ses propres lignes.
#
# Créé EN PAUSE (is_paused_upon_creation) : il ne tourne qu'une fois activé à
# la main dans Airflow, après accord de l'utilisateur et application de la
# migration 018. Jeton : CARDTRADER_API_TOKEN, lu dans le .env monté dans le
# conteneur (load_dotenv de src/common/config.py), jamais journalisé. Le .env
# de l'hôte ayant été remplacé après le démarrage des conteneurs, il faut les
# redémarrer avant d'activer ce DAG (voir README, Deployment).
# Aucune alerte ntfy : un échec est enregistré par record_failure
# (logs/failures.jsonl), relevé par le veilleur.
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from airflow.decorators import dag, task
from airflow.models import DagRun

from src.common.config import load_db_config
from src.common.db import get_connection
from src.extract.cardtrader_prices import CardTraderClient
from src.load.cardtrader_loader import run_cardtrader_prices
from src.monitor.failure_callback import record_failure


@dag(
    schedule="30 6 * * *",
    start_date=datetime(2024, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    dagrun_timeout=timedelta(hours=1),
    on_failure_callback=record_failure,
    default_args={"on_failure_callback": record_failure},
)
def card_cardtrader_prices():
    # Une seule tâche, tout ou rien : la collecte (GET seulement) puis
    # l'écriture dans une transaction. Un set en erreur fait échouer le jour
    # entier plutôt que de laisser des prix partiels ; les retries rejouent
    # le même jour (dag_run.start_date), l'écriture est idempotente.
    @task(retries=2, retry_delay=timedelta(minutes=10), execution_timeout=timedelta(minutes=30))
    def collect_cardtrader_prices(dag_run: DagRun) -> dict:
        date_id = (dag_run.start_date or datetime.now(UTC)).date()
        token = os.environ.get("CARDTRADER_API_TOKEN")
        if not token:
            # Piège vu le 2026-11-18 : un .env REMPLACÉ sur l'hôte (nouveau
            # fichier) n'est pas vu par le conteneur, qui garde l'ancien
            # montage jusqu'à son redémarrage.
            raise RuntimeError(
                "CARDTRADER_API_TOKEN absent du .env monté dans le conteneur : "
                "montage périmé ? Redémarrer airflow-scheduler et airflow-webserver."
            )
        client = CardTraderClient(token)
        with get_connection(load_db_config()) as conn:
            return run_cardtrader_prices(conn, client, date_id)

    collect_cardtrader_prices()


card_cardtrader_prices()
