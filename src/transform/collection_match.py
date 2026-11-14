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
import re
from dataclasses import dataclass, replace


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


def _number(raw: str | None) -> float | None:
    """« 1,670.22 » -> 1670.22 : l'export écrit les milliers avec une virgule."""
    raw = (raw or "").replace(",", "").strip()
    return float(raw) if raw else None


COMMON_RARITIES = ("Common", "Uncommon", "")


def read_collection_csv(
    path: str, *, portfolios: set[str] | None = frozenset({"Main"}), include_commons: bool = False
) -> list[CollectionRow]:
    """Lit un export CSV de collection et applique les filtres d'import :
    Category=Pokemon, portefeuilles `portfolios` (None = tous, y compris les
    master sets), et par défaut sans les Common / Uncommon / rareté vide.
    Ne fait AUCUN rapprochement avec le catalogue (voir match_row ci-dessous,
    appelée séparément par l'appelant).

    Une même carte peut apparaître dans plusieurs portefeuilles (« Main » et
    un master set) : ce sont des exemplaires distincts, regroupés ici sur la
    clé naturelle (set, numéro, nom, variante, état) en additionnant les
    quantités, avec un coût moyen pondéré par la quantité."""
    merged: dict[tuple, CollectionRow] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        market_price_column = _find_market_price_column(reader.fieldnames or [])
        for record in reader:
            if record["Category"] != "Pokemon":
                continue
            if portfolios is not None and record["Portfolio Name"] not in portfolios:
                continue
            if not include_commons and record["Rarity"] in COMMON_RARITIES:
                continue
            quantity = int(_number(record["Quantity"]) or 0)
            if quantity <= 0:
                continue
            market_price_raw = record.get(market_price_column, "") if market_price_column else ""
            row = CollectionRow(
                set_name=record["Set"],
                card_number=record["Card Number"],
                product_name=record["Product Name"],
                variance=record["Variance"] or "",
                grade=record["Grade"] or "",
                rarity=record["Rarity"],
                quantity=quantity,
                average_cost_paid=_number(record["Average Cost Paid"]),
                market_price_at_export=_number(market_price_raw),
            )
            key = (row.set_name, row.card_number, row.product_name, row.variance, row.grade)
            prev = merged.get(key)
            merged[key] = row if prev is None else _merge(prev, row)
    return list(merged.values())


def _merge(a: CollectionRow, b: CollectionRow) -> CollectionRow:
    quantity = a.quantity + b.quantity
    costs = [(r.average_cost_paid, r.quantity) for r in (a, b) if r.average_cost_paid]
    cost = (
        sum(c * q for c, q in costs) / sum(q for _, q in costs)
        if costs
        else a.average_cost_paid if a.average_cost_paid is not None else b.average_cost_paid
    )
    return replace(a, quantity=quantity, average_cost_paid=cost)


# Noms de set de l'export qui diffèrent du catalogue (prod.dim_card.set_name).
SET_ALIASES = {
    "SV: 151": "151",
    "Scarlet & Violet Base Set": "Scarlet & Violet",
    "Scarlet & Violet Promo": "Scarlet & Violet Black Star Promos",
    "Sword & Shield Promo": "SWSH Black Star Promos",
    "Sun & Moon Promo": "SM Black Star Promos",
    "Crown Zenith: Galarian Gallery": "Crown Zenith Galarian Gallery",
}


def _number_candidates(raw_number: str) -> list[str]:
    """Numéros à essayer dans le card_id : la forme normalisée, puis les
    variantes sans zéros internes (« TG05 » -> « TG5 », « SVP 001 » -> « 1 »)."""
    first = normalize_card_number(raw_number)
    out = [first]
    m = re.match(r"^([A-Za-z]*)\s*0*(\d+)([a-z]?)$", raw_number.split("/")[0].strip())
    if m:
        for cand in (m.group(1) + m.group(2) + m.group(3), m.group(2) + m.group(3)):
            if cand and cand not in out:
                out.append(cand)
    return out


def _name_key(name: str) -> str:
    """« Iono 237/091 (Full Art) » -> « iono » : nom réduit pour comparer."""
    name = re.sub(r"\s*\(.*?\)", "", name)
    name = re.sub(r"\s+[\w-]*\d+/\d+$", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def match_row(
    row: CollectionRow,
    set_name_to_id: dict[str, str],
    known_card_ids: set[str],
    names_by_set: dict[str, dict[str, list[str]]] | None = None,
) -> MatchResult:
    """Tente de relier une CollectionRow à une carte connue de
    prod.dim_card. Pas de rapprochement approximatif : le set doit être connu
    (directement ou par SET_ALIASES) et la carte trouvée par son numéro ; à
    défaut, par son nom exact dans ce set s'il n'y en a qu'une
    (`names_by_set` : set_id -> nom réduit -> card_ids). Sinon, quarantaine
    avec une raison explicite plutôt qu'un rapprochement douteux."""
    set_name = SET_ALIASES.get(row.set_name, row.set_name)
    set_id = set_name_to_id.get(set_name)
    if set_id is None:
        return MatchResult(
            row=row, card_id=None, rejection_reason=f"set inconnu : {row.set_name!r}"
        )
    candidates = _number_candidates(row.card_number) if row.card_number else []
    for number in candidates:
        card_id = f"{set_id}-{number}"
        if card_id in known_card_ids:
            return MatchResult(row=row, card_id=card_id, rejection_reason=None)
    if names_by_set is not None:
        same_name = names_by_set.get(set_id, {}).get(_name_key(row.product_name), [])
        if len(same_name) == 1:
            return MatchResult(row=row, card_id=same_name[0], rejection_reason=None)
    tried = f"{set_id}-{candidates[0]}" if candidates else f"{set_id} (sans numéro)"
    return MatchResult(
        row=row,
        card_id=None,
        rejection_reason=f"card_id introuvable dans le catalogue : {tried}",
    )
