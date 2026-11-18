# Mesure ponctuelle : que vaut CardTrader comme source du prix des cartes
# ANGLAISES en Europe ? (étude ~/mere/rapports/cardmarket-prix-2026-11-17.md)
#
# Pour chaque set demandé (identifiants pokemontcg.io, ex. sv2 sv4pt5) :
#   1. trouve l'extension CardTrader (nom normalisé) et ses blueprints ;
#   2. relie les cartes de prod.dim_card par numéro de collection ;
#   3. lit les offres du set deux fois : sans filtre (toutes langues) et
#      language=en ; vérifie que le filtre est respecté ;
#   4. écrit cards.csv (une ligne par carte) et summary.md (couverture,
#      propriétés vues, états vus), plus les réponses brutes en JSON pour
#      vérifier le format à la main.
#
# LECTURE SEULE partout : GET CardTrader (voir src/extract/cardtrader_prices),
# SELECT sur la base de prod via psql dans le conteneur db. Aucune écriture
# en base, aucun conteneur touché. Le jeton (CARDTRADER_API_TOKEN) est lu dans
# l'environnement ou dans le .env du dépôt principal, jamais affiché.
#
# Usage (depuis le worktree) :
#   ~/card-price-tracker/.venv/bin/python -m scripts.measure_cardtrader sv2 sv4pt5
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from src.extract.cardtrader_prices import (
    DEFAULT_MIN_CONDITION,
    CardTraderClient,
    map_blueprints_to_cards,
    match_expansion,
    merge_marketplace,
    offer_language,
    pokemon_game_id,
    summarize_blueprint,
)
from src.extract.tcgdex_map import split_card_id

MAIN_REPO = Path.home() / "card-price-tracker"
_SET_ID_RE = re.compile(r"^[a-z0-9.\-]+$")

# Dernier prix TCGplayer (USD) connu et possession : indicatif seulement,
# aucune conversion de devise (deux marchés différents).
_CARDS_SQL = """
SELECT c.card_id, c.set_id, c.set_name,
       EXISTS (SELECT 1 FROM prod.dim_owned_card o WHERE o.card_id = c.card_id) AS owned,
       (SELECT f.average_sell_price FROM prod.fact_price_history f
          JOIN prod.dim_platform p USING (platform_id)
         WHERE f.card_id = c.card_id AND p.platform_name = 'tcgplayer'
         ORDER BY f.date_id DESC LIMIT 1) AS tcgplayer_usd
  FROM prod.dim_card c
 WHERE c.set_id IN ({ids})
 ORDER BY c.card_id
"""


def load_token(env_file: Path) -> str:
    token = os.environ.get("CARDTRADER_API_TOKEN") or (
        dotenv_values(env_file).get("CARDTRADER_API_TOKEN") if env_file.exists() else None
    )
    if not token:
        sys.exit(f"CARDTRADER_API_TOKEN absent de l'environnement et de {env_file}")
    return token


def load_cards(set_ids: list[str]) -> list[dict]:
    for set_id in set_ids:
        if not _SET_ID_RE.match(set_id):
            sys.exit(f"identifiant de set invalide : {set_id!r}")
    sql = _CARDS_SQL.format(ids=", ".join(f"'{s}'" for s in set_ids))
    out = subprocess.run(
        [
            "docker", "compose", "-f", str(MAIN_REPO / "docker-compose.prod.yml"),
            "exec", "-T", "db", "sh", "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|" '
            "-v ON_ERROR_STOP=1 -c \"BEGIN READ ONLY\" -f - -c COMMIT",
        ],
        input=sql, capture_output=True, text=True, check=True, cwd=MAIN_REPO,
    ).stdout  # fmt: skip
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue  # lignes BEGIN / COMMIT
        card_id, set_id, set_name, owned, usd = parts
        rows.append(
            {
                "card_id": card_id,
                "set_id": set_id,
                "set_name": set_name,
                "owned": owned == "t",
                "tcgplayer_usd": usd or "",
            }
        )
    return rows


