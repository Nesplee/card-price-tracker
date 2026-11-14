-- Migration 014 : expose les posts et articles bruts (raw.news_item) au rôle
-- dashboard_reader, en lecture seule, pour mesurer le buzz par Pokémon dans le
-- board (nombre de posts qui le citent, semaine contre semaine). Seul le schéma
-- prod est ouvert à dashboard_reader : une vue dans prod, sans les colonnes
-- techniques (tentatives, sortie LLM complète).
-- Aucune donnée modifiée : une vue et un GRANT.

BEGIN;

CREATE VIEW prod.v_news_post AS
SELECT n.news_id, n.source, n.url, n.title, n.summary, n.published_at, n.status,
       n.llm_output ->> 'direction' AS direction,
       n.llm_output ->> 'event_type' AS event_type
FROM raw.news_item n;

GRANT SELECT ON prod.v_news_post TO dashboard_reader;

COMMIT;
