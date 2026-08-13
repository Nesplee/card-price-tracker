# Fournit la connexion Postgres utilisée par tous les endpoints de l'API du
# dashboard. Toujours via le rôle dashboard_reader (lecture seule, jamais
# pipeline_app) -- voir src/common/config.py:load_dashboard_reader_config().
from __future__ import annotations

from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from src.common.config import load_dashboard_reader_config


def get_api_connection() -> Iterator[psycopg.Connection]:
    # Générateur utilisé comme dépendance FastAPI (Depends(get_api_connection)) :
    # FastAPI exécute le code avant le yield à l'entrée de la requête HTTP, et
    # le code après (ici, conn.close() dans finally) une fois la réponse envoyée.
    # row_factory=dict_row : chaque ligne renvoyée par conn.execute(...).fetchall()
    # est un dict {nom_colonne: valeur} plutôt qu'un tuple positionnel -- plus
    # lisible et moins fragile si l'ordre des colonnes d'une requête change.
    cfg = load_dashboard_reader_config()
    conn = psycopg.connect(cfg.dsn, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()
