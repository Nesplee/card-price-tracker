# Données du tableau de bord « board » (board/index.html) : deux blocs face à
# face, Collection (valeur et mouvements de prix des cartes possédées) et
# Signaux (achats / ventes portés par les news et la méta compétitive).
# Lecture seule, rôle dashboard_reader, prix TCGplayer (USD) uniquement.
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from psycopg import errors

from src.api import mentions
from src.api.db import get_api_connection
from src.api.queries import NEWS_WINDOW_DAYS

router = APIRouter(prefix="/api/board")

_PLATFORM = "tcgplayer"
# Fenêtres des mouvements, en jours calendaires. Le prix « passé » d'une carte
# est sa dernière observation à D - N ou avant, dans une marge de quelques jours
# (l'historique a des trous : 26/08, 16 au 25/10) : au-delà, pas de variation.
WINDOWS = (1, 7, 30, 90)
_WINDOW_SLACK = {1: 3, 7: 4, 30: 12, 90: 12}
# Raretés « recherchées » (AR, SAR, full art, gold...) : les seules montrées
# dans les mouvements du board, et les seules dont on renvoie la mini-courbe.
# Common / Uncommon / Rare, ex simples (Double Rare), Shiny Rare non full art,
# promos et ACE SPEC comptent dans la valeur mais pas dans les mouvements.
CHASE_RARITIES = (
    "Illustration Rare",
    "Special Illustration Rare",
    "Ultra Rare",
    "Hyper Rare",
    "Shiny Ultra Rare",
    "Mega Hyper Rare",
    "MEGA_ATTACK_RARE",
    "Black White Rare",
    "Trainer Gallery Rare Holo",
    "Rare Ultra",
    "Rare Secret",
    "Rare Rainbow",
    "Rare Shiny GX",
    "Amazing Rare",
    "Futuristic Rare",
)
# Signaux de contexte : déclenchés par une news ou par la méta (le reste vient
# des prix seuls, voir src/signals/opportunities.py).
CONTEXT_SIGNALS = ("actu_hausse", "actu_baisse", "meta_hausse", "meta_baisse")
# Buzz : espèces citées dans au moins BUZZ_MIN_WEEK posts sur 7 jours.
BUZZ_MIN_WEEK = 3
BUZZ_TOP = 20
# Une news compte si confiance x poids >= ce seuil (même règle que la sélection
# des Opportunités, src/api/queries.py).
NEWS_MIN_STRENGTH = 0.25


