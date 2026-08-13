# Point d'entrée exécutable pour importer un export CSV de la collection
# personnelle de l'utilisateur. Réutilisable : peut être relancé à chaque
# nouvel export sans dupliquer les données (voir l'idempotence de
# src/load/collection_loader.py). Pas de DAG Airflow pour cet import : aucune
# façon d'automatiser la réception d'un export personnel depuis une app
# tierce, ce sera toujours un geste manuel de l'utilisateur qui fournit le
# fichier -- voir docs/superpowers/specs/2026-08-09-personal-collection-import-design.md.
#
# Usage : python -m scripts.import_collection /chemin/vers/export.csv
from __future__ import annotations

import logging
import sys
from collections import Counter

import psycopg

from src.common.config import load_db_config, load_pokemontcg_config
from src.extract.pokemontcg_client import PokemonTcgClient
from src.load.collection_loader import load_match_quarantine, load_owned_cards, load_raw_import
from src.transform.collection_match import match_row, read_collection_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage : python -m scripts.import_collection <chemin-vers-export.csv>")
        sys.exit(1)
    csv_path = sys.argv[1]

    # --- Étape 1 : lire le CSV (déjà filtré par read_collection_csv) ---
    rows = read_collection_csv(csv_path)
    logger.info("%d lignes candidates lues depuis %s", len(rows), csv_path)

    # --- Étape 2 : récupérer la correspondance nom de set -> set_id ---
    client = PokemonTcgClient(load_pokemontcg_config())
    sets = client.fetch_sets()
    set_name_to_id = {s["name"]: s["id"] for s in sets}
    logger.info("%d sets connus de pokemontcg.io", len(set_name_to_id))

    # --- Étape 3 : rapprochement + chargement en base ---
    # Connexion ouverte manuellement (pas get_connection()) : plusieurs
    # opérations distinctes (raw_import, puis owned_card OU quarantine)
    # doivent toutes réussir ou toutes échouer ensemble -- un seul commit à
    # la fin, pas de commit intermédiaire par ligne (contrairement à
    # l'extraction de prix, qui a besoin d'un checkpoint par page ; ici le
    # volume est petit, ~1000 lignes max, une seule transaction est
    # largement assez rapide).
    conn = psycopg.connect(load_db_config().dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT card_id FROM prod.dim_card;")
            known_card_ids = {r[0] for r in cur.fetchall()}
        logger.info("%d cartes connues dans prod.dim_card", len(known_card_ids))

        raw_ids = load_raw_import(conn, rows)
        matches = [match_row(row, set_name_to_id, known_card_ids) for row in rows]

        matched = [(rid, m) for rid, m in zip(raw_ids, matches) if m.card_id is not None]
        unmatched = [(rid, m) for rid, m in zip(raw_ids, matches) if m.card_id is None]

        load_owned_cards(conn, matched)
        load_match_quarantine(conn, unmatched)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # --- Étape 4 : rapport (décision utilisateur explicite -- voir le spec) ---
    print()
    print("=== Rapport d'import de la collection ===")
    print(f"Lignes candidates (Pokemon, Main, hors Common/Uncommon) : {len(rows)}")
    print(f"Reliées au catalogue (prod.dim_owned_card)             : {len(matched)}")
    print(f"En quarantaine (non reliées)                            : {len(unmatched)}")
    if unmatched:
        print()
        print("Détail des raisons de quarantaine :")
        reasons = Counter(m.rejection_reason.split(" : ")[0] for _, m in unmatched)
        for reason, count in reasons.most_common():
            print(f"  {count:4d}  {reason}")
        print()
        print("Sets non reconnus (nécessitent une correspondance manuelle) :")
        unknown_sets = Counter(
            m.row.set_name for _, m in unmatched if "set inconnu" in m.rejection_reason
        )
        for set_name, count in unknown_sets.most_common():
            print(f"  {count:4d}  {set_name!r}")


if __name__ == "__main__":
    main()
