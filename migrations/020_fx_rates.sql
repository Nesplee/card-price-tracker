-- Migration 020 : taux de change de référence de la BCE, pour afficher tout le
-- dashboard en francs suisses (2026-11-18, plan
-- ~/mere/rapports/chf-dashboard-plan-2026-11-18.md).
--
-- Les prix TCGplayer restent stockés en USD dans prod.fact_price_history (rien
-- n'est réécrit) ; ils sont convertis à la lecture, au taux du jour, par les
-- vues ci-dessous.
--
-- Source : cours de référence publiés chaque jour ouvré par la Banque centrale
-- européenne (eurofxref, gratuit, sans clé), chargés par le DAG card_fx_rates
-- (src/extract/ecb_rates.py, src/load/fx_loader.py). La BCE publie des cours
-- contre l'euro : USD -> CHF est le taux croisé CHF par EUR / USD par EUR.
--
-- Règle du taux d'un jour D : le dernier cours BCE daté STRICTEMENT avant D.
--   - C'est le cours connu quand les prix du jour D sont relevés (07:00 UTC ;
--     la BCE publie le cours de D vers 15:00 UTC) : un prix converti affiché
--     ne change donc jamais après coup.
--   - Week-end et jours fériés de la BCE : dernier jour ouvré.
--   - Cours vieux de plus de 5 jours (panne de la BCE ou du DAG) : pas de taux.
--     Le prix converti vaut NULL et le board affiche un bandeau, plutôt qu'un
--     prix faux. 5 jours couvrent le plus long trou du calendrier BCE
--     (vendredi saint et lundi de Pâques).

BEGIN;

CREATE TABLE IF NOT EXISTS prod.fact_fx_rate (
    -- Jour du cours BCE (jours ouvrés seulement).
    ecb_date     date PRIMARY KEY,
    usd_per_eur  numeric(12, 6) NOT NULL CHECK (usd_per_eur > 0),
    chf_per_eur  numeric(12, 6) NOT NULL CHECK (chf_per_eur > 0),
    loaded_at    timestamptz NOT NULL DEFAULT now()
);

-- Cours déjà publiés, du 27/07 au 17/11/2026 (eurofxref-hist.zip, lu le
-- 2026-11-18) : l'historique TCGplayer commence le 07/08. Les conversions
-- marchent donc dès cette migration, sans attendre le premier passage du DAG,
-- qui réécrit ces mêmes valeurs (upsert) et ajoute les jours suivants.
INSERT INTO prod.fact_fx_rate (ecb_date, usd_per_eur, chf_per_eur) VALUES
    ('2026-07-27', 1.1389, 0.9284), ('2026-07-28', 1.1367, 0.9319), ('2026-07-29', 1.138, 0.9332),
    ('2026-07-30', 1.1476, 0.9324), ('2026-07-31', 1.1485, 0.9304), ('2026-08-03', 1.1535, 0.932),
    ('2026-08-04', 1.1515, 0.9319), ('2026-08-05', 1.1554, 0.9345), ('2026-08-06', 1.1542, 0.9346),
    ('2026-08-07', 1.1535, 0.9347), ('2026-08-10', 1.1555, 0.934), ('2026-08-11', 1.154, 0.9351),
    ('2026-08-12', 1.1545, 0.9366), ('2026-08-13', 1.1534, 0.9373), ('2026-08-14', 1.1567, 0.939),
    ('2026-08-17', 1.1593, 0.9389), ('2026-08-18', 1.1576, 0.9406), ('2026-08-19', 1.1605, 0.9402),
    ('2026-08-20', 1.1681, 0.9333), ('2026-08-21', 1.1699, 0.9353), ('2026-08-24', 1.1664, 0.9362),
    ('2026-08-25', 1.1662, 0.9361), ('2026-08-26', 1.1669, 0.938), ('2026-08-27', 1.1645, 0.9376),
    ('2026-08-28', 1.1643, 0.9364), ('2026-08-31', 1.1596, 0.9376), ('2026-09-01', 1.159, 0.9394),
    ('2026-09-02', 1.1578, 0.9424), ('2026-09-03', 1.1615, 0.939), ('2026-09-04', 1.1622, 0.9405),
    ('2026-09-07', 1.1622, 0.9405), ('2026-09-08', 1.1614, 0.9425), ('2026-09-09', 1.1652, 0.9404),
    ('2026-09-10', 1.1616, 0.9432), ('2026-09-11', 1.1592, 0.9451), ('2026-09-14', 1.1551, 0.9431),
    ('2026-09-15', 1.1539, 0.9441), ('2026-09-16', 1.1537, 0.9449), ('2026-09-17', 1.1481, 0.9466),
    ('2026-09-18', 1.146, 0.9462), ('2026-09-21', 1.149, 0.9438), ('2026-09-22', 1.1463, 0.9393),
    ('2026-09-23', 1.1411, 0.939), ('2026-09-24', 1.1367, 0.9409), ('2026-09-25', 1.1403, 0.9445),
    ('2026-09-28', 1.1378, 0.9464), ('2026-09-29', 1.1355, 0.9461), ('2026-09-30', 1.1355, 0.9478),
    ('2026-10-01', 1.1298, 0.9437), ('2026-10-02', 1.1225, 0.9279), ('2026-10-05', 1.1204, 0.9311),
    ('2026-10-06', 1.1269, 0.9359), ('2026-10-07', 1.1177, 0.9309), ('2026-10-08', 1.1186, 0.9326),
    ('2026-10-09', 1.1206, 0.9313), ('2026-10-12', 1.1181, 0.9312), ('2026-10-13', 1.1188, 0.9312),
    ('2026-10-14', 1.1195, 0.933), ('2026-10-15', 1.1208, 0.9305), ('2026-10-16', 1.1253, 0.9272),
    ('2026-10-19', 1.1225, 0.9261), ('2026-10-20', 1.1215, 0.9237), ('2026-10-21', 1.1234, 0.9256),
    ('2026-10-22', 1.1205, 0.9211), ('2026-10-23', 1.1193, 0.9227), ('2026-10-26', 1.1188, 0.9243),
    ('2026-10-27', 1.115, 0.9239), ('2026-10-28', 1.1133, 0.9247), ('2026-10-29', 1.113, 0.9222),
    ('2026-10-30', 1.1172, 0.9194), ('2026-11-02', 1.1216, 0.9193), ('2026-11-03', 1.1179, 0.9166),
    ('2026-11-04', 1.1267, 0.9188), ('2026-11-05', 1.1224, 0.918), ('2026-11-06', 1.1165, 0.9143),
    ('2026-11-09', 1.1193, 0.9164), ('2026-11-10', 1.1214, 0.9203), ('2026-11-11', 1.1127, 0.9234),
    ('2026-11-12', 1.1081, 0.9232), ('2026-11-13', 1.1052, 0.9266), ('2026-11-16', 1.1014, 0.9244),
    ('2026-11-17', 1.0989, 0.9233)
