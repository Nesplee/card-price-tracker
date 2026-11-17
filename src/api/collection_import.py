# Import d'un export de collection depuis le board (2026-11-17) : même
# traitement que scripts/import_collection.py (lecture, filtres, rapprochement
# avec le catalogue), en deux temps :
#   1. /preview : rien n'est écrit ; renvoie ce qui serait ajouté, modifié,
#      inchangé, mis en quarantaine, et les cartes en base absentes de l'export ;
#   2. /apply : l'utilisateur a choisi, ligne par ligne, quelles cartes absentes
#      retirer (par défaut : aucune) ; tout est appliqué en une transaction par
#      collection.apply_import (migration 017, SECURITY DEFINER), la seule
#      écriture possible du dashboard dans la collection.
# Idempotent : l'export complet est renvoyé à chaque fois, les lignes connues
# sont mises à jour, les nouvelles ajoutées ; rejouer le même fichier ne change
# rien (tests/test_collection_import.py).
from __future__ import annotations

import io
import json
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.api.board import _json
from src.api.db import get_api_connection
from src.load.collection_loader import aggregate_owned
from src.transform.collection_match import build_catalog, match_row, read_collection_lines

router = APIRouter(prefix="/api/collection-import")

# Un export complet fait ~300 Ko pour ~1 200 lignes : 5 Mo laisse de la marge.
MAX_CSV_CHARS = 5_000_000
REQUIRED_COLUMNS = (
    "Category",
    "Portfolio Name",
    "Set",
    "Card Number",
    "Product Name",
    "Variance",
    "Grade",
    "Rarity",
    "Quantity",
    "Average Cost Paid",
)


class OwnedKey(BaseModel):
    card_id: str
    variance: str = ""
    grade: str = ""


class ImportIn(BaseModel):
    csv: str = Field(min_length=1, max_length=MAX_CSV_CHARS)
    # Mêmes options que le script ; par défaut celles de l'import en place
    # (vérifié le 2026-11-17 sur l'export réel : portefeuille « Main » seul,
    # communes comprises = 0 ajout, 0 modification). Les portefeuilles
    # « MS - <set> » reprennent des cartes déjà dans Main : les additionner
    # compterait 781 cartes en double.
    all_portfolios: bool = False
    include_commons: bool = True


class ApplyIn(ImportIn):
    remove: list[OwnedKey] = Field(default_factory=list, max_length=5000)


def _matches(conn, body: ImportIn):
    text = body.csv.lstrip("﻿")
    header = text.split("\n", 1)[0]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise HTTPException(
            status_code=422, detail="colonnes absentes de l'export : " + ", ".join(missing)
        )
    try:
        rows = read_collection_lines(
            io.StringIO(text, newline=""),
            portfolios=None if body.all_portfolios else {"Main"},
            include_commons=body.include_commons,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"export illisible : {e}") from None
    catalog = build_catalog(
        [
            (r["card_id"], r["name"], r["set_id"], r["set_name"])
            for r in conn.execute(
                "SELECT card_id, name, set_id, set_name FROM prod.dim_card"
            ).fetchall()
        ]
    )
    return [match_row(r, *catalog) for r in rows]


def _key(r: dict) -> tuple[str, str, str]:
    return (r["card_id"], r["variance"] or "", r["grade"] or "")


def build_preview(conn, body: ImportIn) -> dict:
    matches = _matches(conn, body)
    owned = aggregate_owned([(0, m) for m in matches if m.card_id is not None])
    unmatched = [m for m in matches if m.card_id is None]
    current = {_key(r): r for r in conn.execute("""
            SELECT o.card_id, o.variance, o.grade, o.quantity, o.average_cost_paid,
                   c.name, c.set_name, c.rarity, c.image_small, c.image_alt
            FROM prod.dim_owned_card o JOIN prod.dim_card c ON c.card_id = o.card_id
            """).fetchall()}
    names = {
        r["card_id"]: r
        for r in conn.execute(
            "SELECT card_id, name, set_name, rarity, image_small, image_alt "
            "FROM prod.dim_card WHERE card_id = ANY(%(ids)s)",
            {"ids": list({o["card_id"] for o in owned})},
        ).fetchall()
    }
    added, changed, same = [], [], 0
    for o in owned:
        k = _key(o)
        info = {
            kk: names[o["card_id"]][kk]
            for kk in ("name", "set_name", "rarity", "image_small", "image_alt")
        }
        old = current.get(k)
        cost = round(o["average_cost_paid"], 2) if o["average_cost_paid"] is not None else None
        if old is None:
            added.append({**o, **info, "average_cost_paid": cost})
            continue
        old_cost = float(old["average_cost_paid"]) if old["average_cost_paid"] is not None else None
        if old["quantity"] != o["quantity"] or old_cost != cost:
            changed.append(
                {
                    **o,
                    **info,
                    "average_cost_paid": cost,
                    "old_quantity": old["quantity"],
                    "old_average_cost_paid": old_cost,
                }
            )
        else:
            same += 1
    seen = {_key(o) for o in owned}
    missing = [
        {k: (float(v) if k == "average_cost_paid" and v is not None else v) for k, v in r.items()}
        for k2, r in sorted(current.items(), key=lambda kv: (kv[1]["set_name"], kv[1]["name"]))
        if k2 not in seen
    ]
    return {
        "rows": len(matches),
        "copies": sum(o["quantity"] for o in owned),
        "added": added,
        "changed": changed,
        "unchanged": same,
        "missing": missing,
        "unmatched": len(unmatched),
        "unmatched_by_set": Counter(m.row.set_name for m in unmatched).most_common(20),
    }


@router.post("/preview")
def preview(body: ImportIn, conn=Depends(get_api_connection)) -> Response:
    return _json(build_preview(conn, body))


def apply_import(conn, body: ApplyIn) -> dict:
    matches = _matches(conn, body)
    owned = aggregate_owned([(0, m) for m in matches if m.card_id is not None])
    if not owned:
        raise HTTPException(status_code=422, detail="aucune carte reliée au catalogue")
    payload = {
        "rows": [
            {
                "set_name": m.row.set_name,
                "card_number": m.row.card_number,
                "product_name": m.row.product_name,
                "variance": m.row.variance,
                "grade": m.row.grade,
                "rarity": m.row.rarity,
                "quantity": m.row.quantity,
                "average_cost_paid": m.row.average_cost_paid,
                "market_price_at_export": m.row.market_price_at_export,
                "card_id": m.card_id,
                "rejection_reason": m.rejection_reason,
            }
            for m in matches
        ],
        "owned": owned,
        "remove": [r.model_dump() for r in body.remove],
    }
    result = conn.execute(
        "SELECT collection.apply_import(%(p)s::jsonb) AS r", {"p": json.dumps(payload)}
    ).fetchone()["r"]
    conn.commit()
    return result


@router.post("/apply")
def apply(body: ApplyIn, conn=Depends(get_api_connection)) -> Response:
    return _json(apply_import(conn, body))
