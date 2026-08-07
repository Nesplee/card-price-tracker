# Tests du chargement idempotent en base (src/load/raw_loader.py). Ces tests
# tournent contre une VRAIE connexion Postgres (le conteneur docker-compose
# lancé en Task 2), pas un mock : le comportement vérifié ici (upsert via
# ON CONFLICT, contrainte d'unicité...) est une garantie de la base de
# données elle-même, qu'un mock ne reproduirait pas fidèlement.
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.raw_loader import load_cards


def _admin_dsn() -> str:
    # Chaîne de connexion utilisant les identifiants ADMIN (postgres), pas
    # ceux de l'utilisateur applicatif pipeline_app : seul l'admin a le droit
    # de TRUNCATE (pipeline_app n'a que SELECT/INSERT/UPDATE, voir
    # migrations/001_...sql). On en a besoin pour vider la table avant chaque
    # test et repartir d'un état connu.
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    # Partie "setup" du fixture, exécutée avant chaque test qui le demande en
    # paramètre : on ouvre une connexion admin séparée, le temps de vider
    # raw.card_prices (TRUNCATE ... RESTART IDENTITY remet aussi à zéro le
    # compteur bigserial), pour que chaque test parte d'une table vide et ne
    # soit pas pollué par un test précédent (ou par des cartes déjà chargées
    # via scripts/run_extract_load.py).
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute("TRUNCATE TABLE raw.card_prices RESTART IDENTITY;")
        admin_conn.commit()

    # La connexion réellement transmise au test est celle de l'utilisateur
    # applicatif pipeline_app (via load_db_config()) : ce sont ces droits
    # restreints (pas de DELETE) que load_cards() utilise en production, donc
    # ce sont eux qu'on veut exercer ici. "yield conn" redonne la main au
    # test, qui reçoit "conn" en argument ; à la fin du test, l'exécution
    # reprend juste après le yield et le "with get_connection(...)" se
    # termine (fermeture de la connexion).
    with get_connection(load_db_config()) as conn:
        yield conn


def test_load_cards_inserts_new_rows(db_connection) -> None:
    # Cas nominal : une carte inédite doit être insérée telle quelle, et
    # load_cards() doit renvoyer le nombre de lignes traitées (1 ici).
    cards = [{"id": "base1-1", "name": "Alakazam"}]

    inserted = load_cards(db_connection, cards, extracted_date=date(2026, 8, 20))

    assert inserted == 1
    with db_connection.cursor() as cur:
        cur.execute("SELECT card_id, extracted_date FROM raw.card_prices;")
        rows = cur.fetchall()
    assert rows == [("base1-1", date(2026, 8, 20))]


def test_load_cards_is_idempotent_for_same_day(db_connection) -> None:
    # Rejouer l'extraction du MÊME jour (même extracted_date) avec des
    # données mises à jour (price passe de 1.0 à 2.0) ne doit PAS créer une
    # deuxième ligne : la clé (card_id, extracted_date, source) étant
    # identique aux deux appels, l'upsert (ON CONFLICT ... DO UPDATE) doit
    # écraser le payload de la ligne existante au lieu d'en insérer une
    # nouvelle. C'est ce qui permet de relancer le pipeline plusieurs fois
    # dans la même journée (ex : après un crash en cours d'extraction) sans
    # dupliquer les données.
    cards = [{"id": "base1-1", "name": "Alakazam", "price": 1.0}]
    updated_cards = [{"id": "base1-1", "name": "Alakazam", "price": 2.0}]

    load_cards(db_connection, cards, extracted_date=date(2026, 8, 20))
    load_cards(db_connection, updated_cards, extracted_date=date(2026, 8, 20))

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.card_prices WHERE card_id = 'base1-1';")
        (count,) = cur.fetchone()
    assert count == 1


def test_load_cards_keeps_history_across_different_days(db_connection) -> None:
    # LE test qui protège contre une régression silencieuse sur l'historique
    # des prix : charger la MÊME carte à deux dates DIFFÉRENTES doit produire
    # DEUX lignes distinctes, pas une mise à jour de la même ligne. Si un jour
    # quelqu'un change la clé d'idempotence pour n'utiliser que card_id (au
    # lieu de (card_id, extracted_date, source)), ce test échoue
    # immédiatement : count vaudrait 1 au lieu de 2, l'historique inter-jours
    # aurait silencieusement disparu (chaque nouvelle extraction écraserait
    # les prix des jours précédents).
    cards = [{"id": "base1-1", "name": "Alakazam"}]

    load_cards(db_connection, cards, extracted_date=date(2026, 8, 20))
    load_cards(db_connection, cards, extracted_date=date(2026, 8, 21))

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.card_prices WHERE card_id = 'base1-1';")
        (count,) = cur.fetchone()
    assert count == 2
