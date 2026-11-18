# Signaux d'opportunité d'achat / vente (ajouté le 2026-11-01), exécutés par
# le DAG après load_to_warehouse. Pour chaque carte, on calcule en SQL
# quelques indicateurs de prix sur les 35 derniers jours, puis des règles
# simples et explicables (evaluate, fonction pure) décident si la carte est
# une opportunité :
#   - achat : sur le périmètre prod.v_opportunity_universe (rareté minimale
#     selon le bloc, voir migrations/012_...sql) ;
#   - vente : sur les cartes possédées seulement (prod.dim_owned_card, hors
#     cartes gradées, dont le prix n'a rien à voir avec celui de la carte brute).
# Le résultat va dans prod.fact_opportunity ; les vues
# prod.v_opportunity_outcome et prod.v_opportunity_signal_performance mesurent
# ensuite si chaque signal avait raison à J+7 et J+30. Les seuils ci-dessous
# sont un point de départ, à ajuster d'après ces mesures.
#
# Partie 2 (2026-11-06) : deux familles de signaux de contexte s'ajoutent aux
# signaux de prix, sans exiger d'historique de prix (une carte d'un set qui
# vient de sortir n'a pas 15 jours de prix, et c'est là que les news comptent) :
#   - actu_* : news classées par le LLM local (prod.fact_news_event, voir
#     src/signals/news_llm.py) ;
#   - meta_* : usage dans le top 8 des tournois Limitless (raw.meta_card_usage,
#     voir src/extract/limitless_meta.py), cartes Standard seulement.
#
# Partie 3 (2026-11-17) : réglages de l'utilisateur (schéma prefs, migration
# 017, vue prefs.v_card_rules) :
#   - pas d'achat sur une carte possédée (tout exemplaire), sauf si elle est
#     dans une liste de cartes recherchées ;
#   - pas de vente sur une carte invendable, ni sur une carte d'un masterset
#     hors doublons de rareté au-dessus de Rare, ni sous le prix minimum de
#     vente fixé pour la carte ;
#   - « ne plus suivre » : aucun signal ;
#   - cartes recherchées : évaluées même hors du périmètre d'achat, avec deux
#     signaux propres, cible_atteinte et baisse_suivie.
#
# Partie 4 (2026-11-18) : tout en francs suisses (migrations 020 à 022).
#   - Tendance (moyenne 30 j, variations, volatilité, sauts) : prix market
#     TCGplayer converti en CHF au taux BCE de chaque jour (vue
#     prod.v_tcgplayer_chf). Les variations incluent donc le change : c'est ce
#     que l'utilisateur gagne ou perd en francs.
#   - Prix du jour pour les seuils de l'utilisateur (cible d'une carte
#     recherchée, prix minimum de vente) : la première offre anglaise Near
#     Mint d'un vendeur d'Europe ou de Suisse sur CardTrader (CHF, 3 jours au
#     plus), à défaut le market converti. Les deux ne mesurent pas la même
#     chose (offre la plus basse contre prix de vente moyen) : CardTrader
#     n'entre jamais dans la tendance.
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import date

from psycopg import Connection

log = logging.getLogger(__name__)

# En dessous, quelques centimes de variation font des % énormes : bruit pur.
# En CHF (5 USD jusqu'au 2026-11-18, soit environ 4 CHF).
MIN_PRICE = 4.0
# Jours de prix minimum sur 30 j pour que la moyenne mobile ait un sens (il
# y a des trous dans l'historique, voir le 2026-10-16 -> 2026-10-25).
MIN_DAYS_30 = 15
# Saut journalier max toléré sur 7 j (|ln| > 0,7 = prix x2 ou /2 en un jour) :
# au-delà, c'est presque toujours un changement de variante TCGplayer (voir
# _select_tcgplayer_variant dans src/transform/validate.py), pas un vrai
# mouvement de marché.
MAX_DAILY_JUMP = 0.7
# Volatilité journalière plancher : une carte au prix parfaitement plat a une
# volatilité nulle, et n'importe quelle hausse ferait un z-score infini.
VOL_FLOOR = 0.01

# Valeurs de dim_owned_card (export de collection) : grade 'Ungraded' pour une
# carte brute, variance telle que l'outil de portfolio l'écrit.
UNGRADED = ("", "Ungraded")
SAME_PRICE_VARIANCES = ("", "Normal", "Holofoil")

