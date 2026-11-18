# Données du tableau de bord « board » (board/index.html) : deux blocs face à
# face, Collection (valeur et mouvements de prix des cartes possédées) et
# Signaux (achats / ventes portés par les news et la méta compétitive).
# Rôle dashboard_reader. Lecture seule, sauf les réglages de l'utilisateur
# (schéma prefs, voir src/api/prefs.py), dont les règles filtrent aussi les
# signaux affichés ici (prefs.v_card_rules).
#
# Tout en francs suisses depuis le 2026-11-18 (migrations 020 à 022), avec
# deux mesures qui ne se mélangent jamais :
#   - `price`, `past`, courbes, valeurs : prix market TCGplayer (prix de vente
#     moyen, USD) converti au taux BCE de chaque jour (prod.mv_usd_chf). C'est
#     la tendance ; le board le marque « ≈ ». Un jour sans taux n'a pas de prix.
#   - `eu` : première offre anglaise Near Mint d'un vendeur d'Europe ou de
#     Suisse sur CardTrader (CHF, 3 jours au plus avant as_of), hors offres
#     aberrantes (vue prod.v_cardtrader_eu, migration 023). C'est le prix
#     affiché d'une carte quand il existe ; il n'entre dans aucune courbe ni
#     aucune somme de la tendance (total à part : eu_total).
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from psycopg import errors

from src.api import mentions
from src.api.db import get_api_connection
from src.api.queries import NEWS_WINDOW_DAYS, RULES_FILTER

router = APIRouter(prefix="/api/board")

_PLATFORM = "tcgplayer"
CURRENCY = "CHF"
# Âge maximal du prix CardTrader affiché, en jours avant as_of (même règle
# que les signaux, EU_PRICE_MAX_AGE_DAYS dans src/signals/opportunities.py).
EU_MAX_AGE_DAYS = 3
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


def usd_chf_rates(conn) -> dict[date, float]:
    """Taux USD -> CHF de chaque jour (règle de la migration 020)."""
    return {
        r["date_id"]: float(r["usd_chf"])
        for r in conn.execute("SELECT date_id, usd_chf FROM prod.mv_usd_chf").fetchall()
    }


def to_chf(
    rows, rates: dict[date, float], key: str = "price"
) -> tuple[dict[str, list[tuple[date, float]]], date | None]:
    """Regroupe des lignes (card_id, date_id, <key> en USD) par carte, en CHF.
    Les jours sans taux sont écartés. Renvoie aussi le dernier jour de prix
    TCGplayer, converti ou non, pour signaler un taux manquant."""
    by_card: dict[str, list[tuple[date, float]]] = {}
    last_day = None
    for r in rows:
        d = r["date_id"]
        last_day = d if last_day is None or d > last_day else last_day
        rate = rates.get(d)
        if rate is not None:
            by_card.setdefault(r["card_id"], []).append((d, float(r[key]) * rate))
    return by_card, last_day


def fx_info(conn, as_of: date | None, last_day: date | None) -> dict:
    """Taux appliqué au dernier jour affiché, et jour de prix TCGplayer sans
    taux BCE (le board affiche alors un bandeau, prix arrêtés à as_of)."""
    row = None
    if as_of is not None:
        row = conn.execute(
            "SELECT ecb_date, usd_chf FROM prod.mv_usd_chf WHERE date_id = %(d)s", {"d": as_of}
        ).fetchone()
    return {
        "date": as_of,
        "ecb_date": row["ecb_date"] if row else None,
        "usd_chf": float(row["usd_chf"]) if row else None,
        "missing_since": last_day if last_day and (as_of is None or last_day > as_of) else None,
    }


def eu_prices(conn, card_ids: list[str], since: date | None) -> tuple[dict[str, dict], int]:
    """Dernière offre NM Europe CardTrader de chaque carte, datée de `since`
    ou après (vue prod.v_cardtrader_eu, CHF). Si cette dernière offre est
    aberrante (règle de la migration 023), la carte n'a pas de prix Europe :
    elle est comptée à part (second élément), pas remplacée par une offre plus
    ancienne."""
    if not card_ids or since is None:
        return {}, 0
    rows = conn.execute(
        """
        SELECT DISTINCT ON (card_id) card_id, date_id, eu_price, eu_condition,
               eu_seller_country, eu_finish, plausible
        FROM prod.v_cardtrader_eu
        WHERE card_id = ANY(%(ids)s) AND date_id >= %(since)s
        ORDER BY card_id, date_id DESC
        """,
        {"ids": card_ids, "since": since},
    ).fetchall()
    eu = {
        r["card_id"]: {
            "price": float(r["eu_price"]),
            "date": r["date_id"],
            "condition": r["eu_condition"],
            "country": r["eu_seller_country"],
            "finish": r["eu_finish"],
        }
        for r in rows
        if r["plausible"]
    }
    return eu, sum(1 for r in rows if not r["plausible"])


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


