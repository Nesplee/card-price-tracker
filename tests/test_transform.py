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
    # tcgplayer.prices.<variante>. **overrides permet à chaque test de ne
    # modifier QUE le champ qui l'intéresse (ex: set=None) sans réécrire tout
    # le payload, ce qui rend chaque test lisible et concentré sur un seul cas.
    payload = {
        "id": "base1-1",
        "name": "Alakazam",
        "rarity": "Rare Holo",
        "set": {"id": "base1", "name": "Base"},
        "tcgplayer": {
            "prices": {
                "normal": {"low": 8.0, "mid": 13.0, "market": 12.5, "high": 20.0},
            }
        },
    }
    payload.update(overrides)
    return payload


def test_validate_and_clean_accepts_valid_payload() -> None:
    # Cas nominal : un payload complet et cohérent doit être accepté, et les
    # champs du CleanedCard doivent correspondre à ceux du payload d'origine.
    # market -> average_sell_price, low -> low_price, mid -> trend_price (voir
    # docs/superpowers/specs/2026-08-07-tcgplayer-pricing-source-design.md).
    result = validate_and_clean(_make_payload())

    assert result.is_valid
    assert result.cleaned.card_id == "base1-1"
    assert result.cleaned.average_sell_price == 12.5
    assert result.cleaned.low_price == 8.0
    assert result.cleaned.trend_price == 13.0


def test_validate_and_clean_rejects_missing_set() -> None:
    # Une carte sans information de set (id/name) n'est pas exploitable en
    # staging (set_id/set_name sont NOT NULL, voir migrations/002_...sql) :
    # elle doit être rejetée avec une raison mentionnant "set".
    result = validate_and_clean(_make_payload(set=None))

    assert not result.is_valid
    assert "set" in result.rejection_reason


def test_validate_and_clean_rejects_when_no_price_available() -> None:
    # Une carte sans AUCUN prix tcgplayer (prices vide) n'a pas d'intérêt pour
    # un pipeline de suivi de PRIX : mieux vaut la rejeter explicitement en
    # quarantaine (avec sa raison) plutôt que de l'insérer en staging avec les 3
    # prix NULL, ce qui masquerait silencieusement un problème de données
    # source.
    result = validate_and_clean(_make_payload(tcgplayer={"prices": {}}))

    assert not result.is_valid
    assert "prix" in result.rejection_reason
    assert "tcgplayer" in result.rejection_reason


def test_validate_and_clean_rejects_negative_price() -> None:
    # Un prix négatif est physiquement impossible (une carte ne peut pas
    # avoir une valeur marchande négative) : c'est le signe d'une anomalie
    # de la source ou d'un bug, donc la carte est rejetée plutôt
    # qu'acceptée avec une donnée aberrante qui fausserait les analyses.
    result = validate_and_clean(_make_payload(tcgplayer={"prices": {"normal": {"market": -1.0}}}))

    assert not result.is_valid
    assert "négatif" in result.rejection_reason


def test_validate_and_clean_prefers_normal_over_holofoil_variant() -> None:
    # Si plusieurs variantes d'impression sont disponibles, "normal" doit être
    # choisie en priorité (ordre de priorité documenté dans le design spec) —
    # pas la première trouvée dans le dict ni la plus chère.
    result = validate_and_clean(
        _make_payload(
            tcgplayer={
                "prices": {
                    "holofoil": {"low": 6.0, "mid": 20.0, "market": 15.0},
                    "normal": {"low": 1.0, "mid": 2.0, "market": 1.5},
                }
            }
        )
    )

    assert result.is_valid
    assert result.cleaned.average_sell_price == 1.5


def test_validate_and_clean_prefers_holofoil_when_normal_absent() -> None:
    # Une carte Rare Holo n'a souvent AUCUNE variante "normal" (elle n'existe
    # qu'en holofoil) : la sélection doit alors retomber sur "holofoil",
    # deuxième priorité de la liste, pas rejeter la carte faute de "normal".
    result = validate_and_clean(
        _make_payload(
            tcgplayer={
                "prices": {
                    "reverseHolofoil": {"low": 4.0, "mid": 5.0, "market": 4.5},
                    "holofoil": {"low": 6.0, "mid": 9.0, "market": 7.5},
                }
            }
        )
    )

    assert result.is_valid
    assert result.cleaned.average_sell_price == 7.5


