-- Zone de staging : nettoyage, validation et quarantaine des données du raw.
-- Le pipeline extrait des données brutes du raw, les valide et transforme ici avant
-- de les charger dans le schéma prod en étoile. Tout ce qui ne passe pas la validation
-- (contraintes, types, plages de valeurs) est isolé en quarantaine pour inspection.

BEGIN;

-- Table staging.card_prices : contient les prix nettoyés et validés, prêts pour le prod.
-- Chaque ligne a les colonnes extraites du payload JSON du raw et converties aux bons types
-- (de jsonb -> text/numeric/date). Une unique contrainte (card_id, extracted_date, source)
-- assure l'idempotence : rejouer le chargement d'une même journée met à jour les lignes
-- existantes plutôt que de créer des doublons, indispensable pour un pipeline rejouable.
CREATE TABLE IF NOT EXISTS staging.card_prices (
    -- bigserial : auto-incrémenté, identifiant technique interne de chaque insertion.
    id                    bigserial PRIMARY KEY,
    -- card_id : identifiant de la carte depuis la source (ex: pokemontcg.io).
    -- Type text car non garanti numérique, NOT NULL car indispensable à chaque ligne.
    card_id               text NOT NULL,
    -- extracted_date : date du jour où la donnée a été récupérée.
    -- Clé de la UNIQUE constraint pour l'idempotence temporelle.
    extracted_date        date NOT NULL,
    -- Colonnes normalisées extraites du raw.payload JSON : converties aux bons types Postgres.
    name                  text NOT NULL,
    set_id                text NOT NULL,
    set_name              text NOT NULL,
    -- rarity peut être NULL si absent de la source pour cette carte.
    rarity                text,
    -- Prix : numeric(10, 2) = 10 chiffres total, 2 décimales (ex: 1234567.89 au max).
    -- NULL acceptable si la source ne fournit pas ce champ pour cette carte.
    average_sell_price    numeric(10, 2),
    trend_price           numeric(10, 2),
    low_price             numeric(10, 2),
    -- source : nom de la plateforme/API (ex: "cardmarket", "pokemontcg.io").
    -- Utilisé pour filtrer par source dans les analyses, et dans la clé UNIQUE
    -- pour gérer plusieurs sources de prix indépendantes (chacune peut avoir sa vue
    -- pour une même carte et date).
    source                text NOT NULL,
    -- loaded_at : horodatage de l'insertion (rempli auto par défaut = maintenant).
    -- Traçabilité du pipeline : quand la ligne a-t-elle été chargée en staging ?
    loaded_at             timestamptz NOT NULL DEFAULT now(),
    -- Contrainte UNIQUE : (card_id, extracted_date, source).
    -- Clé métier qui crée l'idempotence : rejouer le chargement de la même journée
    -- pour la même source remplace les lignes existantes via ON CONFLICT UPDATE,
    -- ne crée pas de doublons. Critique pour un pipeline rejoué à volonté.
    CONSTRAINT uq_staging_card_prices_card_date_source UNIQUE (card_id, extracted_date, source)
);

-- Table staging.card_prices_quarantine : isolat les données rejetées.
-- Quand le pipeline valide une ligne (contraintes, plages, types, etc.) et qu'elle échoue,
-- on la rejette ici pour inspection manuelle. Cela permet de tracer les données malformées
-- de la source sans bloquer tout le pipeline, et facilite le débogage : voir exactement
-- pourquoi une carte/prix a été rejeté, quelle était la charge originale (raw_payload),
-- quand, et pourquoi.
CREATE TABLE IF NOT EXISTS staging.card_prices_quarantine (
    -- bigserial : numéro de séquence des rejets.
    id                bigserial PRIMARY KEY,
    -- card_id : peut être NULL si le rejet s'est produit avant d'extraire card_id du JSON.
    -- Autres cas : extraction réussie mais validation échouée (prix invalide, rarity hors plage, etc.).
    card_id           text,
    -- extracted_date : date du rejet (NOT NULL car indispensable).
    extracted_date    date NOT NULL,
    -- raw_payload : la charge JSON brute telle que reçue du raw, avant toute transformation.
    -- Stockée telle quelle pour replay/debug : peut-on réinterpréter cette charge ?
    -- La version binaire jsonb (vs json text) permet des indices et des requêtes rapides.
    raw_payload       jsonb NOT NULL,
    -- rejection_reason : message d'erreur structuré expliquant l'échec (ex: "price_out_of_range",
    -- "missing_card_id", "invalid_set_id", etc.). Crucial pour trier les rejets par catégorie.
    rejection_reason  text NOT NULL,
    -- source : nom de la plateforme d'où vient la charge rejetée.
    -- Utile pour identifier si un problème affecte telle source spécifiquement.
    source            text NOT NULL,
    -- loaded_at : horodatage du rejet (rempli auto).
    -- Traçabilité : quand cette donnée a-t-elle été rejetée ?
    loaded_at         timestamptz NOT NULL DEFAULT now()
);

-- Permissions pour pipeline_app sur le schéma staging :
-- Le pipeline ne peut que consulter, insérer et mettre à jour (pas de DELETE).
-- Il n'a aucun droit de création (CREATE TABLE) ou de destruction (DROP).

-- GRANT USAGE : autorise pipeline_app à "voir" le schéma staging et y accéder
-- (sans USAGE, même avec des droits sur une table, l'accès au schéma est refusé).
GRANT USAGE ON SCHEMA staging TO pipeline_app;

-- SELECT : lire les cartes déjà en staging (pour vérifications, audit).
-- INSERT : charger les lignes validées.
-- UPDATE : rejouer une journée et mettre à jour les prix existants.
-- Pas de DELETE : l'historique des prix ne doit jamais être détruit.
GRANT SELECT, INSERT, UPDATE ON staging.card_prices TO pipeline_app;

-- SELECT : consulter les rejets (audit, debug).
-- INSERT : enregistrer une donnée rejetée.
-- Pas de UPDATE ni DELETE : un rejet est immutable, c'est une piste d'audit.
GRANT SELECT, INSERT ON staging.card_prices_quarantine TO pipeline_app;

-- Accès aux séquences auto-incrémentées pour les INSERT (bigserial).
-- Postgres réclame des droits explicites USAGE + SELECT sur la séquence
-- pour que pipeline_app puisse générer les IDs via nextval() implicitement lors des INSERT.
GRANT USAGE, SELECT ON SEQUENCE staging.card_prices_id_seq TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE staging.card_prices_quarantine_id_seq TO pipeline_app;

COMMIT;
