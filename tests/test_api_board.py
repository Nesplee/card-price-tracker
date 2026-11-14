# Contrat des endpoints /api/board/* et du calcul des mouvements. Les requêtes
# SQL sont remplacées par des doubles : aucun accès à une base (les tests à base
# réelle de ce dossier vident des tables, ils ne doivent jamais viser la prod).
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api import board
from src.api.db import get_api_connection
from src.api.main import app

D = date(2026, 11, 14)


class FakeConn:
    """Renvoie, dans l'ordre, les lignes prévues pour chaque appel à execute()."""

    def __init__(self, *results):
        self.results = list(results)

    def execute(self, sql, params=None):
        rows = self.results.pop(0)
        return type("Cur", (), {"fetchall": lambda s: rows, "fetchone": lambda s: rows[0]})()


def test_past_takes_last_point_within_slack():
    h = [(D - timedelta(days=12), 10.0), (D - timedelta(days=8), 12.0), (D, 15.0)]
    assert board._past(h, D - timedelta(days=7), 4) == (D - timedelta(days=8), 12.0)
    # Trop loin dans le passé : pas de prix de référence plutôt qu'un prix faux.
    assert board._past(h, D - timedelta(days=1), 3) is None
    assert board._past(h, D - timedelta(days=30), 4) is None


def test_collection_board_values_and_moves():
    cards = [
        {
            "card_id": "a",
            "name": "Mew ex",
            "set_name": "Paldean Fates",
            "series": "SV",
            "rarity": "Special Illustration Rare",
            "quantity": 2,
            "variances": ["Holofoil"],
            "grades": [None],
        },
        {
            "card_id": "b",
            "name": "Pikachu",
            "set_name": "Base",
            "series": "Base",
            "rarity": "Common",
            "quantity": 1,
            "variances": [None],
            "grades": ["PSA 10"],
        },
    ]
    prices = [
        {"card_id": "a", "date_id": D - timedelta(days=d), "price": p}
        for d, p in ((7, 100), (1, 95), (0, 90))
    ]
    prices += [
        {"card_id": "b", "date_id": D - timedelta(days=d), "price": p}
        for d, p in ((7, 10), (0, 12))
    ]
    sigs = [{"card_id": "a", "side": "sell", "score": 60, "signals": ["chute"], "reason": "r"}]
    out = board.get_collection_board(FakeConn(cards, prices, sigs))
    assert out["as_of"] == D and out["total_value"] == 2 * 90 + 12
    assert out["windows"][7]["delta"] == (2 * 90 + 12) - (2 * 100 + 10)
    # Pas de point à J-1 pour b : la valeur de ce jour-là ne compte que a.
    assert [h["value"] for h in out["history"]] == [210, 190, 192]
    a = next(c for c in out["cards"] if c["card_id"] == "a")
    assert a["price"] == 90 and a["past"][7] == 100 and a["past"][1] == 95
    assert a["signals"][0]["side"] == "sell" and a["variances"] == ["Holofoil"]
    b = next(c for c in out["cards"] if c["card_id"] == "b")
    assert b["grades"] == ["PSA 10"] and b["past"][7] == 10 and b["past"][1] is None
    # Mini-courbes seulement pour les raretés recherchées (pas la Common).
    assert set(out["sparks"]) == {"a"} and len(out["sparks"]["a"]) == len(out["dates"])


@pytest.fixture
def client():
    app.dependency_overrides[get_api_connection] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_signals_endpoint_passes_days_and_serializes_dates(client, monkeypatch):
    seen = {}

    def fake(conn, *, days):
        seen["days"] = days
        return {"as_of": D, "days": days, "items": [], "news": [], "meta": []}

    monkeypatch.setattr(board, "get_signals_board", fake)
    body = client.get("/api/board/signals?days=14").json()
    assert seen["days"] == 14 and body["as_of"] == "2026-11-14"


@pytest.mark.parametrize("days", [0, 31])
def test_signals_endpoint_rejects_out_of_range_days(client, days):
    assert client.get(f"/api/board/signals?days={days}").status_code == 422


def test_master_sets_value_whole_set_and_owned_copies():
    sets = [
        {
            "set_id": "sv2",
            "set_name": "Paldea Evolved",
            "n_cards": 2,
            "owned_cards": 2,
            "entries": 3,
            "copies": 4,
        }
    ]
    cards = [
        {
            "card_id": "sv2-1",
            "name": "Sprigatito",
            "rarity": "Common",
            "quantity": 3,
            "variances": ["Normal", "Reverse Holofoil"],
        },
        {
            "card_id": "sv2-2",
            "name": "Magikarp",
            "rarity": "Illustration Rare",
            "quantity": 1,
            "variances": ["Holofoil"],
        },
    ]
    prices = [
        {"card_id": "sv2-1", "date_id": D - timedelta(days=7), "price": 1.0},
        {"card_id": "sv2-1", "date_id": D, "price": 2.0},
        {"card_id": "sv2-2", "date_id": D - timedelta(days=7), "price": 100.0},
        {"card_id": "sv2-2", "date_id": D, "price": 90.0},
    ]
    out = board.get_master_sets(FakeConn(sets, cards, prices))
    st = out["sets"][0]
    # Set complet = un exemplaire de chaque carte ; nos exemplaires = quantités.
    assert st["history"][-1] == {"date": D, "set_value": 92.0, "owned_value": 96.0, "priced": 2}
    assert st["windows"][7]["delta"] == 92.0 - 101.0
    magikarp = next(c for c in st["cards"] if c["card_id"] == "sv2-2")
    assert magikarp["price"] == 90.0 and magikarp["past"][7] == 100.0
