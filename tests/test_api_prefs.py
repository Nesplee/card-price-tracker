# Réglages saisis dans le board, cartes recherchées et import de la collection
# (migration 017), plusieurs listes et cartes « obtenues » (migration 019),
# contre une base de test réelle et via le rôle dashboard_reader : on vérifie
# aussi ses droits (écriture dans prefs et via collection.apply_import seulement).
from __future__ import annotations

import os
from datetime import date, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from src.api.db import get_api_connection
from src.api.main import app

D = date(2026, 11, 14)


def _dsn(user: str, password: str) -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={user} password={password}"
    )


def _admin_dsn() -> str:
    return _dsn(os.environ["POSTGRES_ADMIN_USER"], os.environ["POSTGRES_ADMIN_PASSWORD"])


def _reader_dsn() -> str:
    return _dsn("dashboard_reader", os.environ["DASHBOARD_READER_PASSWORD"])


CARDS = [
    # (card_id, name, set_id, set_name, rarity)
    ("sv2-1", "Sprigatito", "sv2", "Paldea Evolved", "Common"),
    ("sv2-200", "Magikarp", "sv2", "Paldea Evolved", "Illustration Rare"),
    ("sv3pt5-199", "Charizard ex", "sv3pt5", "151", "Special Illustration Rare"),
    ("sv3pt5-205", "Mew ex", "sv3pt5", "151", "Hyper Rare"),
]


def _reset_wishlists(admin) -> None:
    """Une seule liste « Recherchées » (list_id 1), comme après la migration 017 :
    les tests des listes en créent, renomment et suppriment."""
    admin.execute("TRUNCATE TABLE prefs.wishlist RESTART IDENTITY CASCADE")
    admin.execute("INSERT INTO prefs.wishlist (name) VALUES ('Recherchées')")


@pytest.fixture
def client():
    with psycopg.connect(_admin_dsn()) as admin:
        admin.execute(
            "TRUNCATE TABLE prod.fact_opportunity, prod.dim_owned_card, prod.fact_price_history, "
            "prod.dim_card, collection.raw_import, collection.match_quarantine, "
            "collection.owned_card_removed, prefs.set_setting, prefs.card_setting, "
            "prefs.wishlist_card RESTART IDENTITY CASCADE"
        )
        _reset_wishlists(admin)
        admin.execute("INSERT INTO prefs.set_setting (set_id, masterset) VALUES ('sv2', true)")
        admin.cursor().executemany(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES (%s, %s, %s, %s, %s, 'Scarlet & Violet')",
            CARDS,
        )
        days = [D - timedelta(days=i) for i in range(10)]
        admin.cursor().executemany(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "VALUES (%s, 0, 0, 0, 0) ON CONFLICT DO NOTHING",
            [(d,) for d in days],
        )
        admin.cursor().executemany(
            "INSERT INTO prod.fact_price_history "
            "(card_id, date_id, platform_id, average_sell_price, low_price) "
            "SELECT %s, %s, platform_id, %s, %s FROM prod.dim_platform "
            "WHERE platform_name = 'tcgplayer'",
            [(c[0], d, 100.0 if d < D else 80.0, 60.0) for c in CARDS for d in days],
        )
        admin.execute(
            "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity) VALUES "
            "('sv2-1', 'Normal', 'Ungraded', 1), ('sv2-200', 'Holofoil', 'Ungraded', 2), "
            "('sv3pt5-199', 'Holofoil', 'Ungraded', 1)"
        )
        # Signaux du jour : un achat sur une carte possédée (calculé avant le
        # réglage), une vente sur chaque carte possédée.
        admin.execute(
            "INSERT INTO prod.fact_opportunity (date_id, card_id, side, score, signals, reason, "
            "market_price) VALUES "
            "(%(d)s, 'sv3pt5-199', 'buy', 60, ARRAY['rebond'], 'r', 80), "
            "(%(d)s, 'sv3pt5-205', 'buy', 60, ARRAY['actu_hausse'], 'r', 80), "
            "(%(d)s, 'sv2-1', 'sell', 60, ARRAY['pic'], 'r', 80), "
            "(%(d)s, 'sv2-200', 'sell', 60, ARRAY['pic'], 'r', 80), "
            "(%(d)s, 'sv3pt5-199', 'sell', 60, ARRAY['pic'], 'r', 80)",
            {"d": D},
        )
    conn = psycopg.connect(_reader_dsn(), row_factory=dict_row)

    def _override():
        yield conn

    app.dependency_overrides[get_api_connection] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()
    conn.close()
    with psycopg.connect(_admin_dsn()) as admin:
        _reset_wishlists(admin)


