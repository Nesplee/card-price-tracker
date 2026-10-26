# Contrôle de fraîcheur des prix, exécuté par le DAG après load_to_warehouse.
# Contexte (incident 2026-10-16 -> 2026-10-26) : pokemontcg.io a cessé de
# rafraîchir ses prix TCGplayer (tcgplayer.updatedAt figé au 2026/10/15) tout
# en continuant à répondre normalement. Le pipeline réussissait donc chaque
# jour en rechargeant la même copie des prix, sans aucun signal visible
# pendant 10 jours. Ce module détecte ce cas (aucun prix n'a bougé par
# rapport au jour chargé précédent) et envoie une alerte ntfy.
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import requests
from psycopg import Connection

from src.common.config import NtfyConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FreshnessReport:
    date_id: date
    # Jour chargé juste avant date_id (None s'il n'y en a pas : premier run).
    previous_date_id: date | None
    # Nombre de cartes présentes les deux jours (base de la comparaison).
    compared: int
    # Parmi elles, nombre de cartes dont au moins un prix a changé.
    changed: int
    # Plus récent tcgplayer.updatedAt vu dans le payload brut du jour.
    source_updated_at: str | None

    @property
    def is_stale(self) -> bool:
        # compared == 0 : rien à comparer (premier jour, ou trou de données) --
        # ce n'est pas un gel de la source, on ne lève pas d'alerte.
        return self.compared > 0 and self.changed == 0

    def message(self) -> str:
        return (
            f"CardTracker : aucun prix n'a changé le {self.date_id} par rapport au "
            f"{self.previous_date_id} ({self.compared} cartes comparées). "
            f"Dernière publication des prix TCGplayer par la source : "
            f"{self.source_updated_at or 'inconnue'}. La source est probablement figée."
        )


def check_price_freshness(conn: Connection, date_id: date) -> FreshnessReport:
    # Compare date_id au DERNIER jour chargé avant lui (pas forcément la
    # veille : un run manqué laisse un trou dans fact_price_history).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(date_id) FROM prod.fact_price_history WHERE date_id < %s",
            (date_id,),
        )
        previous = cur.fetchone()[0]
        compared = changed = 0
        if previous is not None:
            cur.execute(
                """
                SELECT count(*),
                       count(*) FILTER (
                           WHERE (f.average_sell_price, f.trend_price, f.low_price)
                                 IS DISTINCT FROM (p.average_sell_price, p.trend_price, p.low_price)
                       )
                FROM prod.fact_price_history f
                JOIN prod.fact_price_history p
                  ON p.card_id = f.card_id
                 AND p.platform_id = f.platform_id
                 AND p.date_id = %s
                WHERE f.date_id = %s
                """,
                (previous, date_id),
            )
            compared, changed = cur.fetchone()
        cur.execute(
            "SELECT max(payload->'tcgplayer'->>'updatedAt') FROM raw.card_prices "
            "WHERE extracted_date = %s",
            (date_id,),
        )
        source_updated_at = cur.fetchone()[0]
    return FreshnessReport(date_id, previous, compared, changed, source_updated_at)


def send_ntfy(config: NtfyConfig, message: str) -> None:
    # Même convention que RELEASE_CHASER : POST du texte brut sur
    # <serveur>/<topic>. raise_for_status() fait échouer la tâche Airflow si
    # l'envoi rate, plutôt que de perdre l'alerte en silence.
    response = requests.post(
        f"{config.server.rstrip('/')}/{config.topic}",
        data=message.encode("utf-8"),
        headers={"Title": "CardTracker : prix figés", "Priority": "4", "Tags": "warning"},
        timeout=15,
    )
    response.raise_for_status()
