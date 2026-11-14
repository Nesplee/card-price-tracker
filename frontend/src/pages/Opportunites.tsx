// Onglet « Opportunités » : signaux d'achat / de vente du dernier jour calculé
// (prod.fact_opportunity). Par défaut on n'affiche que la « sélection » :
// signaux dont le backtest à 30 jours tient, ou confirmés par une news
// concordante, sans news contraire (règle côté API, src/api/queries.py).
// Les tendances (sets, news, méta) donnent le contexte de ces signaux.
import { Fragment, useEffect, useState } from 'react'
import { Line, LineChart, ResponsiveContainer } from 'recharts'
import { Link } from 'react-router-dom'
import type {
  CardHistory,
  MarketNews,
  Opportunity,
  OpportunityList,
  OpportunityTrends,
  SetTrend,
  SignalPerformance,
} from '../api/client'
import {
  fetchCardHistory,
  fetchOpportunities,
  fetchOpportunityTrends,
  fetchSignalPerformance,
} from '../api/client'

const SIGNAL_LABELS: Record<string, string> = {
  annonce_sous_cote: 'annonce sous cote',
  rebond: 'rebond',
  momentum: 'momentum',
  pic: 'pic',
  plus_value: 'plus-value',
  chute: 'chute',
  actu_hausse: 'news hausse',
  actu_baisse: 'news baisse',
  meta_hausse: 'méta hausse',
  meta_baisse: 'méta baisse',
}
const label = (signal: string) => SIGNAL_LABELS[signal] ?? signal

// Un signal à moins de 30 cas mesurés à 30 jours n'est pas jugeable.
const MIN_CASES = 30

const pct = (v: number | null, digits = 1) =>
  v === null ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
const usd = (v: number | null) => (v === null ? '—' : `$${v.toFixed(2)}`)
const tone = (v: number | null) => (v === null || v === 0 ? '' : v > 0 ? 'mover-up' : 'mover-down')

function Sparkline({ cardId }: { cardId: string }) {
  const [history, setHistory] = useState<CardHistory | null>(null)
  useEffect(() => {
    fetchCardHistory(cardId).then(setHistory).catch(() => setHistory(null))
  }, [cardId])
  if (!history) return <div className="mini-mover-chart-placeholder" />
  return (
    <ResponsiveContainer width="100%" height={70}>
      <LineChart data={history.history.slice(-60)}>
        <Line type="monotone" dataKey="average_sell_price" stroke="#2451ff" dot={false} strokeWidth={2} />
      </LineChart>
    </ResponsiveContainer>
  )
}

