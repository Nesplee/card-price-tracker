# Tests du contrôle de fraîcheur (src/monitor/price_freshness.py). Pas de
# connexion Postgres ici : on teste la décision d'alerte et l'envoi ntfy
# (mocké avec responses), la requête SQL a été vérifiée sur la vraie base.
from __future__ import annotations

from datetime import date

import pytest
import requests
import responses

from src.common.config import NtfyConfig
from src.monitor.price_freshness import FreshnessReport, send_ntfy


def _report(compared: int, changed: int) -> FreshnessReport:
    return FreshnessReport(date(2026, 10, 26), date(2026, 10, 25), compared, changed, "2026/10/15")


def test_is_stale_when_no_price_changed():
    assert _report(compared=19545, changed=0).is_stale


def test_not_stale_when_some_prices_changed():
    assert not _report(compared=19545, changed=8705).is_stale


def test_not_stale_without_previous_day_to_compare():
    assert not _report(compared=0, changed=0).is_stale


def test_message_mentions_source_update_date():
    assert "2026/10/15" in _report(compared=19545, changed=0).message()


@responses.activate
def test_send_ntfy_posts_message_to_topic():
    responses.post("https://ntfy.sh/topic-test", status=200)
    send_ntfy(NtfyConfig(server="https://ntfy.sh/", topic="topic-test"), "prix figés")
    assert responses.calls[0].request.body == "prix figés".encode()


@responses.activate
def test_send_ntfy_raises_on_http_error():
    responses.post("https://ntfy.sh/topic-test", status=500)
    with pytest.raises(requests.HTTPError):
        send_ntfy(NtfyConfig(server="https://ntfy.sh", topic="topic-test"), "prix figés")
