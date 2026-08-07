# Test d'intégration bout en bout : vérifie que rejouer DEUX FOIS le pipeline
# complet (raw -> staging -> prod) pour le MÊME jour ne produit ni doublons ni
# lignes supplémentaires -- ET que ce n'est pas un simple "ON CONFLICT DO
# NOTHING" qui ignorerait silencieusement le second passage : une carte
# valide voit son prix mis à jour (UPSERT réel), et une carte invalide dès le
# départ passe par le chemin de quarantaine et y reste idempotente elle aussi
# (une seule ligne malgré 2 passages, cohérent avec la contrainte UNIQUE
# posée par migrations/004_add_quarantine_unique_constraint.sql). C'est la
# propriété d'idempotence que chaque étage a été conçu individuellement pour
# garantir (Mois 1 : UPSERT dans load_cards ; Task 2 : UPSERT staging ET
# quarantaine ; Task 3 : UPSERT staging->prod) -- ce test vérifie que ces
# garanties LOCALES tiennent aussi bout en bout, une fois les trois étages
# enchaînés comme le fait le DAG (mais sans passer par Airflow : on appelle
# directement les fonctions Python, plus rapide et plus simple à déboguer
# qu'un test qui déclencherait un vrai DAG run).
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

    # Carte VALIDE : traverse tout le pipeline jusqu'à prod. Son prix
    # (averageSellPrice) sera modifié entre les deux passages ci-dessous
    # (12.5 -> 20.0) pour prouver que le second passage effectue un vrai
    # UPDATE et non un no-op silencieux (voir la boucle plus bas).
    valid_card = {
        "id": "base1-1",
        "name": "Alakazam",
        "rarity": "Rare Holo",
        "set": {"id": "base1", "name": "Base"},
        "tcgplayer": {"prices": {"normal": {"low": 8.0, "mid": 13.0, "market": 12.5}}},
    }
    # Carte INVALIDE dès le départ (prix négatif) : reprend exactement le cas
    # déjà couvert par test_validate_and_clean_rejects_negative_price dans
    # tests/test_transform.py, pour rester cohérent avec les règles de
    # validation testées ailleurs. Présente dans les DEUX passages, à
    # l'identique (contrairement à valid_card, dont seul le prix change) :
    # l'objectif ici n'est pas de prouver un UPDATE sur son contenu, mais que
    # la ligne de quarantaine ne se DUPLIQUE PAS au second passage.
    invalid_card = {
        "id": "base1-2",
        "name": "Machamp",
        "rarity": "Rare Holo",
        "set": {"id": "base1", "name": "Base"},
        "tcgplayer": {"prices": {"normal": {"market": -1.0}}},
    }

    # Rejoue le pipeline complet DEUX FOIS avec la même extracted_date. Entre
    # les deux passages, le prix de la carte valide change (12.5 -> 20.0) :
    # si load_staging/load_staging_to_warehouse faisaient un "ON CONFLICT DO
    # NOTHING" au lieu d'un vrai UPDATE, le prix final resterait 12.5 --
    # l'assertion sur average_sell_price en fin de test distinguerait donc un
    # UPSERT réel d'un simple "ignorer si déjà présent".
    for i in range(2):
        if i == 1:
            valid_card["tcgplayer"]["prices"]["normal"]["market"] = 20.0
        load_cards(db_connection, [valid_card, invalid_card], extracted_date=extracted_date)
        clean_raw_to_staging(db_connection, extracted_date)
        load_staging_to_warehouse(db_connection, extracted_date)

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.card_prices;")
        (raw_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM staging.card_prices;")
        (staging_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM staging.card_prices_quarantine;")
        (quarantine_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM prod.dim_card;")
        (dim_card_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM prod.fact_price_history;")
        (fact_count,) = cur.fetchone()
        cur.execute(
            "SELECT average_sell_price FROM prod.fact_price_history WHERE card_id = %s;",
            (valid_card["id"],),
        )
        (stored_price,) = cur.fetchone()

    # Deux cartes distinctes (valide + invalide), chacune une seule ligne en
    # raw malgré 2 passages : load_cards est idempotent (Mois 1, UPSERT sur
    # card_id + extracted_date).
    assert raw_count == 2
    # Seule la carte valide traverse jusqu'en staging.
    assert staging_count == 1
    # Seule la carte invalide finit en quarantaine -- et une seule fois
    # malgré 2 passages : c'est la preuve que la contrainte UNIQUE posée par
    # migrations/004_add_quarantine_unique_constraint.sql (et l'UPSERT
    # correspondant dans load_quarantine) empêche bien l'accumulation de
    # doublons en quarantaine, pas seulement en staging.
    assert quarantine_count == 1
    # dim_card ne doit pas dupliquer la carte valide entre les deux passages.
    assert dim_card_count == 1
    assert fact_count == 1
    # Preuve concrète de l'UPSERT (pas d'un no-op) : le prix stocké en prod
    # est celui du SECOND passage (20.0), pas celui du premier (12.5) --
    # si un des étages avait fait un "ON CONFLICT DO NOTHING" au lieu d'un
    # DO UPDATE, cette assertion échouerait avec stored_price == 12.5.
    assert stored_price == 20.0
