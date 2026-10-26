-- Migration 011 : table de correspondance carte pokemontcg.io -> produit TCGplayer.
--
-- Contexte (2026-10-26) : pokemontcg.io ne rafraîchit plus ses prix TCGplayer
-- depuis le 2026-10-15 (tcgplayer.updatedAt figé). Les prix sont désormais lus
-- sur TCGCSV (https://tcgcsv.com, copie quotidienne des prix TCGplayer), indexés
-- par productId TCGplayer. pokemontcg.io ne donne pas ce productId directement,
-- mais son lien tcgplayer.url (https://prices.pokemontcg.io/tcgplayer/<card_id>)
-- redirige vers https://tcgplayer.com/product/<productId>. Résoudre ~20 000
-- redirections chaque jour serait lent et fragile : on les résout UNE fois et on
-- les garde ici (voir src/extract/tcgcsv_prices.py), seules les nouvelles cartes
-- sont résolues lors des runs suivants.
CREATE TABLE IF NOT EXISTS raw.tcgplayer_product_map (
    card_id     TEXT PRIMARY KEY,
    product_id  BIGINT NOT NULL,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON raw.tcgplayer_product_map TO pipeline_app;
