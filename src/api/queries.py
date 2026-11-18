# Requêtes SQL brutes utilisées par l'API du dashboard (src/api/main.py).
# Toutes filtrent explicitement platform_name = 'tcgplayer' (voir Global
# Constraints du plan) -- jamais de mélange avec cardmarket/EUR.
from __future__ import annotations

_PLATFORM = "tcgplayer"


def _card_filters(
    search: str | None,
    series: str | None,
    set_name: str | None,
    rarity: str | None,
    price_min: float | None,
    price_max: float | None,
) -> tuple[str, dict]:
    # Construit dynamiquement la clause WHERE : seuls les filtres réellement
    # fournis par l'appelant (pas None) ajoutent une condition -- évite un
    # empilement de "colonne = %(param)s OR %(param)s IS NULL" plus lent et
    # plus difficile à lire.
    # Le filtre platform_name est déjà appliqué dans le subquery LATERAL, donc
    # pas besoin de le répéter dans la clause WHERE extérieure (le table alias
    # 'p' n'est pas en scope là).
    conditions: list[str] = []
    params: dict = {}
    if search:
        conditions.append("c.name ILIKE %(search)s")
        params["search"] = f"%{search}%"
    if series:
        # ILIKE '%...%' plutôt qu'une égalité stricte : un utilisateur qui
        # tape "Illustration" doit trouver "Illustration Rare" sans connaître
        # le libellé exact -- même logique que "search" sur le nom ci-dessus.
        conditions.append("c.series ILIKE %(series)s")
        params["series"] = f"%{series}%"
    if set_name:
        conditions.append("c.set_name ILIKE %(set_name)s")
        params["set_name"] = f"%{set_name}%"
    if rarity:
        conditions.append("c.rarity ILIKE %(rarity)s")
        params["rarity"] = f"%{rarity}%"
    if price_min is not None:
        conditions.append("latest.average_sell_price >= %(price_min)s")
        params["price_min"] = price_min
    if price_max is not None:
        conditions.append("latest.average_sell_price <= %(price_max)s")
        params["price_max"] = price_max
    return " AND ".join(conditions) if conditions else "true", params


def search_cards(
    conn,
    *,
    search: str | None = None,
    series: str | None = None,
    set_name: str | None = None,
    rarity: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[dict], int]:
    # LEFT JOIN LATERAL : pour chaque carte de dim_card, va chercher SA propre
    # dernière observation de prix (ORDER BY date_id DESC LIMIT 1) -- contrairement
    # à un JOIN classique (INNER), un LEFT JOIN garde les cartes même si elles n'ont
    # pas d'observation de prix (latest.average_sell_price sera NULL dans ce cas).
    # Deux requêtes séparées : une pour le COUNT(*) (sans LIMIT/OFFSET, donc toujours
    # exact même si la page demandée est hors limite), une pour les résultats paginés.
    where_sql, params = _card_filters(search, series, set_name, rarity, price_min, price_max)
    params["platform"] = _PLATFORM
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    # Requête 1 : décompte total indépendant de la pagination
    count_sql = f"""
        SELECT COUNT(*) AS total_count
        FROM prod.dim_card c
        LEFT JOIN LATERAL (
            SELECT fph.average_sell_price
            FROM prod.fact_price_history fph
            JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
            WHERE fph.card_id = c.card_id AND p.platform_name = %(platform)s
            ORDER BY fph.date_id DESC
            LIMIT 1
        ) latest ON true
        WHERE {where_sql}
    """
    total = conn.execute(count_sql, params).fetchone()["total_count"]

    # Requête 2 : résultats paginés
    sql = f"""
        SELECT
            c.card_id, c.name, c.series, c.set_name, c.rarity,
            c.set_id, c.image_small, c.image_alt,
            latest.average_sell_price AS current_price
        FROM prod.dim_card c
        LEFT JOIN LATERAL (
            SELECT fph.average_sell_price
            FROM prod.fact_price_history fph
            JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
            WHERE fph.card_id = c.card_id AND p.platform_name = %(platform)s
            ORDER BY fph.date_id DESC
            LIMIT 1
        ) latest ON true
        WHERE {where_sql}
        ORDER BY c.name
        LIMIT %(limit)s OFFSET %(offset)s
    """
    rows = conn.execute(sql, params).fetchall()
    return rows, total


