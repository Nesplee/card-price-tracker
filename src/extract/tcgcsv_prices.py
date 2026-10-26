# Remplacement des prix TCGplayer de pokemontcg.io par ceux de TCGCSV.
# Contexte (2026-10-26) : pokemontcg.io renvoie depuis le 2026-10-15 des prix
# TCGplayer figés (tcgplayer.updatedAt bloqué), alors que TCGCSV
# (https://tcgcsv.com) republie chaque jour les prix TCGplayer, avec EXACTEMENT
# les mêmes champs (low/mid/high/market/directLow par variante d'impression).
# On garde donc pokemontcg.io pour le catalogue (noms, sets, raretés) et on
# réécrit seulement le bloc payload.tcgplayer de raw.card_prices avant
# clean_to_staging : le reste du pipeline (validate.py, staging, prod) reste
# inchangé.
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests
from psycopg import Connection
from psycopg.types.json import Jsonb
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from src.extract.pokemontcg_client import _is_retryable_error

logger = logging.getLogger(__name__)

# 3 = catégorie "Pokemon" chez TCGplayer (TCGCSV reprend leurs identifiants).
TCGCSV_BASE_URL = "https://tcgcsv.com/tcgplayer/3"
# Règles d'usage TCGCSV (https://tcgcsv.com/docs#usage-guidelines) : un
# User-Agent qui identifie l'application (les User-Agents génériques, dont
# celui de requests, sont bloqués en 401), ~100 ms entre deux requêtes, et une
# seule synchronisation par jour (c'est le cas : une tâche par DagRun).
_HEADERS = {"User-Agent": "CardPriceTracker/1.0"}
_REQUEST_PAUSE_SECONDS = 0.1
_PRODUCT_ID_RE = re.compile(r"/product/(\d+)")


def variant_key(sub_type_name: str) -> str:
    """Convertit le subTypeName TCGCSV au nom de variante pokemontcg.io :
    "Reverse Holofoil" -> "reverseHolofoil", "1st Edition Holofoil" ->
    "1stEditionHolofoil" (camelCase, c'est ce qu'attend validate.py)."""
    words = sub_type_name.split()
    return words[0].lower() + "".join(w[:1].upper() + w[1:] for w in words[1:])


def to_pokemontcg_prices(rows: list[dict]) -> dict:
    """Regroupe les lignes de prix TCGCSV d'un produit au format
    tcgplayer.prices de pokemontcg.io ({variante: {low, mid, ...}})."""
    return {
        variant_key(row["subTypeName"]): {
            "low": row.get("lowPrice"),
            "mid": row.get("midPrice"),
            "high": row.get("highPrice"),
            "market": row.get("marketPrice"),
            "directLow": row.get("directLowPrice"),
        }
        for row in rows
    }


class TcgCsvClient:
    def __init__(self, timeout_seconds: float = 30.0, max_attempts: int = 4) -> None:
        self._timeout_seconds = timeout_seconds
        self._retrying = Retrying(
            retry=retry_if_exception(_is_retryable_error),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        )

    def _get_json(self, url: str) -> dict:
        def do_get() -> dict:
            time.sleep(_REQUEST_PAUSE_SECONDS)
            response = requests.get(url, headers=_HEADERS, timeout=self._timeout_seconds)
            response.raise_for_status()
            return response.json()

        return self._retrying(do_get)

    def last_updated(self) -> str:
        """Horodatage de la dernière publication TCGCSV, ex: 2026-10-25T20:05:50+0000."""

        def do_get() -> str:
            response = requests.get(
                "https://tcgcsv.com/last-updated.txt", headers=_HEADERS, timeout=15
            )
            response.raise_for_status()
            return response.text.strip()

        return self._retrying(do_get)

    def fetch_all_prices(self) -> dict[int, dict]:
        """Tous les prix Pokémon TCGplayer, indexés par productId (~220 groupes,
        une requête par groupe)."""
        groups = self._get_json(f"{TCGCSV_BASE_URL}/groups")["results"]
        rows_by_product: dict[int, list[dict]] = {}
        for group in groups:
            for row in self._get_json(f"{TCGCSV_BASE_URL}/{group['groupId']}/prices")["results"]:
                rows_by_product.setdefault(row["productId"], []).append(row)
        return {pid: to_pokemontcg_prices(rows) for pid, rows in rows_by_product.items()}

    def resolve_product_id(self, tcgplayer_url: str) -> int | None:
        """Suit UNE redirection de prices.pokemontcg.io/tcgplayer/<card_id> pour
        lire le productId TCGplayer dans l'URL cible. None si introuvable."""

        def do_head() -> requests.Response:
            response = requests.head(
                tcgplayer_url, allow_redirects=False, timeout=self._timeout_seconds
            )
            if response.status_code >= 500:
                response.raise_for_status()
            return response

        try:
            location = self._retrying(do_head).headers.get("location", "")
        except requests.RequestException as exc:
            logger.warning("Redirection TCGplayer non résolue (%s) : %s", tcgplayer_url, exc)
            return None
        match = _PRODUCT_ID_RE.search(location)
        return int(match.group(1)) if match else None


