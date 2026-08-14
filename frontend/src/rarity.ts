// Fait correspondre le texte libre de rareté (tel que renvoyé par l'API,
// ex. "Illustration Rare", "Rare Holo") à une couleur d'accent -- utilisé
// pour le point coloré affiché devant chaque carte dans les tableaux.
// Correspondance par mot-clé (pas une liste exhaustive des ~30 libellés
// de rareté Pokémon TCG) : suffisant pour donner un repère visuel rapide
// sans avoir à maintenir une table exacte à jour à chaque nouvelle extension.
export function rarityColor(rarity: string | null): string {
  if (!rarity) return 'var(--rarity-common)'
  const value = rarity.toLowerCase()
  if (value.includes('illustration') || value.includes('secret') || value.includes('special')) {
    return 'var(--rarity-illustration)'
  }
  if (value.includes('holo') || value.includes('ultra') || value.includes('double')) {
    return 'var(--rarity-holo)'
  }
  if (value.includes('rare')) return 'var(--rarity-rare)'
  if (value.includes('uncommon')) return 'var(--rarity-uncommon)'
  return 'var(--rarity-common)'
}
