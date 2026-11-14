-- Migration 013 : news et méta compétitive pour les signaux d'opportunité
-- (2026-11-06, partie 2 de docs/superpowers/specs/2026-11-01-opportunity-signals-design.md).
--
-- Deux nouvelles sources, lues par le DAG card_market_news :
--   - news : flux RSS (Google News, Reddit) -> raw.news_item, classées par le
--     LLM local (Ollama de second_brain) puis rattachées aux cartes dans
--     prod.fact_news_event ;
--   - méta : decklists du top 8 des tournois Limitless TCG -> usage de chaque
--     carte dans raw.meta_card_usage.
-- src/signals/opportunities.py en tire les signaux actu_* et meta_*.

BEGIN;

-- Une ligne par article / post. status :
--   pending    : à analyser par le LLM ;
--   skipped    : écarté par le préfiltre (aucun mot-clé de marché ni nom de set) ;
--   analyzed   : classé, llm_output gardé tel quel pour audit ;
--   error      : échec LLM, retenté jusqu'à 3 tentatives (attempts).
CREATE TABLE raw.news_item (
    news_id       bigserial PRIMARY KEY,
    source        text NOT NULL,
    external_id   text NOT NULL,
    url           text NOT NULL,
    title         text NOT NULL,
    summary       text NOT NULL DEFAULT '',
    published_at  timestamptz NOT NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'skipped', 'analyzed', 'error')),
    attempts      smallint NOT NULL DEFAULT 0,
    llm_model     text,
    llm_output    jsonb,
    analyzed_at   timestamptz,
    CONSTRAINT uq_news_item_source_external_id UNIQUE (source, external_id)
);

CREATE INDEX ix_news_item_status ON raw.news_item (status) WHERE status IN ('pending', 'error');

-- Rattachement d'une news analysée aux cartes qu'elle concerne. weight :
-- 1 si la news cite la carte ET son set, 0,5 la carte seule (toutes ses
-- impressions), 0,3 le set seul (toutes ses cartes du périmètre).
CREATE TABLE prod.fact_news_event (
    news_id       bigint NOT NULL REFERENCES raw.news_item(news_id),
    card_id       text NOT NULL REFERENCES prod.dim_card(card_id),
    published_on  date NOT NULL,
    event_type    text NOT NULL,
    direction     text NOT NULL CHECK (direction IN ('hausse', 'baisse')),
    confidence    numeric(3, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    weight        numeric(3, 2) NOT NULL CHECK (weight BETWEEN 0 AND 1),
    CONSTRAINT pk_fact_news_event PRIMARY KEY (news_id, card_id)
);

CREATE INDEX ix_fact_news_event_card_date ON prod.fact_news_event (card_id, published_on);

-- Tournois Limitless déjà traités (on ne relit jamais leurs classements).
CREATE TABLE raw.meta_tournament (
    tournament_id  text PRIMARY KEY,
    name           text NOT NULL,
    played_on      date NOT NULL,
    format         text NOT NULL,
    players        integer NOT NULL,
    decklists      integer NOT NULL,
    fetched_at     timestamptz NOT NULL DEFAULT now()
);

-- Nombre de decklists du top 8 qui jouent chaque carte (par nom : toutes les
-- impressions d'une carte jouent pareil, et c'est la demande sur le nom qui
-- tire aussi le prix des versions illustrées).
CREATE TABLE raw.meta_card_usage (
    tournament_id  text NOT NULL REFERENCES raw.meta_tournament(tournament_id),
    card_name      text NOT NULL,
    decks          integer NOT NULL,
    CONSTRAINT pk_meta_card_usage PRIMARY KEY (tournament_id, card_name)
);

-- Lecture Metabase / dashboard : la news (dans raw, invisible pour
-- dashboard_reader) avec chacune des cartes qu'elle touche.
CREATE VIEW prod.v_news_event AS
SELECT e.published_on, e.card_id, c.name AS card_name, c.set_name, e.event_type,
       e.direction, e.confidence, e.weight, n.source, n.title, n.url,
       n.llm_output ->> 'resume' AS resume
FROM prod.fact_news_event e
JOIN raw.news_item n ON n.news_id = e.news_id
JOIN prod.dim_card c ON c.card_id = e.card_id;

-- Part des decklists du top 8 jouant chaque carte, par semaine (lundi).
CREATE VIEW prod.v_meta_usage_weekly AS
WITH t AS (
    SELECT tournament_id, date_trunc('week', played_on)::date AS week, decklists
    FROM raw.meta_tournament
), totals AS (
    SELECT week, sum(decklists) AS decklists FROM t GROUP BY week
)
SELECT t.week, u.card_name, sum(u.decks) AS decks,
       round(sum(u.decks)::numeric / nullif(totals.decklists, 0), 3) AS share
FROM raw.meta_card_usage u
JOIN t ON t.tournament_id = u.tournament_id
JOIN totals ON totals.week = t.week
GROUP BY t.week, u.card_name, totals.decklists;

GRANT SELECT, INSERT, UPDATE ON raw.news_item TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE raw.news_item_news_id_seq TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON prod.fact_news_event TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON raw.meta_tournament, raw.meta_card_usage TO pipeline_app;

GRANT SELECT ON prod.fact_news_event, prod.v_news_event, prod.v_meta_usage_weekly
    TO dashboard_reader;

COMMIT;