BUY_LISTING_RATIO = 0.70
BUY_REBOUND_DEV = -0.15
BUY_MOMENTUM_Z = 2.0
BUY_MOMENTUM_30D = 0.10
SELL_PEAK_DEV = 0.25
SELL_GAIN_MULTIPLE = 1.5
SELL_DROP_30D = -0.20
SELL_DROP_7D = -0.05

# News : poids = confiance du LLM x précision du rattachement (voir
# src/transform/news_match.py) x fraîcheur (linéaire sur NEWS_WINDOW_DAYS)
# x source. Un post Reddit est un avis, pas une information : divisé par 2,
# il ne déclenche seul un signal que s'il cite la carte ET son set.
NEWS_WINDOW_DAYS = 7
NEWS_MIN_WEIGHT = 0.25
REDDIT_FACTOR = 0.5
# Méta : part des decklists du top 8 sur 7 j, comparée aux 14 j d'avant.
# Seulement les séries jouables en Standard.
STANDARD_SERIES = ("Scarlet & Violet", "Mega Evolution")
META_MIN_DECKLISTS = 16
META_RISE_MIN_SHARE = 0.10
META_RISE_MIN_DIFF = 0.10
META_FALL_MIN_PREV = 0.20
# Cartes recherchées : baisse sur 7 j qui déclenche un signal sans prix cible.
WISH_DROP_7D = -0.10
# Âge maximal du prix CardTrader retenu pour les seuils, en jours.
EU_PRICE_MAX_AGE_DAYS = 3


@dataclass(frozen=True)
class CardFeatures:
    card_id: str
    in_universe: bool
    owned_quantity: int
    # Prix moyen payé (cartes non gradées), None si absent de l'export.
    avg_cost_paid: float | None
    market: float | None
    low: float | None
    ma30: float | None
    n30: int
    # Prix au dernier jour chargé dans [J-10, J-7] et [J-35, J-30].
    price_7d: float | None
    price_30d: float | None
    volatility: float | None
    max_jump_7d: float | None
    # Médiane de low/market sur les 30 jours précédents (hors jour J).
    low_ratio_median: float | None
    n_low: int
    name: str = ""
    series: str | None = None
    # Signaux de contexte, rattachés après coup (voir fetch_features).
    news_up: float = 0.0
    news_up_title: str | None = None
    news_down: float = 0.0
    news_down_title: str | None = None
    meta_share: float | None = None
    meta_share_prev: float | None = None
    # Réglages de l'utilisateur (prefs.v_card_rules), rattachés après coup.
    buy_blocked: bool = False
    sell_blocked: bool = False
    min_sell_price: float | None = None
    wishlisted: bool = False
    target_price: float | None = None
    # Première offre NM Europe CardTrader (CHF), rattachée après coup.
    eu_price: float | None = None

    @property
    def reference_price(self) -> float | None:
        """Prix comparé aux seuils de l'utilisateur : l'offre NM Europe si
        elle existe, sinon le market converti."""
        return self.eu_price if self.eu_price is not None else self.market

    @property
    def change_7d(self) -> float | None:
        if self.market is None or not self.price_7d:
            return None
        return self.market / self.price_7d - 1

    @property
    def change_30d(self) -> float | None:
        if self.market is None or not self.price_30d:
            return None
        return self.market / self.price_30d - 1

    @property
    def dev_ma30(self) -> float | None:
        if self.market is None or not self.ma30:
            return None
        return self.market / self.ma30 - 1


@dataclass(frozen=True)
class Signal:
    code: str
    # Force entre 0 et 1, combinée dans le score de l'opportunité.
    strength: float
    text: str


@dataclass(frozen=True)
class Opportunity:
    features: CardFeatures
    side: str
    signals: tuple[Signal, ...]

    @property
    def score(self) -> int:
        # 1 - produit des (1 - force) : chaque signal de plus augmente le
        # score sans jamais dépasser 100.
        return round(100 * (1 - math.prod(1 - s.strength for s in self.signals)))

    @property
    def reason(self) -> str:
        return " ; ".join(s.text for s in self.signals)


def _pct(value: float) -> str:
    return f"{value * 100:+.0f} %"


def _chf(value: float) -> str:
    return f"{value:.2f} CHF"


def _approx(value: float) -> str:
    """Montant TCGplayer converti : « ≈ », ce n'est pas un prix suisse."""
    return f"≈ {value:.2f} CHF"