# Réglages d'une carte sans ligne dans prefs (valeurs par défaut).
DEFAULT_PREFS = {
    "unsellable": False,
    "unfollowed": False,
    "min_sell_price": None,
    "in_masterset": False,
    "wishlisted": False,
    "target_price": None,
    "buy_blocked": False,
    "sell_blocked": False,
}


def card_prefs(conn, card_ids: list[str]) -> dict[str, dict]:
    """Réglages et règles qui s'appliquent à ces cartes (prefs.v_card_rules)."""
    if not card_ids:
        return {}
    rows = conn.execute(
        """
        SELECT card_id, unsellable, unfollowed, min_sell_price, in_masterset, wishlisted,
               target_price, buy_blocked, sell_blocked
        FROM prefs.v_card_rules WHERE card_id = ANY(%(ids)s)
        """,
        {"ids": card_ids},
    ).fetchall()
    return {r["card_id"]: {k: _num(v) for k, v in r.items() if k != "card_id"} for r in rows}


def get_collection_board(conn) -> dict:
    params = {"platform": _PLATFORM}

    # Une ligne par carte possédée (variantes et gradées regroupées : le prix
    # TCGplayer est le même pour toutes), avec son historique de prix complet.
    cards = conn.execute("""
        SELECT o.card_id, c.name, c.set_name, c.series, c.rarity,
               c.image_small, c.image_alt,
               sum(o.quantity)::int AS quantity,
               array_agg(DISTINCT nullif(o.variance, '')) AS variances,
               array_agg(DISTINCT nullif(o.grade, 'Ungraded')) AS grades
        FROM prod.dim_owned_card o
        JOIN prod.dim_card c ON c.card_id = o.card_id
        GROUP BY o.card_id, c.name, c.set_name, c.series, c.rarity, c.image_small, c.image_alt
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
    by_card, last_day = to_chf(prices, usd_chf_rates(conn))
    if not by_card:
        return {
            "as_of": None,
            "currency": CURRENCY,
            "fx": fx_info(conn, None, last_day),
            "history": [],
            "windows": {},
            "cards": [],
            "sparks": {},
        }

    # Signaux du dernier jour sur les cartes possédées (prix, news, méta).
    sig_rows = conn.execute(
        """
        SELECT o.card_id, o.side, o.score, o.signals, o.reason
        FROM prod.fact_opportunity o
        LEFT JOIN prefs.v_card_rules r ON r.card_id = o.card_id
        WHERE o.date_id = (SELECT max(date_id) FROM prod.fact_opportunity)
          AND o.card_id IN (SELECT card_id FROM prod.dim_owned_card)
          AND """
        + RULES_FILTER
        + """
        ORDER BY o.score DESC
        """
    ).fetchall()
    sigs: dict[str, list[dict]] = {}
    for r in sig_rows:
        sigs.setdefault(r["card_id"], []).append(_clean(r))
    prefs = card_prefs(conn, [c["card_id"] for c in cards])

    # Valeur de la collection actuelle, jour par jour (quantité x prix market
    # converti).
    qty = {c["card_id"]: c["quantity"] for c in cards}
    per_day: dict[date, float] = {}
    for cid, h in by_card.items():
        for d, v in h:
            per_day[d] = per_day.get(d, 0.0) + qty[cid] * v
    hist = sorted(per_day.items())
    as_of, total = hist[-1]
    eu, eu_ignored = eu_prices(
        conn, [c["card_id"] for c in cards], as_of - timedelta(days=EU_MAX_AGE_DAYS)
    )

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
            "image_small": c["image_small"],
            "image_alt": c["image_alt"],
            "quantity": c["quantity"],
            "variances": [v for v in c["variances"] if v],
            "grades": [g for g in c["grades"] if g],
            "price": cur[1] if cur else None,
            "eu": eu.get(c["card_id"]),
            "past": {},
            "signals": sigs.get(c["card_id"], []),
            "prefs": prefs.get(c["card_id"], DEFAULT_PREFS),
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

    # Total « au prix Europe » : à part, jamais mélangé à la valeur (tendance).
    # Les cartes sans offre NM en Europe n'y sont pas, leur nombre est donné.
    with_eu = [c for c in out_cards if c["eu"]]
    return {
        "as_of": as_of,
        "currency": CURRENCY,
        "fx": fx_info(conn, as_of, last_day),
        "total_value": total,
        "eu_total": {
            "value": round(sum(c["quantity"] * c["eu"]["price"] for c in with_eu), 2),
            "cards": len(with_eu),
            "of": len(out_cards),
            "quantity": sum(c["quantity"] for c in with_eu),
            "date": max((c["eu"]["date"] for c in with_eu), default=None),
            # Offres aberrantes écartées (migration 023) : ces cartes sont « ≈ ».
            "ignored": eu_ignored,
        },
        "history": [{"date": d, "value": round(v, 2)} for d, v in hist],
        "windows": windows,
        "cards": out_cards,
        "dates": dates,
        "sparks": sparks,
        "chase_rarities": list(CHASE_RARITIES),
    }


# Sets suivis : sets dont on possède au moins cette part des cartes (du set
# entier ou de ses cartes recherchées, voir CHASE_RARITIES). Détectés
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
        WITH s AS (SELECT set_id, max(set_name) AS set_name, count(*) AS n,
                          count(*) FILTER (WHERE rarity = ANY(%(chase)s)) AS n_chase
                   FROM prod.dim_card GROUP BY set_id),
        o AS (SELECT c.set_id, count(DISTINCT o.card_id) AS owned_cards,
                     count(DISTINCT o.card_id) FILTER (WHERE c.rarity = ANY(%(chase)s))
                         AS owned_chase,
                     count(*) AS entries, sum(o.quantity)::int AS copies
              FROM prod.dim_owned_card o JOIN prod.dim_card c ON c.card_id = o.card_id
              GROUP BY c.set_id)
        SELECT s.set_id, s.set_name, s.n::int AS n_cards, o.owned_cards::int,
               s.n_chase::int AS n_chase, o.owned_chase::int, o.entries::int, o.copies,
               coalesce(ss.masterset, false) AS masterset
        FROM s JOIN o USING (set_id)
        -- Suivi dès MASTER_SET_MIN_SHARE du set entier OU de ses cartes
        -- recherchées : on collectionne souvent les AR / SAR d'un set sans ses
        -- communes (151 : 32 cartes recherchées sur 42, 33 cartes sur 207).
        LEFT JOIN prefs.set_setting ss ON ss.set_id = s.set_id
        WHERE o.owned_cards >= %(share)s * s.n
           OR (s.n_chase > 0 AND o.owned_chase >= %(share)s * s.n_chase)
           OR coalesce(ss.masterset, false)
        ORDER BY (o.owned_cards = s.n) DESC,
                 greatest(o.owned_cards::float / s.n,
                          o.owned_chase::float / nullif(s.n_chase, 0)) DESC,
                 s.set_name
        """,
        {"share": MASTER_SET_MIN_SHARE, "chase": list(CHASE_RARITIES)},
    ).fetchall()
    rates = usd_chf_rates(conn)
    out = []
    for st in sets:
        cards = conn.execute(
            """
            SELECT c.card_id, c.name, c.rarity, c.image_small, c.image_alt,
                   coalesce(sum(o.quantity), 0)::int AS quantity,
                   array_remove(array_agg(DISTINCT nullif(o.variance, '')), NULL) AS variances
            FROM prod.dim_card c
            LEFT JOIN prod.dim_owned_card o ON o.card_id = c.card_id
            WHERE c.set_id = %(set_id)s
            GROUP BY c.card_id, c.name, c.rarity, c.image_small, c.image_alt
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
        by_card, _ = to_chf(prices, rates)
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
        eu, _ = eu_prices(
            conn, [c["card_id"] for c in cards], as_of - timedelta(days=EU_MAX_AGE_DAYS)
        )
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
                    "image_small": c["image_small"],
                    "image_alt": c["image_alt"],
                    "quantity": c["quantity"],
                    "variances": c["variances"],
                    "price": cur[1] if cur else None,
                    "eu": eu.get(c["card_id"]),
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
    return {
        "currency": CURRENCY,
        "min_share": MASTER_SET_MIN_SHARE,
        "chase_rarities": list(CHASE_RARITIES),
        "sets": out,
    }


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
            "currency": CURRENCY,
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
            SELECT o.* FROM prod.fact_opportunity o
            LEFT JOIN prefs.v_card_rules r ON r.card_id = o.card_id
            WHERE o.date_id > %(as_of)s::date - %(days)s
              AND """
        + RULES_FILTER
        + """
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
                   signals, reason, market_price, eu_price, change_7d, change_30d
            FROM o ORDER BY card_id, side, date_id DESC
        ), own AS (
            SELECT card_id, sum(quantity)::int AS q FROM prod.dim_owned_card GROUP BY card_id
        )
        SELECT a.card_id, a.side, a.n_days, a.dates, a.scores, c.codes, l.last_date, l.score,
               l.signals, l.reason, l.market_price, l.eu_price, l.change_7d, l.change_30d,
               dc.name, dc.set_name, dc.series, dc.rarity, dc.image_small, dc.image_alt,
               coalesce(own.q, 0) AS owned_quantity
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
    prefs = card_prefs(conn, list({t["card_id"] for t in trends}))
    for t in trends:
        t["prefs"] = prefs.get(t["card_id"], DEFAULT_PREFS)

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
        "currency": CURRENCY,
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
