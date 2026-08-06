# Tests du chargement staging -> prod (src/load/warehouse_loader.py). Même
# pattern que tests/test_staging_loader.py (Task 2) et tests/test_raw_loader.py
# (Mois 1) : ces tests tournent contre une VRAIE connexion Postgres (le
# conteneur docker-compose), pas un mock, parce que le comportement vérifié
# (upsert des dimensions AVANT le fait, résolution de platform_id via
# sous-requête, contrainte UNIQUE sur fact_price_history posée par
# migrations/003_create_star_schema.sql) est une garantie de la base de
# données elle-même, pas juste "j'ai bien appelé execute()".
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.staging_loader import load_staging
from src.load.warehouse_loader import load_staging_to_warehouse
from src.transform.validate import CleanedCard


def _admin_dsn() -> str:
    # Connexion ADMIN (postgres), pas pipeline_app : seul l'admin peut
    # TRUNCATE (pipeline_app n'a que SELECT/INSERT/UPDATE, pas de DELETE, voir
    # migrations/002_...sql et migrations/003_...sql). Nécessaire pour
    # repartir d'un état vide avant chaque test.
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    # Setup : on vide staging.card_prices (source de ce loader) ainsi que
    # prod.fact_price_history et prod.dim_card (cibles), pour repartir d'un
    # état propre à chaque test. CASCADE est nécessaire car fact_price_history
    # référence dim_card par clé étrangère (voir migrations/003_...sql) :
    # sans CASCADE, TRUNCATE dim_card échouerait tant que fact_price_history
    # contient des lignes qui la référencent.
    # On ne truncate PAS prod.dim_date ni prod.dim_platform : dim_platform est
    # une table de référence seedée en migration (pas gérée par le pipeline),
    # et dim_date est un calendrier cumulatif partagé entre exécutions - le
    # vider à chaque test n'est pas nécessaire pour isoler ces tests entre eux.
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE staging.card_prices, prod.fact_price_history, prod.dim_card "
            "RESTART IDENTITY CASCADE;"
        )
        admin_conn.commit()

    # La connexion réellement transmise aux tests est celle de l'utilisateur
    # applicatif pipeline_app : ce sont ses droits restreints (pas de DELETE)
    # que load_staging_to_warehouse() utilise en production, donc ce sont eux
    # qu'on veut exercer ici.
    with get_connection(load_db_config()) as conn:
        yield conn


def _seed_card() -> CleanedCard:
    # Carte de test minimale mais complète (tous les champs utilisés par le
    # loader) : sert de fixture partagée aux deux tests ci-dessous, comme
    # dans tests/test_staging_loader.py.
    return CleanedCard(
        card_id="base1-1",
        name="Alakazam",
        set_id="base1",
        set_name="Base",
        rarity="Rare Holo",
        average_sell_price=12.5,
        trend_price=13.0,
        low_price=8.0,
    )


def test_load_staging_to_warehouse_inserts_fact(db_connection) -> None:
    # On part d'une carte déjà présente en staging (résultat de la Task 2),
    # puis on vérifie que load_staging_to_warehouse() la propage bien vers les
    # DEUX côtés du star schema : la dimension dim_card (métadonnées de la
    # carte) ET le fait fact_price_history (l'observation de prix du jour).
    load_staging(db_connection, [_seed_card()], extracted_date=date(2026, 9, 1))

    inserted = load_staging_to_warehouse(db_connection, extracted_date=date(2026, 9, 1))

    assert inserted == 1
    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM prod.dim_card WHERE card_id = 'base1-1';")
        (dim_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM prod.fact_price_history WHERE card_id = 'base1-1';")
        (fact_count,) = cur.fetchone()
    assert dim_count == 1
    assert fact_count == 1


def test_load_staging_to_warehouse_is_idempotent(db_connection) -> None:
    # Rejouer le chargement du MÊME jour ne doit PAS créer un deuxième fait :
    # la contrainte UNIQUE (card_id, date_id, platform_id) posée sur
    # fact_price_history (migrations/003_...sql) + le ON CONFLICT DO UPDATE de
    # _UPSERT_FACT_SQL garantissent qu'un rejeu (ex: retry du DAG après crash,
    # Task 4 du Mois 2) met juste à jour la ligne existante au lieu de la
    # dupliquer. Sans ce test, une régression sur la clause ON CONFLICT
    # passerait inaperçue jusqu'à un doublon en production.
    load_staging(db_connection, [_seed_card()], extracted_date=date(2026, 9, 1))

    load_staging_to_warehouse(db_connection, extracted_date=date(2026, 9, 1))
    load_staging_to_warehouse(db_connection, extracted_date=date(2026, 9, 1))

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM prod.fact_price_history WHERE card_id = 'base1-1';")
        (count,) = cur.fetchone()
    assert count == 1
