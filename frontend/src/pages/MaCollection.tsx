import { useEffect, useState } from 'react'
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { CardFilters, CollectionValuePoint, OwnedCard } from '../api/client'
import { fetchCollection, fetchCollectionValueHistory } from '../api/client'
import { FilterBar } from '../components/FilterBar'
import { rarityColor } from '../rarity'

export function MaCollection() {
  const [filters, setFilters] = useState<CardFilters>({})
  const [items, setItems] = useState<OwnedCard[]>([])
  const [valueHistory, setValueHistory] = useState<CollectionValuePoint[]>([])
  const [error, setError] = useState<string | null>(null)

  // Le tableau + les totaux répondent aux filtres (fetchCollection(filters)) ;
  // le graphe d'évolution reste basé sur la collection ENTIÈRE (l'API
  // /api/collection/value-history n'accepte pas de filtres -- hors scope
  // ici, le besoin exprimé porte sur la liste/les totaux, pas sur la courbe
  // historique) et n'est donc chargé qu'une fois, indépendamment de `filters`.
  useEffect(() => {
    fetchCollection(filters)
      .then((collection) => {
        setItems(collection.items)
        setError(null)
      })
      .catch((err) => setError(String(err)))
  }, [filters])

  useEffect(() => {
    fetchCollectionValueHistory()
      .then(setValueHistory)
      .catch((err) => setError(String(err)))
  }, [])

  function updateFilter(key: keyof CardFilters, value: CardFilters[keyof CardFilters]) {
    setFilters((prev) => ({ ...prev, [key]: value }))
  }

  // Valeur totale actuelle : uniquement les cartes au coût connu (voir
  // cost_unknown) pour rester cohérent avec le graphe de value-history
  // (qui applique déjà exactement ce filtre côté API). Calculée sur les
  // items déjà filtrés par l'API (filters) -- filtrer par bloc/rareté/etc.
  // recalcule donc la valeur du seul sous-ensemble affiché.
  const knownCostItems = items.filter((item) => !item.cost_unknown)
  const totalValue = knownCostItems.reduce((sum, item) => sum + (item.market_value ?? 0), 0)
  const totalGainLoss = knownCostItems.reduce((sum, item) => sum + (item.gain_loss ?? 0), 0)
  const unknownCostCount = items.length - knownCostItems.length

  return (
    <div className="page">
      <h1>Ma collection</h1>

      <FilterBar filters={filters} onChange={updateFilter} />

      {error && <p role="alert">{error}</p>}

      <div className="stat-row">
        <div className="stat-tile">
          <div className="stat-label">Valeur totale</div>
          <div className="stat-value">${totalValue.toFixed(2)}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Plus/moins-value</div>
          <div className="stat-value">${totalGainLoss.toFixed(2)}</div>
        </div>
      </div>

      {unknownCostCount > 0 && (
        <p role="note">{unknownCostCount} carte(s) au coût d'achat inconnu, exclue(s) de ce calcul.</p>
      )}

      <div className="chart-panel">
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={valueHistory}>
            <XAxis dataKey="date_id" tick={{ fontSize: 12 }} />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip />
            <Line type="monotone" dataKey="total_value" name="Valeur totale" stroke="#2451ff" />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <table>
        <thead>
          <tr>
            <th>Nom</th>
            <th>Quantité</th>
            <th className="cell-price">Coût moyen</th>
            <th className="cell-price">Prix actuel</th>
            <th className="cell-price">Valeur</th>
            <th className="cell-price">Plus/moins-value</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td className="card-name-cell">
                <span className="rarity-dot" style={{ background: rarityColor(item.rarity) }} />
                {item.name}
              </td>
              <td>{item.quantity}</td>
              <td className="cell-price">
                {item.cost_unknown ? 'inconnu' : `$${item.average_cost_paid?.toFixed(2)}`}
              </td>
              <td className="cell-price">
                {item.current_price !== null ? `$${item.current_price.toFixed(2)}` : '—'}
              </td>
              <td className="cell-price">
                {item.market_value !== null ? `$${item.market_value.toFixed(2)}` : '—'}
              </td>
              <td className="cell-price">
                {item.gain_loss !== null ? `$${item.gain_loss.toFixed(2)}` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
