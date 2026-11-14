-- Migration 012 : signaux d'opportunité d'achat / vente (2026-11-01).
--
-- Chaque jour, src/signals/opportunities.py calcule des indicateurs de prix
-- sur un périmètre de cartes (vue prod.v_opportunity_universe) et écrit dans
-- prod.fact_opportunity les cartes où un signal se déclenche : achat sur tout
-- le périmètre, vente seulement sur les cartes possédées. Les deux vues de
-- suivi comparent ensuite chaque recommandation au prix réel 7 et 30 jours
-- plus tard, pour mesurer quels signaux marchent vraiment.

BEGIN;

-- Périmètre : rareté minimale selon le bloc. Les listes vivent ici (et pas en
-- Python) pour que Metabase et le pipeline voient exactement le même
-- périmètre. pokemontcg.io ne distingue pas les alt arts des full arts : toutes
-- les Rare Ultra / Rare Secret des blocs anciens sont gardées.
CREATE VIEW prod.v_opportunity_universe AS
SELECT card_id,
       CASE
           WHEN series IN ('Scarlet & Violet', 'Mega Evolution') THEN 'recent'
           WHEN series IN ('Sword & Shield', 'Sun & Moon', 'XY') THEN 'ancien'
           ELSE 'retro'
       END AS bloc
FROM prod.dim_card
WHERE (series IN ('Scarlet & Violet', 'Mega Evolution')
       AND rarity IN ('Illustration Rare', 'Special Illustration Rare', 'Hyper Rare',
                      'Black White Rare', 'Mega Hyper Rare', 'MEGA_ATTACK_RARE',
                      'Futuristic Rare'))
   OR (series IN ('Sword & Shield', 'Sun & Moon', 'XY')
       AND rarity IN ('Rare Ultra', 'Rare Secret', 'Rare Rainbow', 'Trainer Gallery Rare Holo',
                      'Rare Shiny', 'Rare Shiny GX', 'Amazing Rare'))
   OR series IN ('Base', 'Gym', 'Neo', 'E-Card', 'EX', 'Diamond & Pearl', 'Platinum',
                 'HeartGold & SoulSilver', 'POP', 'Black & White', 'NP')
   OR set_name IN ('Best of Game', 'Legendary Collection', 'Southern Islands');

-- Une ligne par (jour, carte, sens). signals : codes des règles déclenchées
-- (voir src/signals/opportunities.py), reason : leur explication en clair.
-- Les indicateurs du jour sont gardés tels quels pour pouvoir relire une
-- recommandation passée sans recalcul.
CREATE TABLE prod.fact_opportunity (
    opportunity_id  bigserial PRIMARY KEY,
    date_id         date NOT NULL REFERENCES prod.dim_date(date_id),
    card_id         text NOT NULL REFERENCES prod.dim_card(card_id),
    side            text NOT NULL CHECK (side IN ('buy', 'sell')),
    score           smallint NOT NULL CHECK (score BETWEEN 0 AND 100),
    signals         text[] NOT NULL,
    reason          text NOT NULL,
    market_price    numeric(10, 2) NOT NULL,
    low_price       numeric(10, 2),
    ma30            numeric(10, 2),
    change_7d       numeric(8, 4),
    change_30d      numeric(8, 4),
    volatility      numeric(8, 4),
    computed_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_fact_opportunity_date_card_side UNIQUE (date_id, card_id, side)
);

CREATE INDEX ix_fact_opportunity_card ON prod.fact_opportunity (card_id);

-- Résultat de chaque recommandation : prix TCGplayer au premier jour chargé
-- dans [J+7, J+10] et [J+30, J+35] (tolérance pour les trous de données).
-- hit_* : un achat a raison si le prix monte, une vente si le prix baisse.
-- NULL tant que l'horizon n'est pas atteint.
CREATE VIEW prod.v_opportunity_outcome AS
SELECT o.opportunity_id, o.date_id, o.card_id, o.side, o.score, o.signals, o.market_price,
       p7.average_sell_price AS price_7d,
       p30.average_sell_price AS price_30d,
       round(p7.average_sell_price / o.market_price - 1, 4) AS return_7d,
       round(p30.average_sell_price / o.market_price - 1, 4) AS return_30d,
       CASE WHEN p7.average_sell_price IS NULL THEN NULL
            WHEN o.side = 'buy' THEN p7.average_sell_price > o.market_price
            ELSE p7.average_sell_price < o.market_price END AS hit_7d,
       CASE WHEN p30.average_sell_price IS NULL THEN NULL
            WHEN o.side = 'buy' THEN p30.average_sell_price > o.market_price
            ELSE p30.average_sell_price < o.market_price END AS hit_30d
FROM prod.fact_opportunity o
LEFT JOIN LATERAL (
    SELECT f.average_sell_price
    FROM prod.fact_price_history f
    JOIN prod.dim_platform pl ON pl.platform_id = f.platform_id AND pl.platform_name = 'tcgplayer'
    WHERE f.card_id = o.card_id AND f.date_id BETWEEN o.date_id + 7 AND o.date_id + 10
    ORDER BY f.date_id
    LIMIT 1
) p7 ON true
LEFT JOIN LATERAL (
    SELECT f.average_sell_price
    FROM prod.fact_price_history f
    JOIN prod.dim_platform pl ON pl.platform_id = f.platform_id AND pl.platform_name = 'tcgplayer'
    WHERE f.card_id = o.card_id AND f.date_id BETWEEN o.date_id + 30 AND o.date_id + 35
    ORDER BY f.date_id
    LIMIT 1
) p30 ON true;

-- Performance par signal : une recommandation à plusieurs signaux compte pour
-- chacun d'eux. avg_edge_* : rendement moyen dans le sens de la
-- recommandation (positif = le signal avait raison en moyenne).
CREATE VIEW prod.v_opportunity_signal_performance AS
SELECT o.side, s.signal,
       count(*) AS n,
       count(o.hit_7d) AS n_7d,
       round(avg(o.hit_7d::int), 3) AS hit_rate_7d,
       round(avg(CASE WHEN o.side = 'buy' THEN o.return_7d ELSE -o.return_7d END), 4) AS avg_edge_7d,
       count(o.hit_30d) AS n_30d,
       round(avg(o.hit_30d::int), 3) AS hit_rate_30d,
       round(avg(CASE WHEN o.side = 'buy' THEN o.return_30d ELSE -o.return_30d END), 4)
           AS avg_edge_30d
FROM prod.v_opportunity_outcome o
CROSS JOIN LATERAL unnest(o.signals) AS s(signal)
GROUP BY o.side, s.signal;

-- pipeline_app : lit le périmètre, écrit les opportunités (jamais DELETE,
-- comme le reste de prod).
GRANT SELECT ON prod.v_opportunity_universe TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON prod.fact_opportunity TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE prod.fact_opportunity_opportunity_id_seq TO pipeline_app;

-- dashboard_reader : déjà couvert par les default privileges de la migration
-- 007, rendu explicite ici pour la lisibilité.
GRANT SELECT ON prod.v_opportunity_universe, prod.fact_opportunity,
    prod.v_opportunity_outcome, prod.v_opportunity_signal_performance TO dashboard_reader;

COMMIT;