def _news_text(title: str | None) -> str:
    return f"actu : « {(title or '')[:120]} »"


def _context_buy_signals(f: CardFeatures) -> list[Signal]:
    signals = []
    if f.news_up >= NEWS_MIN_WEIGHT:
        signals.append(Signal("actu_hausse", min(1.0, f.news_up), _news_text(f.news_up_title)))
    if (
        f.series in STANDARD_SERIES
        and f.meta_share is not None
        and f.meta_share_prev is not None
        and f.meta_share >= META_RISE_MIN_SHARE
        and f.meta_share - f.meta_share_prev >= META_RISE_MIN_DIFF
    ):
        signals.append(
            Signal(
                "meta_hausse",
                min(1.0, (f.meta_share - f.meta_share_prev) * 2.5),
                f"jouée dans {f.meta_share * 100:.0f} % des top 8 Limitless sur 7 j, "
                f"contre {f.meta_share_prev * 100:.0f} % avant",
            )
        )
    return signals


def _context_sell_signals(f: CardFeatures) -> list[Signal]:
    signals = []
    if f.news_down >= NEWS_MIN_WEIGHT:
        signals.append(Signal("actu_baisse", min(1.0, f.news_down), _news_text(f.news_down_title)))
    if (
        f.series in STANDARD_SERIES
        and f.meta_share is not None
        and f.meta_share_prev is not None
        and f.meta_share_prev >= META_FALL_MIN_PREV
        and f.meta_share <= f.meta_share_prev / 2
    ):
        signals.append(
            Signal(
                "meta_baisse",
                min(1.0, (f.meta_share_prev - f.meta_share) * 2.5),
                f"jouée dans {f.meta_share * 100:.0f} % des top 8 Limitless sur 7 j, "
                f"contre {f.meta_share_prev * 100:.0f} % avant",
            )
        )
    return signals


def _buy_signals(f: CardFeatures) -> list[Signal]:
    signals = []
    change_7d, change_30d, dev = f.change_7d, f.change_30d, f.dev_ma30

    # low est l'annonce la moins chère tous états confondus, souvent très
    # sous market (médiane ~47 % sur le rétro) : on la compare donc à son
    # niveau habituel pour CETTE carte, pas à un seuil fixe.
    # Les cartes dont l'annonce la moins chère est d'habitude AU-DESSUS de
    # market sont peu échangées (market = une vente isolée, pas un prix de
    # marché) : on les écarte, comme une annonce du jour encore au-dessus de
    # 90 % de market (pas une vraie affaire).
    if (
        f.low
        and f.low_ratio_median
        and f.low_ratio_median <= 1.0
        and f.n_low >= 10
        and f.low <= 0.9 * f.market
    ):
        ratio = f.low / f.market
        relative = ratio / f.low_ratio_median
        if relative <= BUY_LISTING_RATIO:
            signals.append(
                Signal(
                    "annonce_sous_cote",
                    1 - relative,
                    f"annonce TCGplayer la moins chère à {_approx(f.low)} ({ratio * 100:.0f} % "
                    f"du prix marché, {f.low_ratio_median * 100:.0f} % d'habitude)",
                )
            )

    if dev is not None and dev <= BUY_REBOUND_DEV and change_7d is not None and change_7d >= 0:
        signals.append(
            Signal(
                "rebond",
                min(1.0, -dev / 0.4),
                f"{-dev * 100:.0f} % sous la moyenne 30 j ({_approx(f.ma30)}), "
                f"stable ou en hausse sur 7 j ({_pct(change_7d)})",
            )
        )

    if change_7d is not None and change_30d is not None and change_7d > -1:
        expected_7d = max(f.volatility or 0.0, VOL_FLOOR) * math.sqrt(7)
        z = math.log(1 + change_7d) / expected_7d
        if z >= BUY_MOMENTUM_Z and change_30d >= BUY_MOMENTUM_30D:
            signals.append(
                Signal(
                    "momentum",
                    min(1.0, (z - 1) / 4),
                    f"hausse de {_pct(change_7d)} sur 7 j ({z:.1f}x la variation habituelle) "
                    f"et {_pct(change_30d)} sur 30 j",
                )
            )
    return signals


