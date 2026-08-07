# Ce module a une seule responsabilité : écrire des cartes brutes dans
# raw.card_prices, de façon idempotente. "Idempotent" veut dire qu'exécuter
# la même opération plusieurs fois produit le même résultat que l'exécuter
# une seule fois (pas de doublons créés en rejouant). C'est essentiel pour un
# pipeline data qu'on peut avoir besoin de relancer (après un crash, un bug,
# ou simplement pour tester) sans craindre de corrompre les données déjà en
# base. Aucune transformation des données n'a lieu ici : le payload de l'API
# est stocké tel quel en JSON, la transformation viendra plus tard (staging).
from __future__ import annotations

import json
import logging
from datetime import date

from psycopg import Connection

# logger dédié à ce module (nom "src.load.raw_loader"), même pattern que dans
# src/extract/pokemontcg_client.py : permet de tracer précisément quelle
# étape du pipeline a chargé quoi, sans coupler le code à un choix de sortie
# (console, fichier...) particulier.
logger = logging.getLogger(__name__)

# Requête SQL utilisée pour chaque carte insérée. Elle est définie une seule
# fois au niveau module (plutôt que reconstruite à chaque appel de
# load_cards) car son texte ne change jamais, seuls les paramètres varient.
#
# - %(card_id)s, %(extracted_date)s, ... : paramètres NOMMÉS (par opposition
#   aux %s positionnels). psycopg remplace chaque %(nom)s par la valeur du
#   dictionnaire portant cette clé au moment de l'exécution. Utiliser des
#   paramètres (plutôt que de construire la requête par concaténation de
#   chaînes) protège contre les injections SQL ET gère correctement
#   l'échappement des types (dates, JSON...).
#
# - ON CONFLICT (card_id, extracted_date, source) DO UPDATE : c'est le coeur
#   de l'idempotence. Cette clause s'appuie sur la contrainte UNIQUE posée en
#   Task 2 (migrations/001_create_schemas_and_raw.sql) sur EXACTEMENT ces
#   trois colonnes. Si un INSERT entrerait en conflit avec une ligne
#   existante ayant la même (card_id, extracted_date, source), Postgres
#   n'insère pas de nouvelle ligne : il exécute l'UPDATE à la place, sur la
#   ligne déjà présente. Résultat concret :
#     * même carte, même jour, rejoué -> UPDATE de la ligne existante (le
#       payload le plus récent du jour écrase l'ancien) -> pas de doublon.
#     * même carte, jour DIFFÉRENT -> la clé (card_id, extracted_date,
#       source) est différente -> pas de conflit -> nouvelle ligne insérée
#       -> l'historique des jours précédents reste intact.
#   C'est précisément ce que vérifient les 2e et 3e tests de
#   tests/test_raw_loader.py.
#
# - loaded_at = now() dans le DO UPDATE : remet à jour l'horodatage de
#   chargement à chaque rejeu du même jour, pour que loaded_at reflète
#   toujours la dernière écriture réelle de cette ligne (utile pour
#   diagnostiquer quand une ligne a été rafraîchie en dernier).
_UPSERT_SQL = """
    INSERT INTO raw.card_prices (card_id, extracted_date, source, payload)
    VALUES (%(card_id)s, %(extracted_date)s, %(source)s, %(payload)s)
    ON CONFLICT (card_id, extracted_date, source)
    DO UPDATE SET payload = EXCLUDED.payload, loaded_at = now()
"""


def load_cards(
    conn: Connection,
    cards: list[dict],
    extracted_date: date,
    source: str = "pokemontcg.io",
) -> int:
    """Charge les cartes dans raw.card_prices. Idempotent pour un même
    (card_id, extracted_date, source) : rejouer met à jour la ligne du jour
    au lieu de la dupliquer, sans toucher aux jours précédents."""
    # Transforme la liste de dicts "cartes brutes de l'API" en une liste de
    # dicts "paramètres SQL" alignés sur les noms attendus par _UPSERT_SQL
    # (%(card_id)s, %(extracted_date)s, %(source)s, %(payload)s). Une
    # compréhension de liste (list comprehension) construit cette nouvelle
    # liste en une seule expression, carte par carte.
    rows = [
        {
            # card["id"] : identifiant de la carte tel que fourni par
            # pokemontcg.io (ex: "base1-1"). On suppose que chaque dict de
            # `cards` contient bien la clé "id" (c'est le contrat de l'API,
            # vérifié indirectement par le client d'extraction).
            "card_id": card["id"],
            # Même date pour toutes les cartes de cet appel : load_cards()
            # traite un lot (une page) extrait à un instant donné, donc
            # extracted_date est un paramètre unique de la fonction, pas une
            # valeur par carte.
            "extracted_date": extracted_date,
            "source": source,
            # json.dumps(card) sérialise le dict Python entier (tous les
            # champs renvoyés par l'API : nom, set, prix, images...) en texte
            # JSON. Postgres convertira ce texte en jsonb à l'insertion (la
            # colonne payload est typée jsonb). On stocke TOUT le payload
            # brut, sans en extraire de champs individuels ici : les
            # transformations (ex: extraire juste le prix) sont le rôle de
            # l'étape staging, pas de raw.
            "payload": json.dumps(card),
        }
        for card in cards
    ]
    # cur.executemany(sql, rows) exécute _UPSERT_SQL une fois PAR élément de
    # `rows`, chaque dict fournissant les valeurs de ses propres paramètres
    # nommés. C'est équivalent à boucler manuellement sur `rows` et appeler
    # cur.execute(sql, row) à chaque itération, mais en une seule instruction
    # (plus lisible, et potentiellement optimisé côté driver).
    # Le "with conn.cursor() as cur" ouvre un curseur et le ferme
    # automatiquement à la sortie du bloc (même en cas d'exception) ; il ne
    # fait PAS de commit lui-même : c'est get_connection() (src/common/db.py)
    # qui commit/rollback la transaction englobante à la sortie de son
    # propre "with".
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, rows)
    # Log de fin de lot : combien de cartes ont été traitées, pour quel jour
    # et quelle source. Utile pour suivre la progression d'une extraction
    # longue (des dizaines de pages) dans les logs du script d'orchestration.
    logger.info("Chargé %d cartes (date=%s, source=%s)", len(rows), extracted_date, source)
    # On renvoie le nombre de cartes traitées dans CET appel (pas le total
    # cumulé en base) : l'appelant (scripts/run_extract_load.py) additionne
    # ces valeurs page après page pour connaître le total chargé sur toute
    # l'extraction.
    return len(rows)
