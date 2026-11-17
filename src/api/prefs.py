# Réglages de l'utilisateur saisis dans le board (2026-11-17, migration 017) :
# par carte (invendable, ne plus suivre, prix minimum de vente), par set
# (masterset) et listes de cartes recherchées (prix cible, note). Le seul
# endroit où l'API écrit : dashboard_reader n'a de droits d'écriture que sur
# le schéma prefs. Les règles qui en découlent (prefs.v_card_rules) filtrent
# les signaux au calcul (src/signals/opportunities.py) et à la lecture
# (src/api/board.py, src/api/queries.py).
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from psycopg import errors
from pydantic import BaseModel, Field

from src.api.board import _WINDOW_SLACK, DEFAULT_PREFS, WINDOWS, _clean, _json, _past, card_prefs
from src.api.db import get_api_connection
from src.api.queries import RULES_FILTER

router = APIRouter(prefix="/api/prefs")

_PLATFORM = "tcgplayer"
# Mini-courbe des cartes recherchées : 30 derniers jours.
SPARK_DAYS = 30


class CardPrefIn(BaseModel):
    """Champs absents : inchangés. min_sell_price à null : seuil retiré."""

    unsellable: bool | None = None
    unfollowed: bool | None = None
    min_sell_price: float | None = Field(default=None, gt=0, lt=100000)


class SetPrefIn(BaseModel):
    masterset: bool


class WishlistIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class WishlistCardIn(BaseModel):
    """Champs absents : inchangés (ou vides pour une carte ajoutée).
    target_price à null : cible retirée."""

    target_price: float | None = Field(default=None, gt=0, lt=100000)
    note: str | None = Field(default=None, max_length=200)


def _card_exists(conn, card_id: str) -> None:
    if not conn.execute(
        "SELECT 1 FROM prod.dim_card WHERE card_id = %(id)s", {"id": card_id}
    ).fetchall():
        raise HTTPException(status_code=404, detail="carte inconnue")


@router.put("/cards/{card_id}")
def put_card_pref(card_id: str, body: CardPrefIn, conn=Depends(get_api_connection)) -> Response:
    _card_exists(conn, card_id)
    # Seuls les champs envoyés sont écrits (liste fixe : pas de SQL dynamique
    # sur des noms venus du client).
    fields = [
        f for f in ("unsellable", "unfollowed", "min_sell_price") if f in body.model_fields_set
    ]
    if any(getattr(body, f) is None for f in fields if f != "min_sell_price"):
        raise HTTPException(status_code=422, detail="booléen attendu")
    values = {f: getattr(body, f) for f in fields}
    sets = ", ".join(f"{f} = EXCLUDED.{f}" for f in fields)
    conn.execute(
        f"""
        INSERT INTO prefs.card_setting (card_id, unsellable, unfollowed, min_sell_price)
        VALUES (%(card_id)s, coalesce(%(unsellable)s, false), coalesce(%(unfollowed)s, false),
                %(min_sell_price)s)
        ON CONFLICT (card_id) DO UPDATE SET {sets + ", " if sets else ""}updated_at = now()
        """,
        {
            "card_id": card_id,
            "unsellable": values.get("unsellable"),
            "unfollowed": values.get("unfollowed"),
            "min_sell_price": values.get("min_sell_price"),
        },
    )
    conn.commit()
    return _json({"card_id": card_id, "prefs": card_prefs(conn, [card_id]).get(card_id)})


@router.put("/sets/{set_id}")
def put_set_pref(set_id: str, body: SetPrefIn, conn=Depends(get_api_connection)) -> Response:
    if not conn.execute(
        "SELECT 1 FROM prod.dim_card WHERE set_id = %(id)s LIMIT 1", {"id": set_id}
    ).fetchall():
        raise HTTPException(status_code=404, detail="set inconnu")
    conn.execute(
        """
        INSERT INTO prefs.set_setting (set_id, masterset) VALUES (%(id)s, %(m)s)
        ON CONFLICT (set_id) DO UPDATE SET masterset = EXCLUDED.masterset, updated_at = now()
        """,
        {"id": set_id, "m": body.masterset},
    )
    conn.commit()
    return _json({"set_id": set_id, "masterset": body.masterset})


@router.post("/wishlists", status_code=201)
def create_wishlist(body: WishlistIn, conn=Depends(get_api_connection)) -> Response:
    try:
        row = conn.execute(
            "INSERT INTO prefs.wishlist (name) VALUES (%(n)s) RETURNING list_id, name",
            {"n": body.name.strip()},
        ).fetchone()
    except (errors.UniqueViolation, errors.CheckViolation):
        conn.rollback()
        raise HTTPException(status_code=409, detail="nom déjà pris ou vide") from None
    conn.commit()
    return _json(dict(row))


def _list_exists(conn, list_id: int) -> None:
    if not conn.execute(
        "SELECT 1 FROM prefs.wishlist WHERE list_id = %(id)s", {"id": list_id}
    ).fetchall():
        raise HTTPException(status_code=404, detail="liste inconnue")


