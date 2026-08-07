-- Migration 005 : ajoute "tcgplayer" comme plateforme de prix disponible dans
-- prod.dim_platform, en plus de "cardmarket" (seedée en migration 003).
--
-- Contexte (voir docs/superpowers/specs/2026-08-07-tcgplayer-pricing-source-design.md) :
-- les prix CardMarket relayés par pokemontcg.io sont agrégés toutes langues
-- confondues (anglais, allemand, japonais...), une limitation de Cardmarket
-- lui-même (leur "price guide" ne distingue ni langue ni état de la carte).
-- TCGPlayer, déjà présent dans le même payload pokemontcg.io, sépare les
-- cartes japonaises dans une ligne de produit distincte ("Pokemon Japan") :
-- les prix TCGPlayer pour un card_id pokemontcg.io (catalogue anglais
-- uniquement) représentent donc déjà spécifiquement le marché anglais.
--
-- On ne supprime PAS la ligne "cardmarket" ni les données déjà chargées sous
-- cette plateforme (prod.fact_price_history interdit tout DELETE, voir
-- migrations/003_create_star_schema.sql) : les deux plateformes coexistent,
-- "tcgplayer" devient simplement la nouvelle valeur par défaut côté pipeline
-- (voir src/load/warehouse_loader.py).

BEGIN;

INSERT INTO prod.dim_platform (platform_name) VALUES ('tcgplayer')
ON CONFLICT (platform_name) DO NOTHING;

COMMIT;
