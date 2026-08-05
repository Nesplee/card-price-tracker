# Test d'intégration : vérifie que la base Postgres locale (lancée via
# docker-compose) est correctement initialisée par les migrations, c'est-à-dire
# que le schéma "raw" et la table "raw.card_prices" existent bel et bien, et
# que les GARANTIES posées par la migration (contrainte d'unicité, droits
# restreints de l'utilisateur applicatif) sont réellement effectives et pas
# seulement déclarées dans le fichier SQL.
# Ce test nécessite que `docker compose up -d db` et
# `./scripts/apply_migrations.sh` aient déjà été exécutés.
import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection


def test_raw_schema_and_table_exist() -> None:
    # Ouvre une connexion à la DB en utilisant la config chargée depuis .env.
    with get_connection(load_db_config()) as conn:
        with conn.cursor() as cur:
            # information_schema.tables est une vue système Postgres qui liste
            # toutes les tables existantes ; on vérifie qu'une ligne correspond
            # au schéma "raw" et à la table "card_prices".
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'raw' AND table_name = 'card_prices'
                );
                """)
            # fetchone() renvoie un tuple à un seul élément (le booléen EXISTS) ;
            # on le déballe directement dans la variable "exists".
            (exists,) = cur.fetchone()
    assert exists is True


def test_duplicate_card_id_extracted_date_source_is_rejected() -> None:
    """Vérifie que la contrainte UNIQUE (card_id, extracted_date, source) posée
    dans la migration protège vraiment contre les doublons, et pas seulement
    "sur le papier". On insère deux lignes avec exactement la même clé
    (card_id, extracted_date, source) dans une même transaction, sans passer
    par ON CONFLICT : Postgres doit refuser le deuxième INSERT et lever une
    erreur d'intégrité (psycopg.errors.UniqueViolation)."""
    # pytest.raises(...) : le bloc "with" doit lever exactement ce type
    # d'exception, sinon le test échoue (que ce soit parce qu'aucune exception
    # n'est levée, ou parce qu'une exception d'un autre type est levée).
    with pytest.raises(psycopg.errors.UniqueViolation):
        with get_connection(load_db_config()) as conn:
            with conn.cursor() as cur:
                # Premier INSERT : doit réussir sans problème (clé inédite).
                cur.execute(
                    """
                    INSERT INTO raw.card_prices (card_id, extracted_date, source, payload)
                    VALUES (%s, %s, %s, %s::jsonb)
                    """,
                    ("TEST-DUPLICATE-CARD", "2026-08-05", "test-integrity-check", '{"price": 1}'),
                )
                # Deuxième INSERT : même (card_id, extracted_date, source) que
                # le premier -> viole la contrainte UNIQUE et doit lever
                # UniqueViolation. L'exception remonte à travers get_connection,
                # qui fait un ROLLBACK avant de la relaisser passer (voir
                # src/common/db.py) : les DEUX inserts sont donc annulés, ce
                # qui rend ce test rejouable à l'infini sans nettoyage manuel.
                cur.execute(
                    """
                    INSERT INTO raw.card_prices (card_id, extracted_date, source, payload)
                    VALUES (%s, %s, %s, %s::jsonb)
                    """,
                    ("TEST-DUPLICATE-CARD", "2026-08-05", "test-integrity-check", '{"price": 2}'),
                )


def test_pipeline_app_cannot_delete_from_raw_card_prices() -> None:
    """Vérifie que pipeline_app (l'utilisateur applicatif créé par la
    migration) a bien des droits minimaux : la migration ne lui accorde que
    SELECT/INSERT/UPDATE sur raw.card_prices, jamais DELETE, pour qu'une
    exécution buguée du pipeline ne puisse jamais effacer l'historique des
    prix. get_connection(load_db_config()) se connecte avec les identifiants
    de .env, c'est-à-dire précisément en tant que pipeline_app (et non
    l'admin) : un DELETE doit donc être refusé par Postgres lui-même
    (psycopg.errors.InsufficientPrivilege), indépendamment de toute logique
    applicative."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with get_connection(load_db_config()) as conn:
            with conn.cursor() as cur:
                # La ligne ciblée n'a même pas besoin d'exister : Postgres
                # vérifie les droits sur la table AVANT d'évaluer le WHERE,
                # donc la requête échoue par manque de permission, pas parce
                # que la ligne est absente.
                cur.execute("DELETE FROM raw.card_prices WHERE card_id = 'does-not-exist';")
