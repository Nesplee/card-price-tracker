# Ce module a la responsabilité inverse-mais-complémentaire de
# src/load/raw_loader.py : là où raw_loader écrit du JSON brut tel quel,
# staging_loader écrit des données DÉJÀ validées et nettoyées (CleanedCard,
# produit par src/transform/validate.py) dans staging.card_prices, ou des
# cartes REJETÉES dans staging.card_prices_quarantine. Comme raw_loader,
# aucune règle métier de validation ne vit ici : ce module ne fait
# qu'exécuter des INSERT/UPSERT SQL à partir de données déjà décidées en
# amont. Ça garde chaque module concentré sur un seul problème (validation
# XOR persistance), conformément au pattern déjà établi au Mois 1 : toute
# fonction qui touche la DB prend `conn` en paramètre, seule la couche
# d'orchestration (ici src/transform/clean.py) ouvre get_connection().
from __future__ import annotations

import json
import logging
from datetime import date

from psycopg import Connection

from src.transform.validate import CleanedCard

# Même pattern de logger que dans raw_loader.py : un logger nommé d'après le
# module (__name__ = "src.load.staging_loader"), pour pouvoir filtrer/tracer
# précisément quelle étape du pipeline a produit quel message.
logger = logging.getLogger(__name__)

# Requête d'upsert pour staging.card_prices, définie une seule fois au niveau
# module (le texte SQL ne change jamais, seuls les paramètres varient d'un
# appel à l'autre).
#
# ON CONFLICT (card_id, extracted_date, source) DO UPDATE : repose sur la
# contrainte UNIQUE uq_staging_card_prices_card_date_source posée dans
# migrations/002_create_staging_tables.sql. Exactement le même mécanisme
# d'idempotence qu'en raw (voir raw_loader.py) : rejouer le nettoyage de la
# même journée met à jour les lignes existantes (prix, nom, set...) au lieu
# d'en créer des doublons — indispensable puisque clean_raw_to_staging peut
# être relancé après un crash sans dupliquer les cartes déjà en staging.
#
# loaded_at n'est volontairement PAS remis dans la liste des colonnes du
# INSERT (elle a un DEFAULT now() en base) mais EST explicitement remise à
# jour dans le DO UPDATE (loaded_at = now()) : à l'insertion initiale, le
# DEFAULT suffit ; en cas de ré-exécution, on veut que loaded_at reflète bien
# le moment du dernier rafraîchissement réel de la ligne, pas la première
# insertion.
_UPSERT_STAGING_SQL = """
    INSERT INTO staging.card_prices
        (card_id, extracted_date, name, set_id, set_name, rarity,
         average_sell_price, trend_price, low_price, source)
    VALUES
        (%(card_id)s, %(extracted_date)s, %(name)s, %(set_id)s, %(set_name)s, %(rarity)s,
         %(average_sell_price)s, %(trend_price)s, %(low_price)s, %(source)s)
    ON CONFLICT (card_id, extracted_date, source)
    DO UPDATE SET
        name = EXCLUDED.name,
        set_id = EXCLUDED.set_id,
        set_name = EXCLUDED.set_name,
        rarity = EXCLUDED.rarity,
        average_sell_price = EXCLUDED.average_sell_price,
        trend_price = EXCLUDED.trend_price,
        low_price = EXCLUDED.low_price,
        loaded_at = now()
"""

# Requête d'insertion pour la quarantaine : simple INSERT, PAS d'ON CONFLICT.
# staging.card_prices_quarantine n'a aucune contrainte UNIQUE (voir
# migrations/002_...sql) : un rejet est un événement immuable, on n'a pas
# vocation à le "mettre à jour" — si la même carte est rejetée plusieurs
# fois (ex: rejouer le pipeline le même jour sans corriger la source), on
# accumule volontairement plusieurs lignes de quarantaine, chacune étant une
# trace d'audit distincte de "cette exécution a rejeté cette carte pour
# cette raison".
_INSERT_QUARANTINE_SQL = """
    INSERT INTO staging.card_prices_quarantine
        (card_id, extracted_date, raw_payload, rejection_reason, source)
    VALUES (%(card_id)s, %(extracted_date)s, %(raw_payload)s, %(rejection_reason)s, %(source)s)
"""


