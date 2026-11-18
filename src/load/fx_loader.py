# Chargement quotidien des cours de référence BCE (USD et CHF contre l'euro)
# dans prod.fact_fx_rate (migration 020), puis rafraîchissement de la vue
# matérialisée prod.mv_usd_chf, qui donne le taux USD -> CHF de chaque jour
# (dernier cours BCE daté strictement avant le jour, 5 jours au plus).
#
# Idempotent : upsert par jour BCE. Le passage quotidien lit les 90 derniers
# jours ouvrés, ce qui rattrape aussi les jours manqués (panne du DAG). Si la
# table est vide, l'historique complet est lu depuis le premier jour de prix
# TCGplayer (moins une marge), pour que chaque prix ait son taux.
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from psycopg import Connection

from src.extract.ecb_rates import EcbClient, EcbRate

logger = logging.getLogger(__name__)

# Garde-fou : un USD vaut entre 0,5 et 1,5 CHF depuis des décennies. Hors de
# cette plage, le fichier lu est faux (devise inversée, mauvaise colonne) :
# on refuse tout le lot plutôt que de convertir des prix avec.
USD_CHF_MIN = Decimal("0.5")
USD_CHF_MAX = Decimal("1.5")

_UPSERT_SQL = """
INSERT INTO prod.fact_fx_rate (ecb_date, usd_per_eur, chf_per_eur)
VALUES (%s, %s, %s)
ON CONFLICT (ecb_date) DO UPDATE SET
    usd_per_eur = EXCLUDED.usd_per_eur,
    chf_per_eur = EXCLUDED.chf_per_eur,
    loaded_at = now()
WHERE (prod.fact_fx_rate.usd_per_eur, prod.fact_fx_rate.chf_per_eur)
      IS DISTINCT FROM (EXCLUDED.usd_per_eur, EXCLUDED.chf_per_eur)
"""


def check_rates(rates: list[EcbRate]) -> None:
    if not rates:
        raise ValueError("Aucun cours BCE USD/CHF lu : fichier vide ou format changé.")
    bad = [r for r in rates if not USD_CHF_MIN <= r.usd_chf <= USD_CHF_MAX]
    if bad:
        raise ValueError(
            f"Cours BCE invraisemblables (USD->CHF hors de [{USD_CHF_MIN}, {USD_CHF_MAX}]) : "
            f"{bad[0].ecb_date} = {bad[0].usd_chf:.4f}, {len(bad)} jour(s). Rien n'est écrit."
        )


def write_rates(conn: Connection, rates: list[EcbRate]) -> int:
    """Écrit les cours (upsert) et rafraîchit prod.mv_usd_chf. Renvoie le
    nombre de jours BCE nouveaux ou corrigés. Ne commite pas."""
    check_rates(rates)
    changed = 0
    with conn.cursor() as cur:
        for r in rates:
            cur.execute(_UPSERT_SQL, (r.ecb_date, r.usd_per_eur, r.chf_per_eur))
            changed += cur.rowcount
        cur.execute("SELECT prod.refresh_usd_chf()")
    return changed


def run_fx_rates(conn: Connection, client: EcbClient) -> dict:
    (count,) = conn.execute("SELECT count(*) FROM prod.fact_fx_rate").fetchone()
    if count == 0:
        (first,) = conn.execute(
            "SELECT min(f.date_id) FROM prod.fact_price_history f "
            "JOIN prod.dim_platform p ON p.platform_id = f.platform_id "
            "AND p.platform_name = 'tcgplayer'"
        ).fetchone()
        since = first - timedelta(days=10) if first else None
        rates = client.history(since)
        source = "hist.zip"
    else:
        rates = client.last_90_days()
        source = "hist-90d.xml"
    changed = write_rates(conn, rates)
    result = {
        "source": source,
        "read": len(rates),
        "changed": changed,
        "last_ecb_date": rates[-1].ecb_date.isoformat(),
        "last_usd_chf": round(float(rates[-1].usd_chf), 6),
    }
    logger.info("Cours BCE : %s", result)
    return result