def get_card_history(conn, card_id: str) -> tuple[dict, list[dict]] | None:
    card = conn.execute(
        "SELECT card_id, name FROM prod.dim_card WHERE card_id = %(card_id)s",
        {"card_id": card_id},
    ).fetchone()
    if card is None:
        return None
    history = conn.execute(
        """
        SELECT fph.date_id, fph.average_sell_price, fph.trend_price, fph.low_price
        FROM prod.fact_price_history fph
        JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
        WHERE fph.card_id = %(card_id)s AND p.platform_name = %(platform)s
        ORDER BY fph.date_id
        """,
        {"card_id": card_id, "platform": _PLATFORM},
    ).fetchall()
    return card, history


def get_owned_cards(
    conn,
    *,
    search: str | None = None,
    series: str | None = None,
    set_name: str | None = None,
    rarity: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
) -> list[dict]:
    # cost_unknown : average_cost_paid NULL OU littéralement 0 -- ce dernier
    # cas vient du CSV importé (coût non renseigné saisi comme "0.0000"), pas
    # d'un vrai achat gratuit (même mise en garde que dans
    # docs/superpowers/specs/2026-08-10-interactive-dashboard-design.md).
    # LEFT JOIN LATERAL : une carte possédée sans observation de prix tcgplayer
    # doit toujours apparaître dans "Ma Collection", avec current_price = NULL.
    # Réutilise _card_filters (mêmes filtres/mêmes correspondances partielles
    # que le Catalogue) -- alias de table identiques (c./latest.) donc la
    # clause WHERE générée s'applique sans modification.
    where_sql, params = _card_filters(search, series, set_name, rarity, price_min, price_max)
    params["platform"] = _PLATFORM
    return conn.execute(
        f"""
        SELECT
            o.id, o.card_id, c.name, c.series, c.set_name, c.rarity, o.variance, o.grade,
            o.quantity, o.average_cost_paid,
            (o.average_cost_paid IS NULL OR o.average_cost_paid = 0) AS cost_unknown,
            latest.average_sell_price AS current_price
        FROM prod.dim_owned_card o
        JOIN prod.dim_card c ON c.card_id = o.card_id
        LEFT JOIN LATERAL (
            SELECT fph.average_sell_price
            FROM prod.fact_price_history fph
            JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
            WHERE fph.card_id = o.card_id AND p.platform_name = %(platform)s
            ORDER BY fph.date_id DESC
            LIMIT 1
        ) latest ON true
        WHERE {where_sql}
        ORDER BY c.name
        """,
        params,
    ).fetchall()


def get_collection_value_history(conn) -> list[dict]:
    # total_value = valeur de marché (quantity * average_sell_price), qui ne
    # dépend jamais du coût d'achat -- donc pas de filtre cost_unknown ici
    # (voir get_owned_cards ci-dessus), contrairement à ce que faisait cette
    # requête auparavant. Un coût d'achat inconnu n'a jamais empêché de
    # connaître le prix de marché actuel d'une carte.
    return conn.execute(
        """
        SELECT fph.date_id, SUM(o.quantity * fph.average_sell_price) AS total_value
        FROM prod.dim_owned_card o
        JOIN prod.fact_price_history fph ON fph.card_id = o.card_id
        JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
        WHERE p.platform_name = %(platform)s
        GROUP BY fph.date_id
        ORDER BY fph.date_id
        """,
        {"platform": _PLATFORM},
    ).fetchall()