def _rules(card_id: str) -> dict:
    with psycopg.connect(_admin_dsn(), row_factory=dict_row) as admin:
        return admin.execute(
            "SELECT * FROM prefs.v_card_rules WHERE card_id = %s", (card_id,)
        ).fetchone()


def _trend_keys(client) -> set[tuple[str, str]]:
    body = client.get("/api/board/signals?days=7").json()
    return {(t["card_id"], t["side"]) for t in body["trends"]}


def test_signals_are_filtered_by_rules_at_read_time(client):
    keys = _trend_keys(client)
    # Achat sur une carte possédée : caché ; achat sur une carte non possédée : gardé.
    assert ("sv3pt5-199", "buy") not in keys and ("sv3pt5-205", "buy") in keys
    # Masterset sv2 : la commune seule est invendable, le doublon d'IR reste vendable.
    assert ("sv2-1", "sell") not in keys and ("sv2-200", "sell") in keys
    assert ("sv3pt5-199", "sell") in keys


def test_card_pref_partial_update_and_min_price(client):
    r = client.put("/api/prefs/cards/sv3pt5-199", json={"min_sell_price": 90})
    assert r.status_code == 200 and r.json()["prefs"]["min_sell_price"] == 90
    # Vente à 80 $ sous le seuil de 90 $ : cachée, y compris dans le passé.
    assert ("sv3pt5-199", "sell") not in _trend_keys(client)
    # Un autre champ seul ne touche pas au seuil.
    client.put("/api/prefs/cards/sv3pt5-199", json={"unsellable": True})
    rules = _rules("sv3pt5-199")
    assert rules["unsellable"] and float(rules["min_sell_price"]) == 90 and rules["sell_blocked"]
    # null retire le seuil.
    client.put("/api/prefs/cards/sv3pt5-199", json={"min_sell_price": None, "unsellable": False})
    rules = _rules("sv3pt5-199")
    assert rules["min_sell_price"] is None and not rules["sell_blocked"]
    assert ("sv3pt5-199", "sell") in _trend_keys(client)


def test_card_pref_rejects_unknown_card_and_bad_values(client):
    assert client.put("/api/prefs/cards/nope-1", json={"unsellable": True}).status_code == 404
    assert client.put("/api/prefs/cards/sv2-1", json={"min_sell_price": -3}).status_code == 422
    assert client.put("/api/prefs/cards/sv2-1", json={"unsellable": None}).status_code == 422


def test_unfollow_hides_every_signal(client):
    client.put("/api/prefs/cards/sv3pt5-205", json={"unfollowed": True})
    assert ("sv3pt5-205", "buy") not in _trend_keys(client)


def test_masterset_toggle_and_master_sets_flag(client):
    assert client.put("/api/prefs/sets/sv2", json={"masterset": False}).status_code == 200
    assert ("sv2-1", "sell") in _trend_keys(client)
    assert client.put("/api/prefs/sets/nope", json={"masterset": True}).status_code == 404
    client.put("/api/prefs/sets/sv3pt5", json={"masterset": True})
    sets = {s["set_id"]: s for s in client.get("/api/board/master-sets").json()["sets"]}
    # 151 n'atteint pas 20 % ici, mais il est marqué masterset : il est suivi.
    assert sets["sv3pt5"]["masterset"] is True and sets["sv2"]["masterset"] is False


