# Taux de change dans la base de test (migration 020).
#
# Par défaut, la suite tourne à PARITÉ (1 USD = 1 CHF, posée par conftest pour
# toute la session) : les tests écrits en dollars avant le passage au CHF
# gardent leurs montants, la conversion étant l'identité. Les tests de la
# conversion elle-même posent de vrais taux avec set_rates() et remettent la
# parité ensuite (fixture `rates` de conftest).
from __future__ import annotations

from datetime import date, timedelta

PARITY_FROM = date(2024, 1, 1)
PARITY_TO = date(2027, 12, 31)


def set_rates(conn, rates: dict[date, tuple[float, float]]) -> None:
    """Remplace tous les cours par `rates` : {jour BCE: (USD par EUR, CHF par
    EUR)}, puis rafraîchit prod.mv_usd_chf. Connexion admin."""
    conn.execute("TRUNCATE TABLE prod.fact_fx_rate")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO prod.fact_fx_rate (ecb_date, usd_per_eur, chf_per_eur) "
            "VALUES (%s, %s, %s)",
            [(d, u, c) for d, (u, c) in rates.items()],
        )
    conn.execute("REFRESH MATERIALIZED VIEW prod.mv_usd_chf")
    conn.commit()


def weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def set_parity(conn) -> None:
    set_rates(conn, {d: (1.0, 1.0) for d in weekdays(PARITY_FROM, PARITY_TO)})
