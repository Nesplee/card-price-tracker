# Tests de la logique de reprise (resume) de l'orchestration extract->load.
# Depuis le refactor de la Task 4 (Mois 2), cette logique (_resume_page,
# PAGE_SIZE) vit dans src/extract/pipeline.py (partagée par
# scripts/run_extract_load.py ET le DAG Airflow), plus dans
# scripts/run_extract_load.py lui-même -- seul l'import ci-dessous change,
# la logique testée est identique. Comme tests/test_raw_loader.py, ces tests
# tournent contre une VRAIE connexion Postgres (docker-compose), pas un mock :
# ce qu'on vérifie ici (compter les lignes déjà présentes pour un
# (extracted_date, source) donné) est une garantie qui dépend du contenu réel
# de la table raw.card_prices, pas juste de la logique Python.
#
# Pourquoi tester _resume_page et pas run_extract_load()/main() directement ?
# Ces derniers orchestrent aussi les vrais appels réseau à pokemontcg.io (via
# PokemonTcgClient), ce qui en ferait un test lent et flaky (dépendant de la
# disponibilité de l'API externe). _resume_page est la portion de logique
# testable en isolation : "étant donné ce qui est déjà en base, quelle page
# faut-il reprendre ?".
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.extract.pipeline import PAGE_SIZE, _resume_page
from src.load.raw_loader import load_cards


def _admin_dsn() -> str:
    # Même besoin que dans tests/test_raw_loader.py : TRUNCATE exige les
    # droits admin (postgres), pipeline_app n'a pas ce droit (voir
    # migrations/001_...sql, vérifié par test_db_setup.py).
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    # Vide la table avant chaque test (connexion admin, TRUNCATE), puis fournit
    # une connexion pipeline_app (les droits réellement utilisés en
    # production par load_cards et par _resume_page). Identique au fixture de
    # tests/test_raw_loader.py, dupliqué ici plutôt que factorisé dans un
    # conftest.py pour rester cohérent avec le style déjà en place dans le
    # dépôt (un seul fichier de test = un seul fixture autonome).
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute("TRUNCATE TABLE raw.card_prices RESTART IDENTITY;")
        admin_conn.commit()

    with get_connection(load_db_config()) as conn:
        yield conn


def _fake_cards(n: int, prefix: str = "card") -> list[dict]:
    # Génère n cartes factices avec un "id" unique chacune (load_cards() exige
    # card["id"]). Le contenu des autres champs n'a aucune importance pour ces
    # tests : seul le NOMBRE de lignes insérées pour un (extracted_date,
    # source) donné compte pour _resume_page.
    return [{"id": f"{prefix}-{i}"} for i in range(n)]


def test_resume_page_is_one_when_table_empty(db_connection) -> None:
    # Aucune ligne en base pour ce jour/source -> on doit démarrer à la page 1
    # (pas de reprise, extraction depuis le début).
    resume = _resume_page(db_connection, extracted_date=date(2026, 8, 6))

    assert resume == 1


def test_resume_page_after_exactly_one_full_page(db_connection) -> None:
    # PAGE_SIZE lignes déjà présentes = une page COMPLÈTE déjà chargée -> on
    # doit reprendre à la page 2 (la suivante), pas rejouer la page 1.
    load_cards(db_connection, _fake_cards(PAGE_SIZE), extracted_date=date(2026, 8, 6))

    resume = _resume_page(db_connection, extracted_date=date(2026, 8, 6))

    assert resume == 2


def test_resume_page_after_partial_page_redoes_it(db_connection) -> None:
    # PAGE_SIZE + quelques lignes = la dernière page comptée est INCOMPLÈTE
    # (un crash a dû survenir en plein milieu de son chargement). On doit
    # reprendre à la page 2 (PAS 3) : la page 2 est refaite ENTIÈREMENT depuis
    # son début. C'est sans risque car load_cards() est idempotent (UPSERT
    # sur card_id, extracted_date, source) : réinsérer des cartes déjà
    # présentes les met juste à jour, ça ne les duplique pas.
    load_cards(db_connection, _fake_cards(PAGE_SIZE + 42), extracted_date=date(2026, 8, 6))

    resume = _resume_page(db_connection, extracted_date=date(2026, 8, 6))

    assert resume == 2


def test_resume_page_scoped_to_date_and_source(db_connection) -> None:
    # Des lignes existent bien en base, mais pour un AUTRE extracted_date (la
    # veille) et une autre source ("other-source"). Le calcul de reprise pour
    # AUJOURD'HUI / pokemontcg.io (les valeurs par défaut de _resume_page) ne
    # doit PAS les compter : ce n'est pas un simple count(*) sur toute la
    # table, mais un count scopé sur (extracted_date, source). Si ce scoping
    # était cassé (ex: count(*) global), ce test échouerait car resume
    # vaudrait 2 au lieu de 1 (PAGE_SIZE lignes d'un autre jour compteraient
    # à tort comme une page déjà chargée pour aujourd'hui).
    load_cards(
        db_connection,
        _fake_cards(PAGE_SIZE, prefix="yesterday"),
        extracted_date=date(2026, 8, 5),
    )
    load_cards(
        db_connection,
        _fake_cards(PAGE_SIZE, prefix="other-source"),
        extracted_date=date(2026, 8, 6),
        source="other-source",
    )

    resume = _resume_page(db_connection, extracted_date=date(2026, 8, 6))

    assert resume == 1
