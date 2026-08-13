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
        conditions.append("c.series = %(series)s")
        params["series"] = series
    if set_name:
        conditions.append("c.set_name = %(set_name)s")
        params["set_name"] = set_name
    if rarity:
        conditions.append("c.rarity = %(rarity)s")
        params["rarity"] = rarity
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
    # JOIN LATERAL : pour chaque carte de dim_card, va chercher SA propre
    # dernière observation de prix (ORDER BY date_id DESC LIMIT 1) -- contrairement
    # à un JOIN classique, la sous-requête est ré-évaluée pour chaque ligne de
    # dim_card, ce qui est exactement ce qu'il faut pour "le prix le plus
    # récent PAR carte" (pas un seul prix global).
    # COUNT(*) OVER() : calcule le nombre total de résultats (avant LIMIT/OFFSET)
    # dans la même requête, pour éviter un second aller-retour réseau juste
    # pour la pagination.
    where_sql, params = _card_filters(search, series, set_name, rarity, price_min, price_max)
    params["platform"] = _PLATFORM
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size
    sql = f"""
        SELECT
            c.card_id, c.name, c.series, c.set_name, c.rarity,
            latest.average_sell_price AS current_price,
            COUNT(*) OVER() AS total_count
        FROM prod.dim_card c
        JOIN LATERAL (
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
    total = rows[0]["total_count"] if rows else 0
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


def get_owned_cards(conn) -> list[dict]:
    # cost_unknown : average_cost_paid NULL OU littéralement 0 -- ce dernier
    # cas vient du CSV importé (coût non renseigné saisi comme "0.0000"), pas
    # d'un vrai achat gratuit. Voir la mise en garde déjà posée sur le
    # dashboard Metabase (docs/superpowers/specs/2026-08-10-interactive-dashboard-design.md).
    return conn.execute(
        """
        SELECT
            o.id, o.card_id, c.name, c.series, c.set_name, o.variance, o.grade,
            o.quantity, o.average_cost_paid,
            (o.average_cost_paid IS NULL OR o.average_cost_paid = 0) AS cost_unknown,
            latest.average_sell_price AS current_price
        FROM prod.dim_owned_card o
        JOIN prod.dim_card c ON c.card_id = o.card_id
        JOIN LATERAL (
            SELECT fph.average_sell_price
            FROM prod.fact_price_history fph
            JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
            WHERE fph.card_id = o.card_id AND p.platform_name = %(platform)s
            ORDER BY fph.date_id DESC
            LIMIT 1
        ) latest ON true
        ORDER BY c.name
        """,
        {"platform": _PLATFORM},
    ).fetchall()


def get_collection_value_history(conn) -> list[dict]:
    # Exclut les lignes cost_unknown (voir get_owned_cards ci-dessus) : la
    # spec (docs/superpowers/specs/2026-08-13-custom-dashboard-design.md)
    # demande explicitement de ne pas les inclure, pour ne pas fausser la
    # courbe de valeur avec des cartes dont le coût réel n'est simplement pas
    # connu (même biais que celui déjà identifié sur le dashboard Metabase).
    return conn.execute(
        """
        SELECT fph.date_id, SUM(o.quantity * fph.average_sell_price) AS total_value
        FROM prod.dim_owned_card o
        JOIN prod.fact_price_history fph ON fph.card_id = o.card_id
        JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
        WHERE p.platform_name = %(platform)s
          AND o.average_cost_paid IS NOT NULL AND o.average_cost_paid != 0
        GROUP BY fph.date_id
        ORDER BY fph.date_id
        """,
        {"platform": _PLATFORM},
    ).fetchall()