def _sell_signals(f: CardFeatures) -> list[Signal]:
    signals = []
    change_7d, change_30d, dev = f.change_7d, f.change_30d, f.dev_ma30

    if dev is not None and dev >= SELL_PEAK_DEV:
        signals.append(
            Signal(
                "pic",
                min(1.0, dev / 0.6),
                f"{dev * 100:.0f} % au-dessus de la moyenne 30 j ({_approx(f.ma30)})",
            )
        )

    if (
        f.avg_cost_paid
        and f.market >= SELL_GAIN_MULTIPLE * f.avg_cost_paid
        and change_7d is not None
        and change_7d < 0
    ):
        gain = f.market / f.avg_cost_paid - 1
        signals.append(
            Signal(
                "plus_value",
                min(1.0, gain / 2),
                f"{_pct(gain)} sur le prix payé ({_chf(f.avg_cost_paid)}), "
                f"en baisse sur 7 j ({_pct(change_7d)})",
            )
        )

    if (
        change_30d is not None
        and change_7d is not None
        and change_30d <= SELL_DROP_30D
        and change_7d <= SELL_DROP_7D
    ):
        signals.append(
            Signal(
                "chute",
                min(1.0, -change_30d / 0.5),
                f"baisse de {_pct(change_30d)} sur 30 j qui continue sur 7 j ({_pct(change_7d)})",
            )
        )
    return signals


def _wishlist_signals(f: CardFeatures) -> list[Signal]:
    """Signaux propres aux cartes recherchées, à tout prix (une carte
    recherchée à 3 CHF compte autant qu'une autre)."""
    signals = []
    if f.target_price:
        if f.eu_price is not None:
            # Prix qu'on peut vraiment payer en Europe : une offre NM réelle.
            if f.eu_price <= f.target_price:
                signals.append(
                    Signal(
                        "cible_atteinte",
                        min(1.0, 0.6 + (1 - f.eu_price / f.target_price)),
                        f"offre NM Europe à {_chf(f.eu_price)} (CardTrader), sous ta cible "
                        f"de {_chf(f.target_price)}",
                    )
                )
        elif f.market <= f.target_price:
            # Pas d'offre NM en Europe : repère seulement (prix de vente moyen
            # TCGplayer converti), signal plus faible.
            signals.append(
                Signal(
                    "cible_atteinte",
                    0.4,
                    f"prix market {_approx(f.market)} (TCGplayer converti, aucune offre NM en "
                    f"Europe), sous ta cible de {_chf(f.target_price)}",
                )
            )
    change_7d = f.change_7d
    if (
        change_7d is not None
        and change_7d <= WISH_DROP_7D
        and (f.max_jump_7d is None or f.max_jump_7d <= MAX_DAILY_JUMP)
    ):
        signals.append(
            Signal(
                "baisse_suivie",
                min(1.0, -change_7d / 0.3),
                f"carte recherchée en baisse de {_pct(change_7d)} sur 7 j",
            )
        )
    return signals


def evaluate(f: CardFeatures) -> list[Opportunity]:
    """Règles d'achat / de vente pour une carte. Fonction pure (pas de base),
    testée directement dans tests/test_opportunities.py."""
    if f.market is None:
        return []
    # En dessous de MIN_PRICE, seuls les signaux des cartes recherchées
    # restent (choix explicite de l'utilisateur, pas du bruit de marché).
    cheap = f.market < MIN_PRICE
    # Les signaux de prix ont besoin d'un historique propre ; les signaux de
    # contexte (news, méta) non.
    prices_usable = f.n30 >= MIN_DAYS_30 and (
        f.max_jump_7d is None or f.max_jump_7d <= MAX_DAILY_JUMP
    )
    opportunities = []
    if (f.in_universe or f.wishlisted) and not f.buy_blocked:
        signals = []
        if not cheap:
            signals += (_buy_signals(f) if prices_usable else []) + _context_buy_signals(f)
        if f.wishlisted:
            signals += _wishlist_signals(f)
        if signals:
            opportunities.append(Opportunity(f, "buy", tuple(signals)))
    if (
        f.owned_quantity > 0
        and not cheap
        and not f.sell_blocked
        and (f.min_sell_price is None or f.reference_price >= f.min_sell_price)
    ):
        signals = (_sell_signals(f) if prices_usable else []) + _context_sell_signals(f)
        if signals:
            opportunities.append(Opportunity(f, "sell", tuple(signals)))
    return opportunities


