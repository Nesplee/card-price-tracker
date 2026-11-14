# Contrat des endpoints /api/opportunities*. Les requêtes SQL sont remplacées
# par des doubles : ces tests ne touchent à aucune base (la logique SQL a été
# vérifiée à part, en lecture seule, avec le rôle dashboard_reader).
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.api import queries
from src.api.db import get_api_connection
from src.api.main import app

ROW = {
    "card_id": "neo4-1", "name": "Dark Espeon", "set_name": "Neo Destiny", "series": "Neo",
    "rarity": "Rare Holo", "bloc": "retro", "side": "buy", "score": 90, "signals": ["rebond"],
    "reason": "baisse qui s'arrête", "market_price": 120.0, "low_price": 90.0, "ma30": 150.0,
    "change_7d": 0.02, "change_30d": -0.2, "owned_quantity": 0, "is_selection": True,
    "proven_signal": False, "news_against": 0,
    "news": [{
        "title": "Neo Destiny reprint rumor", "url": "https://example.org/a", "source": "reddit",
        "published_on": date(2026, 11, 11), "event_type": "rumeur", "confidence": 0.9,
        "resume": "résumé",
    }],
}
COUNTS = {"date_id": date(2026, 11, 14), "buy_total": 136, "buy_selection": 13,
          "sell_total": 12, "sell_selection": 2}


@pytest.fixture
def client():
    app.dependency_overrides[get_api_connection] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_opportunities_defaults_to_selection(client, monkeypatch):
    seen = {}

    def fake(conn, **kwargs):
        seen.update(kwargs)
        return [{**ROW, "total": 1}], 1, COUNTS

    monkeypatch.setattr(queries, "get_opportunities", fake)
    body = client.get("/api/opportunities").json()
    assert seen["selection_only"] is True and seen["page"] == 1
    assert body["total"] == 1 and body["counts"]["buy_selection"] == 13
    assert body["items"][0]["news"][0]["source"] == "reddit"


def test_list_opportunities_passes_filters(client, monkeypatch):
    seen = {}

    def fake(conn, **kwargs):
        seen.update(kwargs)
        return [], 0, COUNTS

    monkeypatch.setattr(queries, "get_opportunities", fake)
    resp = client.get("/api/opportunities?side=sell&selection=false&signal=chute&bloc=retro&min_score=60")
    assert resp.status_code == 200
    assert seen["side"] == "sell" and seen["selection_only"] is False
    assert seen["signal"] == "chute" and seen["bloc"] == "retro" and seen["min_score"] == 60


@pytest.mark.parametrize("query", ["side=hold", "bloc=moderne", "min_score=101", "page=0"])
def test_list_opportunities_rejects_invalid_params(client, query):
    assert client.get(f"/api/opportunities?{query}").status_code == 422


def test_performance_and_trends(client, monkeypatch):
    monkeypatch.setattr(queries, "get_signal_performance", lambda conn: [{
        "side": "buy", "signal": "momentum", "n": 10, "n_7d": 8, "hit_rate_7d": 0.5,
        "avg_edge_7d": 0.02, "n_30d": 5, "hit_rate_30d": None, "avg_edge_30d": None}])
    monkeypatch.setattr(queries, "get_opportunity_trends", lambda conn: {
        "sets": [{"set_name": "Neo Destiny", "series": "Neo", "cards": 9, "median_change_30d": -0.1,
                  "buy_signals": 2, "sell_signals": 0, "news_up": 1, "news_down": 0}],
        "news": [], "meta": [{"card_name": "Dragapult ex", "share_now": 0.3, "share_prev": 0.1}]})
    assert client.get("/api/opportunities/performance").json()[0]["signal"] == "momentum"
    trends = client.get("/api/opportunities/trends").json()
    assert trends["sets"][0]["set_name"] == "Neo Destiny" and trends["meta"][0]["share_now"] == 0.3
