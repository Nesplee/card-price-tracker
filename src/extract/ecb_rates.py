# Cours de référence de la Banque centrale européenne (eurofxref), pour
# convertir les prix TCGplayer (USD) en francs suisses (migration 020).
#
# Source officielle, gratuite, sans clé ni compte :
#   - https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml : les
#     90 derniers jours ouvrés (passage quotidien, rattrape aussi les trous) ;
#   - https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip : tout
#     l'historique depuis 1999 (CSV), seulement si la table est vide.
# Les cours sont publiés vers 16:00 CET, les jours ouvrés de la BCE ; ils
# sont exprimés contre l'euro (1 EUR = x USD, 1 EUR = y CHF).
#
# LECTURE SEULE : GET uniquement. Les fonctions de lecture sont pures et
# testées sur fixtures (tests/test_ecb_rates.py).
from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import requests
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from src.extract.pokemontcg_client import _is_retryable_error

ECB_BASE_URL = "https://www.ecb.europa.eu/stats/eurofxref"
HIST_90D_URL = f"{ECB_BASE_URL}/eurofxref-hist-90d.xml"
HIST_ZIP_URL = f"{ECB_BASE_URL}/eurofxref-hist.zip"
_USER_AGENT = "CardPriceTracker/1.0"

# Espace de noms du XML eurofxref (les balises <Cube> en dépendent).
_NS = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"


@dataclass(frozen=True)
class EcbRate:
    ecb_date: date
    usd_per_eur: Decimal
    chf_per_eur: Decimal

    @property
    def usd_chf(self) -> Decimal:
        """Taux croisé : CHF pour 1 USD."""
        return self.chf_per_eur / self.usd_per_eur


def _positive(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        return None
    return number if number > 0 else None


def parse_xml(content: bytes) -> list[EcbRate]:
    """Cours USD et CHF d'un fichier eurofxref XML (quotidien ou 90 jours).
    Un jour où l'une des deux devises manque est ignoré."""
    root = ET.fromstring(content)
    rates = []
    for day in root.iter(f"{_NS}Cube"):
        when = day.get("time")
        if when is None:
            continue
        by_currency = {c.get("currency"): c.get("rate") for c in day.findall(f"{_NS}Cube")}
        usd, chf = _positive(by_currency.get("USD")), _positive(by_currency.get("CHF"))
        if usd and chf:
            rates.append(EcbRate(date.fromisoformat(when), usd, chf))
    return sorted(rates, key=lambda r: r.ecb_date)


def parse_hist_zip(content: bytes, since: date | None = None) -> list[EcbRate]:
    """Cours USD et CHF de l'archive eurofxref-hist.zip (un CSV, une ligne par
    jour ouvré, « N/A » quand une devise n'était pas cotée)."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".csv"))
        text = archive.read(name).decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    header = [h.strip() for h in rows[0]]
    i_date, i_usd, i_chf = header.index("Date"), header.index("USD"), header.index("CHF")
    rates = []
    for row in rows[1:]:
        if len(row) <= max(i_date, i_usd, i_chf) or not row[i_date].strip():
            continue
        when = date.fromisoformat(row[i_date].strip())
        if since and when < since:
            continue
        usd, chf = _positive(row[i_usd]), _positive(row[i_chf])
        if usd and chf:
            rates.append(EcbRate(when, usd, chf))
    return sorted(rates, key=lambda r: r.ecb_date)


class EcbClient:
    """Téléchargement des fichiers eurofxref (GET seulement, nouvelles
    tentatives sur 5xx, 429 et erreurs réseau)."""

    def __init__(
        self, timeout_seconds: float = 60.0, max_attempts: int = 4, backoff_seconds: float = 2.0
    ) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds

    def _get(self, url: str) -> bytes:
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=self._backoff_seconds, max=30),
            retry=retry_if_exception(_is_retryable_error),
            reraise=True,
        ):
            with attempt:
                response = self._session.get(url, timeout=self._timeout_seconds)
                response.raise_for_status()
                return response.content
        raise AssertionError("unreachable")

    def last_90_days(self) -> list[EcbRate]:
        return parse_xml(self._get(HIST_90D_URL))

    def history(self, since: date | None = None) -> list[EcbRate]:
        return parse_hist_zip(self._get(HIST_ZIP_URL), since)
