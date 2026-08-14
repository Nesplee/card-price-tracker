# Application FastAPI du dashboard sur mesure. Chaque endpoint : (1) valide
# ses paramètres, (2) délègue la requête SQL à src/api/queries.py, (3)
# calcule les champs dérivés simples (market_value, gain_loss) et sérialise
# via les modèles de src/api/schemas.py. Aucune écriture n'est possible ici :
# la connexion (get_api_connection) utilise le rôle dashboard_reader,
# lecture seule au niveau Postgres lui-même (migration 007).
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query

from src.api import queries
from src.api.db import get_api_connection
from src.api.schemas import (
    CardHistoryResponse,
    CardListResponse,
    CardSummary,
    CollectionResponse,
    CollectionValuePoint,
    OwnedCard,
    PricePoint,
)

app = FastAPI(title="Card Price Tracker — Dashboard API")


@app.get("/api/health")
def health_check() -> dict:
    # Utilisé par le healthcheck Docker du service dashboard-api (voir
    # docker-compose.prod.yml, Task 5) -- ne touche pas la base de données :
    # un problème de connexion Postgres ne doit pas faire passer le conteneur
    # "unhealthy" alors que le processus FastAPI lui-même tourne normalement.
    return {"status": "ok"}


@app.get("/api/cards", response_model=CardListResponse)
def list_cards(
    conn=Depends(get_api_connection),
    search: str | None = None,
    series: str | None = None,
    set_name: str | None = None,
    rarity: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    page: int = Query(default=1, ge=1),
) -> CardListResponse:
    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=422, detail="price_min doit être <= price_max")
    page_size = 25
    rows, total = queries.search_cards(
        conn,
        search=search,
        series=series,
        set_name=set_name,
        rarity=rarity,
        price_min=price_min,
        price_max=price_max,
        page=page,
        page_size=page_size,
    )
    # Chaque ligne renvoyée par search_cards correspond déjà exactement aux
    # champs de CardSummary (card_id, name, series, set_name, rarity,
    # current_price) -- le COUNT total est calculé séparément par une requête
    # dédiée (voir src/api/queries.py), donc pas de clé parasite à filtrer ici.
    items = [CardSummary(**row) for row in rows]
    return CardListResponse(items=items, total=total, page=page, page_size=page_size)


@app.get("/api/cards/{card_id}/history", response_model=CardHistoryResponse)
def card_history(card_id: str, conn=Depends(get_api_connection)) -> CardHistoryResponse:
    result = queries.get_card_history(conn, card_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Carte inconnue")
    card, history = result
    return CardHistoryResponse(
        card_id=card["card_id"],
        name=card["name"],
        history=[PricePoint(**row) for row in history],
    )


@app.get("/api/collection", response_model=CollectionResponse)
def collection(
    conn=Depends(get_api_connection),
    search: str | None = None,
    series: str | None = None,
    set_name: str | None = None,
    rarity: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
) -> CollectionResponse:
    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=422, detail="price_min doit être <= price_max")
    rows = queries.get_owned_cards(
        conn,
        search=search,
        series=series,
        set_name=set_name,
        rarity=rarity,
        price_min=price_min,
        price_max=price_max,
    )
    items = []
    for row in rows:
        current_price = row["current_price"]
        cost_unknown = row["cost_unknown"]
        # market_value : nécessite juste un prix actuel connu.
        # gain_loss : nécessite EN PLUS un coût d'achat connu (cost_unknown
        # == False) -- sinon on comparerait un vrai prix de marché à un coût
        # arbitraire (0), ce qui produirait un gain/perte trompeur.
        market_value = row["quantity"] * current_price if current_price is not None else None
        gain_loss = (
            row["quantity"] * (current_price - row["average_cost_paid"])
            if current_price is not None and not cost_unknown
            else None
        )
        items.append(OwnedCard(**row, market_value=market_value, gain_loss=gain_loss))
    return CollectionResponse(items=items)


@app.get("/api/collection/value-history", response_model=list[CollectionValuePoint])
def collection_value_history(conn=Depends(get_api_connection)) -> list[CollectionValuePoint]:
    rows = queries.get_collection_value_history(conn)
    return [CollectionValuePoint(**row) for row in rows]