def test_wishlist_add_target_and_remove(client):
    client.put("/api/prefs/cards/sv3pt5-205", json={"unfollowed": True})
    r = client.put("/api/prefs/wishlists/1/cards/sv3pt5-205", json={"target_price": 90})
    assert r.status_code == 200
    # Ajouter à la liste remet la carte en suivi.
    assert _rules("sv3pt5-205")["unfollowed"] is False
    # Carte possédée dans une liste : « obtenue », pas d'achat ; « encore
    # recherchée » (2e exemplaire) : ses achats reviennent.
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-199", json={})
    assert ("sv3pt5-199", "buy") not in _trend_keys(client)
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-199", json={"still_wanted": True})
    assert ("sv3pt5-199", "buy") in _trend_keys(client)
    body = client.get("/api/prefs/wishlists").json()
    assert [(x["list_id"], x["name"]) for x in body["lists"]] == [(1, "Recherchées")]
    mew = next(c for c in body["cards"] if c["card_id"] == "sv3pt5-205")
    assert mew["price"] == 80 and mew["target_price"] == 90 and mew["past"]["7"] == 100
    assert mew["to_target"] == pytest.approx(80 / 90 - 1) and mew["signal"]["today"] is True
    # Note seule : la cible reste.
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-205", json={"note": "  pour le binder "})
    mew = next(
        c
        for c in client.get("/api/prefs/wishlists").json()["cards"]
        if c["card_id"] == "sv3pt5-205"
    )
    assert mew["target_price"] == 90 and mew["note"] == "pour le binder"
    assert client.delete("/api/prefs/wishlists/1/cards/sv3pt5-205").status_code == 204
    assert client.put("/api/prefs/wishlists/9/cards/sv2-1", json={}).status_code == 404


def _wl(client) -> dict:
    return client.get("/api/prefs/wishlists").json()


def _line(client, list_id: int, card_id: str) -> dict:
    return next(
        c for c in _wl(client)["cards"] if c["list_id"] == list_id and c["card_id"] == card_id
    )


def test_wishlist_create_rename_delete(client):
    r = client.post("/api/prefs/wishlists", json={"name": "  Évolutions Rapides "})
    assert r.status_code == 200 and r.json()["name"] == "Évolutions Rapides"
    list_id = r.json()["list_id"]
    # Nom pris (casse exacte), vide ou trop long : refusé.
    assert client.post("/api/prefs/wishlists", json={"name": "Recherchées"}).status_code == 409
    assert client.post("/api/prefs/wishlists", json={"name": "   "}).status_code == 409
    assert client.post("/api/prefs/wishlists", json={"name": "x" * 61}).status_code == 422
    # Renommer : refusé si le nom est pris par une autre liste, permis sinon.
    url = f"/api/prefs/wishlists/{list_id}"
    assert client.put(url, json={"name": "Recherchées"}).status_code == 409
    assert client.put(url, json={"name": " 151 "}).json()["name"] == "151"
    assert client.put("/api/prefs/wishlists/99", json={"name": "z"}).status_code == 404
    assert [x["name"] for x in _wl(client)["lists"]] == ["Recherchées", "151"]
    # Supprimer : la liste et ses cartes partent, l'autre liste garde les siennes.
    client.put(f"{url}/cards/sv3pt5-205", json={"target_price": 70})
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-205", json={"target_price": 90})
    assert client.delete(url).status_code == 204
    body = _wl(client)
    assert [x["list_id"] for x in body["lists"]] == [1]
    assert [(c["list_id"], c["card_id"]) for c in body["cards"]] == [(1, "sv3pt5-205")]
    assert client.delete(url).status_code == 404
    # La carte elle-même (catalogue, réglages) n'est pas touchée.
    assert _rules("sv3pt5-205")["wishlisted"] is True
    # Même la liste de départ peut partir ; la carte n'est plus recherchée.
    assert client.delete("/api/prefs/wishlists/1").status_code == 204
    assert _wl(client)["lists"] == [] and _rules("sv3pt5-205") is None


