import { useEffect, useState } from 'react'
import { Line, LineChart, ResponsiveContainer } from 'recharts'
import { Link } from 'react-router-dom'
import type { CardHistory, MoverCard } from '../api/client'
import { fetchCardHistory, fetchMovers } from '../api/client'

function formatPrice(value: number): string {
  return `$${value.toFixed(2)}`
}

function formatChange(value: number): string {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(1)}%`
}

function MoversTable({ movers }: { movers: MoverCard[] }) {
  if (movers.length === 0) {
    return <p role="note">Aucun mouvement notable sur cette période.</p>
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Nom</th>
          <th>Quantité</th>
          <th className="cell-price">Prix passé</th>
          <th className="cell-price">Prix actuel</th>
          <th className="cell-price">Variation</th>
        </tr>
      </thead>
      <tbody>
        {movers.map((mover) => (
          <tr key={mover.card_id}>
            <td>
              <Link to={`/cartes/${mover.card_id}`}>{mover.name}</Link>
            </td>
            <td className="cell-price">{mover.quantity}</td>
            <td className="cell-price">{formatPrice(mover.past_price)}</td>
            <td className="cell-price">{formatPrice(mover.current_price)}</td>
            <td className="cell-price">
              <span className={`mover-badge mover-badge-${mover.threshold} ${mover.pct_change > 0 ? 'mover-up' : 'mover-down'}`}>
                {formatChange(mover.pct_change)}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// Mini-graphique d'aperçu (pas de sélecteur de plage ni de moyennes
// mobiles -- c'est un aperçu top/flop, pas la fiche carte complète de
// DetailCarte.tsx). `history` peut être `undefined` pendant le chargement
// -- affiche un espace réservé plutôt que de planter Recharts sur un
// tableau vide.
function MiniMoverCard({ mover, history }: { mover: MoverCard; history: CardHistory | undefined }) {
  return (
    <Link to={`/cartes/${mover.card_id}`} className="mini-mover-card">
      <div className="mini-mover-name">{mover.name}</div>
      <div className={`mini-mover-change ${mover.pct_change > 0 ? 'mover-up' : 'mover-down'}`}>
        {formatChange(mover.pct_change)}
      </div>
      <div className="mini-mover-chart">
        {history ? (
          <ResponsiveContainer width="100%" height={60}>
            <LineChart data={history.history}>
              <Line type="monotone" dataKey="average_sell_price" stroke="#2451ff" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="mini-mover-chart-placeholder" />
        )}
      </div>
    </Link>
  )
}

function TopMoversPanel({
  title,
  movers,
  histories,
}: {
  title: string
  movers: MoverCard[]
  histories: Map<string, CardHistory>
}) {
  if (movers.length === 0) return null
  return (
    <div className="top-movers-panel">
      <h3>{title}</h3>
      <div className="top-movers-grid">
        {movers.map((mover) => (
          <MiniMoverCard key={mover.card_id} mover={mover} history={histories.get(mover.card_id)} />
        ))}
      </div>
    </div>
  )
}

export function Rapports() {
  const [daily, setDaily] = useState<MoverCard[]>([])
  const [weekly, setWeekly] = useState<MoverCard[]>([])
  const [histories, setHistories] = useState<Map<string, CardHistory>>(new Map())
  const [dailyError, setDailyError] = useState<string | null>(null)
  const [weeklyError, setWeeklyError] = useState<string | null>(null)

  useEffect(() => {
    fetchMovers(7)
      .then(setDaily)
      .catch((err) => setDailyError(String(err)))
    fetchMovers(30)
      .then(setWeekly)
      .catch((err) => setWeeklyError(String(err)))
  }, [])

  const topGains = weekly.filter((m) => m.pct_change > 0).slice(0, 5)
  const topLosses = [...weekly]
    .filter((m) => m.pct_change < 0)
    .sort((a, b) => a.pct_change - b.pct_change)
    .slice(0, 5)

  useEffect(() => {
    // Un fetch indépendant par carte du top/flop : l'échec d'une seule
    // (ex : historique incomplet) ne doit pas empêcher l'affichage des
    // autres mini-graphiques.
    const cardIds = [...topGains, ...topLosses].map((m) => m.card_id)
    cardIds.forEach((cardId) => {
      if (histories.has(cardId)) return
      fetchCardHistory(cardId)
        .then((history) => {
          setHistories((prev) => new Map(prev).set(cardId, history))
        })
        .catch(() => {
          // Silencieux : MiniMoverCard affiche un espace réservé si
          // l'historique n'arrive jamais pour cette carte.
        })
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weekly])

  return (
    <div className="page">
      <h1>Rapports</h1>

      <h2>Daily (7 jours)</h2>
      {dailyError && <p role="alert">{dailyError}</p>}
      <MoversTable movers={daily} />

      <h2>Weekly (30 jours)</h2>
      {weeklyError && <p role="alert">{weeklyError}</p>}
      <TopMoversPanel title="Top 5 hausses" movers={topGains} histories={histories} />
      <TopMoversPanel title="Top 5 baisses" movers={topLosses} histories={histories} />
      <MoversTable movers={weekly} />
    </div>
  )
}
