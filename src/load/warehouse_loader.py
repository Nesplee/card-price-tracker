# Ce module a la responsabilité finale de la chaîne raw -> staging -> prod :
# il lit les lignes déjà validées et nettoyées de staging.card_prices (Task 2)
# et les charge dans le schéma en étoile de prod (Task 1, migrations/
# 003_create_star_schema.sql). Comme raw_loader.py et staging_loader.py avant
# lui, aucune règle métier de VALIDATION ne vit ici : à ce stade, une ligne de
# staging.card_prices est par définition déjà propre (passée par
# validate_and_clean() puis load_staging()). Ce module ne fait que la
# REDISTRIBUER vers la bonne forme relationnelle (dimensions + fait),
# conformément au pattern établi au Mois 1 et suivi partout depuis : toute
# fonction qui touche la DB prend `conn` en paramètre, seule la couche
# d'orchestration (script/DAG, Task 4 du Mois 2) ouvre get_connection().
#
# Pourquoi un star schema, et pourquoi ce module doit-il exister séparément de
# staging_loader.py ? staging.card_prices est une table "à plat" : chaque
# ligne répète le nom, le set et la rareté de la carte à CHAQUE jour observé.
# C'est voulu en staging (simplicité, un seul upsert par carte/jour) mais
# coûteux et redondant pour l'analyse historique sur plusieurs mois. Le star
# schema sépare ce qui NE CHANGE PRESQUE JAMAIS (les métadonnées d'une carte,
# stockées une seule fois dans dim_card) de ce qui change CHAQUE JOUR (le prix
# observé, une ligne par jour dans fact_price_history qui ne référence la
# carte que par son card_id). Résultat : dim_card reste petite (une ligne par
# carte unique, quel que soit le nombre de jours suivis) et fact_price_history
# ne contient que des mesures numériques + des clés étrangères, ce qui la rend
# rapide à agréger (GROUP BY card_id, date_id, platform_id) et légère à
# stocker dans le temps.
from __future__ import annotations

import logging
from datetime import date

from psycopg import Connection

# Même pattern de logger que dans raw_loader.py et staging_loader.py : un
# logger nommé d'après le module (__name__ = "src.load.warehouse_loader"),
# pour pouvoir filtrer/tracer précisément quelle étape du pipeline a produit
# quel message.
logger = logging.getLogger(__name__)

# Upsert de dim_card : une ligne par carte unique (card_id est sa PRIMARY KEY,
# voir migrations/003_...sql). ON CONFLICT (card_id) DO UPDATE : si la carte
# est déjà connue (observée un jour précédent), on rafraîchit ses métadonnées
# (nom, set, rareté) au cas où la source les aurait légèrement modifiées entre
# deux extractions, plutôt que de les figer à leur toute première valeur vue.
# updated_at = now() trace explicitement CE rafraîchissement (par opposition à
# la date de création de la ligne, qui reste implicite ici : dim_card n'a pas
# de colonne created_at séparée).
_UPSERT_DIM_CARD_SQL = """
    INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity)
    VALUES (%(card_id)s, %(name)s, %(set_id)s, %(set_name)s, %(rarity)s)
    ON CONFLICT (card_id) DO UPDATE SET
        name = EXCLUDED.name, set_id = EXCLUDED.set_id,
        set_name = EXCLUDED.set_name, rarity = EXCLUDED.rarity,
        updated_at = now()
"""

# Upsert de dim_date : une ligne par date calendaire observée. Contrairement à
# dim_card (dont les attributs peuvent changer), une date n'a qu'une seule
# décomposition possible (year/month/day/day_of_week) : si la ligne existe
# déjà, il n'y a rigoureusement rien à mettre à jour. DO NOTHING plutôt que DO
# UPDATE SET ... est donc à la fois plus simple ET plus correct ici : ça évite
# une écriture inutile en base (aucune colonne ne peut avoir changé) tout en
# restant idempotent (rejouer le même jour ne lève pas d'erreur de contrainte
# UNIQUE, ça ignore juste silencieusement le conflit).
_UPSERT_DIM_DATE_SQL = """
    INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week)
    VALUES (%(date_id)s, %(year)s, %(month)s, %(day)s, %(day_of_week)s)
    ON CONFLICT (date_id) DO NOTHING
"""

