# Card Price Tracker — Import de la collection personnelle (CSV) — Design

**Statut :** approuvé par l'utilisateur (2026-08-09), en attente de plan d'implémentation.

## Contexte et problème

L'utilisateur a fourni `export.csv` (2013 lignes), un export de sa collection personnelle de cartes depuis une app tierce de gestion de portefeuille. Il veut suivre, dans la base de données de ce projet, toutes les cartes Pokémon de sa collection dont la rareté n'est ni `Common` ni `Uncommon` — pour poser les fondations du futur dashboard (Task 4, Mois 3, stretch optionnel) : valeur de la collection dans le temps, croisée avec `prod.fact_price_history`.

Le CSV sera **ré-envoyé régulièrement** (décision utilisateur) : l'import doit être un processus réutilisable et idempotent, pas un script à usage unique.

## Analyse des données fournies

- 2013 lignes au total, 2 catégories : `Pokemon` (1952 lignes) et `One Piece` (hors scope, exclu).
- Sur les lignes Pokémon : 1079 restent après exclusion `Common`/`Uncommon`/rareté vide.
- **Trois `Portfolio Name` distincts** : `Main` (1229 lignes, toutes catégories), `MS - Paldea Evolved` (455) et `MS - Paldean Fates` (326). Vérifié : 769 des 781 lignes `MS - *` sont des doublons exacts de lignes déjà dans `Main` (même set/numéro/variante/quantité) — ces portfolios servent à l'utilisateur pour suivre la progression de mastersets en parallèle, pas des cartes distinctes. **Décision : seul `Portfolio Name = Main` est importé**, pour éviter tout double comptage dans la valeur totale de la collection.
- Le CSV ne contient aucun identifiant `pokemontcg.io` — seulement `Set` (nom), `Card Number` (format `"011/193"`, avec le total de la série), `Product Name`. Un rapprochement est nécessaire pour relier chaque ligne à `prod.dim_card`.
- Vérifié directement sur les données de production : `prod.dim_card.card_id` suit **toujours** le format `{set_id}-{numéro}` (ex. `swsh6-132`), et le `numéro` n'est pas complété par des zéros de tête. Ça rend le rapprochement déterministe une fois le `set_id` connu (voir section Rapprochement), sans avoir besoin de comparer des noms de cartes.
- 35 noms de sets uniques dans le CSV (catégorie Pokémon) ; certains ne correspondront probablement pas telles quelles à la nomenclature pokemontcg.io (ex. `"SV: 151"`, `"Miscellaneous Cards & Products"`).
- Une carte peut apparaître en plusieurs déclinaisons distinctes (même set/numéro, `Variance` différente — ex. `Holofoil` vs `Reverse Holofoil` — voire `Grade` différent). Vérifié : `(Set, Card Number, Product Name, Variance)` est unique dans `Main` sur les données actuelles ; `Grade` est inclus dans la clé naturelle de la table finale par précaution (voir plus bas), au cas où l'utilisateur possède un jour la même variante gradée ET non gradée.

## Décision

### Filtres appliqués à l'import

`Portfolio Name = 'Main'` ET `Category = 'Pokemon'` ET `Rarity NOT IN ('Common', 'Uncommon', '')`.

### Rapprochement (matching) avec le catalogue existant

