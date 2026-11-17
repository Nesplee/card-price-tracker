# Point d'entrée exécutable pour importer un export CSV de la collection
# personnelle de l'utilisateur. Réutilisable : peut être relancé à chaque
# nouvel export sans dupliquer les données (voir l'idempotence de
# src/load/collection_loader.py). Pas de DAG Airflow pour cet import : aucune
# façon d'automatiser la réception d'un export personnel depuis une app
# tierce, ce sera toujours un geste manuel de l'utilisateur qui fournit le
# fichier -- voir docs/superpowers/specs/2026-08-09-personal-collection-import-design.md.
#
# Usage :
#   python -m scripts.import_collection export.csv --dry-run      # rapport seul
#   python -m scripts.import_collection export.csv                # Main, communes comprises
#
# Défauts (2026-11-17) identiques à l'import depuis le board
# (src/api/collection_import.py) : portefeuille « Main » seul, communes
# comprises. C'est ce que contient la collection en place (0 différence
# mesurée sur l'export réel) ; les portefeuilles « MS - <set> » reprennent des
# cartes déjà dans Main et les compteraient en double (--all-portfolios).
# L'écriture passe par collection.apply_import (migration 017), comme le board.
#
# Les sets et les noms de cartes viennent de prod.dim_card (le catalogue déjà
# chargé), plus de l'API pokemontcg.io : le rapprochement porte ainsi sur
# exactement les cartes dont on suit les prix.
from __future__ import annotations

import argparse
import logging
from collections import Counter

import psycopg

from src.common.config import load_db_config
from src.load.collection_loader import apply_import
from src.transform.collection_match import build_catalog, match_row, read_collection_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def _catalog(conn) -> tuple[dict[str, str], set[str], dict[str, dict[str, list[str]]]]:
    with conn.cursor() as cur:
        cur.execute("SELECT card_id, name, set_id, set_name FROM prod.dim_card")
        return build_catalog(cur.fetchall())


def main() -> None:
    parser = argparse.ArgumentParser(description="Importe un export CSV de la collection.")
    parser.add_argument("csv_path")
    parser.add_argument(
        "--all-portfolios",
        action="store_true",
        help="tous les portefeuilles (master sets compris), pas seulement « Main »",
    )
    parser.add_argument(
        "--include-commons",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="garder les Common / Uncommon (par défaut ; --no-include-commons pour les exclure)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="rapport seulement, aucune écriture en base"
    )
    args = parser.parse_args()

    rows = read_collection_csv(
        args.csv_path,
        portfolios=None if args.all_portfolios else {"Main"},
        include_commons=args.include_commons,
    )
    logger.info("%d lignes candidates lues depuis %s", len(rows), args.csv_path)

    conn = psycopg.connect(load_db_config().dsn)
    try:
        set_name_to_id, known_card_ids, names_by_set = _catalog(conn)
        logger.info("%d cartes connues dans prod.dim_card", len(known_card_ids))
        matches = [match_row(r, set_name_to_id, known_card_ids, names_by_set) for r in rows]
        matched = [(0, m) for m in matches if m.card_id is not None]
        unmatched = [(0, m) for m in matches if m.card_id is None]
        if not args.dry_run:
            # Même chemin que le board : une transaction, quarantaine tenue à
            # jour (une ligne reliée cette fois en sort), aucun retrait.
            apply_import(conn, matches)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    scope = "tous les portefeuilles" if args.all_portfolios else "portefeuille Main"
    scope += ", toutes raretés" if args.include_commons else ", hors Common/Uncommon"
    copies = sum(m.row.quantity for _, m in matched)
    print()
    print(f"=== Rapport d'import de la collection{' (à blanc)' if args.dry_run else ''} ===")
    print(f"Lignes candidates (Pokemon, {scope}) : {len(rows)}")
    print(
        f"Reliées au catalogue : {len(matched)} lignes, {copies} exemplaires, "
        f"{len({m.card_id for _, m in matched})} cartes distinctes"
    )
    print(f"En quarantaine (non reliées) : {len(unmatched)}")
    if unmatched:
        print()
        print("Non reliées, par set :")
        for set_name, count in Counter(m.row.set_name for _, m in unmatched).most_common():
            print(f"  {count:4d}  {set_name!r}")


if __name__ == "__main__":
    main()
