# Ce module orchestre le passage raw -> staging : c'est la "colle" entre la
# validation pure (validate.py, aucun accès DB) et la persistance
# (staging_loader.py, aucune règle métier). clean_raw_to_staging() est la
# fonction que consommera le DAG/orchestrateur de la Task 4 : elle prend une
# connexion déjà ouverte (`conn`) et une date, lit le raw de ce jour, route
# chaque carte vers staging ou quarantaine selon la validation, et renvoie un
# résumé (nb de cartes valides, nb de cartes rejetées) exploitable pour le
# logging/monitoring du pipeline.
#
# Comme load_cards (Mois 1) et load_staging/load_quarantine (Task 2
# ci-dessus), cette fonction NE FAIT PAS get_connection() elle-même : la
# connexion est reçue en paramètre, conformément au pattern déjà établi —
# seule la couche d'orchestration la plus haute (script/DAG) ouvre la
# connexion, ce qui permet à Task 4 d'englober extraction + nettoyage dans
# une seule transaction si besoin, et aux tests d'injecter une connexion de
# test sans dupliquer la logique de connexion dans chaque module.
from __future__ import annotations

import logging
from datetime import date

from psycopg import Connection

from src.load.staging_loader import load_quarantine, load_staging
from src.transform.validate import validate_and_clean

logger = logging.getLogger(__name__)


def clean_raw_to_staging(
    conn: Connection, extracted_date: date, source: str = "pokemontcg.io"
) -> tuple[int, int]:
    """Lit raw.card_prices pour extracted_date, valide/nettoie chaque carte,
    et route vers staging.card_prices ou la quarantaine. Retourne (valides, rejetées)."""
    # Étape 1 — lecture : on récupère tous les payloads JSON bruts du jour
    # (et de la source) demandés. `row[0]` car cur.fetchall() renvoie une
    # liste de tuples à une seule colonne (SELECT payload ...) : on ne garde
    # que la valeur, pas le tuple englobant.
    # psycopg désérialise automatiquement la colonne jsonb en dict Python
    # (pas besoin de json.loads() manuel ici) : `payload` est donc bien un
    # dict directement utilisable par validate_and_clean().
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM raw.card_prices WHERE extracted_date = %s AND source = %s",
            (extracted_date, source),
        )
        payloads = [row[0] for row in cur.fetchall()]

    # Étape 2 — validation/nettoyage EN MÉMOIRE : on appelle la fonction pure
    # validate_and_clean() pour chaque payload et on répartit le résultat
    # dans deux listes distinctes selon result.is_valid. Aucun accès DB dans
    # cette boucle : c'est pour ça que la validation peut être testée
    # indépendamment (tests/test_transform.py) et que cette boucle reste
    # rapide même pour des milliers de cartes (pas d'aller-retour réseau par
    # carte).
    cleaned = []
    rejected = []
    for payload in payloads:
        result = validate_and_clean(payload)
        if result.is_valid:
            cleaned.append(result.cleaned)
        else:
            # On garde le payload BRUT d'origine (pas le CleanedCard, qui
            # n'existe pas pour une carte rejetée) associé à la raison de
            # rejet : c'est exactement la forme (dict, str) attendue par
            # load_quarantine(), qui stockera ce payload tel quel dans
            # raw_payload pour permettre un audit ultérieur.
            rejected.append((payload, result.rejection_reason))

    # Étape 3 — persistance : deux appels séparés, chacun idempotent de son
    # côté (voir staging_loader.py). Si clean_raw_to_staging est rejouée pour
    # le même jour (ex: après un crash entre les deux appels), load_staging
    # ET load_quarantine mettent chacun à jour les lignes déjà présentes pour
    # ce jour au lieu de les dupliquer — les deux reposent maintenant sur le
    # même mécanisme d'UPSERT (ON CONFLICT ... DO UPDATE), grâce à la
    # contrainte UNIQUE (card_id, extracted_date, source) posée sur
    # staging.card_prices_quarantine par
    # migrations/004_add_quarantine_unique_constraint.sql. Avant cette
    # migration, la quarantaine accumulait une nouvelle ligne à chaque
    # relance au lieu de mettre à jour l'existante ; voir staging_loader.py
    # (_INSERT_QUARANTINE_SQL) pour le détail du comportement actuel, y
    # compris le cas particulier des lignes à card_id NULL qui, elles,
    # continuent de s'accumuler.
    load_staging(conn, cleaned, extracted_date=extracted_date, source=source)
    load_quarantine(conn, rejected, extracted_date=extracted_date, source=source)

    logger.info(
        "Nettoyage terminé (date=%s) : %d valides, %d en quarantaine",
        extracted_date,
        len(cleaned),
        len(rejected),
    )
    # Renvoie le décompte (valides, rejetées) : l'appelant (le futur DAG de
    # Task 4) peut s'en servir pour logger un résumé, déclencher une alerte
    # si le taux de rejet dépasse un seuil, etc. — sans avoir à re-parcourir
    # les listes cleaned/rejected lui-même.
    return len(cleaned), len(rejected)