def dump(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sets", nargs="+", help="set_id pokemontcg.io (ex. sv2 sv4pt5)")
    parser.add_argument("--min-condition", default=DEFAULT_MIN_CONDITION)
    parser.add_argument(
        "--currency",
        default="EUR",
        help="devise du compte CardTrader (les prix arrivent dans cette devise)",
    )
    parser.add_argument("--env-file", type=Path, default=MAIN_REPO / ".env")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or Path.home() / ".local/state/cardtrader-measure" / stamp
    (out / "raw").mkdir(parents=True, exist_ok=True)

    client = CardTraderClient(load_token(args.env_file))
    client.info()  # vérifie le jeton (401 sinon) ; contenu non affiché ni gardé
    print("jeton accepté")

    games = client.games()
    dump(out / "raw/games.json", games)
    game_id = pokemon_game_id(games)
    if game_id is None:
        sys.exit("jeu Pokémon introuvable dans /games (voir raw/games.json)")
    categories = client.categories(game_id)
    dump(out / "raw/categories.json", categories)
    cat_items = categories.get("array", []) if isinstance(categories, dict) else categories
    singles = {c["id"] for c in cat_items if "single" in str(c.get("name", "")).lower()} or None
    expansions = client.expansions()
    pokemon_expansions = [e for e in expansions if e.get("game_id") == game_id]
    dump(out / "raw/expansions_pokemon.json", pokemon_expansions)

    cards = load_cards(args.sets)
    property_keys: Counter[str] = Counter()
    language_values: Counter[str] = Counter()
    conditions: Counter[str] = Counter()
    filter_leaks = 0
    csv_rows: list[dict] = []
    summary_lines = [
        f"# Mesure CardTrader ({stamp})",
        "",
        f"Seuil d'état : {args.min_condition} ou mieux. Prix en {args.currency}, port non compris.",
        "",
    ]

    for set_id in args.sets:
        set_cards = [c for c in cards if c["set_id"] == set_id]
        if not set_cards:
            summary_lines.append(f"- **{set_id}** : aucune carte dans prod.dim_card")
            continue
        set_name = set_cards[0]["set_name"]
        expansion = match_expansion(set_name, expansions, game_id, set_id)
        if expansion is None:
            summary_lines.append(f"- **{set_id}** ({set_name}) : extension CardTrader introuvable")
            continue
        blueprints = client.blueprints(expansion["id"])
        dump(out / f"raw/blueprints_{set_id}.json", blueprints)
        numbered = [(c["card_id"], split_card_id(c["card_id"], set_id) or "") for c in set_cards]
        mapped, unmatched = map_blueprints_to_cards(blueprints, numbered, singles)

        all_lang = client.marketplace(expansion_id=expansion["id"])
        dump(out / f"raw/marketplace_{set_id}_all.json", all_lang)
        english = client.marketplace(expansion_id=expansion["id"], language="en")
        dump(out / f"raw/marketplace_{set_id}_en.json", english)
        for offers in english.values():
            for o in offers:
                if offer_language(o.get("properties_hash") or {}) not in ("en", None):
                    filter_leaks += 1
        merged = merge_marketplace(all_lang, english)
        for offers in merged.values():
            for o in offers:
                props = o.get("properties_hash") or {}
                property_keys.update(props.keys())
                language_values[str(offer_language(props))] += 1
                conditions[str(props.get("condition"))] += 1

        covered = covered_owned = owned_total = 0
        for card in set_cards:
            blueprint_id = mapped.get(card["card_id"])
            summary = (
                summarize_blueprint(
                    blueprint_id, merged.get(blueprint_id, []), args.min_condition, args.currency
                )
                if blueprint_id
                else None
            )
            best = summary.en_best if summary else None
            owned_total += card["owned"]
            if best:
                covered += 1
                covered_owned += card["owned"]
            csv_rows.append(
                {
                    "card_id": card["card_id"],
                    "owned": card["owned"],
                    "blueprint_id": blueprint_id or "",
                    "en_price": best.price if best else "",
                    "currency": args.currency,
                    "en_condition": best.condition if best else "",
                    "en_finish": best.finish if best else "",
                    "en_seller_country": best.seller_country if best else "",
                    "cheapest_by_language": json.dumps(
                        {k: v.price for k, v in summary.cheapest_by_language.items()}
                        if summary
                        else {}
                    ),
                    "offers_seen": summary.offers_seen if summary else 0,
                    "truncated": summary.truncated if summary else "",
                    "tcgplayer_usd_indicatif": card["tcgplayer_usd"],
                }
            )
        summary_lines += [
            f"- **{set_id}** ({set_name}) → extension CardTrader {expansion['id']} "
            f"« {expansion.get('name')} » ({expansion.get('code')}) :",
            f"  - cartes reliées à un blueprint : {len(mapped)} / {len(set_cards)} "
            f"(non reliées : {len(unmatched)})",
            f"  - avec une offre EN ≥ {args.min_condition} : {covered} / {len(set_cards)} ; "
            f"cartes possédées : {covered_owned} / {owned_total}",
            f"  - blueprints avec offres : toutes langues {len(all_lang)}, "
            f"filtre en {len(english)}",
        ]

    with (out / "cards.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(csv_rows[0]) if csv_rows else ["card_id"])
        writer.writeheader()
        writer.writerows(csv_rows)

    summary_lines += [
        "",
        f"Offres hors anglais renvoyées par le filtre language=en : {filter_leaks}",
        "",
        "Propriétés vues (properties_hash) : "
        + ", ".join(f"{k} ×{n}" for k, n in property_keys.most_common()),
        "",
        "Langues vues : " + ", ".join(f"{k} ×{n}" for k, n in language_values.most_common()),
        "",
        "États vus : " + ", ".join(f"{k} ×{n}" for k, n in conditions.most_common()),
    ]
    (out / "summary.md").write_text("\n".join(summary_lines) + "\n")
    print("\n".join(summary_lines))
    print(f"\nRésultats : {out}")


if __name__ == "__main__":
    main()
