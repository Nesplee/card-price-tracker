-- Migration 006 : ajoute une colonne currency à prod.dim_platform.
--
-- Contexte (voir docs/superpowers/specs/2026-08-07-tcgplayer-pricing-source-design.md
-- et la review finale du plan associé) : depuis la bascule CardMarket -> TCGPlayer
-- (migration 005), prod.fact_price_history contient DEUX plateformes cote a cote --
-- "cardmarket" (prix historiques en EUR, jamais supprimes) et "tcgplayer" (prix en
-- USD, nouveaux) -- dans les MEMES colonnes generiques (average_sell_price,
-- trend_price, low_price). Sans indication de devise stockee quelque part, toute
-- requete future qui agregerait fact_price_history sans filtrer explicitement par
-- plateforme melangerait silencieusement des euros et des dollars comme s'ils
-- etaient dans la meme unite -- un risque concret, pas theorique, puisque le
-- dashboard (Task 4, stretch, Mois 3) va justement agreger ces donnees.
--
-- On stocke la devise sur dim_platform (une ligne par plateforme, jamais par
-- observation de prix) plutot que de dupliquer une colonne currency sur chaque
-- ligne de fact_price_history : la devise est un attribut de LA PLATEFORME, pas de
-- l'observation elle-meme, coherent avec le principe du schema en etoile deja en
-- place (dim_card/dim_date : attributs stables factorises une seule fois).

BEGIN;

-- Nullable d'abord (table non vide, ADD COLUMN ... NOT NULL exigerait une valeur
-- par defaut immediate) : on remplit les valeurs connues juste apres, puis on
-- verrouille la contrainte NOT NULL dans la meme transaction -- aucune plateforme
-- ne doit pouvoir exister sans devise definie, y compris les futures.
ALTER TABLE prod.dim_platform ADD COLUMN IF NOT EXISTS currency text;

UPDATE prod.dim_platform SET currency = 'EUR' WHERE platform_name = 'cardmarket' AND currency IS NULL;
UPDATE prod.dim_platform SET currency = 'USD' WHERE platform_name = 'tcgplayer' AND currency IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'prod.dim_platform'::regclass
          AND attname = 'currency'
          AND attnotnull
    ) THEN
        ALTER TABLE prod.dim_platform ALTER COLUMN currency SET NOT NULL;
    END IF;
END
$$;

COMMIT;