# Upsert du fait fact_price_history : LA ligne centrale du star schema, une
# observation de prix pour (carte, date, plateforme).
#
# Pourquoi une sous-requête (SELECT platform_id FROM prod.dim_platform WHERE
# platform_name = %(platform_name)s) plutôt qu'un platform_id codé en dur ?
# platform_id est un serial (entier auto-incrémenté, voir migrations/
# 003_...sql) : sa valeur numérique concrète (1, 2, 3...) est un détail
# d'implémentation de CETTE base, pas une donnée stable qu'on peut connaître
# à l'avance ou recopier dans le code Python. Ce module ne connaît que le NOM
# métier de la plateforme ("cardmarket", passé en paramètre de la fonction),
# et laisse Postgres résoudre ce nom vers son identifiant technique au moment
# de l'INSERT. C'est aussi plus robuste : si l'ordre des lignes de
# dim_platform change un jour (ex: table recréée, migration rejouée dans un
# environnement de test), le code Python n'a rien à changer.
#
# ON CONFLICT (card_id, date_id, platform_id) DO UPDATE : repose sur la
# contrainte UNIQUE uq_fact_price_history_card_date_platform posée par
# migrations/003_...sql. Exactement le même mécanisme d'idempotence que dans
# staging_loader.py : rejouer le chargement de la même journée (ex: retry du
# DAG après un crash partiel, Task 4 du Mois 2) met à jour les prix existants
# au lieu d'insérer une deuxième ligne pour la même combinaison carte/date/
# plateforme.
_UPSERT_FACT_SQL = """
    INSERT INTO prod.fact_price_history
        (card_id, date_id, platform_id, average_sell_price, trend_price, low_price)
    SELECT %(card_id)s, %(date_id)s, platform_id,
           %(average_sell_price)s, %(trend_price)s, %(low_price)s
    FROM prod.dim_platform WHERE platform_name = %(platform_name)s
    ON CONFLICT (card_id, date_id, platform_id) DO UPDATE SET
        average_sell_price = EXCLUDED.average_sell_price,
        trend_price = EXCLUDED.trend_price,
        low_price = EXCLUDED.low_price,
        loaded_at = now()
"""


