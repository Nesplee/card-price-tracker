-- Migration 021 : les seuils de l'utilisateur passent en francs suisses
-- (2026-11-18, dashboard entièrement en CHF).
--
-- prefs.card_setting.min_sell_price et prefs.wishlist_card.target_price sont
-- désormais saisis en CHF :
--   - le prix minimum de vente est comparé à la première offre anglaise Near
--     Mint d'un vendeur d'Europe ou de Suisse (CardTrader, plateforme
--     cardtrader_en), à défaut au prix market TCGplayer converti ;
--   - la cible d'une carte recherchée, de même.
--
-- Garde-fou : aucun seuil n'avait été saisi au moment de la bascule. Si un
-- seuil en USD existe malgré tout, la migration s'arrête plutôt que de le
-- convertir en silence (à un taux de quel jour ?) : à revoir avec
-- l'utilisateur.

BEGIN;

DO $$
DECLARE
    n_sell int;
    n_target int;
BEGIN
    SELECT count(*) INTO n_sell FROM prefs.card_setting
    WHERE min_sell_price IS NOT NULL AND currency <> 'CHF';
    SELECT count(*) INTO n_target FROM prefs.wishlist_card
    WHERE target_price IS NOT NULL AND currency <> 'CHF';
    IF n_sell + n_target > 0 THEN
        RAISE EXCEPTION 'Migration 021 : % prix minimum de vente et % prix cibles saisis en USD. '
            'Rien n''est converti automatiquement : les revoir avec l''utilisateur.',
            n_sell, n_target;
    END IF;
END $$;

ALTER TABLE prefs.card_setting DROP CONSTRAINT card_setting_currency_check;
UPDATE prefs.card_setting SET currency = 'CHF' WHERE currency <> 'CHF';
ALTER TABLE prefs.card_setting
    ALTER COLUMN currency SET DEFAULT 'CHF',
    ADD CONSTRAINT card_setting_currency_check CHECK (currency = 'CHF');

ALTER TABLE prefs.wishlist_card DROP CONSTRAINT wishlist_card_currency_check;
UPDATE prefs.wishlist_card SET currency = 'CHF' WHERE currency <> 'CHF';
ALTER TABLE prefs.wishlist_card
    ALTER COLUMN currency SET DEFAULT 'CHF',
    ADD CONSTRAINT wishlist_card_currency_check CHECK (currency = 'CHF');

COMMIT;
