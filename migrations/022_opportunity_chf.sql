-- Migration 022 : signaux d'achat / de vente et backtest en francs suisses
-- (2026-11-18, dashboard entièrement en CHF).
--
-- Depuis cette version, src/signals/opportunities.py calcule les signaux sur
-- les prix TCGplayer convertis en CHF au taux de chaque jour (vue
-- prod.v_tcgplayer_chf, migration 020) : prod.fact_opportunity est en CHF.
--
-- 1. Deux colonnes :
--    - eu_price : première offre anglaise Near Mint d'un vendeur d'Europe ou
--      de Suisse (CardTrader, CHF) connue ce jour-là, 3 jours au plus. Le
--      prix minimum de vente se compare à coalesce(eu_price, market_price),
--      au calcul comme à la lecture (RULES_FILTER, src/api/queries.py) ;
--    - usd_chf : renseigné seulement sur les lignes calculées en USD avant
--      cette migration, et converties ici au taux de leur jour. Leur texte
--      (reason) reste en dollars, tel qu'il a été écrit. Un recalcul du jour
--      (scripts/backfill_opportunities.py) remet usd_chf à NULL.
-- 2. Les lignes existantes : market_price, low_price et ma30 convertis au taux
--    de leur jour (règle de la migration 020). Les variations et la
--    volatilité sont des rapports : inchangées. Garde-fou : arrêt si un jour
--    n'a pas de taux, plutôt qu'une ligne à moitié convertie.
-- 3. Backtest (v_opportunity_outcome, v_opportunity_signal_performance) :
--    prix à J+7 et J+30 lus en CHF. Les rendements incluent donc le change,
--    c'est-à-dire ce que l'utilisateur gagne ou perd en francs.

BEGIN;

DO $$
DECLARE
    missing int;
BEGIN
    -- Une seule fois : un second passage reconvertirait des prix déjà en CHF.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'prod' AND table_name = 'fact_opportunity'
          AND column_name = 'usd_chf'
    ) THEN
        RAISE NOTICE 'Migration 022 déjà appliquée : rien à convertir.';
        RETURN;
    END IF;

    SELECT count(*) INTO missing
    FROM prod.fact_opportunity o
    LEFT JOIN prod.mv_usd_chf x ON x.date_id = o.date_id
    WHERE x.usd_chf IS NULL;
    IF missing > 0 THEN
        RAISE EXCEPTION 'Migration 022 : % signaux sans taux BCE pour leur jour '
            '(prod.fact_fx_rate incomplète ?). Rien n''est converti.', missing;
    END IF;

    ALTER TABLE prod.fact_opportunity
        ADD COLUMN eu_price numeric(10, 2) CHECK (eu_price > 0),
        ADD COLUMN usd_chf numeric(12, 6) CHECK (usd_chf > 0);

    UPDATE prod.fact_opportunity o
    SET market_price = round(o.market_price * x.usd_chf, 2),
        low_price = round(o.low_price * x.usd_chf, 2),
        ma30 = round(o.ma30 * x.usd_chf, 2),
        usd_chf = x.usd_chf
    FROM prod.mv_usd_chf x
    WHERE x.date_id = o.date_id;
END $$;

DROP VIEW IF EXISTS prod.v_opportunity_signal_performance;
DROP VIEW IF EXISTS prod.v_opportunity_outcome;

-- Résultat de chaque recommandation : prix TCGplayer converti en CHF au
-- premier jour chargé dans [J+7, J+10] et [J+30, J+35] (tolérance pour les
-- trous de données). hit_* : un achat a raison si le prix monte, une vente si
-- le prix baisse. NULL tant que l'horizon n'est pas atteint.
-- Prix de départ : le market converti NON ARRONDI du jour du signal (p0), pas
-- market_price (arrondi au centime après conversion). Sinon une carte au prix
-- inchangé en dollars, convertie au même taux, finirait « en hausse » ou « en
-- baisse » d'une fraction de centime, au hasard de l'arrondi. market_price
-- sert de repli si ce prix manque.
CREATE VIEW prod.v_opportunity_outcome AS
SELECT o.opportunity_id, o.date_id, o.card_id, o.side, o.score, o.signals, o.market_price,
       p7.market_chf AS price_7d,
       p30.market_chf AS price_30d,
       round(p7.market_chf / b.price - 1, 4) AS return_7d,
       round(p30.market_chf / b.price - 1, 4) AS return_30d,
       CASE WHEN p7.market_chf IS NULL THEN NULL
            WHEN o.side = 'buy' THEN p7.market_chf > b.price
            ELSE p7.market_chf < b.price END AS hit_7d,
       CASE WHEN p30.market_chf IS NULL THEN NULL
            WHEN o.side = 'buy' THEN p30.market_chf > b.price
            ELSE p30.market_chf < b.price END AS hit_30d
FROM prod.fact_opportunity o
LEFT JOIN LATERAL (
    SELECT t.market_chf
    FROM prod.v_tcgplayer_chf t
    WHERE t.card_id = o.card_id AND t.date_id = o.date_id
    -- LIMIT : garde une recherche par index ; sans lui, le planificateur
    -- aplatit la sous-requête en jointure sur toute la table (2 s au lieu de
    -- 0,3 s sur la copie de prod).
    LIMIT 1
) p0 ON true
CROSS JOIN LATERAL (SELECT coalesce(p0.market_chf, o.market_price) AS price) b
LEFT JOIN LATERAL (
    SELECT t.market_chf
    FROM prod.v_tcgplayer_chf t
    WHERE t.card_id = o.card_id AND t.date_id BETWEEN o.date_id + 7 AND o.date_id + 10
    ORDER BY t.date_id
    LIMIT 1
) p7 ON true
LEFT JOIN LATERAL (
    SELECT t.market_chf
    FROM prod.v_tcgplayer_chf t
    WHERE t.card_id = o.card_id AND t.date_id BETWEEN o.date_id + 30 AND o.date_id + 35
    ORDER BY t.date_id
    LIMIT 1
) p30 ON true;

-- Performance par signal (inchangée, sur la vue en CHF) : une recommandation
-- à plusieurs signaux compte pour chacun d'eux. avg_edge_* : rendement moyen
-- dans le sens de la recommandation.
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

GRANT SELECT ON prod.v_opportunity_outcome, prod.v_opportunity_signal_performance
    TO dashboard_reader, pipeline_app;

COMMIT;
