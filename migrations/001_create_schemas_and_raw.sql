-- Crée les trois zones du pipeline comme schémas Postgres séparés.
-- staging et prod restent vides jusqu'au mois 2 ; seul raw est peuplé ce mois-ci.

-- BEGIN ... COMMIT enveloppe toute la migration dans une seule transaction :
-- si une instruction échoue en cours de route, Postgres annule tout le
-- fichier (rien n'est appliqué à moitié).
BEGIN;

-- CREATE SCHEMA IF NOT EXISTS : ne fait rien si le schéma existe déjà, ce qui
-- rend cette migration rejouable sans erreur (idempotente).
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS prod;

-- Clé (card_id, extracted_date, source) plutôt que card_id seul : rejouer
-- l'extraction du même jour met à jour la ligne du jour (idempotence) sans
-- écraser l'historique des jours précédents, indispensable pour suivre
-- l'évolution des prix dans le temps.
CREATE TABLE IF NOT EXISTS raw.card_prices (
    -- bigserial : entier auto-incrémenté (identifiant technique interne),
    -- PRIMARY KEY : identifie chaque ligne de façon unique.
    id              bigserial PRIMARY KEY,
    -- Identifiant de la carte tel que fourni par la source (ex: l'API
    -- pokemontcg.io). Type "text" car ce n'est pas garanti d'être numérique.
    card_id         text NOT NULL,
    -- Date du jour où le prix a été extrait (une ligne par jour et par carte).
    extracted_date  date NOT NULL,
    -- Nom de la source des données (ex: "pokemontcg.io"), utile si on ajoute
    -- d'autres sources de prix plus tard.
    source          text NOT NULL,
    -- payload : la réponse brute de l'API stockée telle quelle en JSON.
    -- jsonb (binaire) plutôt que json (texte) : permet d'indexer/interroger
    -- le contenu efficacement plus tard, sans devoir reparser du texte.
    payload         jsonb NOT NULL,
    -- Horodatage de l'insertion en base (rempli automatiquement par défaut),
    -- utile pour savoir quand la ligne a été chargée (traçabilité du pipeline).
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    -- Contrainte d'unicité : empêche d'insérer deux fois la même
    -- (carte, date, source) — un INSERT ... ON CONFLICT pourra s'appuyer
    -- dessus pour faire un "upsert" idempotent.
    CONSTRAINT uq_card_prices_card_date_source UNIQUE (card_id, extracted_date, source)
);

-- Utilisateur applicatif à droits minimaux : jamais le superuser pour les
-- écritures du pipeline. Le mot de passe est fixé séparément (voir
-- scripts/apply_migrations.sh) pour ne jamais committer de secret ici.
-- Le bloc DO $$ ... END $$ est un bloc de code procédural PL/pgSQL : il
-- permet d'exécuter du "if/then" en SQL brut, ce qu'une simple instruction
-- CREATE ROLE ne sait pas faire (pas de IF NOT EXISTS pour les rôles).
DO $$
BEGIN
    -- On vérifie d'abord si le rôle existe déjà dans le catalogue système
    -- pg_roles, pour ne pas planter en rejouant la migration (idempotence).
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pipeline_app') THEN
        -- LOGIN : ce rôle peut se connecter directement (contrairement à un
        -- rôle "groupe" qui ne servirait qu'à regrouper des permissions).
        CREATE ROLE pipeline_app LOGIN;
    END IF;
END
$$;

-- GRANT USAGE : autorise pipeline_app à "voir"/utiliser le schéma raw (sans
-- USAGE, même avec des droits sur une table, l'accès au schéma est refusé).
GRANT USAGE ON SCHEMA raw TO pipeline_app;
-- Droits minimaux nécessaires au pipeline : lire, insérer, mettre à jour les
-- prix. Pas de DELETE (le pipeline ne doit jamais supprimer d'historique) et
-- pas de droits sur les autres tables/schémas.
GRANT SELECT, INSERT, UPDATE ON raw.card_prices TO pipeline_app;
-- Une colonne "bigserial" repose sur une séquence Postgres cachée
-- (card_prices_id_seq) qui génère les IDs auto-incrémentés ; il faut des
-- droits explicites dessus pour que les INSERT depuis pipeline_app fonctionnent.
GRANT USAGE, SELECT ON SEQUENCE raw.card_prices_id_seq TO pipeline_app;

COMMIT;