def load_staging(
    conn: Connection,
    cleaned_cards: list[CleanedCard],
    extracted_date: date,
    source: str = "pokemontcg.io",
) -> int:
    """Charge une liste de CleanedCard (déjà validées) dans staging.card_prices.
    Idempotent pour un même (card_id, extracted_date, source) : rejouer met à
    jour la ligne du jour au lieu de la dupliquer (voir _UPSERT_STAGING_SQL)."""
    # Conversion de chaque CleanedCard (dataclass) en dict de paramètres
    # nommés alignés sur %(nom)s dans _UPSERT_STAGING_SQL. On accède aux
    # champs via l'attribut (c.card_id, c.name...) plutôt que par indexation
    # dict comme en raw_loader, car CleanedCard est un objet typé, pas un
    # dict JSON brut : c'est justement l'intérêt d'avoir déjà nettoyé/typé
    # les données en amont dans validate.py.
    rows = [
        {
            "card_id": c.card_id,
            "extracted_date": extracted_date,
            "name": c.name,
            "set_id": c.set_id,
            "set_name": c.set_name,
            "rarity": c.rarity,
            "average_sell_price": c.average_sell_price,
            "trend_price": c.trend_price,
            "low_price": c.low_price,
            "source": source,
        }
        for c in cleaned_cards
    ]
    # cur.executemany() exécute la requête une fois par ligne de `rows`.
    # Comme dans raw_loader.py, le "with conn.cursor()" ne fait pas de commit
    # lui-même : c'est get_connection() (src/common/db.py) qui commit/rollback
    # la transaction englobante à la sortie de son propre "with". Ici,
    # aucune connexion n'est ouverte dans ce module : `conn` est fourni par
    # l'appelant (clean_raw_to_staging), conformément au pattern du Mois 1.
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_STAGING_SQL, rows)
    logger.info("Staging : %d cartes chargées (date=%s)", len(rows), extracted_date)
    # On renvoie le nombre de cartes traitées dans CET appel (pas un total
    # cumulé en base), pour que l'appelant puisse construire ses propres
    # statistiques (voir clean_raw_to_staging, qui renvoie (valides, rejetées)).
    return len(rows)


def load_quarantine(
    conn: Connection,
    rejected: list[tuple[dict, str]],
    extracted_date: date,
    source: str = "pokemontcg.io",
) -> int:
    """Charge une liste de cartes rejetées dans staging.card_prices_quarantine.
    `rejected` est une liste de tuples (payload_brut, raison_de_rejet) —
    exactement la forme produite par clean_raw_to_staging() à partir des
    ValidationResult invalides renvoyés par validate_and_clean()."""
    rows = [
        {
            # payload.get("id") plutôt que payload["id"] : contrairement à
            # load_staging (qui reçoit des CleanedCard déjà validées, donc
            # card_id garanti non-vide), load_quarantine reçoit des payloads
            # BRUTS potentiellement malformés — c'est justement pour ça
            # qu'ils sont en quarantaine. Le payload peut même ne pas avoir
            # de clé "id" du tout (ex: rejet "card_id ou name manquant").
            # .get() renvoie alors None, ce que la colonne card_id accepte
            # (elle est nullable, voir migrations/002_...sql, contrairement à
            # card_id NOT NULL dans staging.card_prices).
            "card_id": payload.get("id"),
            "extracted_date": extracted_date,
            # json.dumps(payload) sérialise le dict Python en texte JSON,
            # que Postgres convertit en jsonb à l'insertion (colonne
            # raw_payload typée jsonb) : on conserve la charge brute
            # COMPLÈTE et telle quelle, pour permettre un audit ou un replay
            # manuel ultérieur sans avoir à retourner chercher raw.card_prices.
            "raw_payload": json.dumps(payload),
            "rejection_reason": reason,
            "source": source,
        }
        for payload, reason in rejected
    ]
    # Garde explicite pour une liste vide : executemany() sur une liste vide
    # ne ferait techniquement rien de dangereux, mais autant éviter l'aller-
    # retour réseau vers Postgres pour zéro ligne (cas fréquent : la plupart
    # des exécutions du pipeline n'ont aucune carte rejetée).
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_INSERT_QUARANTINE_SQL, rows)
    # logger.warning (pas .info) : une carte en quarantaine est un signal
    # anormal qui mérite d'être visible dans les logs sans avoir à chercher,
    # contrairement au chargement staging qui est le chemin nominal attendu.
    logger.warning("Quarantaine : %d cartes rejetées (date=%s)", len(rows), extracted_date)
    return len(rows)
