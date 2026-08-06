# Point d'entrée exécutable du pipeline "extract -> load" : va chercher
# TOUTES les cartes disponibles sur pokemontcg.io, page par page, et les
# charge dans raw.card_prices. Aucun autre module ne dépend de ce script :
# c'est une orchestration de haut niveau, pas une brique réutilisable (d'où
# sa place dans scripts/ plutôt que src/). On l'exécute avec
# "python -m scripts.run_extract_load" (le -m garantit que les imports
# "src...." fonctionnent, quel que soit le dossier depuis lequel on lance la
# commande).
#
# IMPORTANT -- pourquoi ce script N'UTILISE PAS get_connection() (contrairement
# au reste du pipeline, voir src/common/db.py) :
#
# get_connection() ouvre UNE connexion pour tout le bloc "with" et commit
# seulement à la sortie du bloc (rollback total si une exception est levée
# n'importe où dedans). C'est le bon choix par défaut quand l'unité de travail
# est petite et rapide. Mais ici, l'unité de travail est une extraction
# COMPLÈTE de ~80 pages / ~20 000 cartes, qui peut prendre plusieurs minutes,
# en appelant une API externe (pokemontcg.io) dont l'instabilité a été
# CONSTATÉE en conditions réelles : environ 37% des appels directs échouent en
# 500/502/timeout. Sur ce volume, un échec en cours de route n'est pas une
# hypothèse rare, c'est presque certain à chaque run (vécu concrètement : un
# run a échoué à la page 69/82).
#
# Avec get_connection() englobant toute la boucle, un échec à la page 69 aurait
# fait un rollback des 68 pages précédentes déjà chargées avec succès : tout le
# travail (et tout le temps, et tous les appels API déjà consommés) serait
# perdu, et le prochain run repartirait bêtement de la page 1.
#
# Le choix fait ici est différent : traiter chaque PAGE comme l'unité atomique,
# pas le run entier. C'est possible sans risque car load_cards() (Task 4,
# src/load/raw_loader.py) est déjà idempotent PAR PAGE (upsert sur card_id,
# extracted_date, source) : committer une page dès qu'elle est chargée avec
# succès ne casse aucune garantie -- si on rejoue cette page plus tard (ex:
# reprise après crash), l'upsert la met juste à jour, il ne la duplique pas.
# On gère donc la connexion manuellement (psycopg.connect direct) pour
# contrôler nous-mêmes QUAND committer (après chaque page, pas après tout le
# run) plutôt que de subir le tout-ou-rien de get_connection().
from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import psycopg
from psycopg import Connection

from src.common.config import load_db_config, load_pokemontcg_config
from src.extract.pokemontcg_client import PokemonTcgClient
from src.load.raw_loader import load_cards

# Configure le logging racine AVANT tout le reste : basicConfig() doit être
# appelé une seule fois, tôt dans le programme, pour que les logs de TOUS les
# modules importés ensuite (le client API, le loader...) soient bien
# affichés avec ce format, plutôt que d'être silencieusement ignorés (par
# défaut, Python n'affiche que WARNING et plus grave tant qu'aucune
# configuration n'a été faite).
# format="%(asctime)s %(levelname)s %(name)s %(message)s" affiche : l'heure,
# le niveau (INFO/ERROR...), le nom du logger (donc le module d'origine, ex.
# "src.extract.pokemontcg_client"), puis le message -> permet de savoir QUAND
# et D'OÙ vient chaque ligne de log, utile pour une extraction longue de
# plusieurs minutes.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# Taille de page utilisée pour TOUS les appels à l'API pokemontcg.io dans ce
# script, définie UNE SEULE FOIS au niveau module. Avant ce correctif, l'appel
# à client.fetch_cards_page(page=page) ne précisait pas page_size et reposait
# sur la valeur par défaut du client (250, voir
# src/extract/pokemontcg_client.py). Le problème : le calcul de la page de
# reprise (_resume_page ci-dessous) a besoin de connaître EXACTEMENT la même
# taille de page pour que "nombre de lignes déjà en base // taille de page"
# soit cohérent avec la pagination réelle de l'API. Dupliquer la valeur 250 à
# deux endroits (l'appel API et le calcul de reprise) serait fragile : si l'un
# des deux change sans l'autre, la reprise recalculerait une page fausse. Une
# seule constante partagée élimine ce risque par construction.
PAGE_SIZE = 250


