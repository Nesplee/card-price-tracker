# DAG Airflow orchestrant le pipeline complet "extract -> clean -> load"
# (Task 4, Mois 2) : trois tâches enchaînées, chacune correspondant à une
# étape déjà développée et testée les mois/tâches précédents :
#   1. snapshot_catalog      : copie le dernier catalogue connu dans
#      raw.card_prices pour le jour (depuis le 2026-10-26 ; avant, c'était
#      extract_and_load_raw, déplacée dans card_catalog_refresh_dag.py).
#   1b. refresh_tcgplayer_prices : y écrit les prix TCGplayer du jour, lus
#      sur TCGCSV (pokemontcg.io les renvoie figés depuis le 2026-10-15).
#   2. clean_to_staging      : valide/nettoie raw -> staging (Task 2).
#   3. load_to_warehouse     : charge staging -> star schema prod (Task 3).
#   4. check_freshness       : alerte ntfy si aucun prix n'a bougé depuis le
#      jour précédent (source figée, ajouté le 2026-10-26).
# Ce module est un point d'ENTRÉE (aucun autre module du dépôt ne l'importe) :
# c'est Airflow (le scheduler, en le scannant dans dags/) qui l'exécute.
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from airflow.decorators import dag, task
from airflow.models import DagRun

from src.common.config import load_db_config, load_ntfy_config
from src.common.db import get_connection
from src.extract.catalog_snapshot import copy_previous_catalog
from src.extract.tcgcsv_prices import TcgCsvClient, apply_tcgcsv_prices
from src.load.warehouse_loader import load_staging_to_warehouse
from src.monitor.price_freshness import check_price_freshness, send_ntfy
from src.transform.clean import clean_raw_to_staging


