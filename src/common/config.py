# Ce module centralise la lecture de la configuration de la base de données
# depuis les variables d'environnement (fichier .env). Toutes les tâches qui
# ont besoin de se connecter à Postgres passent par load_db_config() plutôt
# que de relire os.environ elles-mêmes : un seul endroit à modifier si la
# façon de charger la config change.
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# load_dotenv() lit le fichier .env à la racine du projet et injecte ses
# variables dans os.environ (sans écraser des variables déjà définies dans
# l'environnement système). Appelé une seule fois à l'import du module.
load_dotenv()


# @dataclass(frozen=True) génère automatiquement __init__, __repr__, __eq__
# à partir des champs déclarés ci-dessous ; frozen=True rend les instances
# immuables (impossible de modifier un champ après création), ce qui évite
# qu'un bout de code modifie la config par erreur en cours de route.
@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    @property
    def dsn(self) -> str:
        # dsn = "Data Source Name" : la chaîne de connexion au format attendu
        # par psycopg (libpq). @property permet d'appeler config.dsn comme un
        # simple attribut plutôt que config.dsn() comme une méthode.
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


def _require_env(name: str) -> str:
    # Lit une variable d'environnement obligatoire. Si elle est absente ou
    # vide, on échoue tout de suite avec un message clair plutôt que de
    # laisser une chaîne vide se propager silencieusement jusqu'à la
    # connexion Postgres (où l'erreur serait plus difficile à diagnostiquer).
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Variable d'environnement manquante : {name}")
    return value


def load_db_config() -> DatabaseConfig:
    # Construit un DatabaseConfig à partir des variables d'environnement
    # définies dans .env. int(...) convertit le port (chaîne) en entier car
    # psycopg/DatabaseConfig attend un int, pas une string.
    return DatabaseConfig(
        host=_require_env("POSTGRES_HOST"),
        port=int(_require_env("POSTGRES_PORT")),
        dbname=_require_env("POSTGRES_DB"),
        user=_require_env("POSTGRES_APP_USER"),
        password=_require_env("POSTGRES_APP_PASSWORD"),
    )