def _resume_page(conn: Connection, extracted_date: date, source: str = "pokemontcg.io") -> int:
    """Calcule la page à laquelle reprendre l'extraction, à partir du nombre de
    lignes déjà présentes en base pour ce (extracted_date, source).

    Logique : si N lignes sont déjà chargées, cela correspond à N // PAGE_SIZE
    pages COMPLETES déjà traitées (division entière = on ignore le reste).
    On reprend donc à la page suivante : (N // PAGE_SIZE) + 1.

    Cas limite volontaire -- la division entière (//) TRONQUE le reste : si N
    n'est pas un multiple exact de PAGE_SIZE (ex: 250 + 42 = 292 lignes),
    ces 42 lignes en trop ne comptent pour AUCUNE page complète
    (292 // 250 = 1, le reste 42 est simplement ignoré) -- la page à laquelle
    elles appartiennent (la page 2) est donc considérée comme PAS encore
    chargée, et sera intégralement rechargée depuis son début. Deux cas
    peuvent produire ce genre de reste :
      1. Un crash est survenu après le commit d'une page mais avant que le
         compte total reflète une page pleine (scénario défensif).
      2. Plus courant en pratique : ces 42 lignes sont simplement la
         dernière page RÉELLE du catalogue (le nombre total de cartes n'est
         pas un multiple exact de PAGE_SIZE).
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
    return (already_loaded // PAGE_SIZE) + 1


def main() -> None:
    # extracted_date : la date du JOUR de l'extraction (en UTC, pas l'heure
    # locale de la machine, pour que la valeur soit sans ambiguïté quel que
    # soit le fuseau horaire d'où tourne le pipeline). Elle est calculée UNE
    # SEULE fois ici, avant la boucle, puis réutilisée pour TOUTES les pages :
    # toutes les cartes d'une même exécution du script doivent porter la
    # même extracted_date, sinon une extraction à cheval sur minuit créerait
    # artificiellement deux "jours" différents pour un seul run.
    extracted_date = datetime.now(UTC).date()
    # Le client API est construit une seule fois et réutilisé pour tous les
    # appels (il encapsule la config -- clé API, URL de base -- et la
    # politique de retry définies en Task 3).
    client = PokemonTcgClient(load_pokemontcg_config())

    # Connexion ouverte manuellement (psycopg.connect direct), PAS via
    # get_connection() -- voir le commentaire en tête de fichier pour la
    # justification complète. On garde la MÊME connexion ouverte pour toute la
    # durée du run (pas une par page : ce serait coûteux), mais on pilote
    # nous-mêmes le commit/rollback page par page ci-dessous, plutôt que de
    # laisser un contextmanager décider pour tout le run d'un coup.
    conn = psycopg.connect(load_db_config().dsn)
    try:
        # Calcule la page de reprise AVANT la boucle : s'il existe déjà des
        # lignes pour (extracted_date, source) -- typiquement parce qu'un run
        # précédent aujourd'hui a crashé en cours de route -- on reprend après
        # la dernière page déjà chargée, au lieu de tout refaire depuis la
        # page 1 (ce qui gaspillerait du temps ET des appels API sur une
        # source déjà instable).
        page = _resume_page(conn, extracted_date=extracted_date)
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
        # de taille fixe (PAGE_SIZE, passé explicitement ci-dessous pour ne
        # jamais dépendre implicitement de la valeur par défaut du client --
        # voir le commentaire sur PAGE_SIZE plus haut). "while True" + "break"
        # explicite est le pattern naturel ici car on ne connaît PAS à
        # l'avance le nombre total de pages (~19000+ cartes / 250 par page =
        # environ 76-80 pages) : on continue tant que l'API renvoie des
        # cartes, et on s'arrête dès qu'une page est vide.
        while True:
            try:
                cards = client.fetch_cards_page(page=page, page_size=PAGE_SIZE)
                # Liste vide : signal de fin de pagination envoyé par
                # pokemontcg.io (la page demandée dépasse le nombre de cartes
                # disponibles). On arrête la boucle ici plutôt que de
                # continuer à interroger des pages qui seront toujours vides.
                if not cards:
                    break
                # Charge cette page dans raw.card_prices et accumule le
                # nombre de cartes traitées (utile pour le log final
                # récapitulatif).
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
                # PokemonTcgApiError après épuisement des retries -- voir
                # Task 3), soit de load_cards()/du commit lui-même. Dans tous
                # les cas : rollback() n'annule QUE le travail non commité de
                # la page EN COURS (les pages précédentes, déjà commitées
                # individuellement ci-dessus, restent intactes en base -- ce
                # rollback ne les touche pas). On relève ensuite l'exception
                # (raise sans argument = "re-lève l'exception en cours") pour
                # que l'échec soit explicite : pas de except qui avale
                # silencieusement l'erreur, le run doit clairement apparaître
                # en échec (code de sortie non-nul, log ERROR visible) pour
                # qu'un opérateur (ou un scheduler) sache qu'il faut relancer.
                conn.rollback()
                raise
            # Page suivante pour la prochaine itération de la boucle.
            page += 1
    finally:
        # Que le run se termine en succès ou après un rollback + raise
        # ci-dessus, on ferme toujours la connexion pour ne pas fuir de
        # connexion ouverte (finally s'exécute dans tous les cas).
        conn.close()

    # Log final : bilan de l'exécution complète, une fois la connexion
    # fermée. Note : total_loaded ne compte que les cartes chargées PENDANT CE
    # run (pas le total cumulé en base sur d'éventuels runs précédents du même
    # jour) -- cohérent avec le comportement d'avant ce correctif.
    logger.info("Extraction terminée : %d cartes chargées pour le %s", total_loaded, extracted_date)


# Ce garde-fou ("if __name__ == '__main__':") assure que main() ne s'exécute
# que si ce fichier est lancé directement comme script (ex: via
# "python -m scripts.run_extract_load"), et PAS si jamais un autre module
# l'importait (ce qui n'est pas censé arriver ici, mais c'est une convention
# standard en Python pour tout script exécutable).
if __name__ == "__main__":
    main()
