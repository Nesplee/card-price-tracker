# DAG hebdomadaire de rafraîchissement du catalogue pokemontcg.io (créé le
# 2026-10-26). Jusque-là, extract_and_load_raw tournait CHAQUE JOUR dans
# card_price_pipeline. Depuis que les prix viennent de TCGCSV,
# pokemontcg.io ne fournit plus que le catalogue (nouvelles cartes/sets), et
# le DAG quotidien réutilise le dernier catalogue connu (snapshot_catalog) :
# un échec ici ne bloque donc plus les prix du jour. La tâche est déplacée
# telle quelle, avec sa politique de retries et son checkpoint par page.
#
# Lundi 02:00 UTC : avec dagrun_timeout=4h, le run est forcément terminé
# avant le DAG quotidien de 07:00 UTC -- sinon un UPSERT tardif de cette
# tâche réécrirait le bloc tcgplayer (prix pokemontcg.io figés) APRÈS que
# refresh_tcgplayer_prices y a écrit les prix TCGCSV du jour.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from airflow.decorators import dag, task
from airflow.models import DagRun

from src.common.config import load_db_config, load_pokemontcg_config
from src.extract.pipeline import run_extract_load
from src.extract.pokemontcg_client import PokemonTcgClient


@dag(
    schedule="0 2 * * 1",
    # start_date passée et catchup=False : mêmes raisons que dans
    # card_price_pipeline_dag.py (voir ses commentaires).
    start_date=datetime(2024, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=4),
)
def card_catalog_refresh():
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
    # (dans card_price_pipeline_dag.py) sont des opérations SQL locales,
    # rapides et déterministes --
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
        # clean_to_staging et load_to_warehouse dans card_price_pipeline_dag.py,
        # qui suivent le
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

    extract_and_load_raw()


card_catalog_refresh()
