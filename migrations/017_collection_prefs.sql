-- Migration 017 : réglages de la collection, cartes recherchées et import de
-- la collection depuis le board (2026-11-17).
--
-- Jusqu'ici le dashboard ne faisait que lire (rôle dashboard_reader, migration
-- 007). Les réglages saisis dans le board vivent dans un schéma à part,
-- `prefs`, le seul où dashboard_reader peut écrire : prod, raw, staging et
-- collection restent en lecture seule pour lui. L'import d'un export de
-- collection passe par une seule fonction SECURITY DEFINER
-- (collection.apply_import), sur le modèle de raw.purge_oldest_day (migration
-- 016) : le dashboard ne reçoit aucun droit direct sur les tables de la
-- collection.
--
-- Règles des signaux (appliquées par src/signals/opportunities.py au calcul et
-- par l'API à la lecture, via la vue prefs.v_card_rules) :
--   - achat : jamais sur une carte possédée (tout exemplaire, toute variante,
--     gradée comprise), sauf si elle est dans une liste de cartes recherchées ;
--   - vente : jamais sur une carte marquée invendable ; jamais sur une carte
--     d'un set « masterset », sauf ses doublons (au moins 2 exemplaires bruts
--     normal / holo, la variante dont on suit le prix) d'une rareté au-dessus
--     de Common / Uncommon / Rare ; jamais tant que le prix market est sous le
--     prix minimum de vente de la carte ;
--   - « ne plus suivre » : aucun signal, ni achat ni vente.

BEGIN;

CREATE SCHEMA IF NOT EXISTS prefs;

