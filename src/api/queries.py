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
    # d'un vrai achat gratuit. Voir la mise en garde déjà posée sur le
    # dashboard Metabase (docs/superpowers/specs/2026-08-10-interactive-dashboard-design.md).
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
