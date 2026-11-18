-- Migration 019 : plusieurs listes de cartes recherchées, cartes « obtenues »
-- (2026-11-18).
--
-- Le schéma de 017 gérait déjà plusieurs listes (prefs.wishlist, clé
-- (list_id, card_id) dans prefs.wishlist_card) : cette migration n'ajoute que
-- la règle « obtenue ». La liste « Recherchées » et ses cartes restent
-- intactes.
--
-- Règle : une carte d'une liste que l'utilisateur possède (au moins un
-- exemplaire, toute variante, gradée comprise : prod.dim_owned_card) est
-- « obtenue » pour cette liste. Elle y reste (progression), mais ne compte
-- plus comme recherchée ni ne déclenche de signal d'achat, sauf si la case
-- « encore recherchée » (still_wanted) de sa ligne est cochée : pour qui veut
-- un deuxième exemplaire. Une carte possédée n'est donc « recherchée » que si
-- au moins une de ses lignes a cette case ; une carte non possédée l'est dès
-- qu'elle figure dans une liste.
--
-- Plusieurs listes : la cible de prix de chaque ligne est propre à sa liste ;
-- le signal d'achat retient la plus basse des cibles des lignes actives
-- (non obtenues, ou encore recherchées).

BEGIN;

ALTER TABLE prefs.wishlist_card
    ADD COLUMN still_wanted boolean NOT NULL DEFAULT false;

-- Même colonnes, même ordre que 017 : CREATE OR REPLACE VIEW suffit, seules
-- changent les cartes « recherchées » (CTE wish) et leur cible.
CREATE OR REPLACE VIEW prefs.v_card_rules AS
WITH owned AS (
    SELECT card_id,
           sum(quantity)::int AS owned_quantity,
           coalesce(sum(quantity) FILTER (
               WHERE grade IN ('', 'Ungraded') AND variance IN ('', 'Normal', 'Holofoil')
           ), 0)::int AS tracked_quantity
    FROM prod.dim_owned_card
    WHERE quantity > 0
    GROUP BY card_id
), wish AS (
    -- Lignes actives seulement : carte non possédée, ou « encore recherchée ».
    SELECT w.card_id, min(w.target_price) AS target_price
    FROM prefs.wishlist_card w
    LEFT JOIN owned o ON o.card_id = w.card_id
    WHERE o.card_id IS NULL OR w.still_wanted
    GROUP BY w.card_id
), ids AS (
    SELECT card_id FROM owned
    UNION SELECT card_id FROM prefs.card_setting
    UNION SELECT card_id FROM prefs.wishlist_card
)
SELECT ids.card_id,
       coalesce(o.owned_quantity, 0) AS owned_quantity,
       coalesce(o.tracked_quantity, 0) AS tracked_quantity,
       coalesce(cs.unsellable, false) AS unsellable,
       coalesce(cs.unfollowed, false) AS unfollowed,
       cs.min_sell_price,
       coalesce(ss.masterset, false) AS in_masterset,
       w.card_id IS NOT NULL AS wishlisted,
       w.target_price,
       coalesce(cs.unfollowed, false)
           OR (o.card_id IS NOT NULL AND w.card_id IS NULL) AS buy_blocked,
       coalesce(cs.unfollowed, false)
           OR coalesce(cs.unsellable, false)
           OR (coalesce(ss.masterset, false)
               AND NOT (coalesce(o.tracked_quantity, 0) >= 2
                        AND coalesce(c.rarity, '') NOT IN
                            ('', 'Common', 'Uncommon', 'Rare', 'Rare Holo'))) AS sell_blocked
FROM ids
JOIN prod.dim_card c ON c.card_id = ids.card_id
LEFT JOIN owned o ON o.card_id = ids.card_id
LEFT JOIN prefs.card_setting cs ON cs.card_id = ids.card_id
LEFT JOIN prefs.set_setting ss ON ss.set_id = c.set_id
LEFT JOIN wish w ON w.card_id = ids.card_id;

-- Droits : la table a déjà SELECT/INSERT/UPDATE/DELETE pour dashboard_reader
-- (017) ; la nouvelle colonne en hérite. Renommer une liste (UPDATE) et la
-- supprimer (DELETE, cartes en cascade) étaient déjà permis.

COMMIT;
