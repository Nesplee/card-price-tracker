-- Migration 018 : prix des cartes ANGLAISES sur le marché européen, lus sur
-- l'API officielle CardTrader (étude ~/mere/rapports/cardmarket-prix-2026-11-17.md).
--
-- Décisions de l'utilisateur (2026-11-18) :
--   - devise : CHF, la devise du compte CardTrader, stockée telle quelle, sans
--     conversion ;
--   - prix retenu : la première offre anglaise, état Near Mint ou mieux (Mint
--     compris), sans repli, d'un vendeur d'Europe ou de Suisse (UE, EEE, CH,
--     Royaume-Uni) ;
--   - en information : la première offre anglaise toutes origines.
--
-- 1. Nouvelle plateforme `cardtrader_en` (devise CHF) dans prod.dim_platform.
--    Son prix du jour va dans prod.fact_price_history, colonne low_price (c'est
--    l'offre la moins chère, pas une moyenne de ventes) ; average_sell_price et
--    trend_price restent NULL. Toutes les lectures de fact_price_history
--    filtrent la plateforme (tcgplayer) : ces lignes n'y changent rien, ce que
--    prouve tests/test_platform_isolation.py.
-- 2. Table de détail prod.fact_cardtrader_price : une ligne par carte et par
--    jour, avec l'offre européenne retenue, l'offre toutes origines, et de quoi
--    juger la fiabilité (nombre d'offres vues, liste tronquée à 25 par l'API).
--    La devise est celle de la plateforme (jointure sur dim_platform).

BEGIN;

INSERT INTO prod.dim_platform (platform_name, currency) VALUES ('cardtrader_en', 'CHF')
ON CONFLICT (platform_name) DO NOTHING;

CREATE TABLE IF NOT EXISTS prod.fact_cardtrader_price (
    card_id              text NOT NULL REFERENCES prod.dim_card (card_id),
    date_id              date NOT NULL REFERENCES prod.dim_date (date_id),
    platform_id          integer NOT NULL REFERENCES prod.dim_platform (platform_id),
    -- Blueprint CardTrader (fiche de la carte) relié par numéro de collection.
    blueprint_id         integer NOT NULL,
    -- Offre retenue : vendeur d'Europe ou de Suisse. NULL si aucune.
    eu_price             numeric(10, 2) CHECK (eu_price > 0),
    eu_condition         text,
    eu_finish            text,
    eu_seller_country    text,
    -- Information : première offre anglaise, toutes origines. NULL si aucune.
    any_price            numeric(10, 2) CHECK (any_price > 0),
    any_condition        text,
    any_finish           text,
    any_seller_country   text,
    -- Offres anglaises renvoyées par l'API pour ce blueprint (les moins
    -- chères ; l'API en renvoie environ 25 au plus).
    en_offers            smallint NOT NULL CHECK (en_offers >= 0),
    -- Liste pleine (25 offres ou plus) : une offre européenne peut exister
    -- plus loin si eu_price est NULL.
    truncated            boolean NOT NULL,
    loaded_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (card_id, date_id, platform_id),
    -- Le prix retenu ne peut pas être sous le premier prix toutes origines.
    CHECK (eu_price IS NULL OR any_price IS NULL OR eu_price >= any_price)
);

CREATE INDEX IF NOT EXISTS ix_fact_cardtrader_price_date
    ON prod.fact_cardtrader_price (date_id);

-- Même modèle que fact_price_history : le pipeline lit, insère et corrige,
-- sans DELETE ; le dashboard lit seulement.
GRANT SELECT, INSERT, UPDATE ON prod.fact_cardtrader_price TO pipeline_app;
GRANT SELECT ON prod.fact_cardtrader_price TO dashboard_reader;

COMMIT;