function OpportunityRow({
  item,
  setTrend,
  open,
  onToggle,
}: {
  item: Opportunity
  setTrend: SetTrend | undefined
  open: boolean
  onToggle: () => void
}) {
  return (
    <Fragment>
      <tr className="opp-row" onClick={onToggle}>
        <td>
          <Link to={`/cartes/${item.card_id}`} onClick={(e) => e.stopPropagation()}>
            {item.name}
          </Link>
          <div className="opp-sub">
            {item.set_name}
            {item.owned_quantity > 0 && ` · possédée ×${item.owned_quantity}`}
          </div>
        </td>
        <td className="cell-price">
          <strong>{item.score}</strong>
        </td>
        <td>
          {item.signals.map((s) => (
            <span key={s} className="opp-chip">
              {label(s)}
            </span>
          ))}
          {item.proven_signal && <span className="opp-chip opp-chip-proven">éprouvé</span>}
          {item.news.length > 0 && <span className="opp-chip opp-chip-news">{item.news.length} news</span>}
        </td>
        <td className="cell-price">{usd(item.market_price)}</td>
        <td className="cell-price">
          <span className={tone(item.change_7d)}>{pct(item.change_7d)}</span>
        </td>
        <td className="cell-price">
          <span className={tone(item.change_30d)}>{pct(item.change_30d)}</span>
        </td>
      </tr>
      {open && (
        <tr className="opp-detail">
          <td colSpan={6}>
            <div className="opp-detail-grid">
              <div>
                <p>{item.reason}</p>
                <p className="opp-sub">
                  Moyenne 30 j {usd(item.ma30)} · annonce la moins chère {usd(item.low_price)}
                  {setTrend && (
                    <>
                      {' '}· set « {item.set_name} » : médiane {pct(setTrend.median_change_30d)} sur 30 j
                    </>
                  )}
                </p>
                {item.news_against > 0 && (
                  <p className="opp-warn">{item.news_against} news dans le sens contraire.</p>
                )}
                {item.news.map((n) => (
                  <p key={n.url} className="opp-news">
                    <a href={n.url} target="_blank" rel="noreferrer">
                      {n.title}
                    </a>
                    <span className="opp-sub">
                      {' '}
                      {n.source} · {n.published_on}
                      {n.resume && ` — ${n.resume}`}
                    </span>
                  </p>
                ))}
              </div>
              <Sparkline cardId={item.card_id} />
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  )
}

function PerformanceTable({ rows }: { rows: SignalPerformance[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Signal</th>
          <th>Sens</th>
          <th className="cell-price">Cas (30 j)</th>
          <th className="cell-price">Réussite 7 j</th>
          <th className="cell-price">Réussite 30 j</th>
          <th className="cell-price">Rendement moyen 30 j</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.side}-${r.signal}`}>
            <td>{label(r.signal)}</td>
            <td>{r.side === 'buy' ? 'achat' : 'vente'}</td>
            <td className="cell-price">
              {r.n_30d}
              {r.n_30d < MIN_CASES && <span className="opp-chip">trop récent</span>}
            </td>
            <td className="cell-price">{pct(r.hit_rate_7d, 0)}</td>
            <td className="cell-price">{pct(r.hit_rate_30d, 0)}</td>
            <td className="cell-price">
              <span className={tone(r.avg_edge_30d)}>{pct(r.avg_edge_30d)}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function NewsList({ news }: { news: MarketNews[] }) {
  if (news.length === 0) return <p role="note">Aucune news pertinente cette semaine.</p>
  return (
    <ul className="opp-list">
      {news.map((n) => (
        <li key={n.url}>
          <span className={`mover-badge ${n.direction === 'hausse' ? 'mover-up' : 'mover-down'}`}>
            {n.direction}
          </span>{' '}
          <a href={n.url} target="_blank" rel="noreferrer">
            {n.title}
          </a>
          <div className="opp-sub">
            {n.source} · {n.published_on} · {n.cards} cartes{n.sets.length > 0 && ` (${n.sets.join(', ')})`}
            {n.resume && ` — ${n.resume}`}
          </div>
        </li>
      ))}
    </ul>
  )
}

function TrendsPanel({ trends }: { trends: OpportunityTrends }) {
  return (
    <>
      <h2>Tendances des sets (30 jours)</h2>
      <table>
        <thead>
          <tr>
            <th>Set</th>
            <th className="cell-price">Cartes suivies</th>
            <th className="cell-price">Prix médian 30 j</th>
            <th className="cell-price">Signaux achat / vente</th>
            <th className="cell-price">News hausse / baisse (14 j)</th>
          </tr>
        </thead>
        <tbody>
          {trends.sets.slice(0, 15).map((s) => (
            <tr key={s.set_name}>
              <td>{s.set_name}</td>
              <td className="cell-price">{s.cards}</td>
              <td className="cell-price">
                <span className={tone(s.median_change_30d)}>{pct(s.median_change_30d)}</span>
              </td>
              <td className="cell-price">
                {s.buy_signals} / {s.sell_signals}
              </td>
              <td className="cell-price">
                {s.news_up} / {s.news_down}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>News du marché (7 jours)</h2>
      <NewsList news={trends.news} />

      <h2>Méta compétitive (part des top 8, semaine contre précédente)</h2>
      {trends.meta.length === 0 ? (
        <p role="note">Pas assez de tournois pour comparer.</p>
      ) : (
        <ul className="opp-list">
          {trends.meta.map((m) => (
            <li key={m.card_name}>
              {m.card_name} : {(m.share_prev * 100).toFixed(0)}% → {(m.share_now * 100).toFixed(0)}%{' '}
              <span className={tone(m.share_now - m.share_prev)}>
                ({m.share_now >= m.share_prev ? '+' : ''}
                {((m.share_now - m.share_prev) * 100).toFixed(0)} pts)
              </span>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

export function Opportunites() {
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [selection, setSelection] = useState(true)
  const [signal, setSignal] = useState('')
  const [bloc, setBloc] = useState('')
  const [minScore, setMinScore] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState<OpportunityList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [perf, setPerf] = useState<SignalPerformance[]>([])
  const [trends, setTrends] = useState<OpportunityTrends | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)

  useEffect(() => {
    fetchSignalPerformance().then(setPerf).catch(() => setPerf([]))
    fetchOpportunityTrends().then(setTrends).catch(() => setTrends(null))
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => {
      fetchOpportunities({
        side,
        selection,
        signal: signal || undefined,
        bloc: bloc || undefined,
        min_score: minScore === '' ? undefined : Number(minScore),
        search: search || undefined,
        page,
      })
        .then((d) => {
          setData(d)
          setError(null)
        })
        .catch((err) => setError(String(err)))
    }, 250)
    return () => clearTimeout(timer)
  }, [side, selection, signal, bloc, minScore, search, page])

  const change = <T,>(setter: (v: T) => void) => (v: T) => {
    setter(v)
    setPage(1)
  }
  const counts = data?.counts
  const sideSignals = Object.keys(SIGNAL_LABELS).filter((s) =>
    side === 'buy'
      ? ['annonce_sous_cote', 'rebond', 'momentum', 'actu_hausse', 'meta_hausse'].includes(s)
      : ['pic', 'plus_value', 'chute', 'actu_baisse', 'meta_baisse'].includes(s),
  )
  const trendBySet = new Map((trends?.sets ?? []).map((s) => [s.set_name, s]))
  const lastPage = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1

  return (
    <div className="page">
      <h1>Opportunités</h1>
      {counts && (
        <p className="opp-sub">
          Signaux du {counts.date_id} : {counts.buy_selection} achats et {counts.sell_selection} ventes retenus
          sur {counts.buy_total} et {counts.sell_total} détectés.
        </p>
      )}

      <div className="range-selector">
        <button className={`range-btn ${side === 'buy' ? 'active' : ''}`} onClick={() => change(setSide)('buy')}>
          Achats
        </button>
        <button className={`range-btn ${side === 'sell' ? 'active' : ''}`} onClick={() => change(setSide)('sell')}>
          Ventes (ma collection)
        </button>
      </div>

      <div className="filter-bar">
        <div className="filter-field">
          <label htmlFor="opp-selection">Affichage</label>
          <select
            id="opp-selection"
            value={selection ? 'sel' : 'all'}
            onChange={(e) => change(setSelection)(e.target.value === 'sel')}
          >
            <option value="sel">Sélection (qualité)</option>
            <option value="all">Tous les signaux</option>
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="opp-signal">Signal</label>
          <select id="opp-signal" value={signal} onChange={(e) => change(setSignal)(e.target.value)}>
            <option value="">Tous</option>
            {sideSignals.map((s) => (
              <option key={s} value={s}>
                {label(s)}
              </option>
            ))}
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="opp-bloc">Bloc</label>
          <select id="opp-bloc" value={bloc} onChange={(e) => change(setBloc)(e.target.value)}>
            <option value="">Tous</option>
            <option value="recent">Récent</option>
            <option value="ancien">Ancien</option>
            <option value="retro">Rétro</option>
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="opp-score">Score min.</label>
          <input
            id="opp-score"
            type="number"
            min={0}
            max={100}
            value={minScore}
            onChange={(e) => change(setMinScore)(e.target.value)}
          />
        </div>
        <div className="filter-field">
          <label htmlFor="opp-search">Recherche</label>
          <input id="opp-search" value={search} placeholder="Nom de carte" onChange={(e) => change(setSearch)(e.target.value)} />
        </div>
      </div>

      {error && <p role="alert">{error}</p>}
      {data && data.items.length === 0 && (
        <p role="note">
          Aucun signal ne passe ces filtres.
          {selection && ' Passe sur « Tous les signaux » pour voir les signaux non confirmés.'}
        </p>
      )}
      {data && data.items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Carte</th>
              <th className="cell-price">Score</th>
              <th>Signaux</th>
              <th className="cell-price">Prix</th>
              <th className="cell-price">7 j</th>
              <th className="cell-price">30 j</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((item) => (
              <OpportunityRow
                key={`${item.card_id}-${item.side}`}
                item={item}
                setTrend={trendBySet.get(item.set_name)}
                open={openId === item.card_id}
                onToggle={() => setOpenId(openId === item.card_id ? null : item.card_id)}
              />
            ))}
          </tbody>
        </table>
      )}
      {data && lastPage > 1 && (
        <div className="range-selector">
          <button className="range-btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            Précédent
          </button>
          <span className="opp-sub">
            Page {page} / {lastPage}
          </span>
          <button className="range-btn" disabled={page >= lastPage} onClick={() => setPage(page + 1)}>
            Suivant
          </button>
        </div>
      )}

      {trends && <TrendsPanel trends={trends} />}

      <h2>Fiabilité des signaux</h2>
      <p className="opp-sub">
        Chaque recommandation passée est comparée au prix réel 7 et 30 jours plus tard. « Éprouvé » = au
        moins 100 cas à 30 jours, 65 % de réussite et rendement moyen positif.
      </p>
      <PerformanceTable rows={perf} />
    </div>
  )
}