# Indicateurs de toutes les cartes du périmètre ou possédées, au jour %(d)s.
# Les rendements journaliers ne sont calculés qu'entre deux jours consécutifs
# (gap = 1) : un rendement "à travers" un trou de 10 jours fausserait la
# volatilité et le saut maximal.
_FEATURES_SQL = """
WITH owned AS (
    -- Prix payé seulement sur les variantes comparables au prix du fait
    -- (normal ou holo, voir _VARIANT_PRIORITY dans src/transform/validate.py) :
    -- une reverse holo ou une 1re édition n'a pas le même prix de marché.
    SELECT card_id,
           sum(quantity) AS quantity,
           sum(quantity * average_cost_paid) FILTER (WHERE variance = ANY(%(same_price)s))
               / nullif(sum(quantity) FILTER (
                   WHERE variance = ANY(%(same_price)s) AND average_cost_paid IS NOT NULL), 0)
               AS avg_cost_paid
    FROM prod.dim_owned_card
    WHERE grade = ANY(%(ungraded)s)
    GROUP BY card_id
), ids AS (
    -- Périmètre d'achat + cartes possédées + cartes recherchées (prefs,
    -- migration 017), évaluées même hors du périmètre.
    SELECT card_id FROM prod.v_opportunity_universe
    UNION SELECT card_id FROM owned
    UNION SELECT card_id FROM prefs.wishlist_card
), scope AS (
    -- Avec leurs infos de possession : une seule jointure ensuite (joindre
    -- owned après l'agrégation faisait recalculer owned pour chaque ligne de
    -- prix, plusieurs minutes en prod).
    SELECT ids.card_id,
           u.card_id IS NOT NULL AS in_universe,
           coalesce(o.quantity, 0)::int AS owned_quantity,
           o.avg_cost_paid::float8 AS avg_cost_paid,
           c.name, c.series
    FROM ids
    LEFT JOIN prod.v_opportunity_universe u ON u.card_id = ids.card_id
    LEFT JOIN owned o ON o.card_id = ids.card_id
    JOIN prod.dim_card c ON c.card_id = ids.card_id
), h AS (
    -- Prix TCGplayer convertis en CHF au taux de chaque jour (migration 020,
    -- même règle que prod.v_tcgplayer_chf). Jointure INNER sur le taux : un
    -- jour sans taux BCE n'a pas de prix, plutôt qu'un prix faux.
    SELECT f.card_id, f.date_id, s.in_universe, s.owned_quantity, s.avg_cost_paid,
           s.name, s.series,
           (f.average_sell_price * x.usd_chf)::float8 AS market,
           (f.low_price * x.usd_chf)::float8 AS low,
           ln((f.average_sell_price * x.usd_chf)
              / lag(f.average_sell_price * x.usd_chf) OVER w)::float8 AS log_ret,
           f.date_id - lag(f.date_id) OVER w AS gap
    FROM prod.fact_price_history f
    JOIN scope s ON s.card_id = f.card_id
    JOIN prod.dim_platform p ON p.platform_id = f.platform_id AND p.platform_name = 'tcgplayer'
    JOIN prod.mv_usd_chf x ON x.date_id = f.date_id
    WHERE f.date_id BETWEEN %(d)s::date - 35 AND %(d)s::date
      AND f.average_sell_price > 0
    WINDOW w AS (PARTITION BY f.card_id ORDER BY f.date_id)
)
SELECT card_id,
       in_universe,
       owned_quantity,
       avg_cost_paid,
       max(market) FILTER (WHERE date_id = %(d)s) AS market,
       max(low) FILTER (WHERE date_id = %(d)s) AS low,
       avg(market) FILTER (WHERE date_id > %(d)s::date - 30) AS ma30,
       count(*) FILTER (WHERE date_id > %(d)s::date - 30)::int AS n30,
       (array_agg(market ORDER BY date_id DESC)
           FILTER (WHERE date_id BETWEEN %(d)s::date - 10 AND %(d)s::date - 7))[1] AS price_7d,
       (array_agg(market ORDER BY date_id DESC)
           FILTER (WHERE date_id BETWEEN %(d)s::date - 35 AND %(d)s::date - 30))[1] AS price_30d,
       stddev_samp(log_ret) FILTER (WHERE gap = 1 AND date_id > %(d)s::date - 30) AS volatility,
       max(abs(log_ret)) FILTER (WHERE gap = 1 AND date_id > %(d)s::date - 7) AS max_jump_7d,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY low / market)
           FILTER (WHERE date_id > %(d)s::date - 30 AND date_id < %(d)s AND low > 0)
           AS low_ratio_median,
       count(*) FILTER (WHERE date_id > %(d)s::date - 30 AND date_id < %(d)s AND low > 0)::int
           AS n_low,
       name,
       series
FROM h
GROUP BY card_id, in_universe, owned_quantity, avg_cost_paid, name, series
HAVING bool_or(date_id = %(d)s)
"""

