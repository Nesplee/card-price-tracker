# DAG Airflow orchestrant le pipeline complet "extract -> clean -> load"
# (Task 4, Mois 2) : trois tâches enchaînées, chacune correspondant à une
# étape déjà développée et testée les mois/tâches précédents :
#   1. extract_and_load_raw  : va chercher les cartes sur pokemontcg.io et
#      les charge dans raw.card_prices (Mois 1 + Task 4 pour le checkpoint).
#   2. clean_to_staging      : valide/nettoie raw -> staging (Task 2).
#   3. load_to_warehouse     : charge staging -> star schema prod (Task 3).
# Ce module est un point d'ENTRÉE (aucun autre module du dépôt ne l'importe) :
# c'est Airflow (le scheduler, en le scannant dans dags/) qui l'exécute.
from __future__ import annotations

from datetime import UTC, date, datetime

import psycopg
from airflow.decorators import dag, task

from src.common.config import load_db_config, load_pokemontcg_config
from src.common.db import get_connection
from src.extract.pipeline import run_extract_load
from src.extract.pokemontcg_client import PokemonTcgClient
from src.load.warehouse_loader import load_staging_to_warehouse
from src.transform.clean import clean_raw_to_staging


@dag(
    schedule="@daily",
    # start_date DANS LE PASSÉ (pas dans le futur) : ÉCART DÉLIBÉRÉ par
    # rapport à la brief initiale (qui proposait 2026-09-01). Vérifié
    # empiriquement lors du test manuel de ce DAG (Step 5) : avec une
    # start_date future, un déclenchement manuel ("airflow dags trigger")
    # crée un DagRun dont l'intervalle de données se situe AVANT start_date
    # -- Airflow considère alors qu'aucune tâche n'est éligible à s'exécuter
    # pour ce run et le marque "success" trivialement, en ~15ms, sans lancer
    # aucune des 3 tâches (constaté dans les logs du scheduler : "DagRun
    # Finished [...] run_duration=0.014333, state=success" alors qu'aucune
    # TaskInstance n'existe pour ce run). Une start_date dans le passé
    # garantit que la logical_date de tout run (planifié ou déclenché
    # manuellement) est bien postérieure à start_date, donc que les tâches
    # sont effectivement éligibles à s'exécuter.
    start_date=datetime(2024, 1, 1, tzinfo=UTC),
    # catchup=False : ne PAS générer rétroactivement un run par jour manqué
    # entre start_date et aujourd'hui si le DAG est activé tardivement. Pour
    # ce pipeline (un run quotidien qui récupère l'état ACTUEL des prix), un
    # run manqué n'a pas de sens à rattraper a posteriori -- contrairement à
    # un pipeline qui traiterait des données historiques par date.
    catchup=False,
    # retries=20 : valeur augmentée après un run réel qui a échoué avec
    # retries=2 (3 tentatives au total), non pas à cause d'un bug mais de
    # l'instabilité mesurée de pokemontcg.io (~37% d'échecs 5xx observés au
    # Mois 1) sur une extraction de ~80 pages. La preuve concrète (logs du
    # run manuel__2026-08-06T19:01:11) montre que le mécanisme
    # checkpoint+reprise (src/extract/pipeline.py) fonctionne bien À TRAVERS
    # les retries Airflow -- chaque nouvelle tentative reprend exactement où
    # la précédente s'est arrêtée ("Reprise détectée [...] page 40"), sans
    # jamais repartir de zéro ni dupliquer. Le seul problème était le NOMBRE
    # de tentatives, pas la logique de reprise elle-même : avec seulement 3
    # tentatives, la probabilité de traverser ~80 pages à 37% d'échec par
    # page est trop faible. 20 tentatives, combinées au coût quasi nul d'un
    # retry (reprise immédiate, pas de perte), rend un run complet bien plus
    # probable sans risque de corruption -- au pire, encore plus de retries
    # manuels seraient nécessaires, jamais de résultat incorrect.
    default_args={"retries": 20},
)
def card_price_pipeline():
    @task
    def extract_and_load_raw() -> str:
        # ÉCART DÉLIBÉRÉ par rapport à une réécriture "en dur" de la boucle
        # de pagination + checkpoint ICI dans le DAG : on réutilise
        # run_extract_load() (src/extract/pipeline.py, Task 4) plutôt que de
        # dupliquer sa logique. Cette fonction a été extraite précisément
        # pour être partagée entre scripts/run_extract_load.py (le point
        # d'entrée manuel) ET cette tâche Airflow -- dupliquer la boucle ici
        # aurait recréé exactement le risque que ce refactor visait à
        # éliminer (une version divergente, qui perdrait le correctif de
        # checkpoint par page en cas de modification future d'un seul des
        # deux endroits).
        #
        # Pourquoi CETTE tâche ouvre sa connexion avec psycopg.connect(...)
        # directement, PLUTÔT QUE get_connection() (contrairement à
        # clean_to_staging et load_to_warehouse ci-dessous, qui suivent le
        # pattern standard `with get_connection(...) as conn:`) : la
        # justification complète (commit PAR PAGE / checkpoint, pas un
        # commit global de fin de run) vit dans le commentaire en tête de
        # src/extract/pipeline.py -- on ne la duplique pas ici pour éviter
        # que les deux versions divergent avec le temps ; se référer à ce
        # fichier pour le détail. Le même choix est déjà fait, pour la même
        # raison, dans scripts/run_extract_load.py (le point d'entrée manuel
        # équivalent hors Airflow).
        extracted_date = datetime.now(UTC).date()
        client = PokemonTcgClient(load_pokemontcg_config())
        conn = psycopg.connect(load_db_config().dsn)
        try:
            run_extract_load(client, conn, extracted_date)
        finally:
            # finally : la connexion est fermée que le run réussisse ou
            # échoue (run_extract_load fait déjà son propre rollback interne
            # avant de relever l'exception -- voir src/extract/pipeline.py).
            conn.close()
        return extracted_date.isoformat()

    @task
    def clean_to_staging(extracted_date_iso: str) -> str:
        # date.fromisoformat : XCom (le mécanisme Airflow qui transmet la
        # valeur de retour d'une tâche à la suivante) ne sérialise que des
        # types simples (str, int, dict...), pas des objets date Python --
        # d'où l'aller-retour isoformat()/fromisoformat() entre tâches.
        extracted_date = date.fromisoformat(extracted_date_iso)
        # Pattern standard get_connection() : cette tâche fait UNE seule
        # unité de travail (nettoyer un jour de données), pas de checkpoint
        # multi-étapes nécessaire -- commit global en fin de bloc si tout
        # réussit, rollback total sinon (voir src/common/db.py).
        with get_connection(load_db_config()) as conn:
            clean_raw_to_staging(conn, extracted_date)
        return extracted_date_iso

    @task
    def load_to_warehouse(extracted_date_iso: str) -> None:
        extracted_date = date.fromisoformat(extracted_date_iso)
        with get_connection(load_db_config()) as conn:
            load_staging_to_warehouse(conn, extracted_date)

    # Orchestration : chaque tâche reçoit la date (au format ISO, via XCom)
    # renvoyée par la précédente, ce qui crée implicitement la dépendance
    # d'ordre extract_and_load_raw >> clean_to_staging >> load_to_warehouse
    # (Airflow déduit le graphe de dépendances de ces appels enchaînés, pas
    # besoin de >> explicite ici).
    extracted_date_iso = extract_and_load_raw()
    cleaned_date_iso = clean_to_staging(extracted_date_iso)
    load_to_warehouse(cleaned_date_iso)


# Appel du DAG décoré : nécessaire pour qu'Airflow, en importateur le module
# depuis dags/, enregistre effectivement une instance de DAG (le décorateur
# @dag seul ne fait que définir une factory, il faut l'invoquer pour produire
# l'objet DAG que le scheduler va scanner et planifier).
card_price_pipeline()
