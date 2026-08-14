import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Card, CardFilters } from '../api/client'
import { fetchCards } from '../api/client'

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

  function updateFilter<K extends keyof CardFilters>(key: K, value: CardFilters[K]) {
    setFilters((prev) => ({ ...prev, [key]: value, page: 1 }))
  }

  // Fonction séparée dédiée à la pagination : contrairement à updateFilter,
  // elle ne doit PAS réinitialiser la page à 1 (sinon on ne pourrait jamais
  // avancer). Bug corrigé : updateFilter('page', N) écrivait [key]: value
  // PUIS page: 1 dans le même objet littéral -- en JS, la dernière clé
  // gagne, donc "page" valait toujours 1 quel que soit N.
  function goToPage(page: number) {
    setFilters((prev) => ({ ...prev, page }))
  }

  return (
    <div>
      <h1>Catalogue</h1>
      <input
        placeholder="Rechercher une carte..."
        onChange={(e) => updateFilter('search', e.target.value)}
      />
      <input
        placeholder="Bloc"
        onChange={(e) => updateFilter('series', e.target.value)}
      />
      <input
        placeholder="Série"
        onChange={(e) => updateFilter('set_name', e.target.value)}
      />
      <input
        placeholder="Rareté"
        onChange={(e) => updateFilter('rarity', e.target.value)}
      />
      <input
        type="number"
        placeholder="Prix min"
        onChange={(e) => updateFilter('price_min', e.target.value ? Number(e.target.value) : undefined)}
      />
      <input
        type="number"
        placeholder="Prix max"
        onChange={(e) => updateFilter('price_max', e.target.value ? Number(e.target.value) : undefined)}
      />

      {error && <p role="alert">{error}</p>}

      <table>
        <thead>
          <tr>
            <th>Nom</th>
            <th>Bloc</th>
            <th>Série</th>
            <th>Rareté</th>
            <th>Prix</th>
          </tr>
        </thead>
        <tbody>
          {cards.map((card) => (
            <tr key={card.card_id}>
              <td>
                <Link to={`/cartes/${card.card_id}`}>{card.name}</Link>
              </td>
              <td>{card.series ?? '—'}</td>
              <td>{card.set_name}</td>
              <td>{card.rarity ?? '—'}</td>
              <td>{card.current_price !== null ? `$${card.current_price.toFixed(2)}` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <p>{total} résultat(s)</p>
      <button disabled={(filters.page ?? 1) <= 1} onClick={() => goToPage((filters.page ?? 1) - 1)}>
        Précédent
      </button>
      <button disabled={(filters.page ?? 1) * 25 >= total} onClick={() => goToPage((filters.page ?? 1) + 1)}>
        Suivant
      </button>
    </div>
  )
}
