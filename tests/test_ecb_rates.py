# Lecture des cours de référence BCE (src/extract/ecb_rates.py) : fonctions
# pures sur des extraits au format réel (vérifié le 2026-11-18 sur
# eurofxref-hist-90d.xml et eurofxref-hist.zip), HTTP mocké. Pas de base.
from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest
import requests
import responses

from src.extract.ecb_rates import (
    HIST_90D_URL,
    HIST_ZIP_URL,
    EcbClient,
    EcbRate,
    parse_hist_zip,
    parse_xml,
)
from src.load.fx_loader import check_rates

XML = b"""<?xml version="1.0" encoding="UTF-8"?><gesmes:Envelope
 xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<gesmes:subject>Reference rates</gesmes:subject>
<Cube><Cube time="2026-11-17"><Cube currency="USD" rate="1.0989"/><Cube currency="JPY"
 rate="175.05"/><Cube currency="CHF" rate="0.9233"/></Cube>
<Cube time="2026-11-16"><Cube currency="USD" rate="1.1014"/><Cube currency="CHF"
 rate="0.9244"/></Cube>
<Cube time="2026-11-13"><Cube currency="USD" rate="1.1052"/></Cube>
</Cube></gesmes:Envelope>"""


def _zip(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("eurofxref-hist.csv", csv_text)
    return buf.getvalue()


CSV = (
    "Date,USD,JPY,CHF,\n"
    "2026-11-17,1.0989,175.05,0.9233,\n"
    "2026-08-04,1.1515,170.1,0.9319,\n"
    "2026-08-03,1.1535,N/A,0.932,\n"
    "1999-01-04,1.1789,133.73,1.6168,\n"
)


def test_parse_xml_keeps_days_with_both_currencies_sorted():
    rates = parse_xml(XML)
    # Le 13/11 n'a pas de CHF dans cet extrait : ignoré, pas inventé.
    assert [r.ecb_date for r in rates] == [date(2026, 11, 16), date(2026, 11, 17)]
    last = rates[-1]
    assert last.usd_per_eur == Decimal("1.0989") and last.chf_per_eur == Decimal("0.9233")
    # Taux croisé : CHF pour 1 USD.
    assert round(last.usd_chf, 4) == Decimal("0.8402")


def test_parse_hist_zip_filters_and_skips_missing_values():
    rates = parse_hist_zip(_zip(CSV), since=date(2026, 8, 1))
    assert [r.ecb_date for r in rates] == [date(2026, 8, 3), date(2026, 8, 4), date(2026, 11, 17)]
    assert len(parse_hist_zip(_zip(CSV))) == 4
    # Une devise « N/A » : jour ignoré.
    bad = CSV.replace("2026-08-04,1.1515", "2026-08-04,N/A")
    assert date(2026, 8, 4) not in {r.ecb_date for r in parse_hist_zip(_zip(bad))}


def test_check_rates_refuses_empty_or_implausible_batches():
    with pytest.raises(ValueError, match="Aucun cours"):
        check_rates([])
    # Devises inversées (CHF pris pour l'USD) : 1 USD vaudrait 2 CHF.
    inverted = EcbRate(date(2026, 11, 17), Decimal("0.5"), Decimal("1.0"))
    with pytest.raises(ValueError, match="invraisemblables"):
        check_rates([inverted])
    check_rates(parse_xml(XML))


@responses.activate
def test_client_only_gets_and_retries_server_errors():
    responses.get(HIST_90D_URL, status=503)
    responses.get(HIST_90D_URL, body=XML)
    responses.get(HIST_ZIP_URL, body=_zip(CSV))
    client = EcbClient(max_attempts=3, backoff_seconds=0)
    assert len(client.last_90_days()) == 2
    assert [c.request.method for c in responses.calls] == ["GET", "GET"]
    assert len(client.history(since=date(2026, 8, 1))) == 3


@responses.activate
def test_client_does_not_retry_client_errors():
    responses.get(HIST_90D_URL, status=404)
    with pytest.raises(requests.HTTPError):
        EcbClient(max_attempts=3, backoff_seconds=0).last_90_days()
    assert len(responses.calls) == 1