def _map_new_cards(client: TcgCsvClient, conn: Connection, extracted_date: date) -> int:
    """Résout le productId des cartes du jour pas encore présentes dans
    raw.tcgplayer_product_map. Une carte non résolue n'est PAS enregistrée :
    elle sera retentée au run suivant."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.card_id, r.payload->'tcgplayer'->>'url'
            FROM raw.card_prices r
            LEFT JOIN raw.tcgplayer_product_map m ON m.card_id = r.card_id
            WHERE r.extracted_date = %s
              AND r.source = 'pokemontcg.io'
              AND m.card_id IS NULL
              AND r.payload->'tcgplayer'->>'url' IS NOT NULL
            """,
            (extracted_date,),
        )
        todo = cur.fetchall()
    if not todo:
        return 0
    logger.info("Résolution du productId TCGplayer pour %d nouvelles cartes", len(todo))
    with ThreadPoolExecutor(max_workers=6) as pool:
        product_ids = list(pool.map(lambda row: client.resolve_product_id(row[1]), todo))
    resolved = [(card_id, pid) for (card_id, _), pid in zip(todo, product_ids) if pid]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO raw.tcgplayer_product_map (card_id, product_id) VALUES (%s, %s) "
            "ON CONFLICT (card_id) DO UPDATE SET product_id = EXCLUDED.product_id, "
            "resolved_at = now()",
            resolved,
        )
    conn.commit()
    return len(resolved)


def apply_tcgcsv_prices(
    client: TcgCsvClient, conn: Connection, extracted_date: date
) -> tuple[int, int]:
    """Réécrit payload.tcgplayer des cartes du jour avec les prix TCGCSV.
    Retourne (cartes mises à jour, cartes sans prix TCGCSV -- elles gardent
    le bloc pokemontcg.io d'origine). Idempotent : relancer la tâche réécrit
    simplement les mêmes valeurs."""
    _map_new_cards(client, conn, extracted_date)
    prices = client.fetch_all_prices()
    # "2026-10-25T20:05:50+0000" -> "2026/10/25", même format que pokemontcg.io
    # (utilisé par le contrôle de fraîcheur, voir src/monitor/price_freshness.py).
    updated_at = client.last_updated()[:10].replace("-", "/")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.card_id, m.product_id, r.payload->'tcgplayer'->>'url'
            FROM raw.card_prices r
            JOIN raw.tcgplayer_product_map m ON m.card_id = r.card_id
            WHERE r.extracted_date = %s AND r.source = 'pokemontcg.io'
            """,
            (extracted_date,),
        )
        mapped = cur.fetchall()
        updates = [
            (
                Jsonb(
                    {
                        "url": url,
                        "productId": product_id,
                        "prices": prices[product_id],
                        "updatedAt": updated_at,
                        "source": "tcgcsv",
                    }
                ),
                card_id,
                extracted_date,
            )
            for card_id, product_id, url in mapped
            if prices.get(product_id)
        ]
        cur.executemany(
            "UPDATE raw.card_prices SET payload = jsonb_set(payload, '{tcgplayer}', %s) "
            "WHERE card_id = %s AND extracted_date = %s AND source = 'pokemontcg.io'",
            updates,
        )
        cur.execute(
            "SELECT count(*) FROM raw.card_prices WHERE extracted_date = %s "
            "AND source = 'pokemontcg.io'",
            (extracted_date,),
        )
        total = cur.fetchone()[0]
    conn.commit()
    logger.info(
        "Prix TCGCSV (publiés le %s) appliqués à %d/%d cartes", updated_at, len(updates), total
    )
    return len(updates), total - len(updates)
