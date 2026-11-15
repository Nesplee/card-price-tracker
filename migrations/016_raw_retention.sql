-- Migration 016 : rétention glissante de raw.card_prices, staging.card_prices et
-- staging.card_prices_quarantine.
--
-- Pourquoi : raw.card_prices recopiait chaque jour le catalogue complet
-- (~20 800 lignes, ~40 Mo par jour) et pesait 4,3 Go sur 5,2 Go au bout de
-- 101 jours. L'historique des prix vit dans prod.fact_price_history, qui n'est
-- PAS concerné ici (aucun DELETE possible sur le fait, voir README). raw et
-- staging ne servent qu'à rejouer le pipeline : on garde une fenêtre glissante
-- (60 jours par défaut, voir src/maintenance/retention.py), les jours plus
-- anciens restent dans les sauvegardes restic et l'archive zstd.
--
-- Moindre privilège conservé : pipeline_app ne reçoit TOUJOURS PAS le droit
-- DELETE (migrations 001 et 002). Il reçoit seulement le droit d'exécuter une
-- fonction SECURITY DEFINER qui borne ce qu'elle peut supprimer :
--   * trois tables autorisées, pas d'autre ;
--   * fenêtre de 30 jours minimum, codée ici et non côté Python ;
--   * la fenêtre est comptée depuis le dernier jour présent dans raw (pas depuis
--     aujourd'hui) : si le pipeline s'arrête plusieurs semaines, rien n'est
--     supprimé tant qu'aucun nouveau jour n'arrive, et le dernier jour de raw
--     (dont copy_previous_catalog a besoin) n'est jamais touché ;
--   * un seul jour par appel, pour que l'appelant valide (COMMIT) jour par
--     jour : une purge interrompue garde ce qui est déjà fait et reprend au
--     jour suivant.

BEGIN;

-- Index sur extracted_date : la clé unique des trois tables commence par
-- card_id, donc toute requête "WHERE extracted_date = ..." parcourait la table
-- entière (clean_raw_to_staging, refresh_tcgplayer_prices, snapshot_catalog et
-- la purge). IF NOT EXISTS : rejouable.
CREATE INDEX IF NOT EXISTS ix_raw_card_prices_extracted_date
    ON raw.card_prices (extracted_date);
CREATE INDEX IF NOT EXISTS ix_staging_card_prices_extracted_date
    ON staging.card_prices (extracted_date);
CREATE INDEX IF NOT EXISTS ix_staging_card_prices_quarantine_extracted_date
    ON staging.card_prices_quarantine (extracted_date);

CREATE OR REPLACE FUNCTION raw.purge_oldest_day(p_table text, p_keep_days integer)
RETURNS TABLE (purged_date date, deleted_rows bigint)
LANGUAGE plpgsql
SECURITY DEFINER
-- search_path figé : une fonction SECURITY DEFINER ne doit jamais résoudre un
-- nom d'objet dans un schéma que l'appelant pourrait contrôler.
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_latest date;
    v_cutoff date;
    v_oldest date;
    v_count  bigint;
BEGIN
    -- Liste blanche : p_table est ensuite injecté dans du SQL dynamique, il ne
    -- peut donc prendre que ces trois valeurs exactes.
    IF p_table IS NULL OR p_table NOT IN (
        'raw.card_prices', 'staging.card_prices', 'staging.card_prices_quarantine'
    ) THEN
        RAISE EXCEPTION 'purge_oldest_day : table non autorisée (%)', p_table;
    END IF;
    IF p_keep_days IS NULL OR p_keep_days < 30 THEN
        RAISE EXCEPTION 'purge_oldest_day : fenêtre trop courte (% jours, minimum 30)',
            p_keep_days;
    END IF;

    SELECT max(extracted_date) INTO v_latest FROM raw.card_prices;
    IF v_latest IS NULL THEN
        RETURN;
    END IF;
    -- On garde les p_keep_days jours calendaires [v_cutoff, v_latest].
    v_cutoff := v_latest - (p_keep_days - 1);

    EXECUTE format('SELECT min(extracted_date) FROM %s WHERE extracted_date < $1', p_table)
        INTO v_oldest USING v_cutoff;
    IF v_oldest IS NULL THEN
        RETURN;
    END IF;

    EXECUTE format('DELETE FROM %s WHERE extracted_date = $1', p_table) USING v_oldest;
    GET DIAGNOSTICS v_count = ROW_COUNT;

    purged_date := v_oldest;
    deleted_rows := v_count;
    RETURN NEXT;
END
$$;

-- Par défaut, toute fonction est exécutable par PUBLIC : on retire ce droit et
-- on ne l'accorde qu'au rôle du pipeline.
REVOKE ALL ON FUNCTION raw.purge_oldest_day(text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION raw.purge_oldest_day(text, integer) TO pipeline_app;

-- Motifs de quarantaine par jour, lus par le veilleur (~/bin/check-jobs.sh) et
-- écrits dans le log du DAG card_raw_retention. Une vue plutôt qu'une table :
-- rien à maintenir, et elle couvre la même fenêtre que la quarantaine.
CREATE OR REPLACE VIEW staging.v_quarantine_reasons_daily AS
SELECT extracted_date, source, rejection_reason, count(*) AS rejected
FROM staging.card_prices_quarantine
GROUP BY extracted_date, source, rejection_reason;

GRANT SELECT ON staging.v_quarantine_reasons_daily TO pipeline_app;

COMMIT;