def test_same_card_in_two_lists_with_own_target_and_note(client):
    other = client.post("/api/prefs/wishlists", json={"name": "151"}).json()["list_id"]
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-205", json={"target_price": 90, "note": "a"})
    client.put(f"/api/prefs/wishlists/{other}/cards/sv3pt5-205", json={"target_price": 70})
    assert _line(client, 1, "sv3pt5-205")["target_price"] == 90
    assert _line(client, 1, "sv3pt5-205")["note"] == "a"
    assert _line(client, other, "sv3pt5-205")["target_price"] == 70
    assert _line(client, other, "sv3pt5-205")["note"] is None
    # Le signal d'achat garde la cible la plus basse.
    assert _rules("sv3pt5-205")["target_price"] == 70
    # Retirée d'une liste : l'autre la garde, avec sa cible.
    client.delete(f"/api/prefs/wishlists/{other}/cards/sv3pt5-205")
    assert _rules("sv3pt5-205")["target_price"] == 90 and _rules("sv3pt5-205")["wishlisted"]
    # Une liste inconnue ou une carte inconnue : 404.
    assert client.put("/api/prefs/wishlists/99/cards/sv2-1", json={}).status_code == 404
    assert client.put("/api/prefs/wishlists/1/cards/nope-1", json={}).status_code == 404


def test_owned_card_is_obtained_until_still_wanted(client):
    # sv3pt5-199 est possédée (1 exemplaire) ; sv3pt5-205 ne l'est pas.
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-199", json={"target_price": 50})
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-205", json={})
    own, wanted = _line(client, 1, "sv3pt5-199"), _line(client, 1, "sv3pt5-205")
    assert own["obtained"] is True and own["still_wanted"] is False and own["active"] is False
    assert wanted["obtained"] is False and wanted["active"] is True
    # Obtenue : plus recherchée, achat bloqué, pas de signal dans la liste,
    # et sa cible ne compte plus.
    rules = _rules("sv3pt5-199")
    assert rules["wishlisted"] is False and rules["buy_blocked"] is True
    assert rules["target_price"] is None
    assert own["signal"] is None
    assert ("sv3pt5-199", "buy") not in _trend_keys(client)
    # Non possédée : recherchée, achat permis, signal du jour affiché.
    assert _rules("sv3pt5-205")["buy_blocked"] is False and wanted["signal"]["today"] is True
    # « Encore recherchée » : la carte redevient recherchée, avec sa cible.
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-199", json={"still_wanted": True})
    rules = _rules("sv3pt5-199")
    assert rules["wishlisted"] is True and rules["buy_blocked"] is False
    assert rules["target_price"] == 50
    own = _line(client, 1, "sv3pt5-199")
    assert own["obtained"] is True and own["active"] is True and own["signal"]["today"] is True
    assert ("sv3pt5-199", "buy") in _trend_keys(client)
    # Décochée : retour à « obtenue ». Valeur nulle refusée.
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-199", json={"still_wanted": False})
    assert _rules("sv3pt5-199")["buy_blocked"] is True
    r = client.put("/api/prefs/wishlists/1/cards/sv3pt5-199", json={"still_wanted": None})
    assert r.status_code == 422


def test_still_wanted_is_per_list(client):
    # Une carte possédée est recherchée si au moins une de ses lignes est cochée.
    other = client.post("/api/prefs/wishlists", json={"name": "151"}).json()["list_id"]
    client.put("/api/prefs/wishlists/1/cards/sv3pt5-199", json={"target_price": 40})
    client.put(f"/api/prefs/wishlists/{other}/cards/sv3pt5-199", json={"target_price": 60})
    assert _rules("sv3pt5-199")["wishlisted"] is False
    client.put(f"/api/prefs/wishlists/{other}/cards/sv3pt5-199", json={"still_wanted": True})
    rules = _rules("sv3pt5-199")
    # Seule la ligne cochée compte : sa cible (60), pas la plus basse des deux.
    assert rules["wishlisted"] is True and rules["target_price"] == 60
    assert _line(client, 1, "sv3pt5-199")["signal"] is None
    assert _line(client, other, "sv3pt5-199")["signal"]["today"] is True
    # Changer la cible d'une ligne ne touche pas la case.
    client.put(f"/api/prefs/wishlists/{other}/cards/sv3pt5-199", json={"target_price": 55})
    assert _line(client, other, "sv3pt5-199")["still_wanted"] is True


