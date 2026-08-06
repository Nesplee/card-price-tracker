# Test d'intégration bout en bout : vérifie que rejouer DEUX FOIS le pipeline
# complet (raw -> staging -> prod) pour le MÊME jour ne produit ni doublons ni
# lignes supplémentaires. C'est la propriété d'idempotence que chaque étage a
# été conçu individuellement pour garantir (UPSERT dans load_cards, Task 2 :
# UNIQUE sur la quarantaine, Task 3 : UPSERT staging->prod) -- ce test vérifie
# que ces garanties LOCALES tiennent aussi bout en bout, une fois les trois
# étages enchaînés comme le fait le DAG (mais sans passer par Airflow : on
# appelle directement les fonctions Python, plus rapide et plus simple à
# déboguer qu'un test qui déclencherait un vrai DAG run).
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.raw_loader import load_cards
from src.load.warehouse_loader import load_staging_to_warehouse
from src.transform.clean import clean_raw_to_staging


def _admin_dsn() -> str:
    # DSN admin (pas pipeline_app) : nécessaire pour TRUNCATE ... CASCADE
    # ci-dessous, une opération que le rôle applicatif least-privilege
    # (pipeline_app, voir Mois 1) n'a pas le droit d'exécuter.
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    # Nettoyage AVANT le test (pas après) : si un run précédent a planté
    # avant son propre nettoyage, on repart quand même d'un état propre --
    # plus robuste qu'un nettoyage en fin de test qui pourrait être sauté.
    # RESTART IDENTITY CASCADE : réinitialise aussi les séquences (ids auto-
    # incrémentés) des tables liées par clé étrangère (dim_card, fact_price_
    # history...), pour que ce test parte d'un état identique à chaque run.
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE raw.card_prices, staging.card_prices, "
            "staging.card_prices_quarantine, prod.fact_price_history, prod.dim_card "
            "RESTART IDENTITY CASCADE;"
        )
        admin_conn.commit()

    # Connexion applicative (pipeline_app, pas admin) pour le test lui-même :
    # get_connection() gère commit/rollback automatiquement (voir
    # src/common/db.py) -- cohérent avec le rôle que le pipeline utilise
    # réellement en production.
    with get_connection(load_db_config()) as conn:
        yield conn


def test_full_pipeline_is_idempotent_when_replayed(db_connection) -> None:
    extracted_date = date(2026, 9, 5)
    cards = [
        {
            "id": "base1-1",
            "name": "Alakazam",
            "rarity": "Rare Holo",
            "set": {"id": "base1", "name": "Base"},
            "cardmarket": {
                "prices": {"averageSellPrice": 12.5, "trendPrice": 13.0, "lowPrice": 8.0}
            },
        }
    ]

    # Rejoue le pipeline complet DEUX FOIS avec exactement les mêmes données
    # et la même extracted_date : c'est la définition même de l'idempotence
    # testée ici -- si un des trois étages n'était PAS idempotent (ex: un
    # INSERT sans UPSERT quelque part), ce deuxième passage créerait des
    # doublons détectés par les assertions ci-dessous.
    for _ in range(2):
        load_cards(db_connection, cards, extracted_date=extracted_date)
        clean_raw_to_staging(db_connection, extracted_date)
        load_staging_to_warehouse(db_connection, extracted_date)

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.card_prices;")
        (raw_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM staging.card_prices;")
        (staging_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM prod.fact_price_history;")
        (fact_count,) = cur.fetchone()

    # Une seule carte, une seule extracted_date, rejouée deux fois : à chaque
    # étage, on doit retrouver EXACTEMENT une ligne -- pas deux (ce qui
    # signalerait un doublon au lieu d'une mise à jour).
    assert raw_count == 1
    assert staging_count == 1
    assert fact_count == 1
