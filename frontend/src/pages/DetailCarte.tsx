import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { CardHistory } from '../api/client'
import { fetchCardHistory } from '../api/client'

export function DetailCarte() {
  const { cardId } = useParams<{ cardId: string }>()
  const [history, setHistory] = useState<CardHistory | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!cardId) return
    fetchCardHistory(cardId)
      .then((data) => {
        setHistory(data)
        setError(null)
      })
      .catch((err) => setError(String(err)))
  }, [cardId])

  if (error) return <p role="alert">{error}</p>
  if (!history) return <p className="page">Chargement...</p>

  return (
    <div className="page">
      <h1>{history.name}</h1>
      <div className="chart-panel">
        <ResponsiveContainer width="100%" height={380}>
          <LineChart data={history.history}>
            <XAxis dataKey="date_id" tick={{ fontSize: 12 }} />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip />
            <Line type="monotone" dataKey="average_sell_price" name="Prix moyen" stroke="#2451ff" />
            <Line type="monotone" dataKey="trend_price" name="Tendance" stroke="#16a34a" />
            <Line type="monotone" dataKey="low_price" name="Prix bas" stroke="#d97706" />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
