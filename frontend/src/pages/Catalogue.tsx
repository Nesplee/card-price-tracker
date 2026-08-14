import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Card, CardFilters } from '../api/client'
import { fetchCards } from '../api/client'
import { FilterBar } from '../components/FilterBar'
import { rarityColor } from '../rarity'

const PAGE_SIZE = 25

export function Catalogue() {
  const [filters, setFilters] = useState<CardFilters>({ page: 1 })
  const [cards, setCards] = useState<Card[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)

  // Se redéclenche à chaque changement de filtre ou de page -- pas de
  // debounce sur la recherche texte dans cette première version (YAGNI :
  // 19 545 cartes, une requête filtrée reste rapide, à revisiter seulement
  // si un vrai ralentissement est observé).
  useEffect(() => {
    fetchCards(filters)
      .then((response) => {
        setCards(response.items)
        setTotal(response.total)
        setError(null)
      })
      .catch((err) => setError(String(err)))
  }, [filters])

  function updateFilter(key: keyof CardFilters, value: CardFilters[keyof CardFilters]) {
    setFilters((prev) => ({ ...prev, [key]: value, page: 1 }))
  }

  // Fonction séparée dédiée à la pagination : contrairement à updateFilter,
  // elle ne doit PAS réinitialiser la page à 1 (sinon on ne pourrait jamais
  // avancer).
  function goToPage(page: number) {
    setFilters((prev) => ({ ...prev, page }))
  }

  return (
    <div className="page">
      <h1>Catalogue</h1>

      <FilterBar filters={filters} onChange={updateFilter} />

      {error && <p role="alert">{error}</p>}

      <table>
        <thead>
          <tr>
            <th>Nom</th>
            <th>Bloc</th>
            <th>Série</th>
            <th>Rareté</th>
            <th className="cell-price">Prix</th>
          </tr>
        </thead>
        <tbody>
          {cards.map((card) => (
            <tr key={card.card_id}>
              <td>
                <Link to={`/cartes/${card.card_id}`} className="card-name-cell">
                  <span className="rarity-dot" style={{ background: rarityColor(card.rarity) }} />
                  {card.name}
                </Link>
              </td>
              <td>{card.series ?? '—'}</td>
              <td>{card.set_name}</td>
              <td>{card.rarity ?? '—'}</td>
              <td className="cell-price">
                {card.current_price !== null ? `$${card.current_price.toFixed(2)}` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="pagination">
        <span className="pagination-count">{total} résultat(s)</span>
        <button disabled={(filters.page ?? 1) <= 1} onClick={() => goToPage((filters.page ?? 1) - 1)}>
          Précédent
        </button>
        <button
          disabled={(filters.page ?? 1) * PAGE_SIZE >= total}
          onClick={() => goToPage((filters.page ?? 1) + 1)}
        >
          Suivant
        </button>
      </div>
    </div>
  )
}
