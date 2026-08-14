import { useEffect, useState } from 'react'
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { CollectionValuePoint, OwnedCard } from '../api/client'
import { fetchCollection, fetchCollectionValueHistory } from '../api/client'

export function MaCollection() {
  const [items, setItems] = useState<OwnedCard[]>([])
  const [valueHistory, setValueHistory] = useState<CollectionValuePoint[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([fetchCollection(), fetchCollectionValueHistory()])
      .then(([collection, history]) => {
        setItems(collection.items)
        setValueHistory(history)
        setError(null)
      })
      .catch((err) => setError(String(err)))
  }, [])

  if (error) return <p role="alert">{error}</p>

  // Valeur totale actuelle : uniquement les cartes au coût connu (voir
  // cost_unknown) pour rester cohérent avec le graphe de value-history
  // (qui applique déjà exactement ce filtre côté API).
  const knownCostItems = items.filter((item) => !item.cost_unknown)
  const totalValue = knownCostItems.reduce((sum, item) => sum + (item.market_value ?? 0), 0)
  const totalGainLoss = knownCostItems.reduce((sum, item) => sum + (item.gain_loss ?? 0), 0)
  const unknownCostCount = items.length - knownCostItems.length

  return (
    <div>
      <h1>Ma collection</h1>
      <p>
        Valeur totale : ${totalValue.toFixed(2)} (plus/moins-value : ${totalGainLoss.toFixed(2)})
      </p>
      {unknownCostCount > 0 && (
        <p role="note">
          {unknownCostCount} carte(s) au coût d'achat inconnu, exclue(s) de ce calcul.
        </p>
      )}

      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={valueHistory}>
          <XAxis dataKey="date_id" />
          <YAxis />
          <Tooltip />
          <Line type="monotone" dataKey="total_value" name="Valeur totale" stroke="#2563eb" />
        </LineChart>
      </ResponsiveContainer>

      <table>
        <thead>
          <tr>
            <th>Nom</th>
            <th>Quantité</th>
            <th>Coût moyen</th>
            <th>Prix actuel</th>
            <th>Valeur</th>
            <th>Plus/moins-value</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td>{item.name}</td>
              <td>{item.quantity}</td>
              <td>{item.cost_unknown ? 'inconnu' : `$${item.average_cost_paid?.toFixed(2)}`}</td>
              <td>{item.current_price !== null ? `$${item.current_price.toFixed(2)}` : '—'}</td>
              <td>{item.market_value !== null ? `$${item.market_value.toFixed(2)}` : '—'}</td>
              <td>{item.gain_loss !== null ? `$${item.gain_loss.toFixed(2)}` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
