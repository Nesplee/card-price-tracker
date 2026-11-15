# DAG des signaux de contexte (créé le 2026-11-06) : news du marché et méta
# compétitive, lues par compute_opportunities (DAG card_price_pipeline, 07:00).
#   fetch_news   -> analyze_news : flux RSS -> raw.news_item -> LLM local ->
#                                  prod.fact_news_event ;
#   fetch_meta                   : top 8 des tournois Limitless -> raw.meta_*.
#
# 12:00 et 20:00 UTC : hors de la notation second_brain (03:00, lundi-vendredi,
# même Ollama) et du pipeline des prix (07:00, même CPU). Ollama traite une
# requête à la fois : si second_brain l'utilise, analyze_news attend son tour
# (timeout de lecture long côté client, voir src/signals/news_llm.py).
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from airflow.decorators import dag, task

from src.common.config import load_db_config, load_ollama_config
from src.common.db import get_connection
from src.extract.limitless_meta import LimitlessClient, load_meta
from src.extract.news_feeds import fetch_all, store_news
from src.monitor.failure_callback import record_failure
from src.signals.news_llm import OllamaClient, analyze_pending_news


@dag(
    schedule="0 12,20 * * *",
    # start_date passée et catchup=False : mêmes raisons que dans
    # card_price_pipeline_dag.py (voir ses commentaires).
    start_date=datetime(2024, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=2),
    # C1 (2026-11-15) : échecs enregistrés (logs/failures.jsonl), sans ntfy.
    on_failure_callback=record_failure,
    default_args={"on_failure_callback": record_failure},
)
def card_market_news():
    # Sources externes : quelques retries espacés. Un flux en panne est sauté
    # par fetch_all, la tâche n'échoue que si tous le sont.
    @task(retries=2, retry_delay=timedelta(minutes=5))
    def fetch_news() -> None:
        items = fetch_all()
        with get_connection(load_db_config()) as conn:
            store_news(conn, items)

    # Budget interne de 40 min (voir news_llm.TIME_BUDGET_S) ;
    # execution_timeout au-dessus, au cas où un appel Ollama resterait bloqué.
    # retries=1 : seul cas d'échec attendu, Ollama injoignable.
    @task(retries=1, retry_delay=timedelta(minutes=10), execution_timeout=timedelta(minutes=55))
    def analyze_news() -> None:
        with get_connection(load_db_config()) as conn:
            analyze_pending_news(conn, OllamaClient(load_ollama_config()))

    @task(retries=2, retry_delay=timedelta(minutes=5))
    def fetch_meta() -> None:
        with get_connection(load_db_config()) as conn:
            load_meta(conn, LimitlessClient())

    fetch_news() >> analyze_news()
    fetch_meta()


card_market_news()
