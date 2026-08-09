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

from datetime import UTC, date, datetime, timedelta

import psycopg
from airflow.decorators import dag, task
from airflow.models import DagRun

from src.common.config import load_db_config, load_pokemontcg_config
from src.common.db import get_connection
from src.extract.pipeline import run_extract_load
from src.extract.pokemontcg_client import PokemonTcgClient
from src.load.warehouse_loader import load_staging_to_warehouse
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
    dagrun_timeout=timedelta(hours=4),
)
def card_price_pipeline():
    # retries=60 (porté de 20 à 60 le 2026-08-08, voir
    # docs/superpowers/specs/2026-08-08-dag-reliability-design.md) SCOPÉ À
    # CETTE SEULE TÂCHE (pas à default_args du DAG comme avant une review de
    # code) : valeur augmentée après un run réel (scheduled__2026-08-07) qui
    # a épuisé ses 20 retries (21 tentatives au total) sans finir
    # l'extraction, à cause d'une panne pokemontcg.io large et prolongée
    # cette nuit-là (erreurs 500/502 sur des pages différentes selon les
    # tentatives : page 1, 28, 49...). Preuve concrète tirée des logs de ce
    # run : chaque tentative progressait bien via le checkpoint (page 1 ->
    # 28 -> 49 sur 21 tentatives, ~2,3 pages gagnées par tentative en
    # moyenne), mais 21 tentatives n'ont pas suffi à couvrir les ~80 pages
    # du catalogue -- il en aurait fallu environ 35 à ce rythme. 60 laisse
    # une marge confortable pour une panne encore pire, tout en restant
    # borné, sans aucun risque de donnée : le mécanisme de checkpoint
    # (commit par page, voir src/extract/pipeline.py) garantit qu'aucune
    # tentative ne repart de zéro ni ne duplique.
    #
    # Pire cas RÉEL (corrigé le 2026-08-09, review finale) : ce n'est PAS
    # "60 x retry_delay=30s = ~30 minutes" -- ce calcul ne comptait que les
    # pauses entre tentatives, pas le temps de travail de chaque tentative.
    # Mesure empirique tirée de l'incident lui-même : 21 tentatives en ~26
    # minutes, soit ~74s par tentative (30s de pause + ~44s de travail
    # réel) -- au même rythme, 61 tentatives ~= 75 minutes. En pire cas
    # théorique (pannes lentes par timeout plutôt que 500 immédiats, voir
    # PokemonTcgClient : jusqu'à ~54s perdus sur une seule page avant
    # d'abandonner), l'ordre de grandeur monte à 1h30-3h. C'est
    # dagrun_timeout (voir le décorateur @dag ci-dessus), PAS
    # execution_timeout ci-dessous, qui plafonne ce pire cas CUMULÉ --
    # execution_timeout ne borne qu'une seule tentative à la fois, voir son
    # propre commentaire pour le détail de cette distinction (round 2 de
    # cette review a corrigé une première version erronée de ce
    # commentaire, qui attribuait ce rôle à execution_timeout).
    #
    # retry_delay=30s (pas les 5 minutes par défaut d'Airflow) : la reprise
    # est immédiate et peu coûteuse grâce au checkpoint par page (aucun
    # travail perdu, on repart de la dernière page confirmée) -- attendre 5
    # minutes entre chaque tentative n'apporterait rien ici (l'API ne "guérit"
    # pas parce qu'on attend plus longtemps que 30s) et multiplierait juste
    # inutilement la durée totale d'un run qui doit déjà absorber jusqu'à 60
    # tentatives.
    #
    # Pourquoi retries=60 N'EST PAS mis sur les 3 tâches (via default_args du
    # DAG, comme c'était le cas avant) : clean_to_staging et load_to_warehouse
    # ci-dessous sont des opérations SQL locales, rapides et déterministes --
    # elles n'appellent aucune API externe instable. Si l'une d'elles échoue,
    # c'est très probablement un vrai bug (pas un aléa réseau), et le laisser
    # masqué derrière 60 tentatives x le délai entre essais retarderait sa
    # visibilité pour rien. Elles gardent donc retries=2 (une valeur modeste,
    # pour absorber un aléa transitoire de connexion à la DB locale, sans
    # cacher un vrai bug pendant longtemps).
    # execution_timeout=timedelta(hours=2) (ajouté le 2026-08-09, review
    # finale) : NE plafonne PAS le pire cas cumulé (~1h30-3h sur l'ensemble
    # des 60 tentatives, voir commentaire ci-dessus) -- c'est un plafond PAR
    # TENTATIVE, remis à zéro à chaque nouveau retry (vérifié : c'est le
    # comportement documenté d'Airflow, pas une supposition -- une première
    # version de ce commentaire affirmait à tort le contraire, corrigée au
    # round 2 de cette review). Sa seule utilité réelle ici : protéger
    # contre UNE tentative qui resterait bloquée indéfiniment sans jamais
    # lever d'exception (ex: un appel réseau qui ne "timeout" pas
    # proprement côté librairie) -- un cas marginal vu que chaque tentative
    # observée en pratique dure ~44-74s, mais un filet de sécurité peu
    # coûteux à garder. Le plafond qui borne réellement le CUMUL de toutes
    # les tentatives est dagrun_timeout, sur le décorateur @dag ci-dessus.
    @task(retries=60, retry_delay=timedelta(seconds=30), execution_timeout=timedelta(hours=2))
    def extract_and_load_raw(dag_run: DagRun) -> str:
        # dag_run: DagRun -- paramètre injecté AUTOMATIQUEMENT par Airflow
        # (TaskFlow reconnaît "dag_run" comme nom de paramètre spécial et le
        # peuple avec l'objet DagRun courant, sans rien à configurer côté
        # appel, voir l'invocation extract_and_load_raw() en bas de fichier
        # -- inchangée). extracted_date se base sur dag_run.start_date (et
        # NON PLUS uniquement datetime.now(UTC).date() comme avant ce
        # correctif, voir
        # docs/superpowers/specs/2026-08-08-dag-reliability-design.md) :
        # dag_run.start_date reste FIXE entre les tentatives d'une même
        # tâche une fois le DagRun repassé en RUNNING -- contrairement à
        # datetime.now(), qui se réévalue à CHAQUE tentative. Avec
        # retries=60 (voir ci-dessus), une séquence de retries peut
        # désormais durer assez longtemps pour traverser minuit UTC ;
        # recalculer "aujourd'hui" à chaque tentative aurait alors changé
        # extracted_date en cours de route, cassant le checkpoint
        # (_resume_page compte les lignes déjà chargées pour L'ANCIENNE
        # date, en trouve zéro pour la nouvelle, et repart de la page 1 --
        # perdant toute la progression déjà faite pour la date d'origine).
        # dag_run.start_date élimine ce risque : la valeur reste identique
        # du début à la toute dernière tentative, quelle que soit la durée
        # totale du run.
        #
        # `or datetime.now(UTC)` (ajouté le 2026-08-09, review finale) :
        # dag_run.start_date peut valoir None -- pas seulement avant la
        # toute première exécution, mais aussi juste après un `airflow
        # tasks clear` (utilisé pour rejouer un run bloqué, voir
        # docs/superpowers/plans/2026-08-08-dag-reliability.md, Task 2).
        # Vérifié dans le code source d'Airflow 2.9.3
        # (airflow/models/taskinstance.py:clear_task_instances) : un clear
        # remet le DagRun en QUEUED et met start_date à None ; c'est le
        # SCHEDULER qui le repeuple au passage QUEUED -> RUNNING
        # (airflow/jobs/scheduler_job_runner.py). Entre les deux, si cette
        # tâche s'exécutait, dag_run.start_date.date() lèverait
        # AttributeError sur None. Le repli sur datetime.now(UTC) couvre
        # cette fenêtre sans rien changer au cas normal (start_date déjà
        # peuplé dans l'immense majorité des exécutions).
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
        extracted_date = (dag_run.start_date or datetime.now(UTC)).date()
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
