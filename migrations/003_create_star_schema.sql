-- Zone de production : schéma en étoile (star schema) pour l'analyse.
--
-- Architecture : une table centrale de faits (fact_price_history) qui enregistre les observations
-- de prix, entourée de tables de dimensions normalisées (dim_card, dim_date, dim_platform).
-- Les dimensions éliminent la redondance : au lieu de dupliquer name/set_id/rarity sur chaque
-- prix, on les stocke une fois dans dim_card et on référence par card_id.
-- Les analyses (agrégations par plateforme/date/carte) sont plus rapides et les données plus
-- cohérentes (une carte a un nom unique, même si elle s'affiche différemment en staging).
--
-- Avantages du schéma en étoile :
-- 1. Performances : les dimensions sont petites (peu de lignes), jointures rapides.
-- 2. Cohérence : une carte = une ligne en dim_card, un seul source de vérité.
-- 3. Flexibilité : ajouter une propriété à une carte ne nécessite pas de modifier fact_price_history.
-- 4. Facilité des BI : les dimensions sont des listes "de référence" faciles à explorer en UI.

BEGIN;

-- Dimension : dim_card (métadonnées d'une carte, normalisées et sans historique).
-- Une ligne par carte unique (card_id). Si une carte change de nom/set dans la source,
-- on met à jour la ligne existante (pas d'historique, au contraire du raw).
-- Clé : card_id (identifiant primaire de la source).
CREATE TABLE IF NOT EXISTS prod.dim_card (
    -- card_id : identifiant unique de la carte (ex: "sv04.5-1" pour Pokémon TCG).
    -- PRIMARY KEY : une seule ligne par carte, unicité garantie.
    card_id     text PRIMARY KEY,
    -- name : nom anglais/officiel de la carte.
    -- NOT NULL car indispensable, tiré du raw et nettoyé en staging.
    name        text NOT NULL,
    -- set_id, set_name : identifiant et nom du set/extension auquel appartient la carte.
    -- NOT NULL car indispensable à la compréhension de la carte (une même numérotation
    -- peut se répéter dans plusieurs sets).
    set_id      text NOT NULL,
    set_name    text NOT NULL,
    -- rarity : raretés (ex: "Common", "Rare", "Holo Rare"). NULL acceptable si absent de la source.
    rarity      text,
    -- updated_at : horodatage de la dernière mise à jour de cette ligne.
    -- Rempli auto à l'insertion et mis à jour au fur et à mesure que le pipeline
    -- découvre de nouveaux attributs pour cette carte.
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Dimension : dim_date (calendrier pré-rempli pour les agrégations par date).
-- Une ligne par date observée ou prévisible. Pré-rempli avec date_id = la date elle-même,
-- plutôt que de générer dynamiquement dans les requêtes (ex: DATE_PART('year', date_column)).
-- Cela permet des agrégations SQL simples et rapides (grouper par year/month/day demande juste
-- une jointure sur cette table pré-calculée).
CREATE TABLE IF NOT EXISTS prod.dim_date (
    -- date_id : la date au format date (ex: 2024-08-06). PRIMARY KEY unique.
    -- Utile pour les jointures : fact_price_history.date_id = dim_date.date_id.
    date_id      date PRIMARY KEY,
    -- Colonnes pré-calculées pour les agrégations BI sans fonctions SQL complexes :
    -- des requêtes simples GROUP BY year, month ORDER BY day_of_week lisent directement
    -- ces colonnes au lieu de calculer DATE_PART() et EXTRACT() pour chaque ligne.
    year         smallint NOT NULL,
    month        smallint NOT NULL,
    day          smallint NOT NULL,
    day_of_week  smallint NOT NULL
);

-- Dimension : dim_platform (plateformes de prix disponibles).
-- Une ligne par plateforme distincte. Au Mois 2, seule "cardmarket" est active
-- (source fournie par pokemontcg.io). Si on ajoute un vendeur 3e tiers plus tard
-- (ex: "ebay", "tcgplayer"), on ajoute une ligne ici (c'est une modification de schéma/data,
-- pas une action que le pipeline automatique doit faire).
CREATE TABLE IF NOT EXISTS prod.dim_platform (
    -- platform_id : identifiant numérique de la plateforme (serial auto-incrémenté).
    -- PRIMARY KEY : une seule ligne par plateforme.
    platform_id    serial PRIMARY KEY,
    -- platform_name : nom textuel unique (ex: "cardmarket", "ebay").
    -- NOT NULL et UNIQUE : une seule ligne par nom, indispensable pour les jointures.
    platform_name  text NOT NULL UNIQUE
);

-- Fact : fact_price_history (observations de prix, table d'événements/faits centraux).
-- Une ligne par observation de prix : (carte, date, plateforme) = un prix observé.
-- Les colonnes de prix (average_sell_price, trend_price, low_price) sont les "mesures"
-- (valeurs numériques analysables). Les colonnes card_id, date_id, platform_id sont
-- des "dimensions" (références aux tables de contexte).
CREATE TABLE IF NOT EXISTS prod.fact_price_history (
    -- fact_id : identifiant technique interne (bigserial auto-incrémenté).
    -- PRIMARY KEY pour la piste d'audit et les requêtes d'une ligne spécifique.
    fact_id              bigserial PRIMARY KEY,
    -- card_id : clé étrangère vers dim_card. Une observation de prix concerne toujours
    -- une carte connue en prod. NOT NULL car indispensable.
    -- REFERENCES prod.dim_card (card_id) : garantit l'intégrité référentielle.
    -- On ne peut pas insérer un price pour une carte qui n'existe pas en dim_card.
    card_id              text NOT NULL REFERENCES prod.dim_card (card_id),
    -- date_id : clé étrangère vers dim_date. L'observation a lieu un jour donné.
    -- NOT NULL, fait partie de la clé métier (voir UNIQUE ci-dessous).
    date_id              date NOT NULL REFERENCES prod.dim_date (date_id),
    -- platform_id : clé étrangère vers dim_platform. Quel vendeur/plateforme a ce prix ?
    -- NOT NULL, fait partie de la clé métier.
    platform_id          integer NOT NULL REFERENCES prod.dim_platform (platform_id),
    -- Mesures numériques : les prix observés pour cette combinaison de carte/date/plateforme.
    -- Toutes nullable car la source peut ne pas fournir tous les types de prix pour un événement.
    average_sell_price   numeric(10, 2),
    trend_price          numeric(10, 2),
    low_price            numeric(10, 2),
    -- loaded_at : horodatage de l'insertion (rempli auto par défaut).
    -- Trace quand ce fait a été chargé du staging vers la prod.
    loaded_at            timestamptz NOT NULL DEFAULT now(),
    -- Contrainte UNIQUE : (card_id, date_id, platform_id).
    -- Clé métier : une seule observation de prix par combinaison de carte/date/plateforme.
    -- Évite les doublons si le pipeline réjoue la même journée. ON CONFLICT UPDATE
    -- peut s'appuyer dessus pour mettre à jour les prix si la source change ses estimations.
    CONSTRAINT uq_fact_price_history_card_date_platform UNIQUE (card_id, date_id, platform_id)
);

-- Initialisation des données de référence : amorçage de dim_platform.
-- Au Mois 2, pokemontcg.io ne fournit que des prix "cardmarket" ; seule cette plateforme
-- doit exister au départ. Ajouter une plateforme est un acte administratif (migration/DDL),
-- pas une décision du pipeline automatique, donc cet INSERT est ici et non dans le loader.
-- ON CONFLICT (platform_name) DO NOTHING : idempotent, si la ligne existe déjà, ne rien faire.
INSERT INTO prod.dim_platform (platform_name) VALUES ('cardmarket')
ON CONFLICT (platform_name) DO NOTHING;

-- Permissions pour pipeline_app sur le schéma prod :
-- Le pipeline peut lire les dimensions (pour vérifier si une carte existe avant d'insérer un prix)
-- et insérer/mettre à jour les faits. Pas de DELETE : l'historique des prix est immuable.

-- GRANT USAGE : autorise pipeline_app à voir le schéma prod.
GRANT USAGE ON SCHEMA prod TO pipeline_app;

-- Dimensions : SELECT, INSERT, UPDATE. Le pipeline peut vérifier si une carte existe,
-- ajouter une nouvelle carte découverte en staging, et mettre à jour ses attributs
-- (ex: si set_name change chez la source).
GRANT SELECT, INSERT, UPDATE ON prod.dim_card TO pipeline_app;
GRANT SELECT, INSERT ON prod.dim_date TO pipeline_app;

-- dim_platform : SELECT seulement. Ajouter une plateforme est un acte administratif,
-- pas une décision du pipeline. pipeline_app ne peut que lire les plateformes existantes
-- pour résoudre le nom "cardmarket" -> son platform_id.
GRANT SELECT ON prod.dim_platform TO pipeline_app;

-- fact_price_history : SELECT (audit), INSERT (charger les observations), UPDATE (correction des prix).
-- Pas de DELETE : l'historique des prix ne doit jamais être détruit, même si une donnée
-- s'avère erronée (on laisse trace, quitte à la marquer avec flag/version).
GRANT SELECT, INSERT, UPDATE ON prod.fact_price_history TO pipeline_app;

-- Accès à la séquence auto-incrémentée fact_id pour les INSERT.
GRANT USAGE, SELECT ON SEQUENCE prod.fact_price_history_fact_id_seq TO pipeline_app;

COMMIT;
