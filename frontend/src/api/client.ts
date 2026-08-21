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
