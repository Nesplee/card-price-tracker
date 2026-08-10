# Tests du chargement en staging (src/load/staging_loader.py). Même pattern
# que tests/test_raw_loader.py (Mois 1) : ces tests tournent contre une VRAIE
# connexion Postgres (le conteneur docker-compose), pas un mock, parce que le
# comportement vérifié (upsert via ON CONFLICT, contrainte UNIQUE posée par
# migrations/002_create_staging_tables.sql) est une garantie de la base de
# données elle-même. Un mock ne testerait que "j'ai bien appelé execute()",
# pas "la contrainte SQL empêche vraiment les doublons".
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.staging_loader import load_quarantine, load_staging
from src.transform.validate import CleanedCard


def _admin_dsn() -> str:
    # Connexion ADMIN (postgres), pas pipeline_app : seul l'admin peut
    # TRUNCATE (pipeline_app n'a que SELECT/INSERT/UPDATE sur staging.*,
    # voir migrations/002_create_staging_tables.sql). Nécessaire pour
    # repartir d'un état vide avant chaque test.
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    # Setup : on vide les DEUX tables staging (card_prices ET sa quarantaine)
    # avant chaque test qui demande ce fixture, pour que les tests
    # d'idempotence et de quarantaine ne se polluent pas entre eux (ou avec
    # des données laissées par une exécution précédente du pipeline).
    # RESTART IDENTITY remet aussi à zéro les séquences bigserial des deux
    # tables.
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE staging.card_prices, staging.card_prices_quarantine RESTART IDENTITY;"
        )
        admin_conn.commit()

    # La connexion réellement transmise aux tests est celle de l'utilisateur
    # applicatif pipeline_app : ce sont ses droits restreints (pas de DELETE)
    # que load_staging()/load_quarantine() utilisent en production, donc ce
    # sont eux qu'on veut exercer ici.
    with get_connection(load_db_config()) as conn:
        yield conn


def test_load_staging_is_idempotent_for_same_day(db_connection) -> None:
    # Rejouer le chargement de la MÊME carte, pour le MÊME jour, ne doit PAS
    # créer une deuxième ligne : la contrainte UNIQUE (card_id, extracted_date,
    # source) + le ON CONFLICT DO UPDATE de _UPSERT_STAGING_SQL garantissent
    # qu'un rejeu (après un crash du DAG, par exemple) met juste à jour la
    # ligne existante. Sans ce test, une régression sur la clause ON CONFLICT
    # passerait inaperçue jusqu'à un doublon en production.
    card = CleanedCard(
        card_id="base1-1",
        name="Alakazam",
        set_id="base1",
        set_name="Base",
        series="Base",
        rarity="Rare Holo",
        average_sell_price=12.5,
        trend_price=13.0,
        low_price=8.0,
    )

    load_staging(db_connection, [card], extracted_date=date(2026, 9, 1))
    load_staging(db_connection, [card], extracted_date=date(2026, 9, 1))

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM staging.card_prices WHERE card_id = 'base1-1';")
        (count,) = cur.fetchone()
    assert count == 1


def test_load_quarantine_records_rejected_rows(db_connection) -> None:
    # load_quarantine() reçoit une liste de tuples (payload_original, raison)
    # — exactement la forme produite par clean_raw_to_staging() quand
    # validate_and_clean() rejette une carte. On vérifie ici que la raison de
    # rejet est bien celle stockée en base (essentiel pour l'audit manuel des
    # cartes en quarantaine), et que load_quarantine() renvoie bien le nombre
    # de lignes insérées.
    rejected = [({"id": "base1-2", "name": "Blastoise"}, "prix négatif (averageSellPrice=-1)")]

    inserted = load_quarantine(db_connection, rejected, extracted_date=date(2026, 9, 1))

    assert inserted == 1
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT rejection_reason FROM staging.card_prices_quarantine WHERE card_id = 'base1-2';"
        )
        (reason,) = cur.fetchone()
    assert reason == "prix négatif (averageSellPrice=-1)"


def test_load_quarantine_is_idempotent_for_same_day(db_connection) -> None:
    # Ajouté suite à une review de code (finding "Important") : avant la
    # contrainte UNIQUE (card_id, extracted_date, source) posée par
    # migrations/004_add_quarantine_unique_constraint.sql, rejouer
    # load_quarantine() pour la MÊME carte rejetée, le MÊME jour, créait une
    # DEUXIÈME ligne de quarantaine au lieu de mettre à jour la première — ce
    # qui casse l'idempotence attendue à chaque retry du DAG (Task 4 du Mois
    # 2, `retries: 2`). Même structure que
    # test_load_staging_is_idempotent_for_same_day ci-dessus et
    # test_load_cards_is_idempotent_for_same_day dans tests/test_raw_loader.py
    # (Mois 1) : on rejoue deux fois le même appel et on vérifie qu'une seule
    # ligne subsiste.
    rejected = [({"id": "base1-3", "name": "Gyarados"}, "prix négatif (averageSellPrice=-1)")]

    load_quarantine(db_connection, rejected, extracted_date=date(2026, 9, 1))
    load_quarantine(db_connection, rejected, extracted_date=date(2026, 9, 1))

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM staging.card_prices_quarantine WHERE card_id = 'base1-3';"
        )
        (count,) = cur.fetchone()
    assert count == 1
