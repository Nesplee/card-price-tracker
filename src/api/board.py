# Données du tableau de bord « board » (board/index.html) : deux blocs face à
# face, Collection (valeur et mouvements de prix des cartes possédées) et
# Signaux (achats / ventes portés par les news et la méta compétitive).
# Lecture seule, rôle dashboard_reader, prix TCGplayer (USD) uniquement.
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from src.api.db import get_api_connection
from src.api.queries import NEWS_WINDOW_DAYS

router = APIRouter(prefix="/api/board")

_PLATFORM = "tcgplayer"
# Fenêtres des mouvements, en jours calendaires. Le prix « passé » d'une carte
# est sa dernière observation à D - N ou avant, dans une marge de quelques jours
# (l'historique a des trous : 26/08, 16 au 25/10) : au-delà, pas de variation.
WINDOWS = (1, 7, 30, 90)
_WINDOW_SLACK = {1: 3, 7: 4, 30: 12, 90: 12}
# Mini-courbes : seulement pour les cartes qui figurent dans les N plus gros
# impacts (en $) d'au moins une fenêtre, pour garder la réponse légère.
SPARK_TOP = 30
# Signaux de contexte : déclenchés par une news ou par la méta (le reste vient
# des prix seuls, voir src/signals/opportunities.py).
CONTEXT_SIGNALS = ("actu_hausse", "actu_baisse", "meta_hausse", "meta_baisse")
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

    # Cartes dont on renvoie la courbe : le top SPARK_TOP en impact absolu ($)
    # de chaque fenêtre. Les courbes partagent l'axe `dates` (None = trou).
    spark_ids: set[str] = set()
    for w in WINDOWS:
        ranked = sorted(
            (c for c in out_cards if c["price"] is not None and c["past"].get(w) is not None),
            key=lambda c: -abs(c["quantity"] * (c["price"] - c["past"][w])),
        )
        spark_ids.update(c["card_id"] for c in ranked[:SPARK_TOP])
    dates = [d for d, _ in hist]
    sparks = {}
    for cid in sorted(spark_ids):
        m = dict(by_card[cid])
        sparks[cid] = [m.get(d) for d in dates]

    return {
        "as_of": as_of,
        "total_value": total,
        "history": [{"date": d, "value": round(v, 2)} for d, v in hist],
        "windows": windows,
        "cards": out_cards,
        "dates": dates,
        "sparks": sparks,
    }


def get_signals_board(conn, *, days: int) -> dict:
    params = {
        "days": days,
        "ctx": list(CONTEXT_SIGNALS),
        "news_days": NEWS_WINDOW_DAYS,
        "min_strength": NEWS_MIN_STRENGTH,
    }
    # Signaux des `days` derniers jours portés par une news ou la méta : un code
    # de contexte (actu_* / meta_*), ou un signal de prix appuyé par une news
    # concordante. Une ligne par carte et par sens, la plus récente.
    items = conn.execute(
        """
        WITH d AS (SELECT max(date_id) AS date_id FROM prod.fact_opportunity),
        base AS (
            SELECT o.date_id, o.card_id, o.side, o.score, o.signals, o.reason,
                   o.market_price, o.change_7d, o.change_30d,
                   c.name, c.set_name, c.series, c.rarity,
                   coalesce(own.quantity, 0)::int AS owned_quantity,
                   nw.news, coalesce(nw.n_for, 0) AS n_for, ag.news_against
            FROM prod.fact_opportunity o
            CROSS JOIN d
            JOIN prod.dim_card c ON c.card_id = o.card_id
            LEFT JOIN LATERAL (
                SELECT sum(quantity) AS quantity FROM prod.dim_owned_card
                WHERE card_id = o.card_id
            ) own ON true
            LEFT JOIN LATERAL (
                SELECT coalesce(jsonb_agg(jsonb_build_object(
                           'title', title, 'url', url, 'source', source,
                           'published_on', published_on, 'event_type', event_type,
                           'direction', direction, 'confidence', confidence, 'resume', resume)
                       ORDER BY strength DESC), '[]'::jsonb) AS news,
                       count(*)::int AS n_for
                FROM (
                    SELECT e.*, e.confidence * e.weight AS strength FROM prod.v_news_event e
                    WHERE e.card_id = o.card_id
                      AND e.published_on BETWEEN o.date_id - %(news_days)s AND o.date_id
                      AND e.direction = CASE o.side WHEN 'buy' THEN 'hausse' ELSE 'baisse' END
                      AND e.confidence * e.weight >= %(min_strength)s
                    ORDER BY e.confidence * e.weight DESC LIMIT 3
                ) x
            ) nw ON true
            LEFT JOIN LATERAL (
                SELECT count(*)::int AS news_against FROM prod.v_news_event e
                WHERE e.card_id = o.card_id
                  AND e.published_on BETWEEN o.date_id - %(news_days)s AND o.date_id
                  AND e.direction = CASE o.side WHEN 'buy' THEN 'baisse' ELSE 'hausse' END
                  AND e.confidence * e.weight >= %(min_strength)s
            ) ag ON true
            WHERE o.date_id > d.date_id - %(days)s
        )
        SELECT DISTINCT ON (card_id, side) *
        FROM base
        WHERE signals && %(ctx)s::text[] OR n_for > 0
        ORDER BY card_id, side, date_id DESC
        """,
        params,
    ).fetchall()
    as_of = conn.execute("SELECT max(date_id) AS d FROM prod.fact_opportunity").fetchone()["d"]

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
        "items": [_clean(r) for r in items],
        "news": [_clean(r) for r in news],
        "meta": [_clean(r) for r in meta],
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


@router.get("/signals")
def signals_board(
    conn=Depends(get_api_connection), days: int = Query(default=7, ge=1, le=30)
) -> Response:
    return _json(get_signals_board(conn, days=days))