def get_collection_movers(conn, *, window: int) -> list[dict]:
    # ranked : classe chaque observation de prix d'une carte par ancienneté
    # décroissante (rn=1 = la plus récente). cur = rn=1 (prix actuel), past
    # = rn=1+window (prix il y a "window" observations, PAS "window jours
    # calendaires" -- même convention que les moyennes mobiles déjà
    # implémentées côté frontend sur la fiche carte). LEFT JOIN : une carte
    # dont l'historique est trop court pour atteindre le rang demandé garde
    # past_price = NULL plutôt que d'être silencieusement exclue par un JOIN
    # classique -- l'exclusion se décide en Python (Task 3), pas ici.
    # average_sell_price est NULLable (la pipeline admet des lignes où seuls
    # mid/low sont connus) -- on les exclut du classement pour que le rang ne
    # compte que de vraies observations de prix, sinon une ligne NULL en
    # rn=1 ferait disparaître une carte à tort, ou décalerait ce que "N
    # observations plus tôt" signifie réellement.
    #
    # prod.dim_owned_card est unique sur (card_id, variance, grade), pas sur
    # card_id seul : une carte possédée à la fois en Normal et en Reverse
    # Holo (ou brute + gradée) donne plusieurs lignes o.*. On agrège donc par
    # card_id (SUM(quantity)) pour ne renvoyer qu'une ligne par carte -- les
    # prix cur/past sont identiques sur chaque ligne du groupe (ils viennent
    # de `ranked`, indexé par card_id), donc les inclure dans le GROUP BY ne
    # fragmente pas l'agrégat.
    return conn.execute(
        """
        WITH ranked AS (
            SELECT
                fph.card_id, fph.average_sell_price,
                ROW_NUMBER() OVER (PARTITION BY fph.card_id ORDER BY fph.date_id DESC) AS rn
            FROM prod.fact_price_history fph
            JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
            WHERE p.platform_name = %(platform)s AND fph.average_sell_price IS NOT NULL
        )
        SELECT
            o.card_id, c.name, SUM(o.quantity) AS quantity,
            cur.average_sell_price AS current_price,
            past.average_sell_price AS past_price
        FROM prod.dim_owned_card o
        JOIN prod.dim_card c ON c.card_id = o.card_id
        LEFT JOIN ranked cur ON cur.card_id = o.card_id AND cur.rn = 1
        LEFT JOIN ranked past ON past.card_id = o.card_id AND past.rn = 1 + %(window)s
        GROUP BY o.card_id, c.name, cur.average_sell_price, past.average_sell_price
        """,
        {"platform": _PLATFORM, "window": window},
    ).fetchall()


# --- Opportunités (signaux d'achat / de vente, voir src/signals/opportunities.py)

# Un signal est "éprouvé" quand son backtest à 30 jours tient : au moins 100
# cas mesurés, 65 % de réussite et un rendement moyen positif dans le sens de
# la recommandation (prod.v_opportunity_signal_performance). Les seuils vivent
# ici et non en base pour pouvoir les durcir sans migration.
PROVEN_MIN_CASES = 100
PROVEN_MIN_HIT_RATE = 0.65
# Score minimal d'une recommandation à signal éprouvé pour entrer dans la
# sélection. Une news concordante (même sens, confiance x poids >= 0,25 sur 14
# jours) suffit à elle seule : c'est le contexte qui manque aux signaux de prix.
SELECTION_MIN_SCORE = 50
NEWS_WINDOW_DAYS = 14

# Filtre des signaux selon les réglages de l'utilisateur (prefs.v_card_rules,
# migration 017), à joindre sous l'alias r sur une ligne o de
# prod.fact_opportunity : même règle qu'au calcul (src/signals/opportunities.py),
# le seuil de vente comparé au prix du jour du signal.
RULES_FILTER = """
NOT (o.side = 'buy' AND coalesce(r.buy_blocked, false))
AND NOT (o.side = 'sell' AND (coalesce(r.sell_blocked, false)
                              OR (r.min_sell_price IS NOT NULL
                                  AND o.market_price < r.min_sell_price)))
"""

