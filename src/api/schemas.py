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
    rarity: str | None
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


class MoverCard(BaseModel):
    card_id: str
    name: str
    quantity: int
    current_price: float
    past_price: float
    pct_change: float
    threshold: int


class OpportunityNews(BaseModel):
    title: str
    url: str
    source: str
    published_on: date
    event_type: str
    confidence: float
    resume: str | None


class Opportunity(BaseModel):
    card_id: str
    name: str
    set_name: str
    series: str | None
    rarity: str | None
    bloc: str | None
    side: str
    score: int
    signals: list[str]
    reason: str
    market_price: float
    low_price: float | None
    ma30: float | None
    change_7d: float | None
    change_30d: float | None
    owned_quantity: int
    # Au moins un signal dont le backtest à 30 jours tient (voir
    # queries.PROVEN_MIN_*), ou une news concordante, et aucune news contraire.
    is_selection: bool
    proven_signal: bool
    news: list[OpportunityNews]
    news_against: int


class OpportunityCounts(BaseModel):
    date_id: date | None
    buy_total: int
    buy_selection: int
    sell_total: int
    sell_selection: int


class OpportunityListResponse(BaseModel):
    items: list[Opportunity]
    total: int
    page: int
    page_size: int
    counts: OpportunityCounts


class SignalPerformance(BaseModel):
    side: str
    signal: str
    n: int
    n_7d: int
    hit_rate_7d: float | None
    avg_edge_7d: float | None
    n_30d: int
    hit_rate_30d: float | None
    avg_edge_30d: float | None


class SetTrend(BaseModel):
    set_name: str
    series: str | None
    cards: int
    median_change_30d: float
    buy_signals: int
    sell_signals: int
    news_up: int
    news_down: int


class MarketNews(BaseModel):
    title: str
    url: str
    source: str
    published_on: date
    direction: str
    event_type: str
    confidence: float
    resume: str | None
    cards: int
    sets: list[str]


class MetaShift(BaseModel):
    card_name: str
    share_now: float
    share_prev: float


class OpportunityTrends(BaseModel):
    sets: list[SetTrend]
    news: list[MarketNews]
    meta: list[MetaShift]