def test_wishlist_progress(client):
    other = client.post("/api/prefs/wishlists", json={"name": "151"}).json()["list_id"]
    # Liste 1 : une possédée (sv3pt5-199), une à 80 $ (sv3pt5-205), une à 80 $ (sv2-1 ? possédée).
    for cid in ("sv3pt5-199", "sv3pt5-205", "sv2-1", "sv2-200"):
        client.put(f"/api/prefs/wishlists/1/cards/{cid}", json={})
    client.put(f"/api/prefs/wishlists/{other}/cards/sv3pt5-205", json={})
    body = _wl(client)
    lists = {x["list_id"]: x for x in body["lists"]}
    # Possédées : sv3pt5-199, sv2-1, sv2-200 ; restante : sv3pt5-205 à 80 $.
    assert (lists[1]["n_cards"], lists[1]["n_obtained"]) == (4, 3)
    assert lists[1]["remaining_value"] == 80 and lists[1]["n_unpriced"] == 0
    assert (lists[other]["n_cards"], lists[other]["n_obtained"]) == (1, 0)
    assert lists[other]["remaining_value"] == 80
    # « Toutes » : chaque carte une fois, la valeur restante n'est pas doublée.
    assert body["total"]["n_cards"] == 4 and body["total"]["n_obtained"] == 3
    assert body["total"]["remaining_value"] == 80
    # « Encore recherchée » ne change pas la progression : la carte est possédée.
    client.put("/api/prefs/wishlists/1/cards/sv2-1", json={"still_wanted": True})
    assert {x["list_id"]: x for x in _wl(client)["lists"]}[1]["n_obtained"] == 3
    # Une carte sans prix du jour est comptée à part, pas à 0 sans le dire.
    with psycopg.connect(_admin_dsn()) as admin:
        admin.execute("DELETE FROM prod.fact_price_history WHERE card_id = 'sv3pt5-205'")
    lst = {x["list_id"]: x for x in _wl(client)["lists"]}[other]
    assert lst["remaining_value"] == 0 and lst["n_unpriced"] == 1


def test_reader_still_cannot_write_outside_prefs(client):
    with psycopg.connect(_reader_dsn()) as reader:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute(
                "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity) "
                "VALUES ('sv2-1', 'Reverse Holofoil', '', 1)"
            )
    with psycopg.connect(_reader_dsn()) as reader:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("UPDATE collection.raw_import SET quantity = 0")


# ---------- import depuis le board

HEADER = (
    "Portfolio Name,Category,Set,Product Name,Card Number,Rarity,Variance,Grade,"
    "Card Condition,Average Cost Paid,Quantity,Market Price (As of 2026-11-14),"
    "Price Override,Watchlist,Date Added,Notes\n"
)


def _csv(*lines: str) -> str:
    return HEADER + "".join(line + "\n" for line in lines)


EXPORT = _csv(
    "Main,Pokemon,Paldea Evolved,Sprigatito,001/193,Common,Normal,Ungraded,,0.10,1,0.12,,,,",
    "Main,Pokemon,Paldea Evolved,Magikarp,200/193,Illustration Rare,Holofoil,Ungraded,,40.00,2,"
    "80.00,,,,",
    "Main,Pokemon,SV: 151,Charizard ex,199/165,Special Illustration Rare,Holofoil,Ungraded,,"
    "150.00,1,80.00,,,,",
    "Main,Pokemon,SV: 151,Mew ex,205/165,Hyper Rare,Holofoil,Ungraded,,70.00,1,80.00,,,,",
    "Main,Pokemon,Unknown Set,Nobody,1/1,Rare,Holofoil,Ungraded,,1.00,1,1.00,,,,",
    "Main,One Piece,Romance Dawn,Luffy,OP01-001,L,,,,1.00,1,1.00,,,,",
)


def _owned() -> list[tuple]:
    with psycopg.connect(_admin_dsn()) as admin:
        return admin.execute(
            "SELECT card_id, variance, grade, quantity, average_cost_paid "
            "FROM prod.dim_owned_card ORDER BY 1, 2, 3"
        ).fetchall()