_OPPORTUNITY_CTES = """
WITH d AS (
    SELECT coalesce(%(date_id)s::date, max(date_id)) AS date_id FROM prod.fact_opportunity
), proven AS (
    SELECT side, array_agg(signal) AS signals
    FROM prod.v_opportunity_signal_performance
    WHERE n_30d >= %(proven_n)s AND hit_rate_30d >= %(proven_rate)s AND avg_edge_30d > 0
    GROUP BY side
), base AS (
    SELECT o.opportunity_id, o.date_id, o.card_id, o.side, o.score, o.signals, o.reason,
           o.market_price, o.low_price, o.ma30, o.change_7d, o.change_30d,
           c.name, c.set_name, c.series, c.rarity, u.bloc,
           coalesce(o.signals && p.signals, false) AS proven_signal,
           coalesce(own.quantity, 0) AS owned_quantity,
           nw.news, nw.n_for, ag.news_against
    FROM prod.fact_opportunity o
    JOIN d ON d.date_id = o.date_id
    JOIN prod.dim_card c ON c.card_id = o.card_id
    LEFT JOIN prod.v_opportunity_universe u ON u.card_id = o.card_id
    LEFT JOIN proven p ON p.side = o.side
    LEFT JOIN LATERAL (
        SELECT sum(quantity) AS quantity FROM prod.dim_owned_card WHERE card_id = o.card_id
    ) own ON true
    LEFT JOIN LATERAL (
        SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'title', title, 'url', url, 'source', source, 'published_on', published_on,
                   'event_type', event_type, 'confidence', confidence, 'resume', resume)
               ORDER BY confidence * weight DESC), '[]'::jsonb) AS news,
               count(*)::int AS n_for
        FROM (
            SELECT * FROM prod.v_news_event e
            WHERE e.card_id = o.card_id
              AND e.published_on BETWEEN o.date_id - %(news_days)s AND o.date_id
              AND e.direction = CASE o.side WHEN 'buy' THEN 'hausse' ELSE 'baisse' END
              AND e.confidence * e.weight >= 0.25
            ORDER BY e.confidence * e.weight DESC LIMIT 3
        ) x
    ) nw ON true
    LEFT JOIN LATERAL (
        SELECT count(*)::int AS news_against
        FROM prod.v_news_event e
        WHERE e.card_id = o.card_id
          AND e.published_on BETWEEN o.date_id - %(news_days)s AND o.date_id
          AND e.direction = CASE o.side WHEN 'buy' THEN 'baisse' ELSE 'hausse' END
          AND e.confidence * e.weight >= 0.25
    ) ag ON true
    -- Réglages de l'utilisateur (migration 017) : appliqués aussi en lecture,
    -- pour qu'un réglage saisi aujourd'hui vaille tout de suite et sur les
    -- jours passés.
    LEFT JOIN prefs.v_card_rules r ON r.card_id = o.card_id
    WHERE {rules}
), scored AS (
    SELECT *,
           (news_against = 0 AND ((proven_signal AND score >= %(min_selection)s) OR n_for > 0))
               AS is_selection
    FROM base
)
"""
_OPPORTUNITY_CTES = _OPPORTUNITY_CTES.replace("{rules}", RULES_FILTER.strip())


