-- Migration 004 : ajoute une contrainte d'unicité sur staging.card_prices_quarantine,
-- pour rendre load_quarantine() idempotent au même titre que raw.card_prices et
-- staging.card_prices (voir migrations/001 et 002).
--
-- Pourquoi cette migration arrive après coup, plutôt que de modifier 002 directement
-- (les migrations déjà appliquées 001-003 ne sont jamais modifiées après coup, règle
-- du projet) : le retour d'expérience du Mois 1 sur les échecs partiels d'API
-- (timeouts, erreurs 5xx en cours de pagination — voir src/extract/pokemontcg_client.py
-- et le mécanisme de checkpoint-par-page de scripts/run_extract_load.py) a montré que
-- le pipeline DOIT pouvoir être rejoué après un crash sans dupliquer de données. Ce
-- même raisonnement s'applique à clean_raw_to_staging (Mois 2) : le DAG du Mois 2
-- (Task 4) prévoit `retries: 2` intégré à l'étape de nettoyage. Si cette étape échoue
-- APRÈS avoir déjà écrit des rejets en quarantaine pour la journée en cours, la
-- relance automatique par Airflow rejoue clean_raw_to_staging en entier. Sans
-- contrainte d'unicité, chaque carte encore rejetée à la relance créerait une
-- NOUVELLE ligne de quarantaine en double au lieu d'une mise à jour, cassant
-- l'invariant du projet "idempotence par UPSERT, jamais delete-and-reload" — déjà
-- respecté par raw.card_prices et staging.card_prices, mais pas par la quarantaine
-- jusqu'ici (elle faisait un simple INSERT, sur l'hypothèse erronée qu'un rejet est
-- un événement d'audit à accumuler sans limite).
--
-- Cas particulier card_id NULL : une carte peut être rejetée AVANT extraction de son
-- id (payload totalement malformé — voir load_quarantine() dans
-- src/load/staging_loader.py, payload.get("id") qui renvoie None dans ce cas). Une
-- contrainte UNIQUE Postgres standard traite chaque valeur NULL comme distincte de
-- toute autre NULL : deux lignes avec card_id NULL, même extracted_date, même
-- source, NE sont PAS considérées en conflit l'une avec l'autre. Ces lignes
-- continueront donc à s'accumuler à chaque relance. C'est un cas résiduel accepté
-- (pas une régression) : sans identifiant de carte, on ne peut de toute façon pas
-- savoir si une nouvelle ligne à card_id NULL représente "la même" carte rejetée
-- qu'avant ou une carte différente — inventer un identifiant de substitution
-- masquerait le vrai problème (une source qui renvoie des payloads sans id du tout)
-- plutôt que de le rendre visible en quarantaine.

BEGIN;

-- DO $$ ... END $$ : même pattern que dans migrations/001_create_schemas_and_raw.sql
-- pour la création idempotente du rôle pipeline_app. On vérifie l'existence de la
-- contrainte dans le catalogue système pg_constraint avant de l'ajouter, car
-- "ALTER TABLE ... ADD CONSTRAINT" ne supporte pas IF NOT EXISTS nativement en SQL
-- Postgres (contrairement à CREATE TABLE IF NOT EXISTS) : sans ce garde-fou, rejouer
-- ce fichier une seconde fois échouerait avec une erreur "constraint already exists".
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_staging_card_prices_quarantine_card_date_source'
    ) THEN
        ALTER TABLE staging.card_prices_quarantine
            ADD CONSTRAINT uq_staging_card_prices_quarantine_card_date_source
            UNIQUE (card_id, extracted_date, source);
    END IF;
END
$$;

-- Conséquence directe de la contrainte ci-dessus : src/load/staging_loader.py passe
-- maintenant d'un simple INSERT à un "INSERT ... ON CONFLICT DO UPDATE" pour la
-- quarantaine (voir _INSERT_QUARANTINE_SQL). Le DO UPDATE exécute, en base, une
-- opération UPDATE sur les lignes en conflit — pas seulement un INSERT. Or
-- migrations/002_create_staging_tables.sql n'accordait à pipeline_app que
-- SELECT, INSERT sur staging.card_prices_quarantine (la quarantaine était pensée
-- comme immuable à l'époque, donc UPDATE était volontairement absent). Sans ce GRANT
-- supplémentaire, le nouveau chemin ON CONFLICT DO UPDATE échouerait en production
-- avec "permission denied for table card_prices_quarantine" dès le premier conflit
-- réel — ce GRANT fait donc partie intégrante de ce correctif, pas un changement
-- séparé. GRANT est idempotent nativement (rejouable sans erreur), pas besoin de
-- garde IF NOT EXISTS ici contrairement à ADD CONSTRAINT plus haut.
GRANT UPDATE ON staging.card_prices_quarantine TO pipeline_app;

COMMIT;
