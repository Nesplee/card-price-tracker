# Modèles Pydantic des réponses de l'API. FastAPI s'en sert pour valider et
# sérialiser automatiquement les réponses JSON (response_model= sur chaque
# endpoint, voir src/api/main.py) -- si un endpoint renvoyait un champ
# manquant ou mal typé, FastAPI lèverait une erreur explicite plutôt que de
# laisser passer une réponse JSON incohérente vers le frontend.
from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class CardSummary(BaseModel):
    card_id: str
    name: str
    series: str | None
    set_name: str
    rarity: str | None
    current_price: float | None


class CardListResponse(BaseModel):
    items: list[CardSummary]
    total: int
    page: int
    page_size: int


class PricePoint(BaseModel):
    date_id: date
    average_sell_price: float | None
    trend_price: float | None
    low_price: float | None


class CardHistoryResponse(BaseModel):
    card_id: str
    name: str
    history: list[PricePoint]


class OwnedCard(BaseModel):
    id: int
    card_id: str
    name: str
    series: str | None
    set_name: str
    variance: str
    grade: str
    quantity: int
    average_cost_paid: float | None
    cost_unknown: bool
    current_price: float | None
    market_value: float | None
    gain_loss: float | None


class CollectionResponse(BaseModel):
    items: list[OwnedCard]


class CollectionValuePoint(BaseModel):
    date_id: date
    total_value: float