def get_opportunities(
    conn,
    *,
    side: str | None = None,
    selection_only: bool = True,
    signal: str | None = None,
    bloc: str | None = None,
    min_score: int | None = None,
    search: str | None = None,
    date_id=None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[dict], int, dict]:
    """Renvoie (lignes de la page, total filtré, compteurs du jour).

    Les compteurs ignorent les filtres de signal / bloc / score / recherche
    (seul `side` et `selection_only` ne comptent pas non plus) : ils décrivent
    la journée entière, pour l'en-tête de la page."""
    params = {
        "date_id": date_id,
        "proven_n": PROVEN_MIN_CASES,
        "proven_rate": PROVEN_MIN_HIT_RATE,
        "min_selection": SELECTION_MIN_SCORE,
        "news_days": NEWS_WINDOW_DAYS,
    }
    conditions = ["true"]
    if side:
        conditions.append("side = %(side)s")
        params["side"] = side
    if selection_only:
        conditions.append("is_selection")
    if signal:
        conditions.append("%(signal)s = ANY(signals)")
        params["signal"] = signal
    if bloc:
        conditions.append("bloc = %(bloc)s")
        params["bloc"] = bloc
    if min_score is not None:
        conditions.append("score >= %(min_score)s")
        params["min_score"] = min_score
    if search:
        conditions.append("name ILIKE %(search)s")
        params["search"] = f"%{search}%"
    where = " AND ".join(conditions)
    rows = conn.execute(
        _OPPORTUNITY_CTES + f"""
        SELECT *, count(*) OVER () AS total
        FROM scored WHERE {where}
        ORDER BY is_selection DESC, score DESC, market_price DESC, card_id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    ).fetchall()
    counts = conn.execute(
        _OPPORTUNITY_CTES + """
        SELECT (SELECT date_id FROM d) AS date_id,
               count(*) FILTER (WHERE side = 'buy') AS buy_total,
               count(*) FILTER (WHERE side = 'buy' AND is_selection) AS buy_selection,
               count(*) FILTER (WHERE side = 'sell') AS sell_total,
               count(*) FILTER (WHERE side = 'sell' AND is_selection) AS sell_selection
        FROM scored
        """,
        params,
    ).fetchone()
    total = rows[0]["total"] if rows else 0
    return rows, total, counts


def get_signal_performance(conn) -> list[dict]:
    return conn.execute("""
        SELECT side, signal, n, n_7d, hit_rate_7d, avg_edge_7d, n_30d, hit_rate_30d, avg_edge_30d
        FROM prod.v_opportunity_signal_performance
        ORDER BY side, n_30d DESC, signal
        """).fetchall()


def get_opportunity_trends(conn, *, date_id=None) -> dict:
    """Tendances qui entourent les signaux : sets (prix + signaux + news),
    news de marché de la semaine, évolution de la méta compétitive."""
    params = {"date_id": date_id, "platform": _PLATFORM, "news_days": NEWS_WINDOW_DAYS}
    sets = conn.execute(
        """
        WITH d AS (
            SELECT coalesce(%(date_id)s::date, max(date_id)) AS date_id FROM prod.fact_opportunity
        ), px AS (
            SELECT u.card_id, c.set_name, c.series,
                   cur.average_sell_price AS cur_price, past.average_sell_price AS past_price
            FROM prod.v_opportunity_universe u
            JOIN prod.dim_card c ON c.card_id = u.card_id
            CROSS JOIN d
            JOIN LATERAL (
                SELECT f.average_sell_price FROM prod.fact_price_history f
                JOIN prod.dim_platform p ON p.platform_id = f.platform_id
                WHERE f.card_id = u.card_id AND p.platform_name = %(platform)s
                  AND f.date_id BETWEEN d.date_id - 3 AND d.date_id
                  AND f.average_sell_price >= 5
                ORDER BY f.date_id DESC LIMIT 1
            ) cur ON true
            JOIN LATERAL (
                SELECT f.average_sell_price FROM prod.fact_price_history f
                JOIN prod.dim_platform p ON p.platform_id = f.platform_id
                WHERE f.card_id = u.card_id AND p.platform_name = %(platform)s
                  AND f.date_id BETWEEN d.date_id - 36 AND d.date_id - 30
                  AND f.average_sell_price > 0
                ORDER BY f.date_id DESC LIMIT 1
            ) past ON true
        ), by_set AS (
            SELECT set_name, max(series) AS series, count(*)::int AS cards,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY cur_price / past_price - 1)
                       AS median_change_30d
            FROM px GROUP BY set_name HAVING count(*) >= 5
        ), sig AS (
            SELECT c.set_name,
                   count(*) FILTER (WHERE o.side = 'buy')::int AS buy_signals,
                   count(*) FILTER (WHERE o.side = 'sell')::int AS sell_signals
            FROM prod.fact_opportunity o
            JOIN d ON d.date_id = o.date_id
            JOIN prod.dim_card c ON c.card_id = o.card_id
            LEFT JOIN prefs.v_card_rules r ON r.card_id = o.card_id
            WHERE """
        + RULES_FILTER
        + """
            GROUP BY c.set_name
        ), nw AS (
            SELECT e.set_name,
                   count(DISTINCT e.url) FILTER (WHERE e.direction = 'hausse')::int AS news_up,
                   count(DISTINCT e.url) FILTER (WHERE e.direction = 'baisse')::int AS news_down
            FROM prod.v_news_event e CROSS JOIN d
            WHERE e.published_on BETWEEN d.date_id - %(news_days)s AND d.date_id
            GROUP BY e.set_name
        )
        SELECT b.set_name, b.series, b.cards, round(b.median_change_30d::numeric, 4)
                   AS median_change_30d,
               coalesce(s.buy_signals, 0) AS buy_signals,
               coalesce(s.sell_signals, 0) AS sell_signals,
               coalesce(n.news_up, 0) AS news_up, coalesce(n.news_down, 0) AS news_down
        FROM by_set b
        LEFT JOIN sig s ON s.set_name = b.set_name
        LEFT JOIN nw n ON n.set_name = b.set_name
        ORDER BY abs(b.median_change_30d) DESC
        LIMIT 40
        """,
        params,
    ).fetchall()
    news = conn.execute(
        """
        WITH d AS (
            SELECT coalesce(%(date_id)s::date, max(date_id)) AS date_id FROM prod.fact_opportunity
        )
        SELECT e.title, e.url, e.source, e.published_on, e.direction, e.event_type,
               max(e.confidence) AS confidence, max(e.resume) AS resume,
               count(DISTINCT e.card_id)::int AS cards,
               (array_agg(DISTINCT e.set_name))[1:3] AS sets
        FROM prod.v_news_event e CROSS JOIN d
        WHERE e.published_on BETWEEN d.date_id - 7 AND d.date_id
        GROUP BY e.title, e.url, e.source, e.published_on, e.direction, e.event_type
        ORDER BY max(e.confidence) DESC, e.published_on DESC
        LIMIT 12
        """,
        params,
    ).fetchall()
    # Méta : part des decklists du top 8 par carte, dernière semaine contre la
    # précédente. Les cartes à moins de 5 % dans les deux semaines sont ignorées.
    meta = conn.execute("""
        WITH w AS (SELECT max(week) AS cur FROM prod.v_meta_usage_weekly),
        -- Chaque semaine filtrée AVANT le FULL JOIN : avec le filtre de b dans
        -- la clause ON, les lignes de toutes les semaines passées remontaient.
        a AS (SELECT card_name, share FROM prod.v_meta_usage_weekly, w WHERE week = w.cur),
        b AS (SELECT card_name, share FROM prod.v_meta_usage_weekly, w WHERE week = w.cur - 7)
        SELECT card_name, coalesce(a.share, 0) AS share_now, coalesce(b.share, 0) AS share_prev
        FROM a FULL JOIN b USING (card_name)
        WHERE greatest(coalesce(a.share, 0), coalesce(b.share, 0)) >= 0.05
        ORDER BY abs(coalesce(a.share, 0) - coalesce(b.share, 0)) DESC
        LIMIT 10
        """).fetchall()
    return {"sets": sets, "news": news, "meta": meta}
