# Lecture et rapprochement de la collection personnelle de l'utilisateur
# (export CSV d'une app tierce) avec le catalogue déjà en base
# (prod.dim_card, alimenté par le pipeline de prix). Fonctions PURES (aucun
# accès DB ni réseau ici) : la lecture du fichier local est un effet de bord
# limité et déterministe, mais aucune de ces fonctions n'ouvre de connexion
# DB ni n'appelle une API -- exactement la même philosophie que
# src/transform/validate.py (testable en mémoire, sans fixture DB).
#
# Voir docs/superpowers/specs/2026-08-09-personal-collection-import-design.md
# pour le raisonnement complet derrière le format de rapprochement choisi.
from __future__ import annotations

import csv
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionRow:
    """Une ligne du CSV après filtrage (Category=Pokemon, Portfolio
    Name=Main, Rarity hors Common/Uncommon/vide), avant tentative de
    rapprochement avec le catalogue."""

    set_name: str
    card_number: str
    product_name: str
    variance: str
    grade: str
    rarity: str
    quantity: int
    average_cost_paid: float | None
    market_price_at_export: float | None


@dataclass(frozen=True)
class MatchResult:
    """Résultat du rapprochement d'une CollectionRow avec prod.dim_card :
    soit card_id est renseigné (rapprochement réussi) et rejection_reason
    vaut None, soit l'inverse -- même pattern either/or que ValidationResult
    dans src/transform/validate.py."""

    row: CollectionRow
    card_id: str | None
    rejection_reason: str | None


def normalize_card_number(raw_number: str) -> str:
    """Convertit un numéro de carte au format du CSV ("011/193", avec le
    total de la série) vers le format utilisé par pokemontcg.io dans ses
    card_id ("11", sans le total ni les zéros de tête). Vérifié directement
    contre les données de production : prod.dim_card.card_id suit toujours
    le format "{set_id}-{numéro}", où le numéro n'est jamais complété par
    des zéros -- lstrip("0") retire ces zéros, `or "0"` protège le cas
    limite d'un numéro qui ne serait QUE des zéros (ex: "000"), qui
    deviendrait une chaîne vide sans cette protection.

    Certaines cartes (essentiellement des promos) n'ont pas de "/total"
    dans leur numéro d'origine (ex: "SWSH001") : split("/")[0] renvoie alors
    la chaîne complète telle quelle, inchangée."""
    number = raw_number.split("/")[0]
    return number.lstrip("0") or "0"


def _find_market_price_column(fieldnames: list[str]) -> str | None:
    """Le nom de la colonne "Market Price (As of ...)" intègre la date de
    l'export (ex: "Market Price (As of 2026-08-07)") -- ce nom change à
    CHAQUE nouvel export CSV envoyé par l'utilisateur (import réutilisable,
    voir le spec). On la retrouve donc par préfixe plutôt que par un nom de
    colonne figé qui casserait au prochain export. Renvoie None si absente
    (champ optionnel, une valeur de référence historique seulement -- ne
    doit jamais faire échouer tout l'import si elle manque)."""
    for name in fieldnames:
        if name.startswith("Market Price"):
            return name
    return None


def read_collection_csv(path: str) -> list[CollectionRow]:
    """Lit un export CSV de collection et applique les filtres d'import
    (Category=Pokemon, Portfolio Name=Main, Rarity hors Common/Uncommon/
    vide) -- voir Global Constraints du plan pour la justification de
    chaque filtre. Ne fait AUCUN rapprochement avec le catalogue (voir
    match_row ci-dessous, appelée séparément par l'appelant)."""
    rows: list[CollectionRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        market_price_column = _find_market_price_column(reader.fieldnames or [])
        for record in reader:
            if record["Category"] != "Pokemon":
                continue
            if record["Portfolio Name"] != "Main":
                continue
            if record["Rarity"] in ("Common", "Uncommon", ""):
                continue
            market_price_raw = record.get(market_price_column, "") if market_price_column else ""
            rows.append(
                CollectionRow(
                    set_name=record["Set"],
                    card_number=record["Card Number"],
                    product_name=record["Product Name"],
                    variance=record["Variance"] or "",
                    grade=record["Grade"] or "",
                    rarity=record["Rarity"],
                    quantity=int(record["Quantity"]),
                    average_cost_paid=float(record["Average Cost Paid"])
                    if record["Average Cost Paid"]
                    else None,
                    market_price_at_export=float(market_price_raw) if market_price_raw else None,
                )
            )
    return rows


def match_row(
    row: CollectionRow, set_name_to_id: dict[str, str], known_card_ids: set[str]
) -> MatchResult:
    """Tente de relier une CollectionRow à une carte connue de
    prod.dim_card. Aucun matching approximatif (voir le spec) : un nom de
    set absent de set_name_to_id, ou un card_id construit absent de
    known_card_ids, part en quarantaine avec une raison explicite plutôt
    que de risquer un rapprochement silencieusement erroné."""
    set_id = set_name_to_id.get(row.set_name)
    if set_id is None:
        return MatchResult(
            row=row, card_id=None, rejection_reason=f"set inconnu : {row.set_name!r}"
        )
    number = normalize_card_number(row.card_number)
    card_id = f"{set_id}-{number}"
    if card_id not in known_card_ids:
        return MatchResult(
            row=row,
            card_id=None,
            rejection_reason=f"card_id introuvable dans le catalogue : {card_id}",
        )
    return MatchResult(row=row, card_id=card_id, rejection_reason=None)