def test_validate_and_clean_falls_back_to_first_available_variant() -> None:
    # Si aucune variante de la liste de priorité n'est présente (ex: une
    # variante récente type "pokeBallPattern", non gérée spécifiquement en v1
    # — décision produit explicite, voir le design spec), on retombe sur la
    # première variante du payload plutôt que de rejeter la carte.
    result = validate_and_clean(
        _make_payload(
            tcgplayer={"prices": {"pokeBallPattern": {"low": 3.0, "mid": 4.0, "market": 3.5}}}
        )
    )

    assert result.is_valid
    assert result.cleaned.average_sell_price == 3.5


def test_validate_and_clean_skips_variant_present_but_without_usable_price() -> None:
    # Bug réel trouvé en review (2026-08-09) : "normal" est présente dans le
    # payload (priorité la plus haute) mais entièrement vide -- l'ancienne
    # implémentation la sélectionnait quand même (présente = choisie), rejetant
    # la carte alors que "reverseHolofoil", juste à côté dans le MÊME payload,
    # a un vrai prix exploitable. La sélection doit ignorer les variantes sans
    # prix utilisable et continuer à chercher, pas s'arrêter à la première
    # clé présente.
    result = validate_and_clean(
        _make_payload(
            tcgplayer={
                "prices": {
                    "normal": {"low": None, "mid": None, "market": None},
                    "reverseHolofoil": {"low": 0.5, "mid": 1.0, "market": 0.8},
                }
            }
        )
    )

    assert result.is_valid
    assert result.cleaned.average_sell_price == 0.8


def test_validate_and_clean_handles_null_variant_without_crashing() -> None:
    # Bug réel trouvé en review (2026-08-09) : l'API peut renvoyer une
    # variante explicitement à `null` en JSON (donc None une fois désérialisé
    # par psycopg), pas seulement absente de la clé. L'ancienne implémentation
    # renvoyait ce None tel quel, et l'appel `.get("market")` suivant levait
    # AttributeError -- plantant tout clean_to_staging du jour (aucun
    # try/except par carte dans src/transform/clean.py) pour une seule carte
    # malformée. La sélection doit ignorer une variante None et continuer,
    # pas planter.
    result = validate_and_clean(
        _make_payload(
            tcgplayer={
                "prices": {
                    "normal": None,
                    "holofoil": {"low": 6.0, "mid": 9.0, "market": 7.5},
                }
            }
        )
    )

    assert result.is_valid
    assert result.cleaned.average_sell_price == 7.5


def test_validate_and_clean_rejects_when_all_variants_present_but_empty() -> None:
    # Cas limite du fix : si TOUTES les variantes présentes sont vides/None,
    # la carte doit toujours être rejetée (pas de régression sur la règle 3
    # existante) -- le fix ne doit pas faire accepter des cartes qui n'ont
    # vraiment aucun prix nulle part dans le payload.
    result = validate_and_clean(
        _make_payload(
            tcgplayer={
                "prices": {
                    "normal": {"low": None, "mid": None, "market": None},
                    "holofoil": None,
                }
            }
        )
    )

    assert not result.is_valid
    assert "prix" in result.rejection_reason
    assert "tcgplayer" in result.rejection_reason


def test_validate_and_clean_extracts_series() -> None:
    # series est le "bloc" Pokémon TCG (ex: "Scarlet & Violet" regroupe
    # plusieurs sets/séries individuelles). Présent dans payload["set"]
    # exactement comme set_id/set_name.
    result = validate_and_clean(
        _make_payload(set={"id": "sv2", "name": "Paldea Evolved", "series": "Scarlet & Violet"})
    )

    assert result.is_valid
    assert result.cleaned.series == "Scarlet & Violet"


def test_validate_and_clean_accepts_missing_series() -> None:
    # series absent du payload.set ne doit pas rejeter la carte (même
    # tolérance que rarity) -- juste series=None dans le résultat.
    result = validate_and_clean(_make_payload(set={"id": "base1", "name": "Base"}))

    assert result.is_valid
    assert result.cleaned.series is None
