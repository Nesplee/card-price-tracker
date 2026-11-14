// Point d'entrée unique pour tous les appels HTTP vers l'API FastAPI
// (src/api/main.py côté backend). Les pages (src/pages/*.tsx) importent ces
// fonctions plutôt que d'appeler fetch() directement -- un seul endroit à
// modifier si l'URL de base ou le format des réponses change.

export interface Card {
  card_id: string
  name: string
  series: string | null
  set_name: string
  rarity: string | null
  current_price: number | null
}

export interface CardListResponse {
  items: Card[]
  total: number
  page: number
  page_size: number
}

export interface PricePoint {
  date_id: string
  average_sell_price: number | null
  trend_price: number | null
  low_price: number | null
}

export interface CardHistory {
  card_id: string
  name: string
  history: PricePoint[]
}

export interface OwnedCard {
  id: number
  card_id: string
  name: string
  series: string | null
  set_name: string
  rarity: string | null
  variance: string
  grade: string
  quantity: number
  average_cost_paid: number | null
  cost_unknown: boolean
  current_price: number | null
  market_value: number | null
  gain_loss: number | null
}

export interface CollectionValuePoint {
  date_id: string
  total_value: number
}

export interface CardFilters {
  search?: string
  series?: string
  set_name?: string
  rarity?: string
  price_min?: number
  price_max?: number
  page?: number
}

// Construit la query string en ignorant les filtres non renseignés
// (undefined) -- évite d'envoyer "search=&series=" vides à l'API, qui les
// traiterait différemment de leur absence (voir _card_filters côté backend).
function buildQuery(filters: CardFilters): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== '') {
      params.set(key, String(value))
    }
  }
  return params.toString()
}

export async function fetchCards(filters: CardFilters): Promise<CardListResponse> {
  const response = await fetch(`/api/cards?${buildQuery(filters)}`)
  if (!response.ok) {
    throw new Error(`Erreur API /api/cards : ${response.status}`)
  }
  return response.json()
}

export async function fetchCardHistory(cardId: string): Promise<CardHistory> {
  const response = await fetch(`/api/cards/${encodeURIComponent(cardId)}/history`)
  if (!response.ok) {
    throw new Error(`Erreur API /api/cards/${cardId}/history : ${response.status}`)
  }
  return response.json()
}

export async function fetchCollection(
  filters: Omit<CardFilters, 'page'> = {},
): Promise<{ items: OwnedCard[] }> {
  const response = await fetch(`/api/collection?${buildQuery(filters)}`)
  if (!response.ok) {
    throw new Error(`Erreur API /api/collection : ${response.status}`)
  }
  return response.json()
}

export async function fetchCollectionValueHistory(): Promise<CollectionValuePoint[]> {
  const response = await fetch('/api/collection/value-history')
  if (!response.ok) {
    throw new Error(`Erreur API /api/collection/value-history : ${response.status}`)
  }
  return response.json()
}

export interface MoverCard {
  card_id: string
  name: string
  quantity: number
  current_price: number
  past_price: number
  pct_change: number
  threshold: number
}

export async function fetchMovers(windowSize: 7 | 30): Promise<MoverCard[]> {
  const response = await fetch(`/api/reports/movers?window=${windowSize}`)
  if (!response.ok) {
    throw new Error(`Erreur API /api/reports/movers : ${response.status}`)
  }
  return response.json()
}

// --- Opportunités (signaux d'achat / de vente)

export interface OpportunityNews {
  title: string
  url: string
  source: string
  published_on: string
  event_type: string
  confidence: number
  resume: string | null
}

export interface Opportunity {
  card_id: string
  name: string
  set_name: string
  series: string | null
  rarity: string | null
  bloc: string | null
  side: 'buy' | 'sell'
  score: number
  signals: string[]
  reason: string
  market_price: number
  low_price: number | null
  ma30: number | null
  change_7d: number | null
  change_30d: number | null
  owned_quantity: number
  is_selection: boolean
  proven_signal: boolean
  news: OpportunityNews[]
  news_against: number
}

export interface OpportunityCounts {
  date_id: string | null
  buy_total: number
  buy_selection: number
  sell_total: number
  sell_selection: number
}

export interface OpportunityList {
  items: Opportunity[]
  total: number
  page: number
  page_size: number
  counts: OpportunityCounts
}

export interface OpportunityFilters {
  side?: 'buy' | 'sell'
  selection?: boolean
  signal?: string
  bloc?: string
  min_score?: number
  search?: string
  page?: number
}

export interface SignalPerformance {
  side: string
  signal: string
  n: number
  n_7d: number
  hit_rate_7d: number | null
  avg_edge_7d: number | null
  n_30d: number
  hit_rate_30d: number | null
  avg_edge_30d: number | null
}

export interface SetTrend {
  set_name: string
  series: string | null
  cards: number
  median_change_30d: number
  buy_signals: number
  sell_signals: number
  news_up: number
  news_down: number
}

export interface MarketNews {
  title: string
  url: string
  source: string
  published_on: string
  direction: 'hausse' | 'baisse'
  event_type: string
  confidence: number
  resume: string | null
  cards: number
  sets: string[]
}

export interface MetaShift {
  card_name: string
  share_now: number
  share_prev: number
}

export interface OpportunityTrends {
  sets: SetTrend[]
  news: MarketNews[]
  meta: MetaShift[]
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) {
    throw new Error(`Erreur API ${path.split('?')[0]} : ${response.status}`)
  }
  return response.json()
}

export function fetchOpportunities(filters: OpportunityFilters): Promise<OpportunityList> {
  return getJson(`/api/opportunities?${buildQuery(filters as CardFilters)}`)
}

export function fetchSignalPerformance(): Promise<SignalPerformance[]> {
  return getJson('/api/opportunities/performance')
}

export function fetchOpportunityTrends(): Promise<OpportunityTrends> {
  return getJson('/api/opportunities/trends')
}
