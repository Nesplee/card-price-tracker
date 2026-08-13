-- Migration 007 : crée le rôle Postgres dashboard_reader, en lecture seule,
-- scopé au schéma prod -- destiné à être utilisé par un outil d'exploration
-- de données externe (Metabase), jamais par le pipeline lui-même (qui garde
-- pipeline_app). Voir docs/superpowers/specs/2026-08-09-metabase-db-ui-design.md.
--
-- Pourquoi un rôle séparé plutôt que de réutiliser pipeline_app : least
-- privilege -- un outil de visualisation externe n'a besoin QUE de lire les
-- données finales (prod), jamais d'écrire, et surtout jamais d'accéder à
-- raw/staging (détails internes du pipeline, pas destinés à une consultation
-- externe). Réutiliser pipeline_app donnerait à Metabase des droits
-- d'écriture dont il n'a aucun besoin -- un risque inutile si les
-- identifiants Metabase fuitaient un jour.

BEGIN;

-- Bloc DO $$ ... END $$ idempotent : même pattern que la création de
-- pipeline_app (migration 001) -- CREATE ROLE n'a pas de IF NOT EXISTS
-- natif, ce bloc procédural PL/pgSQL vérifie l'existence avant de créer.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader') THEN
        CREATE ROLE dashboard_reader LOGIN;
    END IF;
END
$$;

-- GRANT USAGE : nécessaire pour accéder au schéma prod, même avec des droits
-- sur les tables (sans USAGE, l'accès au schéma est refusé).
GRANT USAGE ON SCHEMA prod TO dashboard_reader;

-- SELECT sur toutes les tables ACTUELLES de prod.
GRANT SELECT ON ALL TABLES IN SCHEMA prod TO dashboard_reader;

-- ALTER DEFAULT PRIVILEGES : s'applique aux tables créées À L'AVENIR dans
-- prod PAR LE RÔLE QUI EXÉCUTE CETTE COMMANDE (ici POSTGRES_ADMIN_USER, voir
-- scripts/apply_migrations.sh -- toutes les migrations sont appliquées par ce
-- rôle admin, y compris les CREATE TABLE de prod) -- tant que les futures
-- migrations continuent d'être appliquées par ce même rôle admin,
-- dashboard_reader lira automatiquement toute nouvelle table de prod, sans
-- migration supplémentaire.
ALTER DEFAULT PRIVILEGES IN SCHEMA prod GRANT SELECT ON TABLES TO dashboard_reader;

COMMIT;