def test_import_preview_writes_nothing(client):
    before = _owned()
    p = client.post("/api/collection-import/preview", json={"csv": EXPORT}).json()
    assert _owned() == before
    assert [a["card_id"] for a in p["added"]] == ["sv3pt5-205"]
    # Les trois cartes déjà là reçoivent un prix payé : modifiées.
    assert {c["card_id"] for c in p["changed"]} == {"sv2-1", "sv2-200", "sv3pt5-199"}
    assert p["missing"] == [] and p["unmatched"] == 1


def test_import_apply_is_idempotent(client):
    r1 = client.post("/api/collection-import/apply", json={"csv": EXPORT}).json()
    assert r1 == {"raw": 5, "owned": 4, "quarantine": 1, "removed": 0}
    state = _owned()
    assert ("sv3pt5-205", "Holofoil", "Ungraded", 1, 70) in [
        (a, b, c, d, float(e)) for a, b, c, d, e in state
    ]
    # Même fichier une deuxième fois : rien à faire, rien ne change.
    p = client.post("/api/collection-import/preview", json={"csv": EXPORT}).json()
    assert p["added"] == [] and p["changed"] == [] and p["unchanged"] == 4
    client.post("/api/collection-import/apply", json={"csv": EXPORT})
    assert _owned() == state
    with psycopg.connect(_admin_dsn()) as admin:
        assert admin.execute("SELECT count(*) FROM collection.raw_import").fetchone()[0] == 5
        assert admin.execute("SELECT count(*) FROM collection.match_quarantine").fetchone()[0] == 1


def test_import_missing_cards_kept_unless_removed(client):
    client.post("/api/collection-import/apply", json={"csv": EXPORT})
    smaller = "\n".join(line for line in EXPORT.split("\n") if "Mew ex" not in line)
    p = client.post("/api/collection-import/preview", json={"csv": smaller}).json()
    assert [(m["card_id"], m["quantity"]) for m in p["missing"]] == [("sv3pt5-205", 1)]
    # Par défaut : gardée.
    client.post("/api/collection-import/apply", json={"csv": smaller})
    assert any(r[0] == "sv3pt5-205" for r in _owned())
    # Retrait choisi : la carte quitte la collection mais reste archivée.
    remove = [{"card_id": "sv3pt5-205", "variance": "Holofoil", "grade": "Ungraded"}]
    r = client.post("/api/collection-import/apply", json={"csv": smaller, "remove": remove})
    assert r.json()["removed"] == 1
    assert not any(r[0] == "sv3pt5-205" for r in _owned())
    with psycopg.connect(_admin_dsn()) as admin:
        assert admin.execute(
            "SELECT card_id, quantity FROM collection.owned_card_removed"
        ).fetchall() == [("sv3pt5-205", 1)]


def test_import_never_removes_a_card_present_in_the_export(client):
    remove = [{"card_id": "sv2-1", "variance": "Normal", "grade": "Ungraded"}]
    r = client.post("/api/collection-import/apply", json={"csv": EXPORT, "remove": remove})
    assert r.json()["removed"] == 0 and any(x[0] == "sv2-1" for x in _owned())


def test_import_reads_main_portfolio_only_by_default(client):
    # Les portefeuilles « MS - <set> » reprennent des cartes de Main : par
    # défaut ils sont ignorés, sinon chaque carte compterait double.
    doubled = EXPORT + "MS - Paldea Evolved,Pokemon,Paldea Evolved,Magikarp,200/193,"
    doubled += "Illustration Rare,Holofoil,Ungraded,,40.00,2,80.00,,,,\n"
    p = client.post("/api/collection-import/preview", json={"csv": doubled}).json()
    magikarp = next(c for c in p["changed"] if c["card_id"] == "sv2-200")
    assert magikarp["quantity"] == 2
    p = client.post(
        "/api/collection-import/preview", json={"csv": doubled, "all_portfolios": True}
    ).json()
    assert next(c for c in p["changed"] if c["card_id"] == "sv2-200")["quantity"] == 4


def test_import_rejects_bad_files(client):
    bad = client.post("/api/collection-import/preview", json={"csv": "a,b\n1,2\n"})
    assert bad.status_code == 422 and "colonnes absentes" in bad.json()["detail"]
    empty = client.post("/api/collection-import/apply", json={"csv": _csv()})
    assert empty.status_code == 422
    assert len(_owned()) == 3


