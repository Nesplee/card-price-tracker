# Ce module contient la logique RÉUTILISABLE d'orchestration "extraction ->
# chargement raw", partagée par DEUX appelants :
#   1. scripts/run_extract_load.py : le point d'entrée exécutable manuel
#      (ligne de commande, cron externe...).
#   2. dags/card_price_pipeline_dag.py : la tâche Airflow `extract_and_load_raw`
#      (Task 4 du Mois 2).
# Avant ce refactor, cette logique vivait UNIQUEMENT dans scripts/run_extract_load.py,
# ce qui aurait forcé le DAG Airflow à la dupliquer (ou pire, à réécrire une
# version simplifiée EN DUR qui perd le correctif ci-dessous). On la déplace
# donc ici, dans src/, pour qu'un seul et même code corrigé serve aux deux
# appelants -- aucun des deux ne doit connaître les détails du checkpoint/
# reprise, ils appellent juste run_extract_load(client, conn, extracted_date).
#
# IMPORTANT -- pourquoi cette fonction reçoit `conn` DÉJÀ OUVERTE plutôt que
# d'ouvrir elle-même la connexion (contrairement à ce qu'on pourrait attendre
# d'un module "haut niveau") :
#
# Ce module NE PILOTE PAS le commit/rollback global de la connexion : il pilote
# seulement des commits PARTIELS (un par page, voir plus bas). Décider QUAND et
# COMMENT ouvrir la connexion (get_connection() vs psycopg.connect() manuel)
# reste la responsabilité du code appelant -- exactement le même pattern que
# load_cards() (src/load/raw_loader.py), clean_raw_to_staging()
# (src/transform/clean.py) et load_staging_to_warehouse()
# (src/load/warehouse_loader.py), qui prennent TOUS `conn` en paramètre plutôt
# que de l'ouvrir eux-mêmes. Ici, le bon choix pour l'appelant est PRÉCISÉMENT
# de NE PAS utiliser get_connection() (voir le commentaire détaillé ci-dessous
# sur la stratégie de checkpoint), mais cette fonction elle-même reste agnostique
# de ce choix : elle se contente d'utiliser la connexion qu'on lui donne.
#
# --- Pourquoi un checkpoint PAR PAGE, et pas une seule transaction pour tout
# le run (comme le ferait naïvement `with get_connection(...) as conn: ...`
# englobant toute la boucle) ? ---
#
# L'unité de travail ici est une extraction COMPLÈTE de ~80 pages / ~20 000
# cartes, qui peut prendre plusieurs minutes, en appelant une API externe
# (pokemontcg.io) dont l'instabilité a été CONSTATÉE en conditions réelles :
# environ 37% des appels directs échouent en 500/502/timeout. Sur ce volume,
# un échec en cours de route n'est pas une hypothèse rare, c'est presque
# certain à chaque run (vécu concrètement : un run a échoué à la page 69/82).
#
# Avec une seule transaction pour tout le run, un échec à la page 69 aurait
# fait un rollback des 68 pages précédentes déjà chargées avec succès : tout
# le travail (et tout le temps, et tous les appels API déjà consommés) serait
# perdu, et le prochain run repartirait bêtement de la page 1.
#
# Le choix fait ici est différent : traiter chaque PAGE comme l'unité atomique,
# pas le run entier. C'est possible sans risque car load_cards() (Mois 1,
# src/load/raw_loader.py) est déjà idempotent PAR PAGE (upsert sur card_id,
# extracted_date, source) : committer une page dès qu'elle est chargée avec
# succès ne casse aucune garantie -- si on rejoue cette page plus tard (ex:
# reprise après crash), l'upsert la met juste à jour, il ne la duplique pas.
from __future__ import annotations

import logging
from datetime import date

from psycopg import Connection

from src.extract.pokemontcg_client import PokemonTcgClient
from src.load.raw_loader import load_cards

logger = logging.getLogger(__name__)

# Valeur par défaut de la taille de page, utilisée par run_extract_load ET
# _resume_page ci-dessous. Reste un module-level constant (plutôt qu'une
# valeur en dur répétée à deux endroits) précisément pour la raison qui
# justifiait déjà PAGE_SIZE dans l'ancien scripts/run_extract_load.py : le
# calcul de la page de reprise (_resume_page) a besoin de connaître EXACTEMENT
# la même taille de page que celle utilisée pour l'appel API, sinon "nombre de
# lignes déjà en base // taille de page" serait incohérent avec la pagination
# réelle. Cette constante sert maintenant de valeur par défaut au paramètre
# page_size (voir plus bas) : les deux appelants (script et DAG) peuvent soit
# s'appuyer sur cette valeur par défaut, soit la surcharger explicitement.
PAGE_SIZE = 250


