# Point d'entrée exécutable du pipeline "extract -> load" : va chercher
# TOUTES les cartes disponibles sur pokemontcg.io, page par page, et les
# charge dans raw.card_prices. Depuis le refactor de la Task 4 (Mois 2), ce
# script est volontairement FIN : toute la logique d'extraction/checkpoint/
# reprise vit désormais dans src/extract/pipeline.py (run_extract_load), pour
# être partagée avec la tâche Airflow `extract_and_load_raw`
# (dags/card_price_pipeline_dag.py) sans dupliquer le code ni son correctif.
# Ce script ne fait plus que : construire les objets nécessaires (date, client
# API, connexion), appeler run_extract_load(), et logger le résultat -- c'est
# la même responsabilité qu'assume la tâche Airflow équivalente, juste
# déclenchée manuellement en ligne de commande ("python -m
# scripts.run_extract_load", le -m garantit que les imports "src...."
# fonctionnent, quel que soit le dossier depuis lequel on lance la commande)
# plutôt que par le scheduler Airflow.
#
# IMPORTANT -- pourquoi ce script N'UTILISE PAS get_connection() (contrairement
# au reste du pipeline, voir src/common/db.py), et ouvre sa connexion
# manuellement avec psycopg.connect() :
#
# get_connection() ouvre UNE connexion pour tout le bloc "with" et commit
# seulement à la sortie du bloc (rollback total si une exception est levée
# n'importe où dedans) -- le bon choix par défaut pour une unité de travail
# petite et rapide. Mais run_extract_load() pilote elle-même un commit PAR
# PAGE (checkpoint), pas un commit global en fin de run : il faut donc lui
# passer une connexion sur laquelle CE script (l'appelant) ne fait PAS déjà un
# commit/rollback automatique à sa sortie, sans quoi les deux mécanismes de
# commit entreraient en conflit. Voir le commentaire en tête de
# src/extract/pipeline.py pour l'explication complète de la stratégie de
# checkpoint (pourquoi page par page, et le cas limite de _resume_page) --
# elle n'est pas dupliquée ici pour ne pas risquer que les deux versions
# divergent avec le temps.
from __future__ import annotations

import logging
from datetime import UTC, datetime

import psycopg

from src.common.config import load_db_config, load_pokemontcg_config
from src.extract.pipeline import run_extract_load
from src.extract.pokemontcg_client import PokemonTcgClient

# Configure le logging racine AVANT tout le reste : basicConfig() doit être
# appelé une seule fois, tôt dans le programme, pour que les logs de TOUS les
# modules importés ensuite (le client API, le loader, src/extract/pipeline...)
# soient bien affichés avec ce format, plutôt que d'être silencieusement
# ignorés (par défaut, Python n'affiche que WARNING et plus grave tant
# qu'aucune configuration n'a été faite). Cette configuration reste dans le
# SCRIPT (point d'entrée), pas dans src/extract/pipeline.py : un module
# importé ne doit jamais configurer le logging global lui-même (ça
# écraserait silencieusement la configuration que l'appelant -- ce script,
# ou Airflow qui a sa propre configuration de logging -- a mise en place).
# format="%(asctime)s %(levelname)s %(name)s %(message)s" affiche : l'heure,
# le niveau (INFO/ERROR...), le nom du logger (donc le module d'origine, ex.
# "src.extract.pipeline"), puis le message -> permet de savoir QUAND
# et D'OÙ vient chaque ligne de log, utile pour une extraction longue de
# plusieurs minutes.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    # extracted_date : la date du JOUR de l'extraction (en UTC, pas l'heure
    # locale de la machine, pour que la valeur soit sans ambiguïté quel que
    # soit le fuseau horaire d'où tourne le pipeline). Calculée une seule fois
    # ici et transmise telle quelle à run_extract_load(), qui l'utilise pour
    # TOUTES les pages de ce run.
    extracted_date = datetime.now(UTC).date()
    # Le client API est construit une seule fois et réutilisé pour tous les
    # appels (il encapsule la config -- clé API, URL de base -- et la
    # politique de retry définies au Mois 1).
    client = PokemonTcgClient(load_pokemontcg_config())

    # Connexion ouverte manuellement (psycopg.connect direct), PAS via
    # get_connection() -- voir le commentaire en tête de fichier pour la
    # justification complète. On garde la MÊME connexion ouverte pour toute la
    # durée du run (pas une par page : ce serait coûteux) ; c'est
    # run_extract_load() qui pilote elle-même QUAND committer (après chaque
    # page, pas après tout le run).
    conn = psycopg.connect(load_db_config().dsn)
    try:
        total_loaded = run_extract_load(client, conn, extracted_date)
    finally:
        # Que le run se termine en succès ou après un rollback + raise dans
        # run_extract_load(), on ferme toujours la connexion pour ne pas fuir
        # de connexion ouverte (finally s'exécute dans tous les cas).
        conn.close()

    # Log final : bilan de l'exécution complète, une fois la connexion
    # fermée. total_loaded ne compte que les cartes chargées PENDANT CE run
    # (pas le total cumulé en base sur d'éventuels runs précédents du même
    # jour), valeur renvoyée par run_extract_load().
    logger.info("Extraction terminée : %d cartes chargées pour le %s", total_loaded, extracted_date)


# Ce garde-fou ("if __name__ == '__main__':") assure que main() ne s'exécute
# que si ce fichier est lancé directement comme script (ex: via
# "python -m scripts.run_extract_load"), et PAS si jamais un autre module
# l'importait (ce qui n'est pas censé arriver ici, mais c'est une convention
# standard en Python pour tout script exécutable).
if __name__ == "__main__":
    main()
