// Barre de filtres réutilisée par Catalogue et MaCollection -- factorisée
// ici car les deux vues exposent désormais exactement les mêmes champs
// (recherche, bloc, série, rareté, plage de prix). onChange reçoit
// directement les valeurs déjà typées (pas d'événement DOM) pour rester
// agnostique de la façon dont l'appelant stocke ses filtres.
import type { CardFilters } from '../api/client'

interface FilterBarProps {
  filters: CardFilters
  onChange: (key: keyof CardFilters, value: string | number | undefined) => void
}

export function FilterBar({ filters, onChange }: FilterBarProps) {
  return (
    <div className="filter-bar">
      <div className="filter-field">
        <label htmlFor="filter-search">Recherche</label>
        <input
          id="filter-search"
          value={filters.search ?? ''}
          placeholder="Nom de carte"
          onChange={(e) => onChange('search', e.target.value)}
        />
      </div>
      <div className="filter-field">
        <label htmlFor="filter-series">Bloc</label>
        <input
          id="filter-series"
          value={filters.series ?? ''}
          placeholder="Scarlet & Violet..."
          onChange={(e) => onChange('series', e.target.value)}
        />
      </div>
      <div className="filter-field">
        <label htmlFor="filter-set-name">Série</label>
        <input
          id="filter-set-name"
          value={filters.set_name ?? ''}
          placeholder="Base Set..."
          onChange={(e) => onChange('set_name', e.target.value)}
        />
      </div>
      <div className="filter-field">
        <label htmlFor="filter-rarity">Rareté</label>
        <input
          id="filter-rarity"
          value={filters.rarity ?? ''}
          placeholder="Illustration..."
          onChange={(e) => onChange('rarity', e.target.value)}
        />
      </div>
      <div className="filter-field">
        <label htmlFor="filter-price-min">Prix min</label>
        <input
          id="filter-price-min"
          type="number"
          value={filters.price_min ?? ''}
          onChange={(e) => onChange('price_min', e.target.value ? Number(e.target.value) : undefined)}
        />
      </div>
      <div className="filter-field">
        <label htmlFor="filter-price-max">Prix max</label>
        <input
          id="filter-price-max"
          type="number"
          value={filters.price_max ?? ''}
          onChange={(e) => onChange('price_max', e.target.value ? Number(e.target.value) : undefined)}
        />
      </div>
    </div>
  )
}