@router.put("/wishlists/{list_id}/cards/{card_id}")
def put_wishlist_card(
    list_id: int, card_id: str, body: WishlistCardIn, conn=Depends(get_api_connection)
) -> Response:
    _list_exists(conn, list_id)
    _card_exists(conn, card_id)
    fields = [f for f in ("target_price", "note") if f in body.model_fields_set]
    sets = ", ".join(f"{f} = EXCLUDED.{f}" for f in fields)
    note = body.note.strip() or None if body.note is not None else None
    conn.execute(
        f"""
        INSERT INTO prefs.wishlist_card (list_id, card_id, target_price, note)
        VALUES (%(l)s, %(c)s, %(t)s, %(n)s)
        ON CONFLICT (list_id, card_id) DO UPDATE SET {sets + ", " if sets else ""}updated_at = now()
        """,
        {"l": list_id, "c": card_id, "t": body.target_price, "n": note},
    )
    # Rechercher une carte, c'est vouloir la suivre : « ne plus suivre » tombe.
    conn.execute(
        "UPDATE prefs.card_setting SET unfollowed = false, updated_at = now() "
        "WHERE card_id = %(c)s AND unfollowed",
        {"c": card_id},
    )
    conn.commit()
    return _json({"list_id": list_id, "card_id": card_id})


@router.delete("/wishlists/{list_id}/cards/{card_id}", status_code=204)
def delete_wishlist_card(list_id: int, card_id: str, conn=Depends(get_api_connection)) -> Response:
    conn.execute(
        "DELETE FROM prefs.wishlist_card WHERE list_id = %(l)s AND card_id = %(c)s",
        {"l": list_id, "c": card_id},
    )
    conn.commit()
    return Response(status_code=204)


def get_wishlists(conn) -> dict:
    """Listes de cartes recherchées avec, par carte : prix market et annonce
    la moins chère du jour, prix passés (1 à 90 j), mini-courbe sur 30 j,
    écart à la cible et signal d'achat du jour."""
    lists = [
        dict(r)
        for r in conn.execute(
            "SELECT list_id, name FROM prefs.wishlist ORDER BY list_id"
        ).fetchall()
    ]
    rows = conn.execute("""
        SELECT w.list_id, w.card_id, w.target_price, w.note, w.added_at::date AS added_on,
               c.name, c.set_id, c.set_name, c.series, c.rarity, c.image_small, c.image_alt,
               coalesce((SELECT sum(quantity) FROM prod.dim_owned_card o
                         WHERE o.card_id = w.card_id), 0)::int AS owned_quantity
        FROM prefs.wishlist_card w
        JOIN prod.dim_card c ON c.card_id = w.card_id
        ORDER BY w.list_id, w.added_at DESC
        """).fetchall()
    ids = list({r["card_id"] for r in rows})
    by_card: dict[str, list[tuple[date, float, float | None]]] = {}
    sig: dict[str, dict] = {}
    if ids:
        for r in conn.execute(
            """
            SELECT f.card_id, f.date_id, f.average_sell_price AS price, f.low_price AS low
            FROM prod.fact_price_history f
            WHERE f.card_id = ANY(%(ids)s)
              AND f.platform_id = (SELECT platform_id FROM prod.dim_platform
                                   WHERE platform_name = %(platform)s)
              AND f.average_sell_price IS NOT NULL
            ORDER BY f.card_id, f.date_id
            """,
            {"ids": ids, "platform": _PLATFORM},
        ).fetchall():
            by_card.setdefault(r["card_id"], []).append(
                (r["date_id"], float(r["price"]), float(r["low"]) if r["low"] is not None else None)
            )
        # Signal d'achat du dernier jour calculé, et nombre de jours avec un
        # signal sur les 14 derniers (mêmes règles que le reste du board).
        for r in conn.execute(
            """
            WITH d AS (SELECT max(date_id) AS d FROM prod.fact_opportunity)
            SELECT o.card_id,
                   count(*)::int AS days_14,
                   (array_agg(o.score ORDER BY o.date_id DESC))[1] AS score,
                   (array_agg(o.signals ORDER BY o.date_id DESC))[1] AS signals,
                   (array_agg(o.reason ORDER BY o.date_id DESC))[1] AS reason,
                   max(o.date_id) = max(d.d) AS today
            FROM prod.fact_opportunity o
            CROSS JOIN d
            LEFT JOIN prefs.v_card_rules r ON r.card_id = o.card_id
            WHERE o.card_id = ANY(%(ids)s) AND o.side = 'buy'
              AND o.date_id > d.d - 14
              AND """
            + RULES_FILTER
            + """
            GROUP BY o.card_id
            """,
            {"ids": ids},
        ).fetchall():
            sig[r["card_id"]] = _clean(r)
    prefs = card_prefs(conn, ids)
    as_of = max((h[-1][0] for h in by_card.values()), default=None)
    cards = []
    for r in rows:
        h = by_card.get(r["card_id"], [])
        cur = h[-1] if h and as_of and (as_of - h[-1][0]).days <= 3 else None
        past = {}
        spark = []
        if cur:
            hp = [(d, p) for d, p, _ in h]
            for w in WINDOWS:
                p = _past(hp, as_of - timedelta(days=w), _WINDOW_SLACK[w])
                past[w] = p[1] if p and p[1] > 0 else None
            spark = [[d, round(p, 2)] for d, p, _ in h if (as_of - d).days <= SPARK_DAYS]
        target = float(r["target_price"]) if r["target_price"] is not None else None
        price = cur[1] if cur else None
        cards.append(
            {
                **_clean({k: v for k, v in r.items() if k != "target_price"}),
                "target_price": target,
                "price": price,
                "low": cur[2] if cur else None,
                "past": past,
                "spark": spark,
                "to_target": (price / target - 1) if price and target else None,
                "signal": sig.get(r["card_id"]),
                "prefs": prefs.get(r["card_id"], DEFAULT_PREFS),
            }
        )
    return {"as_of": as_of, "lists": lists, "cards": cards}


@router.get("/wishlists")
def wishlists(conn=Depends(get_api_connection)) -> Response:
    return _json(get_wishlists(conn))
