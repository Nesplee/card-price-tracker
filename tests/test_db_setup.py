# Test d'intégration : vérifie que la base Postgres locale (lancée via
# docker-compose) est correctement initialisée par les migrations, c'est-à-dire
# que le schéma "raw" et la table "raw.card_prices" existent bel et bien.
# Ce test nécessite que `docker compose up -d db` et
# `./scripts/apply_migrations.sh` aient déjà été exécutés.
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