1. Récupérer la liste complète des sets pokemontcg.io via `GET /v2/sets` (même client HTTP que `PokemonTcgClient`, nouvelle méthode) — construit une correspondance `nom de set (CSV) -> set_id (pokemontcg.io)`.
2. Pour chaque ligne du CSV : normaliser `Card Number` (retirer le total après le `/`, retirer les zéros de tête : `"011/193"` -> `"11"`), construire `card_id = f"{set_id}-{numéro}"`, vérifier son existence dans `prod.dim_card`.
3. **Aucun matching approximatif (fuzzy)** : un nom de set sans correspondance exacte dans `/v2/sets`, ou un `card_id` construit absent de `dim_card`, part en quarantaine avec la raison précise plutôt que de risquer un rapprochement silencieusement erroné (une carte reliée à la mauvaise fiche fausserait la valeur affichée — inacceptable pour une donnée qui représente de l'argent réel). Une table de correspondance manuelle pour les noms de sets non reconnus automatiquement (ex. `"SV: 151"`) pourra être ajoutée plus tard, hors scope de ce premier import.

### Schéma de données

Nouveau schéma `collection`, séparé de `raw`/`staging`/`prod` (domaine distinct : "ce que l'utilisateur possède", pas "quels sont les prix du marché") :

```sql
CREATE TABLE collection.raw_import (
    id bigserial PRIMARY KEY,
    set_name text NOT NULL,
    card_number text NOT NULL,      -- format brut du CSV, ex "011/193"
    product_name text NOT NULL,
    variance text,
    grade text,
    rarity text NOT NULL,
    quantity int NOT NULL,
    average_cost_paid numeric,
    market_price_at_export numeric, -- valeur "Market Price (As of ...)" du CSV, référence historique seulement
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (set_name, card_number, product_name, variance, grade)
);

CREATE TABLE collection.match_quarantine (
    id bigserial PRIMARY KEY,
    raw_import_id bigint NOT NULL REFERENCES collection.raw_import(id),
    rejection_reason text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);
```

Table finale, dans `prod` (jointe au reste du schéma en étoile pour le futur dashboard) :

```sql
CREATE TABLE prod.dim_owned_card (
    id bigserial PRIMARY KEY,
    card_id text NOT NULL REFERENCES prod.dim_card(card_id),
    variance text,
    grade text,
    quantity int NOT NULL,
    average_cost_paid numeric,
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (card_id, variance, grade)
);
```

`dashboard_reader` (rôle Metabase existant) reçoit `SELECT` sur cette nouvelle table, même pattern que le reste de `prod`.

### Réimport (idempotence)

Un nouvel export CSV se réimporte sans dupliquer : `collection.raw_import` et `prod.dim_owned_card` utilisent tous deux un `UPSERT` (`ON CONFLICT ... DO UPDATE`) sur leur clé naturelle — une quantité modifiée entre deux imports met à jour la ligne existante, exactement le même principe que `load_cards`/`load_staging` déjà en place dans le pipeline de prix.

### Point de terminaison

Script manuel réutilisable `scripts/import_collection.py <chemin-vers-csv>`, suivant le même pattern que `scripts/run_extract_load.py` (point d'entrée hors Airflow, pas de DAG — il n'existe aucune façon d'automatiser la réception d'un export personnel depuis une app tierce, ce sera toujours un geste manuel de l'utilisateur qui fournit le fichier).

## Confidentialité

`export.csv` contient des données personnelles (ce que l'utilisateur possède, ce qu'il a payé) : ajouté à `.gitignore`, jamais commité dans ce repo portfolio public/privé.

## Vérification prévue

- Tests unitaires sur la logique de normalisation du numéro de carte et de construction du `card_id` (fonction pure, testable sans DB).
- Test d'intégration : importer un échantillon du vrai `export.csv`, vérifier le compte de cartes reliées vs. en quarantaine, et confirmer qu'un réimport identique ne duplique rien.
- **Résultat du rapprochement communiqué explicitement à l'utilisateur** (nombre de cartes reliées, nombre en quarantaine, sets non reconnus) — décision utilisateur explicite de vouloir ce retour avant de considérer l'import terminé.

## Hors scope (rappel)

- Table de correspondance manuelle pour les noms de sets non reconnus automatiquement (améliorera le taux de rapprochement plus tard).
- Les portfolios `MS - Paldea Evolved`/`MS - Paldean Fates` (suivi de progression de mastersets) — idée distincte, pas traitée ici.
- Construction effective du dashboard (Task 4 du Mois 3) — ce plan pose seulement les données, pas les graphiques.