def _resume_page(
    conn: Connection,
    extracted_date: date,
    page_size: int = PAGE_SIZE,
    source: str = "pokemontcg.io",
) -> int:
    """Calcule la page à laquelle reprendre l'extraction, à partir du nombre de
    lignes déjà présentes en base pour ce (extracted_date, source).

    Logique : si N lignes sont déjà chargées, cela correspond à N // page_size
    pages COMPLETES déjà traitées (division entière = on ignore le reste).
    On reprend donc à la page suivante : (N // page_size) + 1.

    Cas limite volontaire -- la division entière (//) TRONQUE le reste : si N
    n'est pas un multiple exact de page_size (ex: 250 + 42 = 292 lignes),
    ces 42 lignes en trop ne comptent pour AUCUNE page complète
    (292 // 250 = 1, le reste 42 est simplement ignoré) -- la page à laquelle
    elles appartiennent (la page 2) est donc considérée comme PAS encore
    chargée, et sera intégralement rechargée depuis son début. Deux cas
    peuvent produire ce genre de reste :
      1. Un crash est survenu après le commit d'une page mais avant que le
         compte total reflète une page pleine (scénario défensif).
      2. Plus courant en pratique : ces 42 lignes sont simplement la
         dernière page RÉELLE du catalogue (le nombre total de cartes n'est
         pas un multiple exact de page_size).
    Dans les deux cas, refaire cette page depuis son début est SANS RISQUE
    car load_cards() est idempotent (UPSERT sur card_id, extracted_date,
    source) : réinsérer des cartes déjà présentes ne fait que les mettre à
    jour, jamais les dupliquer. Refaire une page coûte au pire un appel API
    en plus, mais ne peut jamais corrompre les données ni créer de doublon --
    c'est un compromis délibéré en faveur de la simplicité et de la sûreté
    plutôt que d'une reprise "au carton près".
    """
    with conn.cursor() as cur:
        # count(*) scopé sur (extracted_date, source) : on ne veut compter QUE
        # les lignes du run qu'on est en train de reprendre, pas l'historique
        # des jours précédents ni d'autres sources éventuelles (ex: tests).
        cur.execute(
            """
            SELECT count(*) FROM raw.card_prices
            WHERE extracted_date = %(extracted_date)s AND source = %(source)s
            """,
            {"extracted_date": extracted_date, "source": source},
        )
        (already_loaded,) = cur.fetchone()
    return (already_loaded // page_size) + 1


def run_extract_load(
    client: PokemonTcgClient,
    conn: Connection,
    extracted_date: date,
    page_size: int = PAGE_SIZE,
) -> int:
    """Extrait TOUTES les pages disponibles sur pokemontcg.io pour extracted_date
    et les charge dans raw.card_prices, avec un checkpoint (commit) après chaque
    page chargée avec succès. Reprend automatiquement à la bonne page si des
    lignes existent déjà pour (extracted_date, source) -- voir _resume_page.

    Ne gère PAS l'ouverture/fermeture de `conn` : c'est la responsabilité de
    l'appelant (script ou tâche Airflow), qui décide aussi de la stratégie de
    connexion (voir le commentaire en tête de module). Retourne le nombre total
    de cartes chargées PENDANT CET APPEL (pas le total cumulé en base sur
    d'éventuels runs précédents du même jour).
    """
    # Calcule la page de reprise AVANT la boucle : s'il existe déjà des
    # lignes pour (extracted_date, source) -- typiquement parce qu'un run
    # précédent aujourd'hui a crashé en cours de route -- on reprend après
    # la dernière page déjà chargée, au lieu de tout refaire depuis la
    # page 1 (ce qui gaspillerait du temps ET des appels API sur une
    # source déjà instable).
    page = _resume_page(conn, extracted_date=extracted_date, page_size=page_size)
    if page > 1:
        # Log explicite : une reprise n'est pas le comportement "normal"
        # (page 1), c'est un signal utile pour quiconque lit les logs de
        # comprendre que ce run continue un run précédent incomplet.
        logger.info(
            "Reprise détectée : des cartes sont déjà chargées pour le %s, "
            "reprise à la page %d (au lieu de la page 1)",
            extracted_date,
            page,
        )

    total_loaded = 0
    # Boucle de pagination : pokemontcg.io renvoie les cartes par pages
    # de taille fixe (page_size). "while True" + "break" explicite est le
    # pattern naturel ici car on ne connaît PAS à l'avance le nombre total
    # de pages (~19000+ cartes / 250 par page = environ 76-80 pages) : on
    # continue tant que l'API renvoie des cartes, et on s'arrête dès qu'une
    # page est vide.
    while True:
        try:
            cards = client.fetch_cards_page(page=page, page_size=page_size)
            # Liste vide : signal de fin de pagination envoyé par
            # pokemontcg.io (la page demandée dépasse le nombre de cartes
            # disponibles). On arrête la boucle ici plutôt que de
            # continuer à interroger des pages qui seront toujours vides.
            if not cards:
                break
            # Charge cette page dans raw.card_prices et accumule le
            # nombre de cartes traitées (utile pour le log récapitulatif
            # final produit par l'appelant).
            total_loaded += load_cards(conn, cards, extracted_date=extracted_date)
            # CHECKPOINT : on commit dès que CETTE page est chargée avec
            # succès, plutôt que d'attendre la fin de tout le run. C'est
            # le coeur du correctif -- si l'API échoue à la page suivante
            # (ou que le processus est tué pour une autre raison), tout ce
            # qui a été commité jusqu'ici reste acquis en base : le
            # prochain run reprendra juste après, pas depuis zéro.
            conn.commit()
            logger.info(
                "Checkpoint : page %d validée (%d cartes), total cumulé = %d",
                page,
                len(cards),
                total_loaded,
            )
        except Exception:
            # Une exception ici peut venir soit de l'appel API (ex:
            # PokemonTcgApiError après épuisement des retries), soit de
            # load_cards()/du commit lui-même. Dans tous les cas :
            # rollback() n'annule QUE le travail non commité de la page EN
            # COURS (les pages précédentes, déjà commitées individuellement
            # ci-dessus, restent intactes en base -- ce rollback ne les
            # touche pas). On relève ensuite l'exception (raise sans
            # argument = "re-lève l'exception en cours") pour que l'échec
            # soit explicite : pas de except qui avale silencieusement
            # l'erreur, le run doit clairement apparaître en échec (code de
            # sortie non-nul côté script, tâche rouge côté Airflow) pour
            # qu'un opérateur (ou le scheduler) sache qu'il faut relancer.
            conn.rollback()
            raise
        # Page suivante pour la prochaine itération de la boucle.
        page += 1

    return total_loaded