@dag(
    # schedule="0 7 * * *" (cron : minute=0, heure=7, tous les jours) plutôt
    # que le raccourci "@daily" (équivalent à "0 0 * * *", minuit UTC) :
    # décision utilisateur de décaler le déclenchement automatique à 7h00
    # UTC. Valeur FIXE en UTC (pas de fuseau horaire local type
    # Europe/Zurich) : le DAG reste entièrement en UTC comme le reste de ce
    # fichier (voir start_date ci-dessous) -- ça évite toute complication
    # liée au changement d'heure été/hiver suisse, qui décalerait sinon
    # l'heure UTC réelle du déclenchement de 1h selon la saison.
    schedule="0 7 * * *",
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
    # max_active_runs=1 (ajouté le 2026-08-09, review finale du plan DAG
    # reliability) : empêche deux DagRuns de tourner en même temps pour ce
    # DAG (défaut Airflow : 16). Constaté en conditions réelles le jour même
    # de ce correctif : un déclenchement manuel + une reprise de run bloqué
    # + le nouveau run automatique se sont retrouvés à cibler la MÊME
    # extracted_date en parallèle (extract_and_load_raw calcule sa date via
    # dag_run.start_date, "aujourd'hui" pour n'importe quel run qui
    # s'exécute maintenant). Sans risque de données -- _resume_page +
    # l'UPSERT de load_cards rendent ça idempotent, vérifié empiriquement,
    # aucune ligne dupliquée -- mais chaque run concurrent relit son propre
    # compteur de reprise et refait des appels API déjà faits par l'autre :
    # du gaspillage pur contre une source déjà instable, sans aucun
    # bénéfice. max_active_runs=1 sérialise les runs de ce DAG (un DagRun
    # supplémentaire reste simplement en file d'attente au lieu de
    # s'exécuter en parallèle) sans changer aucun comportement fonctionnel.
    max_active_runs=1,
    # dagrun_timeout=timedelta(hours=4) (ajouté le 2026-08-09, round 3 de la
    # review finale -- valeur corrigée d'un round 2 qui n'avait AUCUNE marge
    # réelle, voir plus bas) : plafonne la durée CUMULÉE d'un DagRun entier,
    # retries inclus. Nécessaire ici et PAS interchangeable avec
    # execution_timeout (voir le commentaire sur extract_and_load_raw
    # ci-dessous) : execution_timeout est un plafond PAR TENTATIVE, remis à
    # zéro à chaque nouveau retry -- il ne borne en rien la durée totale
    # d'une tâche qui échoue rapidement (~1 min) puis retente jusqu'à 60
    # fois. dagrun_timeout, lui, s'applique au DagRun dans son ensemble :
    # passé ce délai, Airflow marque le DagRun en échec (les task instances
    # encore en cours passent à SKIPPED, pas FAILED -- distinction sans
    # conséquence ici, ce fichier ne définit aucun on_failure_callback qui
    # dépendrait de l'état précis de la tâche) même si une tâche est encore
    # en train de retenter -- le seul mécanisme qui transforme réellement un
    # run bloqué en signal terminal visible plutôt qu'un état "up_for_retry"
    # indéfini.
    #
    # Calcul de marge (round 3 -- le round 2 fixait 3h, exactement
    # l'estimation haute d'extract_and_load_raw SEULE, donc sans AUCUNE
    # marge dès qu'on compte le reste du DagRun) : pire cas
    # extract_and_load_raw ~3h (voir son commentaire plus bas) + pire cas
    # clean_to_staging et load_to_warehouse (retries=2 chacun, PAS de
    # retry_delay explicite -> défaut Airflow 5 min par retry, soit jusqu'à
    # ~10 min de pure attente par tâche en plus d'une exécution SQL locale
    # rapide) ~= 3h + 2x11 min ~= 3h22. 4h laisse ~38 min de marge réelle
    # au-dessus de ce total, pas seulement au-dessus d'une seule des trois
    # tâches.
    #
    # RISQUE CONNU, documenté et accepté (trouvé en review, 2026-08-09) : si
    # dagrun_timeout coupe le DagRun PENDANT qu'extract_and_load_raw est
    # encore en train de retenter, les pages déjà commitées dans
    # raw.card_prices pour cette extracted_date restent orphelines --
    # clean_to_staging et load_to_warehouse ne tournent jamais pour ce jour
    # (le DagRun est déjà FAILED), et le run automatique du lendemain
    # calcule une extracted_date DIFFÉRENTE (voir dag_run.start_date
    # ci-dessous) : rien ne revient automatiquement chercher le jour
    # abandonné. Scénario volontairement laissé sans détection automatique
    # (complexité disproportionnée pour un cas qui exigerait une panne
    # pokemontcg.io de plusieurs heures, pire que tout ce qui a été observé
    # à ce jour) -- RÉCUPÉRATION MANUELLE, déjà éprouvée en production le
    # 2026-08-09 pour combler un trou de données similaire (extracted_date
    # 2026-08-08) : appeler directement run_extract_load(client, conn,
    # date(...)) avec la date orpheline (reprend automatiquement via le
    # checkpoint existant dans raw.card_prices), puis clean_raw_to_staging
    # et load_staging_to_warehouse pour la même date. Voir
    # docs/superpowers/plans/2026-08-08-dag-reliability.md pour le contexte
    # de l'incident original (2026-08-08, sans lien avec ce risque précis --
    # comblé à l'époque par la même procédure manuelle décrite ci-dessus,
    # qui reste donc éprouvée et réutilisable si dagrun_timeout se déclenche
    # un jour en cours de retries).
    dagrun_timeout=timedelta(hours=4),
)
def card_price_pipeline():
    # snapshot_catalog (remplace extract_and_load_raw le 2026-10-26) : le
    # catalogue du jour est copié depuis le dernier jour chargé, sans appel à
    # pokemontcg.io (voir src/extract/catalog_snapshot.py). L'extraction
    # pokemontcg.io, avec sa politique de retries, vit désormais dans le DAG
    # hebdomadaire dags/card_catalog_refresh_dag.py. Tâche SQL locale ->
    # retries=2 comme clean_to_staging.
    @task(retries=2)
    def snapshot_catalog(dag_run: DagRun) -> str:
        # dag_run.start_date plutôt que datetime.now() : même raison que dans
        # l'ancienne extract_and_load_raw (voir card_catalog_refresh_dag.py),
        # la date reste fixe entre les tentatives d'un même run.
        extracted_date = (dag_run.start_date or datetime.now(UTC)).date()
        with get_connection(load_db_config()) as conn:
            copy_previous_catalog(conn, extracted_date)
        return extracted_date.isoformat()

    # refresh_tcgplayer_prices (ajouté le 2026-10-26) : pokemontcg.io renvoie
    # des prix TCGplayer figés depuis le 2026-10-15. Cette tâche remplace le
    # bloc payload.tcgplayer du jour par les prix TCGCSV (même format), voir
    # src/extract/tcgcsv_prices.py. Source externe -> retries comme
    # l'extraction, mais TCGCSV est un simple fichier statique bien plus
    # fiable que pokemontcg.io : 5 tentatives espacées d'1 min suffisent.
    @task(retries=5, retry_delay=timedelta(minutes=1))
    def refresh_tcgplayer_prices(extracted_date_iso: str) -> str:
        with get_connection(load_db_config()) as conn:
            apply_tcgcsv_prices(TcgCsvClient(), conn, date.fromisoformat(extracted_date_iso))
        return extracted_date_iso

    @task(retries=2)
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

    @task(retries=2)
    def load_to_warehouse(extracted_date_iso: str) -> str:
        extracted_date = date.fromisoformat(extracted_date_iso)
        with get_connection(load_db_config()) as conn:
            load_staging_to_warehouse(conn, extracted_date)
        return extracted_date_iso

    # check_freshness (ajouté le 2026-10-26) : pokemontcg.io a renvoyé les
    # mêmes prix du 2026-10-16 au 2026-10-26 sans aucune erreur, et le
    # pipeline "réussissait" donc en silence. Cette tâche compare le jour
    # chargé au précédent et envoie une alerte ntfy si aucun prix n'a bougé
    # (voir src/monitor/price_freshness.py). Elle ne fait PAS échouer le
    # DagRun quand les prix sont figés : les données du jour sont chargées
    # correctement, c'est la source qui est en cause, pas le pipeline.
    @task(retries=2)
    def check_freshness(loaded_date_iso: str) -> None:
        log = logging.getLogger(__name__)
        with get_connection(load_db_config()) as conn:
            report = check_price_freshness(conn, date.fromisoformat(loaded_date_iso))
        log.info("Fraîcheur des prix : %s", report)
        if not report.is_stale:
            return
        ntfy = load_ntfy_config()
        if ntfy is None:
            log.warning("NTFY_TOPIC absent, alerte NON envoyée : %s", report.message())
            return
        send_ntfy(ntfy, report.message())

    # Orchestration : chaque tâche reçoit la date (au format ISO, via XCom)
    # renvoyée par la précédente, ce qui crée implicitement la dépendance
    # d'ordre snapshot_catalog >> ... >> check_freshness
    # (Airflow déduit le graphe de dépendances de ces appels enchaînés, pas
    # besoin de >> explicite ici).
    extracted_date_iso = snapshot_catalog()
    priced_date_iso = refresh_tcgplayer_prices(extracted_date_iso)
    cleaned_date_iso = clean_to_staging(priced_date_iso)
    loaded_date_iso = load_to_warehouse(cleaned_date_iso)
    check_freshness(loaded_date_iso)


# Appel du DAG décoré : nécessaire pour qu'Airflow, en importateur le module
# depuis dags/, enregistre effectivement une instance de DAG (le décorateur
# @dag seul ne fait que définir une factory, il faut l'invoquer pour produire
# l'objet DAG que le scheduler va scanner et planifier).
card_price_pipeline()
