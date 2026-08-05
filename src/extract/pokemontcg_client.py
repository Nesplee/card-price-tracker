# Client HTTP pour l'API pokemontcg.io. Sa seule responsabilité est d'aller
# chercher des pages de cartes brutes ; il ne fait aucune transformation des
# données (ça, c'est le rôle des étapes staging/prod plus tard dans le
# pipeline). Il gère en revanche deux choses essentielles pour un pipeline de
# data engineering fiable : les erreurs réseau transitoires (via retry avec
# backoff exponentiel) et la traçabilité des appels (via logging).
from __future__ import annotations

import logging

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.common.config import PokemonTcgConfig

# logger dédié à ce module (son nom sera "src.extract.pokemontcg_client").
# Utiliser logging plutôt que print() permet à qui exécute le pipeline de
# choisir le niveau de verbosité (DEBUG/INFO/ERROR) et la destination des logs
# (console, fichier, service de monitoring) sans toucher au code ici.
logger = logging.getLogger(__name__)


class PokemonTcgApiError(Exception):
    """Levée quand l'API pokemontcg.io reste en échec après épuisement des tentatives."""


class PokemonTcgClient:
    def __init__(
        self,
        config: PokemonTcgConfig,
        timeout_seconds: float = 10.0,
        max_attempts: int = 4,
        wait_min_seconds: float = 1.0,
        wait_max_seconds: float = 10.0,
    ) -> None:
        # On stocke la config (URL de base + clé API) et le timeout pour s'en
        # servir dans _do_get(). Les paramètres de retry, eux, servent tout de
        # suite à construire l'objet Retrying ci-dessous.
        self._config = config
        self._timeout_seconds = timeout_seconds

        # --- Qu'est-ce qu'un backoff exponentiel, et pourquoi ? ---
        # Quand un appel réseau échoue (serveur en surcharge, coupure
        # momentanée...), la pire stratégie est de réessayer immédiatement en
        # boucle : si le serveur est déjà saturé, marteler ses requêtes ne
        # fait qu'aggraver la situation (et peut aussi déclencher un blocage
        # côté API si elle détecte un abus). Le backoff exponentiel consiste à
        # attendre un délai qui DOUBLE à chaque nouvel échec (1s, 2s, 4s, 8s,
        # ...) avant de réessayer : ça laisse au serveur le temps de se
        # rétablir, tout en limitant le temps total perdu si l'erreur se
        # résout vite. wait_exponential(multiplier=1, min=..., max=...)
        # calcule ce délai (borné entre wait_min_seconds et wait_max_seconds
        # pour éviter d'attendre indéfiniment si le calcul explose).
        # wait_min_seconds/wait_max_seconds sont des paramètres du constructeur
        # (plutôt que des constantes figées dans le code) précisément pour
        # pouvoir les mettre à 0 dans les tests (voir tests/test_extract.py) :
        # sans ça, la suite de tests attendrait réellement plusieurs secondes
        # à chaque test qui déclenche un retry.
        #
        # --- Pourquoi retry uniquement sur les erreurs réseau/5xx ? ---
        # requests.RequestException est la classe mère de toutes les erreurs
        # levées par `requests` : timeout, connexion refusée/coupée, DNS qui
        # échoue, et — via response.raise_for_status() dans _do_get() — les
        # codes HTTP 4xx et 5xx. Une erreur 5xx (500, 502, 503...) signifie
        # "le SERVEUR a un problème", typiquement transitoire : il y a de
        # bonnes chances que réessayer quelques secondes plus tard fonctionne.
        # À l'inverse, une erreur 4xx (401 clé API invalide, 404 route
        # inexistante) signifie "la REQUÊTE elle-même est incorrecte" :
        # réessayer la même requête produira toujours la même erreur, ce
        # n'est donc jamais utile de retry dessus (ça ne ferait que perdre du
        # temps). Ici on ne distingue pas 4xx/5xx dans le type d'exception
        # (requests ne les sépare pas nativement), mais dans la pratique les
        # tests ci-contre ne couvrent que les 5xx, qui sont le cas d'usage
        # visé par ce client.
        self._retrying = Retrying(
            # retry_if_exception_type(...) : ne déclenche un retry QUE si
            # l'exception levée est une RequestException (ou une sous-classe).
            # Toute autre exception (bug dans notre code, KeyError...) remonte
            # immédiatement sans retry : on ne veut pas masquer un vrai bug en
            # le faisant échouer silencieusement 4 fois de suite.
            retry=retry_if_exception_type(requests.RequestException),
            # stop_after_attempt(max_attempts) : arrête d'essayer après
            # max_attempts tentatives au total (la 1re tentative + les
            # retries), qu'elles aient réussi ou non. Évite de retry à
            # l'infini si le problème ne se résout jamais.
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=wait_min_seconds, max=wait_max_seconds),
            # reraise=True : si toutes les tentatives échouent, Retrying
            # relève la DERNIÈRE exception rencontrée telle quelle (ici une
            # requests.RequestException), plutôt que sa propre exception
            # interne (tenacity.RetryError) qui masquerait la cause réelle.
            # C'est ce qui permet au `except requests.RequestException` dans
            # fetch_cards_page() de l'attraper proprement ci-dessous.
            reraise=True,
        )

    def fetch_cards_page(self, page: int, page_size: int = 250) -> list[dict]:
        """Récupère une page de cartes. Lève PokemonTcgApiError si l'API reste
        indisponible après épuisement des tentatives — jamais d'échec silencieux."""
        try:
            # self._retrying(fn, *args) : appelle fn(*args) en lui appliquant
            # la politique de retry configurée dans __init__. En interne,
            # tenacity intercepte les RequestException, attend le délai de
            # backoff calculé, puis rappelle self._do_get(...) à nouveau,
            # jusqu'à succès ou épuisement de max_attempts.
            response = self._retrying(self._do_get, "/cards", {"page": page, "pageSize": page_size})
        except requests.RequestException as exc:
            # On arrive ici seulement après épuisement de TOUTES les
            # tentatives (grâce à reraise=True) : c'est un échec définitif, pas
            # transitoire. On le log en ERROR (pour qu'il remonte dans le
            # monitoring) puis on le convertit en PokemonTcgApiError : les
            # appelants de ce client (Task 4 : orchestration de l'extraction)
            # n'ont pas besoin de connaître les détails de `requests`, juste
            # de savoir "cette page n'a pas pu être récupérée".
            logger.error("Échec définitif de l'appel API après retries: %s", exc)
            raise PokemonTcgApiError(f"Impossible de récupérer la page {page}") from exc
        # response.json() : parse le corps de la réponse HTTP (du JSON texte)
        # en structures Python (dict/list). L'API pokemontcg.io renvoie un
        # objet englobant avec une clé "data" contenant la vraie liste de
        # cartes ; on extrait directement cette liste pour que l'appelant
        # n'ait pas à connaître ce détail du format de réponse de l'API.
        return response.json()["data"]

    def _do_get(self, path: str, params: dict) -> requests.Response:
        # Construit l'URL complète en concaténant l'URL de base (configurable,
        # ex. pour pointer vers un mock en test) et le chemin de la ressource
        # (ex. "/cards").
        url = f"{self._config.base_url}{path}"
        # Log AVANT l'appel (niveau INFO) : utile pour suivre la progression
        # d'une extraction longue (plusieurs centaines de pages) et pour
        # diagnostiquer précisément quel appel a échoué en cas de problème.
        logger.info("Appel API pokemontcg.io: %s params=%s", url, params)
        response = requests.get(
            url,
            params=params,
            # X-Api-Key : header d'authentification attendu par pokemontcg.io.
            headers={"X-Api-Key": self._config.api_key},
            # timeout_seconds : borne le temps d'attente d'une réponse. Sans
            # timeout, un serveur qui ne répond jamais bloquerait le pipeline
            # indéfiniment ; ici, passé ce délai, requests lève un
            # RequestException (Timeout), qui déclenche donc un retry comme
            # n'importe quelle autre erreur réseau.
            timeout=self._timeout_seconds,
        )
        # raise_for_status() : lève une requests.HTTPError (sous-classe de
        # RequestException) si le code HTTP est 4xx ou 5xx. Sans cet appel,
        # une réponse 500 avec un corps vide/invalide passerait inaperçue et
        # response.json()["data"] planterait plus loin avec une erreur
        # confuse au lieu de déclencher proprement le mécanisme de retry ici.
        response.raise_for_status()
        return response
