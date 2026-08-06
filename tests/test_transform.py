# Tests unitaires de la couche de validation (src/transform/validate.py).
# Contrairement à tests/test_raw_loader.py, ces tests ne touchent PAS la base
# de données : validate_and_clean() est une fonction pure (payload dict ->
# ValidationResult), donc on peut la tester en mémoire, rapidement et sans
# dépendre d'un conteneur Postgres démarré. C'est précisément l'intérêt de
# séparer "valider/nettoyer" (pur) de "charger en base" (effet de bord) :
# on peut couvrir toutes les règles métier de validation par de simples
# assertions Python, sans fixture ni TRUNCATE.
from __future__ import annotations

from src.transform.validate import validate_and_clean


def _make_payload(**overrides) -> dict:
    # Construit un payload "carte pokemontcg.io" valide par défaut, avec la
    # forme exacte renvoyée par l'API (voir src/extract/pokemontcg_client.py) :
    # id/name au niveau racine, set imbriqué (id/name), prix imbriqués sous
    # cardmarket.prices. **overrides permet à chaque test de ne modifier QUE
    # le champ qui l'intéresse (ex: set=None) sans réécrire tout le payload,
    # ce qui rend chaque test lisible et concentré sur un seul cas de rejet.
    payload = {
        "id": "base1-1",
        "name": "Alakazam",
        "rarity": "Rare Holo",
        "set": {"id": "base1", "name": "Base"},
        "cardmarket": {"prices": {"averageSellPrice": 12.5, "trendPrice": 13.0, "lowPrice": 8.0}},
    }
    payload.update(overrides)
    return payload


def test_validate_and_clean_accepts_valid_payload() -> None:
    # Cas nominal : un payload complet et cohérent doit être accepté, et les
    # champs du CleanedCard doivent correspondre à ceux du payload d'origine.
    result = validate_and_clean(_make_payload())

    assert result.is_valid
    assert result.cleaned.card_id == "base1-1"
    assert result.cleaned.average_sell_price == 12.5


def test_validate_and_clean_rejects_missing_set() -> None:
    # Une carte sans information de set (id/name) n'est pas exploitable en
    # staging (set_id/set_name sont NOT NULL, voir migrations/002_...sql) :
    # elle doit être rejetée avec une raison mentionnant "set".
    result = validate_and_clean(_make_payload(set=None))

    assert not result.is_valid
    assert "set" in result.rejection_reason


def test_validate_and_clean_rejects_when_no_price_available() -> None:
    # Une carte sans AUCUN prix cardmarket (les 3 champs absents) n'a pas
    # d'intérêt pour un pipeline de suivi de PRIX : mieux vaut la rejeter
    # explicitement en quarantaine (avec sa raison) plutôt que l'insérer en
    # staging avec average_sell_price/trend_price/low_price tous NULL, ce qui
    # masquerait silencieusement un problème de données côté source.
    result = validate_and_clean(_make_payload(cardmarket={"prices": {}}))

    assert not result.is_valid
    assert "prix" in result.rejection_reason


def test_validate_and_clean_rejects_negative_price() -> None:
    # Un prix négatif est physiquement impossible (une carte ne peut pas
    # avoir une valeur marchande négative) : c'est le signe d'une anomalie
    # de la source ou d'un bug, donc la carte est rejetée plutôt
    # qu'acceptée avec une donnée aberrante qui fausserait les analyses.
    result = validate_and_clean(_make_payload(cardmarket={"prices": {"averageSellPrice": -1.0}}))

    assert not result.is_valid
    assert "négatif" in result.rejection_reason
