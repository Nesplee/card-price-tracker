-- Migration 008 : schéma pour l'import de la collection personnelle de
-- l'utilisateur (CSV externe, jamais commité -- voir .gitignore), séparé de
-- raw/staging/prod du pipeline de prix : domaine différent ("ce que
-- l'utilisateur possède" vs "quels sont les prix du marché"). Voir
-- docs/superpowers/specs/2026-08-09-personal-collection-import-design.md.

BEGIN;

CREATE SCHEMA IF NOT EXISTS collection;

-- collection.raw_import : copie des lignes du CSV qui ont passé les filtres
-- d'import (Category=Pokemon, Portfolio Name=Main, Rarity hors
-- Common/Uncommon/vide) -- AVANT tentative de rapprochement avec le
-- catalogue. Permet de rejouer la logique de matching plus tard (ex: une
-- fois une table de correspondance de sets améliorée) sans avoir besoin de
-- ré-uploader le CSV original.
--
-- variance/grade en NOT NULL DEFAULT '' (pas nullable) : Postgres ne
-- considère JAMAIS deux NULL comme égaux dans une contrainte UNIQUE -- si
-- ces colonnes étaient nullables, deux imports successifs d'une carte sans
-- variante/grade renseignés créeraient deux lignes distinctes au lieu de
-- mettre à jour la même, cassant l'idempotence du réimport.
CREATE TABLE collection.raw_import (
    id                      bigserial PRIMARY KEY,
    set_name                text NOT NULL,
    card_number             text NOT NULL,
    product_name            text NOT NULL,
    variance                text NOT NULL DEFAULT '',
    grade                   text NOT NULL DEFAULT '',
    rarity                  text NOT NULL,
    quantity                integer NOT NULL,
    average_cost_paid       numeric(10, 2),
    market_price_at_export  numeric(10, 2),
    imported_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_collection_raw_import_natural_key
        UNIQUE (set_name, card_number, product_name, variance, grade)
);

-- collection.match_quarantine : lignes de collection.raw_import pour
-- lesquelles le rapprochement avec prod.dim_card a échoué (set non reconnu,
-- ou card_id construit absent du catalogue). raw_import_id référence la
-- ligne d'origine -- pas de duplication du contenu ici, juste la raison.
CREATE TABLE collection.match_quarantine (
    id              bigserial PRIMARY KEY,
    raw_import_id   bigint NOT NULL REFERENCES collection.raw_import(id),
    rejection_reason  text NOT NULL,
    imported_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_collection_match_quarantine_raw_import_id UNIQUE (raw_import_id)
);

-- prod.dim_owned_card : cartes de la collection personnelle RECONNUES dans
-- le catalogue -- prête à être jointe à prod.fact_price_history pour le
-- futur dashboard (valeur de la collection = quantity * prix courant).
-- card_id REFERENCES prod.dim_card : ne peut pas exister sans une carte
-- déjà connue du catalogue de prix.
CREATE TABLE prod.dim_owned_card (
    id                  bigserial PRIMARY KEY,
    card_id             text NOT NULL REFERENCES prod.dim_card(card_id),
    variance            text NOT NULL DEFAULT '',
    grade               text NOT NULL DEFAULT '',
    quantity            integer NOT NULL,
    average_cost_paid   numeric(10, 2),
    imported_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_dim_owned_card_natural_key UNIQUE (card_id, variance, grade)
);

-- Permissions pipeline_app : le script d'import (scripts/import_collection.py,
-- Task 5) se connecte avec les identifiants pipeline_app, comme
-- scripts/run_extract_load.py. Mêmes droits que sur le reste du pipeline
-- (lecture/écriture, jamais DELETE).
GRANT USAGE ON SCHEMA collection TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON collection.raw_import TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE collection.raw_import_id_seq TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON collection.match_quarantine TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE collection.match_quarantine_id_seq TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON prod.dim_owned_card TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE prod.dim_owned_card_id_seq TO pipeline_app;

-- dashboard_reader : lecture seule sur la nouvelle table prod, même pattern
-- que le reste de prod (migration 007) -- pour que Metabase puisse
-- l'exploiter dans le futur dashboard.
GRANT SELECT ON prod.dim_owned_card TO dashboard_reader;

COMMIT;