-- Réglages par carte. Une ligne n'existe que si l'utilisateur a touché à la
-- carte ; l'absence de ligne vaut les valeurs par défaut.
-- currency : prix TCGplayer en USD pour l'instant. EUR viendra avec une source
-- Cardmarket (étude en cours) : la contrainte sera alors élargie.
CREATE TABLE prefs.card_setting (
    card_id         text PRIMARY KEY REFERENCES prod.dim_card(card_id),
    unsellable      boolean NOT NULL DEFAULT false,
    unfollowed      boolean NOT NULL DEFAULT false,
    min_sell_price  numeric(10, 2) CHECK (min_sell_price > 0),
    currency        text NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Réglages par set (set_id de prod.dim_card ; pas de table des sets, d'où
-- l'absence de clé étrangère, vérifiée par l'API).
CREATE TABLE prefs.set_setting (
    set_id      text PRIMARY KEY,
    masterset   boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Listes de cartes recherchées (à acheter). Plusieurs listes possibles, une
-- seule affichée au départ (« Recherchées », créée ici).
CREATE TABLE prefs.wishlist (
    list_id     serial PRIMARY KEY,
    name        text NOT NULL UNIQUE CHECK (length(btrim(name)) BETWEEN 1 AND 60),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE prefs.wishlist_card (
    list_id       integer NOT NULL REFERENCES prefs.wishlist(list_id) ON DELETE CASCADE,
    card_id       text NOT NULL REFERENCES prod.dim_card(card_id),
    target_price  numeric(10, 2) CHECK (target_price > 0),
    currency      text NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    note          text CHECK (length(note) <= 200),
    added_at      timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (list_id, card_id)
);

CREATE INDEX ix_wishlist_card_card ON prefs.wishlist_card (card_id);

INSERT INTO prefs.wishlist (name) VALUES ('Recherchées');
-- Mastersets de départ (choix de l'utilisateur, le 2026-11-17) : les deux
-- sets complets. Modifiables ensuite depuis le board.
INSERT INTO prefs.set_setting (set_id, masterset) VALUES ('sv2', true), ('sv4pt5', true);

-- Une ligne par carte concernée par une règle : possédée, réglée ou
-- recherchée. Les cartes absentes n'ont aucune restriction.
--   buy_blocked  : pas de signal d'achat ;
--   sell_blocked : pas de signal de vente (le seuil min_sell_price, lui,
--                  dépend du prix du jour et s'applique à part).
CREATE VIEW prefs.v_card_rules AS
WITH owned AS (
    SELECT card_id,
           sum(quantity)::int AS owned_quantity,
           -- Exemplaires bruts de la variante suivie (normal / holo) : ceux
           -- qui comptent pour les doublons d'un masterset.
           coalesce(sum(quantity) FILTER (
               WHERE grade IN ('', 'Ungraded') AND variance IN ('', 'Normal', 'Holofoil')
           ), 0)::int AS tracked_quantity
    FROM prod.dim_owned_card
    WHERE quantity > 0
    GROUP BY card_id
), wish AS (
    SELECT card_id, min(target_price) AS target_price
    FROM prefs.wishlist_card
    GROUP BY card_id
), ids AS (
    SELECT card_id FROM owned
    UNION SELECT card_id FROM prefs.card_setting
    UNION SELECT card_id FROM wish
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

-- Cartes retirées de la collection lors d'un import (absentes du nouvel
-- export, retrait choisi ligne par ligne par l'utilisateur) : la ligne quitte
-- prod.dim_owned_card mais reste archivée ici, rien n'est perdu.
CREATE TABLE collection.owned_card_removed (
    id                  bigserial PRIMARY KEY,
    card_id             text NOT NULL,
    variance            text NOT NULL,
    grade               text NOT NULL,
    quantity            integer NOT NULL,
    average_cost_paid   numeric(10, 2),
    imported_at         timestamptz NOT NULL,
    removed_at          timestamptz NOT NULL DEFAULT now()
);

-- Import d'un export de collection validé dans le board. Même effet que
-- scripts/import_collection.py (src/load/collection_loader.py), en une seule
-- transaction : upsert des lignes de l'export dans collection.raw_import,
-- upsert des cartes reliées dans prod.dim_owned_card, quarantaine des lignes
-- non reliées, puis retrait (archivé) des seules cartes que l'utilisateur a
-- choisi de retirer. Idempotent : rejouer le même import ne change rien.
-- p_import : {"rows": [{set_name, card_number, product_name, variance, grade,
--             rarity, quantity, average_cost_paid, market_price_at_export,
--             card_id | null, rejection_reason | null}],
--            "owned": [{card_id, variance, grade, quantity, average_cost_paid}],
--            "remove": [{card_id, variance, grade}]}
-- Le rapprochement (card_id) est fait par l'API avec les mêmes fonctions que
-- le script ; « owned » est déjà agrégé par (card_id, variance, grade).
CREATE OR REPLACE FUNCTION collection.apply_import(p_import jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
-- search_path figé : une fonction SECURITY DEFINER ne doit jamais résoudre un
-- nom dans un schéma que l'appelant contrôle.
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_raw int := 0;
    v_owned int := 0;
    v_quarantine int := 0;
    v_removed int := 0;
    v_rows int;
BEGIN
    IF jsonb_typeof(p_import -> 'rows') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_import -> 'owned') IS DISTINCT FROM 'array'
       OR jsonb_typeof(coalesce(p_import -> 'remove', '[]'::jsonb)) <> 'array' THEN
        RAISE EXCEPTION 'apply_import : rows, owned et remove doivent être des tableaux';
    END IF;
    -- Garde-fou contre un fichier vide ou tronqué : il faut au moins une carte.
    IF jsonb_array_length(p_import -> 'owned') = 0 THEN
        RAISE EXCEPTION 'apply_import : aucune carte reliée au catalogue, import refusé';
    END IF;

    DROP TABLE IF EXISTS _imp;
    CREATE TEMP TABLE _imp ON COMMIT DROP AS
    SELECT * FROM jsonb_to_recordset(p_import -> 'rows') AS r(
        set_name text, card_number text, product_name text, variance text, grade text,
        rarity text, quantity integer, average_cost_paid numeric, market_price_at_export numeric,
        card_id text, rejection_reason text);

    IF EXISTS (SELECT 1 FROM _imp WHERE quantity IS NULL OR quantity <= 0
                                     OR set_name IS NULL OR card_number IS NULL
                                     OR product_name IS NULL) THEN
        RAISE EXCEPTION 'apply_import : ligne incomplète ou quantité <= 0';
    END IF;

    INSERT INTO collection.raw_import AS t
        (set_name, card_number, product_name, variance, grade, rarity, quantity,
         average_cost_paid, market_price_at_export)
    SELECT set_name, card_number, product_name, coalesce(variance, ''), coalesce(grade, ''),
           coalesce(rarity, ''), quantity, average_cost_paid, market_price_at_export
    FROM _imp
    ON CONFLICT (set_name, card_number, product_name, variance, grade) DO UPDATE SET
        rarity = EXCLUDED.rarity,
        quantity = EXCLUDED.quantity,
        average_cost_paid = EXCLUDED.average_cost_paid,
        market_price_at_export = EXCLUDED.market_price_at_export,
        imported_at = now();
    GET DIAGNOSTICS v_raw = ROW_COUNT;

    INSERT INTO prod.dim_owned_card AS t (card_id, variance, grade, quantity, average_cost_paid)
    SELECT card_id, coalesce(variance, ''), coalesce(grade, ''), quantity, average_cost_paid
    FROM jsonb_to_recordset(p_import -> 'owned') AS o(
        card_id text, variance text, grade text, quantity integer, average_cost_paid numeric)
    ON CONFLICT (card_id, variance, grade) DO UPDATE SET
        quantity = EXCLUDED.quantity,
        average_cost_paid = EXCLUDED.average_cost_paid,
        imported_at = now();
    GET DIAGNOSTICS v_owned = ROW_COUNT;

    INSERT INTO collection.match_quarantine (raw_import_id, rejection_reason)
    SELECT r.id, i.rejection_reason
    FROM _imp i
    JOIN collection.raw_import r
      ON (r.set_name, r.card_number, r.product_name, r.variance, r.grade)
       = (i.set_name, i.card_number, i.product_name, coalesce(i.variance, ''),
          coalesce(i.grade, ''))
    WHERE i.card_id IS NULL
    ON CONFLICT (raw_import_id) DO UPDATE SET
        rejection_reason = EXCLUDED.rejection_reason,
        imported_at = now();
    GET DIAGNOSTICS v_quarantine = ROW_COUNT;

    -- Retraits : seulement des cartes absentes de l'export (jamais une carte
    -- que l'export vient de charger), archivées avant d'être retirées.
    WITH rm AS (
        SELECT DISTINCT x.card_id, coalesce(x.variance, '') AS variance,
               coalesce(x.grade, '') AS grade
        FROM jsonb_to_recordset(coalesce(p_import -> 'remove', '[]'::jsonb))
             AS x(card_id text, variance text, grade text)
        EXCEPT
        SELECT card_id, coalesce(variance, ''), coalesce(grade, '')
        FROM jsonb_to_recordset(p_import -> 'owned')
             AS o(card_id text, variance text, grade text)
    ), gone AS (
        DELETE FROM prod.dim_owned_card d
        USING rm
        WHERE (d.card_id, d.variance, d.grade) = (rm.card_id, rm.variance, rm.grade)
        RETURNING d.card_id, d.variance, d.grade, d.quantity, d.average_cost_paid, d.imported_at
    )
    INSERT INTO collection.owned_card_removed
        (card_id, variance, grade, quantity, average_cost_paid, imported_at)
    SELECT * FROM gone;
    GET DIAGNOSTICS v_removed = ROW_COUNT;

    RETURN jsonb_build_object('raw', v_raw, 'owned', v_owned,
                              'quarantine', v_quarantine, 'removed', v_removed);
END;
$$;

REVOKE ALL ON FUNCTION collection.apply_import(jsonb) FROM PUBLIC;
GRANT USAGE ON SCHEMA collection TO dashboard_reader;
GRANT EXECUTE ON FUNCTION collection.apply_import(jsonb) TO dashboard_reader;

-- Droits : dashboard_reader lit et écrit les réglages (et seulement eux) ;
-- pipeline_app (calcul des signaux) les lit.
GRANT USAGE ON SCHEMA prefs TO dashboard_reader, pipeline_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON prefs.card_setting, prefs.set_setting,
    prefs.wishlist, prefs.wishlist_card TO dashboard_reader;
GRANT USAGE, SELECT ON SEQUENCE prefs.wishlist_list_id_seq TO dashboard_reader;
GRANT SELECT ON prefs.card_setting, prefs.set_setting, prefs.wishlist,
    prefs.wishlist_card, prefs.v_card_rules TO pipeline_app;
GRANT SELECT ON prefs.v_card_rules TO dashboard_reader;
GRANT SELECT ON collection.owned_card_removed TO pipeline_app;

COMMIT;
