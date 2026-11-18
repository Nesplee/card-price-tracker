-- Migration 023 : offre NM Europe retenue pour le dashboard en CHF, sans les
-- offres aberrantes (2026-11-18).
--
-- prod.fact_cardtrader_price.eu_price est la première offre anglaise Near Mint
-- d'un vendeur d'Europe ou de Suisse (migration 018). Quand une carte n'a que
-- peu d'offres en Europe, cette « première offre » peut être une annonce qui
-- n'est pas faite pour vendre : le 18/11, onze cartes à 9 494,94 ou 9 695,27
-- CHF (un même genre d'annonce italienne), pour une vente moyenne de 6 à 320
-- CHF. Affichée, elle doublait la valeur « au prix Europe » de la collection ;
-- comparée au prix minimum de vente, elle aurait laissé passer un signal de
-- vente.
--
-- Règle : une offre est écartée si elle dépasse à la fois 3 fois la vente
-- moyenne TCGplayer convertie ET cette vente moyenne de plus de 10 CHF (une
-- commune à 0,30 CHF contre 0,05 de vente moyenne reste un vrai prix). Vente
-- moyenne de référence : la plus récente des 7 jours jusqu'au jour de l'offre
-- (un jour sans relevé TCGplayer ne laisse pas passer une offre aberrante).
-- Le 18/11 : 44 offres écartées sur 5 473, dont 4 cartes possédées. Sans
-- aucune vente moyenne sur 7 jours, l'offre est gardée (rien pour en juger).
-- La carte retombe alors sur le prix « ≈ » (vente moyenne convertie).
--
-- Les données brutes ne changent pas : la règle est appliquée à la lecture,
-- par cette vue, que lisent le board, l'API et les signaux.

BEGIN;

CREATE OR REPLACE VIEW prod.v_cardtrader_eu AS
SELECT c.card_id, c.date_id, c.eu_price, c.eu_condition, c.eu_seller_country, c.eu_finish,
       t.market_chf,
       NOT coalesce(c.eu_price > 3 * t.market_chf AND c.eu_price - t.market_chf > 10, false)
           AS plausible
FROM prod.fact_cardtrader_price c
LEFT JOIN LATERAL (
    SELECT v.market_chf
    FROM prod.v_tcgplayer_chf v
    WHERE v.card_id = c.card_id AND v.date_id BETWEEN c.date_id - 7 AND c.date_id
      AND v.market_chf IS NOT NULL
    ORDER BY v.date_id DESC
    LIMIT 1
) t ON true
WHERE c.eu_price IS NOT NULL;

GRANT SELECT ON prod.v_cardtrader_eu TO pipeline_app, dashboard_reader;

COMMIT;