# ---------- corrections de revue (2026-11-17)


def _quarantine() -> list[str]:
    with psycopg.connect(_admin_dsn()) as admin:
        return [
            r[0]
            for r in admin.execute(
                "SELECT r.product_name FROM collection.match_quarantine q "
                "JOIN collection.raw_import r ON r.id = q.raw_import_id ORDER BY 1"
            ).fetchall()
        ]


def _add_unknown_set_card() -> None:
    with psycopg.connect(_admin_dsn()) as admin:
        admin.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES ('unk-1', 'Nobody', 'unk', 'Unknown Set', 'Rare', 'Scarlet & Violet')"
        )


def test_quarantined_row_leaves_quarantine_once_matched(client):
    client.post("/api/collection-import/apply", json={"csv": EXPORT})
    assert _quarantine() == ["Nobody"]
    # Le catalogue connaît maintenant la carte : le même export la relie.
    _add_unknown_set_card()
    r = client.post("/api/collection-import/apply", json={"csv": EXPORT}).json()
    assert r["quarantine"] == 0 and _quarantine() == []
    assert any(x[0] == "unk-1" for x in _owned())


def test_script_and_board_give_the_same_result(client, tmp_path, monkeypatch):
    # Script (rôle pipeline_app), mêmes défauts que le board : Main seul,
    # communes comprises, écriture par collection.apply_import.
    from scripts import import_collection

    path = tmp_path / "export.csv"
    path.write_text(
        EXPORT + "MS - Paldea Evolved,Pokemon,Paldea Evolved,Magikarp,200/193,Illustration Rare,"
        "Holofoil,Ungraded,,40.00,2,80.00,,,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["import_collection", str(path)])
    import_collection.main()
    by_script = _owned()
    assert ("sv2-1", "Normal", "Ungraded", 1) in [r[:4] for r in by_script]  # commune gardée
    assert ("sv2-200", "Holofoil", "Ungraded", 2) in [r[:4] for r in by_script]  # Main seul
    assert _quarantine() == ["Nobody"]
    client.post("/api/collection-import/apply", json={"csv": path.read_text(encoding="utf-8")})
    assert _owned() == by_script
    # Le script sort aussi de la quarantaine une ligne reliée cette fois.
    _add_unknown_set_card()
    import_collection.main()
    assert _quarantine() == []


def test_import_too_big_is_refused_with_a_clear_message(client):
    big = EXPORT + "x" * 1_000_001
    for url in ("/api/collection-import/preview", "/api/collection-import/apply"):
        r = client.post(url, json={"csv": big})
        assert r.status_code == 413
        assert r.json()["detail"] == "Fichier trop gros : 1 Mo au plus (le serveur refuse au-delà)."
    assert len(_owned()) == 3


def test_min_sell_price_filters_past_sell_signals_at_read_time(client):
    # Vente passée à 70 $ (J-3) et vente du jour à 80 $ : un seuil à 75 $
    # cache seulement la première.
    with psycopg.connect(_admin_dsn()) as admin:
        admin.execute(
            "INSERT INTO prod.fact_opportunity (date_id, card_id, side, score, signals, "
            "reason, market_price) VALUES (%s, 'sv3pt5-199', 'sell', 60, ARRAY['pic'], 'r', 70)",
            (D - timedelta(days=3),),
        )

    def sell_dates():
        body = client.get("/api/board/signals?days=7").json()
        t = next(t for t in body["trends"] if (t["card_id"], t["side"]) == ("sv3pt5-199", "sell"))
        return t["dates"]

    assert sell_dates() == [str(D - timedelta(days=3)), str(D)]
    client.put("/api/prefs/cards/sv3pt5-199", json={"min_sell_price": 75})
    assert sell_dates() == [str(D)]
    # Sans seuil (NULL), rien n'est filtré.
    client.put("/api/prefs/cards/sv3pt5-199", json={"min_sell_price": None})
    assert sell_dates() == [str(D - timedelta(days=3)), str(D)]
