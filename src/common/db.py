# Ce module fournit un unique point d'entrée pour ouvrir une connexion
# Postgres, avec une gestion automatique du commit/rollback. Toutes les
# tâches du pipeline qui lisent/écrivent en base utilisent get_connection()
# plutôt que d'appeler psycopg.connect() directement.
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import Connection

from src.common.config import DatabaseConfig, load_db_config


# @contextmanager transforme cette fonction génératrice en objet utilisable
# avec "with get_connection(...) as conn: ...". Le code avant le yield
# s'exécute à l'entrée du bloc "with", le code après (dans try/except/finally)
# s'exécute à la sortie du bloc, que celui-ci ait levé une exception ou non.
@contextmanager
def get_connection(config: DatabaseConfig | None = None) -> Iterator[Connection]:
    """Ouvre une connexion Postgres. COMMIT si le bloc réussit, ROLLBACK sinon —
    garantit qu'une exécution du pipeline n'écrit jamais un état partiel."""
    # Si aucune config n'est fournie par l'appelant, on la charge depuis .env.
    # Cela permet à la fois d'utiliser get_connection() directement (cas
    # courant) et d'injecter une config custom (utile pour les tests).
    cfg = config or load_db_config()
    conn = psycopg.connect(cfg.dsn)
    try:
        # yield conn redonne la main au bloc "with" appelant, qui reçoit la
        # connexion ouverte et exécute ses requêtes dessus.
        yield conn
        # Si le bloc "with" se termine sans exception, on valide (commit)
        # toutes les écritures faites pendant la connexion.
        conn.commit()
    except Exception:
        # Si une exception est levée dans le bloc "with", on annule (rollback)
        # toute écriture partielle avant de relaisser l'exception remonter
        # (raise sans argument = "re-lève l'exception en cours").
        conn.rollback()
        raise
    finally:
        # Que tout se soit bien passé ou non, on ferme toujours la connexion
        # pour ne pas fuir des connexions ouvertes (finally s'exécute dans
        # tous les cas : succès, exception, ou même après le except ci-dessus).
        conn.close()