def _num(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def _clean(row: dict) -> dict:
    return {k: _num(v) for k, v in row.items()}


def _past(history: list[tuple[date, float]], target: date, slack: int) -> tuple[date, float] | None:
    """Dernier point à `target` ou avant, sans remonter de plus de `slack` jours."""
    best = None
    for d, v in history:
        if d > target:
            break
        best = (d, v)
    if best is None or (target - best[0]).days > slack:
        return None
    return best


def get_collection_board(conn) -> dict:
    params = {"platform": _PLATFORM}

    # Une ligne par carte possédée (variantes et gradées regroupées : le prix
    # TCGplayer est le même pour toutes), avec son historique de prix complet.
    cards = conn.execute("""
        SELECT o.card_id, c.name, c.set_name, c.series, c.rarity,
               sum(o.quantity)::int AS quantity,
               array_agg(DISTINCT nullif(o.variance, '')) AS variances,
               array_agg(DISTINCT nullif(o.grade, 'Ungraded')) AS grades
        FROM prod.dim_owned_card o
        JOIN prod.dim_card c ON c.card_id = o.card_id
        GROUP BY o.card_id, c.name, c.set_name, c.series, c.rarity
        """).fetchall()
    # LATERAL par carte : passe par l'index (card_id, date_id, platform_id) au
    # lieu d'un parcours complet de fact_price_history (2 s contre ~50 ms).
    prices = conn.execute(
        """
        SELECT o.card_id, f.date_id, f.average_sell_price AS price
        FROM (SELECT DISTINCT card_id FROM prod.dim_owned_card) o
        JOIN LATERAL (
            SELECT f.date_id, f.average_sell_price
            FROM prod.fact_price_history f
            WHERE f.card_id = o.card_id
              AND f.platform_id = (SELECT platform_id FROM prod.dim_platform
                                   WHERE platform_name = %(platform)s)
              AND f.average_sell_price IS NOT NULL
        ) f ON true
        ORDER BY o.card_id, f.date_id
        """,
        params,
    ).fetchall()
    by_card: dict[str, list[tuple[date, float]]] = {}
    for r in prices:
        by_card.setdefault(r["card_id"], []).append((r["date_id"], float(r["price"])))
    if not by_card:
        return {"as_of": None, "history": [], "windows": {}, "cards": [], "sparks": {}}

    # Signaux du dernier jour sur les cartes possédées (prix, news, méta).
    sig_rows = conn.execute("""
        SELECT o.card_id, o.side, o.score, o.signals, o.reason
        FROM prod.fact_opportunity o
        WHERE o.date_id = (SELECT max(date_id) FROM prod.fact_opportunity)
          AND o.card_id IN (SELECT card_id FROM prod.dim_owned_card)
        ORDER BY o.score DESC
        """).fetchall()
    sigs: dict[str, list[dict]] = {}
    for r in sig_rows:
        sigs.setdefault(r["card_id"], []).append(_clean(r))

    # Valeur de la collection actuelle, jour par jour (quantité x prix market).
    qty = {c["card_id"]: c["quantity"] for c in cards}
    per_day: dict[date, float] = {}
    for cid, h in by_card.items():
        for d, v in h:
            per_day[d] = per_day.get(d, 0.0) + qty[cid] * v
    hist = sorted(per_day.items())
    as_of, total = hist[-1]

    out_cards = []
    for c in cards:
        h = by_card.get(c["card_id"], [])
        cur = h[-1] if h and (as_of - h[-1][0]).days <= 3 else None
        row = {
            "card_id": c["card_id"],
            "name": c["name"],
            "set_name": c["set_name"],
            "series": c["series"],
            "rarity": c["rarity"],
            "quantity": c["quantity"],
            "variances": [v for v in c["variances"] if v],
            "grades": [g for g in c["grades"] if g],
            "price": cur[1] if cur else None,
            "past": {},
            "signals": sigs.get(c["card_id"], []),
        }
        if cur:
            for w in WINDOWS:
                p = _past(h, as_of - timedelta(days=w), _WINDOW_SLACK[w])
                row["past"][w] = p[1] if p and p[1] > 0 else None
        out_cards.append(row)

    windows = {}
    for w in WINDOWS:
        p = _past(hist, as_of - timedelta(days=w), _WINDOW_SLACK[w])
        windows[w] = {"date": p[0], "value": p[1], "delta": total - p[1]} if p else None

    # Mini-courbes : toutes les cartes recherchées qui ont un prix (~400).
    spark_ids = {
        c["card_id"] for c in out_cards if c["rarity"] in CHASE_RARITIES and c["price"] is not None
    }
    dates = [d for d, _ in hist]
    sparks = {}
    for cid in sorted(spark_ids):
        m = dict(by_card[cid])
        sparks[cid] = [round(m[d], 2) if d in m else None for d in dates]

    return {
        "as_of": as_of,
        "total_value": total,
        "history": [{"date": d, "value": round(v, 2)} for d, v in hist],
        "windows": windows,
        "cards": out_cards,
        "dates": dates,
        "sparks": sparks,
        "chase_rarities": list(CHASE_RARITIES),
    }


# Sets suivis : sets dont on possède au moins cette part des cartes. Détectés
# dans prod.dim_owned_card (pas de liste à tenir à jour) ; aujourd'hui Paldea
# Evolved et Paldean Fates, complets à 100 % ; le seuil est de 20 %.
MASTER_SET_MIN_SHARE = 0.2


def get_master_sets(conn) -> dict:
    """Évolution des sets complets ou presque : valeur du set entier (un
    exemplaire de chaque carte, au prix de sa variante principale), valeur de
    nos exemplaires, et cartes du set avec leur prix aux fenêtres 1 à 90 j.
    Les variantes (Reverse Holo...) n'ont pas de prix propre ici : une carte
    compte au prix de sa variante principale (voir src/transform/validate.py)."""
    sets = conn.execute(
        """
        WITH s AS (SELECT set_id, max(set_name) AS set_name, count(*) AS n
                   FROM prod.dim_card GROUP BY set_id),
        o AS (SELECT c.set_id, count(DISTINCT o.card_id) AS owned_cards, count(*) AS entries,
                     sum(o.quantity)::int AS copies
              FROM prod.dim_owned_card o JOIN prod.dim_card c ON c.card_id = o.card_id
              GROUP BY c.set_id)
        SELECT s.set_id, s.set_name, s.n::int AS n_cards, o.owned_cards::int, o.entries::int,
               o.copies
        FROM s JOIN o USING (set_id)
        WHERE o.owned_cards >= %(share)s * s.n
        ORDER BY o.owned_cards::float / s.n DESC, s.set_name
        """,
        {"share": MASTER_SET_MIN_SHARE},
    ).fetchall()
    out = []
    for st in sets:
        cards = conn.execute(
            """
            SELECT c.card_id, c.name, c.rarity,
                   coalesce(sum(o.quantity), 0)::int AS quantity,
                   array_remove(array_agg(DISTINCT nullif(o.variance, '')), NULL) AS variances
            FROM prod.dim_card c
            LEFT JOIN prod.dim_owned_card o ON o.card_id = c.card_id
            WHERE c.set_id = %(set_id)s
            GROUP BY c.card_id, c.name, c.rarity
            """,
            {"set_id": st["set_id"]},
        ).fetchall()
        prices = conn.execute(
            """
            SELECT f.card_id, f.date_id, f.average_sell_price AS price
            FROM prod.fact_price_history f
            WHERE f.card_id = ANY(%(ids)s)
              AND f.platform_id = (SELECT platform_id FROM prod.dim_platform
                                   WHERE platform_name = %(platform)s)
              AND f.average_sell_price IS NOT NULL
            ORDER BY f.card_id, f.date_id
            """,
            {"ids": [c["card_id"] for c in cards], "platform": _PLATFORM},
        ).fetchall()
        by_card: dict[str, list[tuple[date, float]]] = {}
        for r in prices:
            by_card.setdefault(r["card_id"], []).append((r["date_id"], float(r["price"])))
        qty = {c["card_id"]: c["quantity"] for c in cards}
        per_day: dict[date, list[float]] = {}
        for cid, h in by_card.items():
            for d, v in h:
                e = per_day.setdefault(d, [0.0, 0.0, 0])
                e[0] += v
                e[1] += qty[cid] * v
                e[2] += 1
        if not per_day:
            continue
        hist = sorted(per_day.items())
        as_of = hist[-1][0]
        set_hist = [(d, e[0]) for d, e in hist]
        windows = {}
        for w in WINDOWS:
            p = _past(set_hist, as_of - timedelta(days=w), _WINDOW_SLACK[w])
            windows[w] = (
                {"date": p[0], "value": p[1], "delta": set_hist[-1][1] - p[1]} if p else None
            )
        out_cards = []
        for c in cards:
            h = by_card.get(c["card_id"], [])
            cur = h[-1] if h and (as_of - h[-1][0]).days <= 3 else None
            past = {}
            if cur:
                for w in WINDOWS:
                    p = _past(h, as_of - timedelta(days=w), _WINDOW_SLACK[w])
                    past[w] = p[1] if p and p[1] > 0 else None
            out_cards.append(
                {
                    "card_id": c["card_id"],
                    "name": c["name"],
                    "rarity": c["rarity"],
                    "quantity": c["quantity"],
                    "variances": c["variances"],
                    "price": cur[1] if cur else None,
                    "past": past,
                }
            )
        out.append(
            {
                **st,
                "as_of": as_of,
                "history": [
                    {
                        "date": d,
                        "set_value": round(e[0], 2),
                        "owned_value": round(e[1], 2),
                        "priced": e[2],
                    }
                    for d, e in hist
                ],
                "windows": windows,
                "cards": out_cards,
            }
        )
    return {"min_share": MASTER_SET_MIN_SHARE, "sets": out}


def get_signals_board(conn, *, days: int) -> dict:
    params = {
        "days": days,
        "ctx": list(CONTEXT_SIGNALS),
        "news_days": NEWS_WINDOW_DAYS,
        "min_strength": NEWS_MIN_STRENGTH,
        # Un signal « répété » revient au moins un jour sur trois de la période.
        "min_days": max(3, round(days * 0.35)),
    }
    as_of = conn.execute("SELECT max(date_id) AS d FROM prod.fact_opportunity").fetchone()["d"]
    if as_of is None:
        return {
            "as_of": None,
            "days": days,
            "computed": [],
            "trends": [],
            "buzz": None,
            "news": [],
            "meta": [],
        }
    params["as_of"] = as_of
    # Jours où les signaux ont été calculés : l'axe des rubans du board.
    computed = [
        r["date_id"]
        for r in conn.execute(
            """
            SELECT DISTINCT date_id FROM prod.fact_opportunity
            WHERE date_id > %(as_of)s::date - %(days)s ORDER BY date_id
            """,
            params,
        ).fetchall()
    ]

    # Tendances : une ligne par carte et par sens sur la période, avec les jours
    # où le signal est sorti. Gardées : les cartes possédées, les signaux qui se
    # répètent, et ceux portés par une news ou la méta.
    trends = conn.execute(
        """
        WITH o AS (
            SELECT * FROM prod.fact_opportunity
            WHERE date_id > %(as_of)s::date - %(days)s
        ), agg AS (
            SELECT card_id, side, count(*)::int AS n_days,
                   array_agg(date_id ORDER BY date_id) AS dates,
                   array_agg(score ORDER BY date_id) AS scores
            FROM o GROUP BY card_id, side
        ), codes AS (
            SELECT card_id, side, array_agg(DISTINCT s ORDER BY s) AS codes
            FROM o, unnest(o.signals) AS s GROUP BY card_id, side
        ), last AS (
            SELECT DISTINCT ON (card_id, side) card_id, side, date_id AS last_date, score,
                   signals, reason, market_price, change_7d, change_30d
            FROM o ORDER BY card_id, side, date_id DESC
        ), own AS (
            SELECT card_id, sum(quantity)::int AS q FROM prod.dim_owned_card GROUP BY card_id
        )
        SELECT a.card_id, a.side, a.n_days, a.dates, a.scores, c.codes, l.last_date, l.score,
               l.signals, l.reason, l.market_price, l.change_7d, l.change_30d,
               dc.name, dc.set_name, dc.series, coalesce(own.q, 0) AS owned_quantity
        FROM agg a
        JOIN codes c USING (card_id, side)
        JOIN last l USING (card_id, side)
        JOIN prod.dim_card dc ON dc.card_id = a.card_id
        LEFT JOIN own ON own.card_id = a.card_id
        WHERE own.q IS NOT NULL OR a.n_days >= %(min_days)s OR c.codes && %(ctx)s::text[]
        ORDER BY (own.q IS NOT NULL) DESC, a.n_days DESC, l.score DESC
        LIMIT 400
        """,
        params,
    ).fetchall()
    trends = [_clean(r) for r in trends]

    # News rattachées à ces cartes sur la période, pour ou contre le signal.
    ids = list({t["card_id"] for t in trends})
    news_by_card: dict[str, list[dict]] = {}
    if ids:
        for r in conn.execute(
            """
            SELECT card_id, direction, title, url, source, published_on, event_type,
                   confidence, resume, confidence * weight AS strength
            FROM prod.v_news_event
            WHERE card_id = ANY(%(ids)s)
              AND published_on BETWEEN %(as_of)s::date - %(news_days)s AND %(as_of)s
              AND confidence * weight >= %(min_strength)s
            ORDER BY confidence * weight DESC, published_on DESC
            """,
            {**params, "ids": ids},
        ).fetchall():
            news_by_card.setdefault(r["card_id"], []).append(_clean(r))
    for t in trends:
        want = "hausse" if t["side"] == "buy" else "baisse"
        linked = news_by_card.get(t["card_id"], [])
        t["news"] = [n for n in linked if n["direction"] == want][:3]
        t["n_for"] = sum(1 for n in linked if n["direction"] == want)
        t["news_against"] = sum(1 for n in linked if n["direction"] != want)

    buzz = _buzz(conn, trends, as_of)
    if buzz is not None:
        for t in trends:
            sp = buzz["species_of"].get(t["name"])
            t["species"] = sp
            t["mentions_week"] = buzz["by_species"][sp]["week"] if sp in buzz["by_species"] else 0
        buzz = {k: v for k, v in buzz.items() if k not in ("species_of", "by_species")}

    # News directionnelles des 14 derniers jours, une ligne par article, avec
    # les cartes touchées (possédées d'abord).
    news = conn.execute(
        """
        WITH d AS (SELECT max(date_id) AS date_id FROM prod.fact_opportunity),
        ev AS (
            SELECT e.*, (o.card_id IS NOT NULL) AS owned
            FROM prod.v_news_event e CROSS JOIN d
            LEFT JOIN (SELECT DISTINCT card_id FROM prod.dim_owned_card) o
                   ON o.card_id = e.card_id
            WHERE e.published_on BETWEEN d.date_id - %(news_days)s AND d.date_id
              AND e.confidence * e.weight >= %(min_strength)s
        )
        SELECT url, max(title) AS title, max(source) AS source,
               max(published_on) AS published_on, max(event_type) AS event_type,
               max(direction) AS direction, max(confidence) AS confidence,
               max(resume) AS resume,
               count(DISTINCT card_id)::int AS cards,
               count(DISTINCT card_id) FILTER (WHERE owned)::int AS owned_cards,
               (array_agg(DISTINCT card_name) FILTER (WHERE owned))[1:4] AS owned_names,
               (array_agg(DISTINCT set_name))[1:3] AS sets
        FROM ev
        GROUP BY url
        ORDER BY max(published_on) DESC, max(confidence) DESC
        LIMIT 20
        """,
        params,
    ).fetchall()

    # Méta : part des decklists du top 8 par carte, semaine courante contre la
    # précédente (même règle que les Opportunités), et nombre de cartes possédées
    # du même nom.
    meta = conn.execute("""
        WITH w AS (SELECT max(week) AS cur FROM prod.v_meta_usage_weekly),
        a AS (SELECT card_name, share FROM prod.v_meta_usage_weekly, w WHERE week = w.cur),
        b AS (SELECT card_name, share FROM prod.v_meta_usage_weekly, w WHERE week = w.cur - 7),
        m AS (
            SELECT card_name, coalesce(a.share, 0) AS share_now, coalesce(b.share, 0) AS share_prev
            FROM a FULL JOIN b USING (card_name)
            WHERE greatest(coalesce(a.share, 0), coalesce(b.share, 0)) >= 0.05
        )
        SELECT m.*, (SELECT cur FROM w) AS week,
               coalesce((SELECT sum(o.quantity) FROM prod.dim_owned_card o
                         JOIN prod.dim_card c ON c.card_id = o.card_id
                         WHERE c.name = m.card_name), 0)::int AS owned_quantity
        FROM m
        ORDER BY abs(share_now - share_prev) DESC
        LIMIT 12
        """).fetchall()

    return {
        "as_of": as_of,
        "days": days,
        "computed": computed,
        "trends": trends,
        "buzz": buzz,
        "news": [_clean(r) for r in news],
        "meta": [_clean(r) for r in meta],
    }


def _buzz(conn, trends: list[dict], as_of: date) -> dict | None:
    """Espèces les plus citées dans les posts et articles des 14 derniers jours,
    rattachées aux cartes possédées et aux signaux. None tant que la vue
    prod.v_news_post (migration 014) n'existe pas."""
    today = datetime.now(UTC).date()
    try:
        posts = conn.execute(
            """
            SELECT source, url, title, summary, (published_at AT TIME ZONE 'UTC')::date
                   AS published_on, direction
            FROM prod.v_news_post
            WHERE published_at >= %(start)s
            """,
            {"start": today - timedelta(days=13)},
        ).fetchall()
        since = {
            r["source"]: r["first"]
            for r in conn.execute(
                "SELECT source, min(published_at)::date AS first FROM prod.v_news_post "
                "GROUP BY source"
            ).fetchall()
        }
    except (errors.UndefinedTable, errors.InsufficientPrivilege):
        conn.rollback()
        return None
    catalog = [r["name"] for r in conn.execute("SELECT DISTINCT name FROM prod.dim_card")]
    index = mentions.build_index(catalog)
    counts = mentions.count_mentions(posts, index, today)

    owned: dict[str, dict] = {}
    for r in conn.execute("""
        SELECT c.name, sum(o.quantity)::int AS q FROM prod.dim_owned_card o
        JOIN prod.dim_card c ON c.card_id = o.card_id GROUP BY c.name
        """).fetchall():
        sp = index.species_of(r["name"])
        if sp:
            e = owned.setdefault(sp, {"quantity": 0, "cards": []})
            e["quantity"] += r["q"]
            e["cards"].append(r["name"])
    sig: dict[str, dict] = {}
    species_of = {}
    for t in trends:
        sp = species_of.setdefault(t["name"], index.species_of(t["name"]))
        if sp and t["last_date"] == as_of:
            e = sig.setdefault(sp, {"buy": 0, "sell": 0})
            e[t["side"]] += 1

    rows = sorted(
        (c for c in counts.values() if c["week"] >= BUZZ_MIN_WEEK),
        key=lambda c: (-c["week"], -(c["week"] - (c["prev_week"] or 0)), c["species"]),
    )[:BUZZ_TOP]
    for c in rows:
        c["owned"] = owned.get(c["species"])
        c["signals"] = sig.get(c["species"])
    return {
        "today": today,
        "since": since,
        "posts": len(posts),
        "rows": rows,
        "owned_mentions": sorted(
            (
                {
                    "species": sp,
                    **owned[sp],
                    "week": counts[sp]["week"],
                    "prev_week": counts[sp]["prev_week"],
                    "daily": counts[sp]["daily"],
                }
                for sp in owned
                if sp in counts and counts[sp]["week"] > 0
            ),
            key=lambda e: -e["week"],
        ),
        "species_of": species_of,
        "by_species": counts,
    }


def _json(payload: dict) -> Response:
    # json.dumps direct plutôt que l'encodeur générique de FastAPI : sur ~500
    # cartes et leurs courbes, ce dernier prenait près de 2 s.
    body = json.dumps(payload, default=_default, separators=(",", ":"), ensure_ascii=False)
    return Response(body, media_type="application/json")


def _default(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


@router.get("/collection")
def collection_board(conn=Depends(get_api_connection)) -> Response:
    return _json(get_collection_board(conn))


@router.get("/master-sets")
def master_sets_board(conn=Depends(get_api_connection)) -> Response:
    return _json(get_master_sets(conn))


@router.get("/signals")
def signals_board(
    conn=Depends(get_api_connection), days: int = Query(default=14, ge=7, le=30)
) -> Response:
    return _json(get_signals_board(conn, days=days))
