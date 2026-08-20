import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { CardHistory, PricePoint } from '../api/client'
import { fetchCardHistory } from '../api/client'

type RangeOption = '7' | '30' | '90' | 'all'

const RANGE_OPTIONS: { value: RangeOption; label: string }[] = [
  { value: '7', label: '7j' },
  { value: '30', label: '30j' },
  { value: '90', label: '90j' },
  { value: 'all', label: 'Tout' },
]

interface ChartPoint extends PricePoint {
  ma7: number | null
  ma15: number | null
  ma30: number | null
}

// Moyenne mobile simple sur average_sell_price, calculée sur la série
// complète (avant tout filtrage de plage) -- sinon la MA30 serait tronquée
// sur les premiers jours d'une plage affichée de 30j. Ignore les valeurs
// nulles éventuelles plutôt que de propager null dès qu'un jour manque de
// prix.
function withMovingAverages(history: PricePoint[]): ChartPoint[] {
  function movingAverage(index: number, window: number): number | null {
    const slice = history.slice(Math.max(0, index - window + 1), index + 1)
    const values = slice.map((p) => p.average_sell_price).filter((v): v is number => v !== null)
    if (values.length === 0) return null
    return values.reduce((sum, v) => sum + v, 0) / values.length
  }

  return history.map((point, index) => ({
    ...point,
    ma7: movingAverage(index, 7),
    ma15: movingAverage(index, 15),
    ma30: movingAverage(index, 30),
  }))
}

export function DetailCarte() {
  const { cardId } = useParams<{ cardId: string }>()
  const [history, setHistory] = useState<CardHistory | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [range, setRange] = useState<RangeOption>('30')

  useEffect(() => {
    if (!cardId) return
    fetchCardHistory(cardId)
      .then((data) => {
        setHistory(data)
        setError(null)
      })
      .catch((err) => setError(String(err)))
  }, [cardId])

  const fullSeries = useMemo(() => (history ? withMovingAverages(history.history) : []), [history])

  const visibleSeries = useMemo(() => {
    if (range === 'all') return fullSeries
    return fullSeries.slice(-Number(range))
  }, [fullSeries, range])

  const stats = useMemo(() => {
    if (fullSeries.length === 0) return null
    const currentPrice = fullSeries[fullSeries.length - 1].average_sell_price
    const priceNDaysAgo = (n: number) => {
      const index = fullSeries.length - 1 - n
      return index >= 0 ? fullSeries[index].average_sell_price : null
    }
    const pctChange = (n: number) => {
      const past = priceNDaysAgo(n)
      if (currentPrice === null || past === null || past === 0) return null
      return ((currentPrice - past) / past) * 100
    }
    const visiblePrices = visibleSeries
      .map((p) => p.average_sell_price)
      .filter((v): v is number => v !== null)
    return {
      currentPrice,
      change7d: pctChange(7),
      change30d: pctChange(30),
      min: visiblePrices.length > 0 ? Math.min(...visiblePrices) : null,
      max: visiblePrices.length > 0 ? Math.max(...visiblePrices) : null,
    }
  }, [fullSeries, visibleSeries])

  if (error) return <p role="alert">{error}</p>
  if (!history) return <p className="page">Chargement...</p>

  function formatPrice(value: number | null): string {
    return value !== null ? `$${value.toFixed(2)}` : '—'
  }

  function formatChange(value: number | null): string {
    if (value === null) return '—'
    const sign = value > 0 ? '+' : ''
    return `${sign}${value.toFixed(1)}%`
  }

  return (
    <div className="page">
      <h1>{history.name}</h1>

      {stats && (
        <div className="stat-row">
          <div className="stat-tile">
            <div className="stat-label">Prix actuel</div>
            <div className="stat-value">{formatPrice(stats.currentPrice)}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-label">Variation 7j</div>
            <div className="stat-value">{formatChange(stats.change7d)}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-label">Variation 30j</div>
            <div className="stat-value">{formatChange(stats.change30d)}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-label">Min / Max ({RANGE_OPTIONS.find((r) => r.value === range)?.label})</div>
            <div className="stat-value">
              {formatPrice(stats.min)} / {formatPrice(stats.max)}
            </div>
          </div>
        </div>
      )}

      <div className="chart-panel">
        <div className="range-selector">
          {RANGE_OPTIONS.map((option) => (
            <button
              key={option.value}
              className={`range-btn${range === option.value ? ' active' : ''}`}
              onClick={() => setRange(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <ResponsiveContainer width="100%" height={380}>
          <LineChart data={visibleSeries}>
            <XAxis dataKey="date_id" tick={{ fontSize: 12 }} />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip />
            <Line type="monotone" dataKey="average_sell_price" name="Prix moyen" stroke="#2451ff" />
            <Line type="monotone" dataKey="trend_price" name="Tendance" stroke="#16a34a" />
            <Line type="monotone" dataKey="low_price" name="Prix bas" stroke="#d97706" />
            <Line type="monotone" dataKey="ma7" name="MM 7j" stroke="#9333ea" dot={false} strokeDasharray="4 2" />
            <Line type="monotone" dataKey="ma15" name="MM 15j" stroke="#db2777" dot={false} strokeDasharray="4 2" />
            <Line type="monotone" dataKey="ma30" name="MM 30j" stroke="#0891b2" dot={false} strokeDasharray="4 2" />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
