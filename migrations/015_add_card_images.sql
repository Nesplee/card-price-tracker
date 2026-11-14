-- Migration 015 : URL d'image de carte pour le dashboard (affichage en
-- hotlink, aucune image n'est téléchargée ni stockée chez nous).
--
-- image_small : miniature fournie par la source du catalogue
--   (raw.card_prices.payload->'images'->>'small'). Déduire l'URL du card_id est
--   faux pour ~1 000 cartes (cel25c-15_A1, ex10-?) et pour les sets récents
--   dont l'image est sur images.scrydex.com : on stocke donc l'URL réelle.
-- image_alt : repli = miniature du CDN TCGplayer, via raw.tcgplayer_product_map
--   (https://tcgplayer-cdn.tcgplayer.com/product/<productId>_200w.jpg, format
--   documenté par TCGCSV pour son champ imageUrl). Sert aux cartes sans
--   image_small et, côté frontend, si image_small ne charge plus
--   (pokemontcg.io est déclarée obsolète, clés valables jusqu'au 2027-03-01).

BEGIN;

ALTER TABLE staging.card_prices ADD COLUMN IF NOT EXISTS image_small text;
ALTER TABLE prod.dim_card ADD COLUMN IF NOT EXISTS image_small text;
ALTER TABLE prod.dim_card ADD COLUMN IF NOT EXISTS image_alt text;

UPDATE prod.dim_card dc
SET image_small = nullif(sub.small, '')
FROM (
    SELECT DISTINCT ON (payload->>'id')
        payload->>'id' AS card_id,
        payload->'images'->>'small' AS small
    FROM raw.card_prices
    WHERE source = 'pokemontcg.io'
    ORDER BY payload->>'id', extracted_date DESC
) sub
WHERE dc.card_id = sub.card_id;

UPDATE prod.dim_card dc
SET image_alt = 'https://tcgplayer-cdn.tcgplayer.com/product/' || m.product_id || '_200w.jpg'
FROM raw.tcgplayer_product_map m
WHERE m.card_id = dc.card_id;

COMMIT;