# Meilleure news de chaque sens par carte : poids décroissant linéairement
# avec l'âge (1 le jour même, ~0 après NEWS_WINDOW_DAYS).
_NEWS_SQL = """
SELECT DISTINCT ON (e.card_id, e.direction)
       e.card_id, e.direction,
       (e.confidence * e.weight
        * (1 - (%(d)s::date - e.published_on)::numeric / %(window)s)
        * CASE WHEN n.source = 'reddit' THEN %(reddit)s ELSE 1 END)::float8 AS strength,
       n.title
FROM prod.fact_news_event e
JOIN raw.news_item n ON n.news_id = e.news_id
WHERE e.published_on BETWEEN %(d)s::date - (%(window)s - 1) AND %(d)s
ORDER BY e.card_id, e.direction, strength DESC
"""

# Part des decklists du top 8 jouant chaque carte (par nom) : 7 derniers
# jours contre les 14 précédents. NULL si une fenêtre a trop peu de decklists.
_META_SQL = """
WITH t AS (
    SELECT tournament_id, decklists, played_on > %(d)s::date - 7 AS recent
    FROM raw.meta_tournament
    WHERE played_on > %(d)s::date - 21 AND played_on <= %(d)s
), totals AS (
    SELECT sum(decklists) FILTER (WHERE recent) AS recent,
           sum(decklists) FILTER (WHERE NOT recent) AS prev
    FROM t
)
SELECT lower(u.card_name),
       -- ::float8 avant la division : entier / entier = division entière (0).
       coalesce(sum(u.decks) FILTER (WHERE t.recent), 0)::float8
           / nullif(totals.recent, 0) AS share,
       coalesce(sum(u.decks) FILTER (WHERE NOT t.recent), 0)::float8
           / nullif(totals.prev, 0) AS share_prev
FROM raw.meta_card_usage u
JOIN t ON t.tournament_id = u.tournament_id
CROSS JOIN totals
WHERE totals.recent >= %(min_decklists)s AND totals.prev >= %(min_decklists)s
GROUP BY lower(u.card_name), totals.recent, totals.prev
"""

# ON CONFLICT DO UPDATE : un rejeu du même jour réécrit la même ligne (calcul
# déterministe sur les mêmes prix). Pas de DELETE possible (droits
# pipeline_app) : si un rejeu après correction de prix ne déclenche plus un
# signal, l'ancienne ligne reste -- acceptable, computed_at le signale.
# En CHF depuis la migration 022 : usd_chf (taux des lignes converties depuis
# l'USD) est remis à NULL par un recalcul.
_UPSERT_SQL = """
INSERT INTO prod.fact_opportunity
    (date_id, card_id, side, score, signals, reason, market_price, low_price, ma30,
     change_7d, change_30d, volatility, eu_price)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (date_id, card_id, side) DO UPDATE SET
    score = EXCLUDED.score,
    signals = EXCLUDED.signals,
    reason = EXCLUDED.reason,
    market_price = EXCLUDED.market_price,
    low_price = EXCLUDED.low_price,
    ma30 = EXCLUDED.ma30,
    change_7d = EXCLUDED.change_7d,
    change_30d = EXCLUDED.change_30d,
    volatility = EXCLUDED.volatility,
    eu_price = EXCLUDED.eu_price,
    usd_chf = NULL,
    computed_at = now()
"""


