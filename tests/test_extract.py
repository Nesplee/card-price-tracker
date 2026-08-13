# Tests du client HTTP pour l'API pokemontcg.io (src/extract/pokemontcg_client.py).
# On utilise la bibliothèque "responses" pour intercepter les appels HTTP
# sortants de `requests` et leur substituer des réponses fabriquées : les
# tests s'exécutent donc sans réseau, sans vraie clé API et de façon
# reproductible (pas de flakiness liée à la disponibilité de l'API réelle).
import pytest
import responses

from src.common.config import PokemonTcgConfig
from src.extract.pokemontcg_client import PokemonTcgApiError, PokemonTcgClient


@pytest.fixture
def client() -> PokemonTcgClient:
    # api_key="test-key" : valeur factice, jamais vérifiée côté "responses"
    # (les mocks ne valident pas les headers par défaut), donc n'importe
    # quelle chaîne convient pour les tests.
    # wait_min_seconds=0, wait_max_seconds=0 : sans ça, les tests qui
    # déclenchent des retries (ci-dessous) attendraient réellement plusieurs
    # secondes entre chaque tentative à cause du backoff exponentiel — inutile
    # et lent dans une suite de tests. On veut tester la LOGIQUE de retry, pas
    # attendre le vrai délai.
    config = PokemonTcgConfig(api_key="test-key")
    return PokemonTcgClient(config, wait_min_seconds=0, wait_max_seconds=0)


@responses.activate
def test_fetch_cards_page_returns_data(client: PokemonTcgClient) -> None:
    # Cas nominal : l'API répond 200 dès le premier appel. On vérifie que
    # fetch_cards_page extrait bien la liste sous la clé "data" de la réponse
    # JSON (et non l'objet JSON complet).
    responses.add(
        responses.GET,
        "https://api.pokemontcg.io/v2/cards",
        json={"data": [{"id": "base1-1", "name": "Alakazam"}]},
        status=200,
    )

    cards = client.fetch_cards_page(page=1)

    assert cards == [{"id": "base1-1", "name": "Alakazam"}]


@responses.activate
def test_fetch_cards_page_retries_then_succeeds(client: PokemonTcgClient) -> None:
    # `responses.add` empilé plusieurs fois : la bibliothèque "responses" sert
    # les réponses enregistrées dans l'ordre, une par appel HTTP effectué. Ici
    # les deux premiers appels échouent (500 = erreur serveur, transitoire par
    # nature), et le troisième réussit. C'est exactement le scénario que le
    # backoff exponentiel est censé gérer : au lieu d'abandonner à la première
    # erreur 5xx, le client réessaie automatiquement quelques fois avant de
    # renoncer, car une erreur 5xx peut disparaître d'elle-même quelques
    # secondes plus tard (redémarrage du serveur, pic de charge temporaire...).
    responses.add(responses.GET, "https://api.pokemontcg.io/v2/cards", status=500)
    responses.add(responses.GET, "https://api.pokemontcg.io/v2/cards", status=500)
    responses.add(
        responses.GET,
        "https://api.pokemontcg.io/v2/cards",
        json={"data": [{"id": "base1-2", "name": "Blastoise"}]},
        status=200,
    )

    cards = client.fetch_cards_page(page=1)

    assert cards == [{"id": "base1-2", "name": "Blastoise"}]
    # len(responses.calls) : nombre total de requêtes HTTP effectivement
    # envoyées. On vérifie ici qu'il y a bien eu 3 tentatives (2 échecs + 1
    # succès), c'est-à-dire que le retry a bien eu lieu et ne s'est pas arrêté
    # après le premier échec.
    assert len(responses.calls) == 3


@responses.activate
def test_fetch_cards_page_raises_after_exhausting_retries(client: PokemonTcgClient) -> None:
    # Les 4 réponses échouent toutes (500) : avec max_attempts=4 (valeur par
    # défaut de PokemonTcgClient), le client doit épuiser ses 4 tentatives
    # puis abandonner en levant PokemonTcgApiError, plutôt que de continuer à
    # réessayer indéfiniment ou d'échouer silencieusement en retournant une
    # liste vide. Un pipeline data qui avale les erreurs sans les signaler est
    # un pipeline qui produit des données silencieusement incomplètes.
    for _ in range(4):
        responses.add(responses.GET, "https://api.pokemontcg.io/v2/cards", status=500)

    with pytest.raises(PokemonTcgApiError):
        client.fetch_cards_page(page=1)

    # Vérifie qu'il y a eu exactement 4 tentatives (pas plus, pas moins) :
    # stop_after_attempt(max_attempts=4) doit stopper net à la 4e, ni avant
    # (ce qui abandonnerait trop tôt) ni après (ce qui ignorerait la limite).
    assert len(responses.calls) == 4


@responses.activate
def test_fetch_cards_page_fails_immediately_on_client_error(client: PokemonTcgClient) -> None:
    # Une réponse 401 (clé API invalide/manquante) est une erreur DÉFINITIVE :
    # la requête elle-même est incorrecte, donc la répéter produira toujours
    # exactement la même erreur 401. Contrairement aux tests 500 ci-dessus, on
    # ne veut ici PAS de retry : le client doit lever PokemonTcgApiError dès le
    # premier échec, sans consommer les 4 tentatives pour rien (ce qui ne
    # ferait que retarder inutilement un échec déjà certain, sans jamais
    # changer le résultat).
    responses.add(responses.GET, "https://api.pokemontcg.io/v2/cards", status=401)

    with pytest.raises(PokemonTcgApiError):
        client.fetch_cards_page(page=1)

    # Un seul appel HTTP effectué : la preuve que le retry n'a pas eu lieu sur
    # cette erreur 4xx (contrairement aux 4 appels du test précédent sur 5xx).
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_sets_returns_data(client: PokemonTcgClient) -> None:
    # Même logique que test_fetch_cards_page_returns_data : cas nominal, on
    # vérifie que fetch_sets() extrait bien la liste sous la clé "data" de la
    # réponse JSON.
    responses.add(
        responses.GET,
        "https://api.pokemontcg.io/v2/sets",
        json={
            "data": [
                {"id": "swsh6", "name": "Chilling Reign"},
                {"id": "xy5", "name": "Primal Clash"},
            ]
        },
        status=200,
    )

    sets = client.fetch_sets()

    assert sets == [
        {"id": "swsh6", "name": "Chilling Reign"},
        {"id": "xy5", "name": "Primal Clash"},
    ]
