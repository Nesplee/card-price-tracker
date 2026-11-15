# Garde-fou : la suite d'intégration vide (TRUNCATE) les tables raw, staging,
# prod et collection avant chaque test. Elle ne doit JAMAIS tourner contre la
# base de production.
#
# Deux verrous, vérifiés une fois au démarrage de la session pytest :
#   1. La base de test doit être désignée EXPLICITEMENT par les variables
#      d'environnement du processus (comme en CI). src/common/config.py appelle
#      load_dotenv() : sans ce verrou, un simple `pytest` lancé dans le dossier
#      de prod prendrait silencieusement les identifiants du .env de prod. On
#      lit donc os.environ ICI, à l'import de conftest, avant tout import de
#      src (pytest importe conftest.py avant les modules de test).
#   2. La base ne doit pas contenir de vraies données : au-delà de
#      MAX_TEST_ROWS lignes dans une table volumineuse de prod, on refuse.
from __future__ import annotations

import os

import psycopg
import pytest

_DB_VARS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB")
_EXPLICIT_DB = all(os.environ.get(name) for name in _DB_VARS)

# Les tests n'insèrent que quelques dizaines de lignes ; la prod en a des
# millions (raw.card_prices) ou des centaines de milliers (fact_price_history).
MAX_TEST_ROWS = 5000
_GUARDED_TABLES = ("raw.card_prices", "staging.card_prices", "prod.fact_price_history")


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']} connect_timeout=5"
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    if not _EXPLICIT_DB:
        pytest.exit(
            "Refus : la base de test doit être désignée par les variables d'environnement "
            f"{', '.join(_DB_VARS)} (et POSTGRES_ADMIN_*, POSTGRES_APP_*), jamais par le .env "
            "du projet. Lancer un Postgres jetable et exporter ces variables (voir README, Tests).",
            returncode=2,
        )
    try:
        conn = psycopg.connect(_admin_dsn())
    except (KeyError, psycopg.OperationalError):
        # Base injoignable ou identifiants admin absents : les tests
        # d'intégration échoueront d'eux-mêmes, sans rien toucher.
        return
    with conn:
        for table in _GUARDED_TABLES:
            (exists,) = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
            if not exists:
                continue
            (rows,) = conn.execute(
                f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT {MAX_TEST_ROWS + 1}) t"
            ).fetchone()
            if rows > MAX_TEST_ROWS:
                pytest.exit(
                    f"Refus : {table} contient plus de {MAX_TEST_ROWS} lignes, cette base "
                    "ressemble à la production. Les tests la videraient (TRUNCATE).",
                    returncode=2,
                )