def fetch_features(conn: Connection, date_id: date) -> list[CardFeatures]:
    with conn.cursor() as cur:
        cur.execute(
            _FEATURES_SQL,
            {"d": date_id, "ungraded": list(UNGRADED), "same_price": list(SAME_PRICE_VARIANCES)},
        )
        features = [CardFeatures(*row) for row in cur.fetchall()]
        cur.execute(_NEWS_SQL, {"d": date_id, "window": NEWS_WINDOW_DAYS, "reddit": REDDIT_FACTOR})
        news = {
            (card_id, direction): (strength, title) for card_id, direction, strength, title in cur
        }
        cur.execute(_META_SQL, {"d": date_id, "min_decklists": META_MIN_DECKLISTS})
        meta = {name: (share, share_prev) for name, share, share_prev in cur}
        cur.execute(_RULES_SQL)
        rules = {row[0]: row[1:] for row in cur}
        cur.execute(_EU_PRICE_SQL, {"d": date_id, "max_age": EU_PRICE_MAX_AGE_DAYS})
        eu = dict(cur.fetchall())
    return [
        replace(_with_rules(_with_context(f, news, meta), rules), eu_price=eu.get(f.card_id))
        for f in features
    ]


# Dernière offre NM Europe CardTrader de chaque carte, dans [J-3, J] : jamais
# un prix postérieur au jour calculé (le backtest rejoue les jours passés).
# Une dernière offre aberrante (migration 023, prod.v_cardtrader_eu) ne compte
# pas : la carte est alors comparée à la vente moyenne convertie.
_EU_PRICE_SQL = """
SELECT card_id, eu_price FROM (
    SELECT DISTINCT ON (card_id) card_id, eu_price::float8, plausible
    FROM prod.v_cardtrader_eu
    WHERE date_id BETWEEN %(d)s::date - %(max_age)s AND %(d)s
    ORDER BY card_id, date_id DESC
) last WHERE plausible
"""


# Réglages de l'utilisateur (migration 017) : une ligne par carte possédée,
# réglée ou recherchée.
_RULES_SQL = """
SELECT card_id, buy_blocked, sell_blocked, min_sell_price::float8, wishlisted,
       target_price::float8
FROM prefs.v_card_rules
"""


def _with_rules(f: CardFeatures, rules: dict[str, tuple]) -> CardFeatures:
    r = rules.get(f.card_id)
    if r is None:
        return f
    buy_blocked, sell_blocked, min_sell_price, wishlisted, target_price = r
    return replace(
        f,
        buy_blocked=buy_blocked,
        sell_blocked=sell_blocked,
        min_sell_price=min_sell_price,
        wishlisted=wishlisted,
        target_price=target_price,
    )


def _with_context(
    f: CardFeatures,
    news: dict[tuple[str, str], tuple[float, str]],
    meta: dict[str, tuple[float, float]],
) -> CardFeatures:
    up_strength, up_title = news.get((f.card_id, "hausse"), (0.0, None))
    down_strength, down_title = news.get((f.card_id, "baisse"), (0.0, None))
    # Une carte absente des top 8 sur les deux fenêtres n'est pas dans
    # meta : part 0 % des deux côtés, aucun signal.
    share, share_prev = meta.get(f.name.lower(), (None, None))
    if f.series in STANDARD_SERIES and meta and share is None:
        share, share_prev = 0.0, 0.0
    return replace(
        f,
        # Des news contradictoires se compensent : seul l'écart compte.
        news_up=max(0.0, up_strength - down_strength),
        news_up_title=up_title,
        news_down=max(0.0, down_strength - up_strength),
        news_down_title=down_title,
        meta_share=share,
        meta_share_prev=share_prev,
    )


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def compute_opportunities(conn: Connection, date_id: date) -> dict[str, int]:
    """Calcule et enregistre les opportunités du jour. Retourne le nombre de
    cartes évaluées et d'opportunités par sens (pour les logs du DAG)."""
    features = fetch_features(conn, date_id)
    opportunities = [o for f in features for o in evaluate(f)]
    rows = [
        (
            date_id,
            o.features.card_id,
            o.side,
            o.score,
            [s.code for s in o.signals],
            o.reason,
            round(o.features.market, 2),
            _round(o.features.low, 2),
            _round(o.features.ma30, 2),
            _round(o.features.change_7d, 4),
            _round(o.features.change_30d, 4),
            _round(o.features.volatility, 4),
            _round(o.features.eu_price, 2),
        )
        for o in opportunities
    ]
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, rows)
    counts = {
        "cards": len(features),
        "buy": sum(o.side == "buy" for o in opportunities),
        "sell": sum(o.side == "sell" for o in opportunities),
    }
    log.info("Opportunités du %s : %s", date_id, counts)
    return counts
