# Point d'entrée exécutable du pipeline "extract -> load" : va chercher
# TOUTES les cartes disponibles sur pokemontcg.io, page par page, et les
# charge dans raw.card_prices. Aucun autre module ne dépend de ce script :
# c'est une orchestration de haut niveau, pas une brique réutilisable (d'où
# sa place dans scripts/ plutôt que src/). On l'exécute avec
# "python -m scripts.run_extract_load" (le -m garantit que les imports
# "src...." fonctionnent, quel que soit le dossier depuis lequel on lance la
# commande).
from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.common.config import load_db_config, load_pokemontcg_config
from src.common.db import get_connection
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

    page = 1
    total_loaded = 0
    # get_connection(...) ouvre une connexion Postgres pour TOUTE la durée de
    # l'extraction (pas une connexion par page) : moins coûteux que
    # reconnecter à chaque page, et get_connection commit automatiquement à
    # la sortie du "with" si tout s'est bien passé (voir src/common/db.py),
    # ou rollback si une exception remonte (ex: PokemonTcgApiError après
    # épuisement des retries) -- on ne veut jamais laisser une extraction
    # partiellement chargée si elle plante en cours de route.
    with get_connection(load_db_config()) as conn:
        # Boucle de pagination : pokemontcg.io renvoie les cartes par pages
        # de taille fixe (pageSize=250 par défaut dans fetch_cards_page).
        # "while True" + "break" explicite est le pattern naturel ici car on
        # ne connaît PAS à l'avance le nombre total de pages (~19000+ cartes
        # / 250 par page = environ 76-80 pages) : on continue tant que l'API
        # renvoie des cartes, et on s'arrête dès qu'une page est vide.
        while True:
            cards = client.fetch_cards_page(page=page)
            # Liste vide : signal de fin de pagination envoyé par
            # pokemontcg.io (la page demandée dépasse le nombre de cartes
            # disponibles). On arrête la boucle ici plutôt que de continuer à
            # interroger des pages qui seront toujours vides.
            if not cards:
                break
            # Charge cette page dans raw.card_prices et accumule le nombre
            # de cartes traitées (utile pour le log final récapitulatif).
            total_loaded += load_cards(conn, cards, extracted_date=extracted_date)
            # Page suivante pour la prochaine itération de la boucle.
            page += 1

    # Log final : bilan de l'exécution complète, une fois la connexion
    # fermée (donc après le commit implicite de get_connection).
    logger.info("Extraction terminée : %d cartes chargées pour le %s", total_loaded, extracted_date)


# Ce garde-fou ("if __name__ == '__main__':") assure que main() ne s'exécute
# que si ce fichier est lancé directement comme script (ex: via
# "python -m scripts.run_extract_load"), et PAS si jamais un autre module
# l'importait (ce qui n'est pas censé arriver ici, mais c'est une convention
# standard en Python pour tout script exécutable).
if __name__ == "__main__":
    main()
