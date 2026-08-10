-- Migration 009 : ajoute la colonne series (le "bloc" Pokémon TCG, ex:
-- "Scarlet & Violet" regroupe les sets "Paldea Evolved", "Obsidian Flames"...)
-- à staging.card_prices et prod.dim_card, avec backfill immédiat de
-- dim_card pour toutes les cartes déjà connues. Voir
-- docs/superpowers/specs/2026-08-10-interactive-dashboard-design.md.
--
-- Aucun nouvel appel API nécessaire : series est déjà présent dans
-- raw.card_prices.payload->'set'->'series' depuis le Mois 1 (vérifié
-- directement sur les données de production avant d'écrire cette
-- migration) -- le backfill relit simplement ce qui est déjà stocké.

BEGIN;

ALTER TABLE staging.card_prices ADD COLUMN IF NOT EXISTS series text;
ALTER TABLE prod.dim_card ADD COLUMN IF NOT EXISTS series text;

-- Backfill : pour chaque carte déjà connue de dim_card, retrouve son
-- payload le plus récent dans raw.card_prices et en extrait series.
-- DISTINCT ON (payload->>'id') ... ORDER BY payload->>'id', extracted_date
-- DESC : si plusieurs payloads existent pour la même carte (plusieurs
-- jours d'extraction), on ne garde que le plus récent -- pas besoin de
-- les agréger, series ne change normalement jamais pour une carte donnée.
UPDATE prod.dim_card dc
SET series = sub.series
FROM (
    SELECT DISTINCT ON (payload->>'id')
        payload->>'id' AS card_id,
        payload->'set'->>'series' AS series
    FROM raw.card_prices
    WHERE source = 'pokemontcg.io'
    ORDER BY payload->>'id', extracted_date DESC
) sub
WHERE dc.card_id = sub.card_id;

COMMIT;