def load_staging_to_warehouse(
    conn: Connection,
    extracted_date: date,
    source: str = "pokemontcg.io",
    platform_name: str = "cardmarket",
) -> int:
    """Charge staging.card_prices vers le star schema. Les dimensions
    (dim_card, dim_date) sont upsertées avant le fait pour respecter les
    contraintes de clé étrangère de fact_price_history."""
    # Pourquoi les dimensions doivent être upsertées AVANT le fait, et pas
    # l'inverse ni en parallèle : fact_price_history.card_id et .date_id sont
    # déclarées `REFERENCES prod.dim_card (card_id)` / `REFERENCES
    # prod.dim_date (date_id)` dans migrations/003_...sql. Ce sont des
    # contraintes de CLÉ ÉTRANGÈRE : Postgres refuse catégoriquement d'insérer
    # une ligne de fait qui pointerait vers une carte ou une date qui
    # n'existe pas encore dans la dimension correspondante (erreur
    # "violates foreign key constraint"). Il faut donc TOUJOURS garantir que
    # la ligne de dimension existe avant d'insérer le fait qui la référence -
    # d'où l'ordre strict dans cette fonction : dim_date une fois en tête,
    # puis pour chaque carte, dim_card avant fact_price_history.
    with conn.cursor() as cur:
        # Lecture des lignes de staging pour CE jour et CETTE source
        # uniquement (filtre WHERE extracted_date = ... AND source = ...) :
        # une exécution du pipeline ne doit traiter que les données
        # fraîchement extraites aujourd'hui, pas réinjecter tout l'historique
        # de staging à chaque run. `source` filtre pokemontcg.io des autres
        # sources potentielles futures (même logique que dans
        # staging_loader.py).
        cur.execute(
            """
            SELECT card_id, name, set_id, set_name, rarity,
                   average_sell_price, trend_price, low_price
            FROM staging.card_prices
            WHERE extracted_date = %s AND source = %s
            """,
            (extracted_date, source),
        )
        rows = cur.fetchall()

        # dim_date n'a besoin d'être upsertée qu'UNE SEULE fois par appel
        # (toutes les lignes de `rows` partagent le même extracted_date, donc
        # le même date_id) : contrairement à dim_card qui varie par carte, on
        # la sort de la boucle for ci-dessous pour éviter un aller-retour SQL
        # redondant par carte.
        # isoweekday() renvoie 1 (lundi) à 7 (dimanche), une convention stable
        # et indépendante de la locale système (contrairement à weekday() ou
        # strftime("%w") qui dépendent de conventions différentes selon le
        # contexte).
        cur.execute(
            _UPSERT_DIM_DATE_SQL,
            {
                "date_id": extracted_date,
                "year": extracted_date.year,
                "month": extracted_date.month,
                "day": extracted_date.day,
                "day_of_week": extracted_date.isoweekday(),
            },
        )

        # Boucle carte par carte : pour chaque ligne de staging, on upserte
        # d'abord sa dimension (dim_card) PUIS son fait (fact_price_history).
        # On aurait pu séparer en deux boucles (toutes les dim_card, puis
        # tous les faits), mais faire les deux upserts carte par carte est
        # tout aussi correct ici (chaque paire dim_card/fact partage la même
        # transaction, voir get_connection()) et garde le code plus simple à
        # lire : "pour cette carte, assure sa dimension, puis enregistre son
        # prix du jour".
        for (
            card_id,
            name,
            set_id,
            set_name,
            rarity,
            average_sell_price,
            trend_price,
            low_price,
        ) in rows:
            cur.execute(
                _UPSERT_DIM_CARD_SQL,
                {
                    "card_id": card_id,
                    "name": name,
                    "set_id": set_id,
                    "set_name": set_name,
                    "rarity": rarity,
                },
            )
            cur.execute(
                _UPSERT_FACT_SQL,
                {
                    "card_id": card_id,
                    "date_id": extracted_date,
                    "average_sell_price": average_sell_price,
                    "trend_price": trend_price,
                    "low_price": low_price,
                    "platform_name": platform_name,
                },
            )
            # Pourquoi ce contrôle est indispensable : _UPSERT_FACT_SQL est un
            # INSERT ... SELECT ... FROM prod.dim_platform WHERE platform_name
            # = %(platform_name)s. Si `platform_name` ne correspond à AUCUNE
            # ligne de dim_platform (faute de frappe, mauvaise casse,
            # plateforme pas encore seedée en migration), le SELECT renvoie
            # zéro ligne : l'INSERT n'a alors rien à insérer, mais ce n'est
            # PAS une erreur au sens de Postgres (0 ligne insérée est un
            # comportement parfaitement valide d'un INSERT ... SELECT). Sans
            # ce contrôle, cur.rowcount ne serait jamais lu, et la fonction
            # continuerait silencieusement : dim_card aurait déjà été mis à
            # jour juste au-dessus, donc le pipeline "réussirait" alors que
            # l'observation de prix du jour pour cette carte a disparu sans
            # aucune exception ni avertissement - une perte de données
            # silencieuse, la pire catégorie de bug dans un pipeline dont le
            # but est justement de préserver un historique de prix fiable.
            # On applique ici le même principe "échec explicite plutôt que
            # silencieux" que _require_env() dans src/common/config.py et
            # PokemonTcgApiError dans src/extract/pokemontcg_client.py :
            # mieux vaut un run qui plante bruyamment qu'un run qui "réussit"
            # en ayant discrètement perdu des données.
            if cur.rowcount == 0:
                raise RuntimeError(
                    f"platform_name inconnu ou absent de prod.dim_platform : {platform_name!r}"
                )

    logger.info("Warehouse : %d faits chargés (date=%s)", len(rows), extracted_date)
    # Comme load_staging() dans staging_loader.py, on renvoie le nombre de
    # lignes traitées dans CET appel (pas un total cumulé en base) : ça
    # permet à l'appelant (Task 4, le DAG) de construire ses propres
    # statistiques d'exécution et de vérifier facilement dans les tests
    # d'idempotence que "rejouer ne recharge pas plus de faits que prévu".
    return len(rows)