ON CONFLICT (ecb_date) DO NOTHING;

-- Taux USD -> CHF de chaque jour calendaire, selon la règle ci-dessus. Un jour
-- sans cours assez récent n'a pas de ligne : une jointure INNER écarte le
-- prix, une jointure LEFT donne NULL.
-- Vue MATÉRIALISÉE : la même règle en vue simple était recalculée pour chaque
-- ligne jointe (backtest : 2,6 s au lieu de 0,2 s sur la copie de prod). Elle
-- est rafraîchie par prod.refresh_usd_chf() après chaque chargement de cours.
CREATE MATERIALIZED VIEW IF NOT EXISTS prod.mv_usd_chf AS
SELECT g.d::date AS date_id,
       r.ecb_date,
       (r.chf_per_eur / r.usd_per_eur)::numeric(12, 6) AS usd_chf
FROM generate_series(
         (SELECT min(ecb_date) + 1 FROM prod.fact_fx_rate),
         (SELECT max(ecb_date) + 5 FROM prod.fact_fx_rate),
         interval '1 day'
     ) AS g(d)
JOIN LATERAL (
    SELECT f.ecb_date, f.usd_per_eur, f.chf_per_eur
    FROM prod.fact_fx_rate f
    WHERE f.ecb_date < g.d::date AND f.ecb_date >= g.d::date - 5
    ORDER BY f.ecb_date DESC
    LIMIT 1
) r ON true
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_usd_chf_date ON prod.mv_usd_chf (date_id);

-- Le pipeline n'est pas propriétaire de la vue : il la rafraîchit par cette
-- fonction SECURITY DEFINER (même modèle que raw.purge_oldest_day, migration
-- 016), qui ne fait rien d'autre.
CREATE OR REPLACE FUNCTION prod.refresh_usd_chf()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
-- search_path figé : une fonction SECURITY DEFINER ne doit jamais résoudre un
-- nom d'objet dans un schéma que l'appelant pourrait contrôler.
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW prod.mv_usd_chf;
END $$;

REVOKE ALL ON FUNCTION prod.refresh_usd_chf() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION prod.refresh_usd_chf() TO pipeline_app;

-- Prix TCGplayer convertis en CHF, jour par jour. market_chf / low_chf sont
-- NULL les jours sans taux (voir plus haut). C'est la série de TENDANCE du
-- dashboard (courbes, variations, valeur, signaux, backtest) : une moyenne de
-- ventes, convertie. Le prix CardTrader (offre la moins chère en Europe) n'y
-- entre jamais : les deux ne mesurent pas la même chose.
CREATE OR REPLACE VIEW prod.v_tcgplayer_chf AS
SELECT f.card_id,
       f.date_id,
       f.average_sell_price * x.usd_chf AS market_chf,
       f.low_price * x.usd_chf AS low_chf,
       f.trend_price * x.usd_chf AS trend_chf,
       x.usd_chf,
       x.ecb_date
FROM prod.fact_price_history f
JOIN prod.dim_platform p ON p.platform_id = f.platform_id AND p.platform_name = 'tcgplayer'
LEFT JOIN prod.mv_usd_chf x ON x.date_id = f.date_id;

-- Même modèle que les autres faits : le pipeline lit, insère et corrige, sans
-- DELETE ; le dashboard lit seulement.
GRANT SELECT, INSERT, UPDATE ON prod.fact_fx_rate TO pipeline_app;
GRANT SELECT ON prod.fact_fx_rate, prod.mv_usd_chf, prod.v_tcgplayer_chf
    TO pipeline_app, dashboard_reader;

COMMIT;
