# Card Price Tracker — Bascule CardMarket → TCGPlayer comme source de prix — Design

**Statut :** implémenté et déployé en production (2026-08-07). Backfill exécuté sans réappel API : `tcgplayer` 19693 lignes, `cardmarket` 19085 lignes conservées intactes.

## Contexte et problème

En explorant les données réellement chargées en prod (Mois 3, après déploiement sur le VPS), l'utilisateur a repéré que les prix CardMarket relayés par pokemontcg.io sont **agrégés toutes langues confondues** (anglais, allemand, japonais, français...). Vérifié directement dans la documentation Cardmarket : leur fonctionnalité "price guide" (celle qu'on consomme) ne distingue ni la langue ni l'état de la carte, par conception. Aucun champ de langue n'existe non plus dans le payload pokemontcg.io lui-même (vérifié dans `raw.card_prices.payload` déjà stocké).

**Piste explorée et écartée :** migrer vers `tcgdex.dev` (mentionné pour son support de 14 langues). Vérification directe de leur documentation API : leur prix Cardmarket a **exactement la même limitation** ("Les données ne sont pas spécifiques à une langue... Cardmarket fournit des prix agrégés... indépendamment de la langue d'interrogation"). tcgdex.dev catalogue mieux les cartes multi-langues, mais le prix lui-même vient de la même donnée Cardmarket sous-jacente, donc migrer n'aurait rien résolu.

**Piste retenue :** le payload pokemontcg.io contient déjà, en plus de `cardmarket`, un bloc `tcgplayer` avec des prix distincts (vérifié directement dans nos données stockées). TCGPlayer sépare les cartes japonaises dans une ligne de produit distincte ("Pokemon Japan"), séparée du catalogue anglais standard — donc les prix TCGPlayer pour un `card_id` pokemontcg.io (qui ne catalogue que des cartes anglaises) représentent déjà spécifiquement le marché anglais.

## Décision

Ne pas changer de source de données (toujours pokemontcg.io, même client HTTP, même schéma raw). Changer uniquement quel champ du payload déjà stocké est lu par la couche transformation, de `cardmarket.prices.*` vers `tcgplayer.prices.*`.

## Mapping des champs

TCGPlayer structure ses prix par variante d'impression (`normal`, `holofoil`, `reverseHolofoil`, `1stEditionHolofoil`, etc.), chacune avec `low`/`mid`/`high`/`market`/`directLow`. On garde les colonnes existantes (déjà génériques, non préfixées "cardmarket") :

| Champ TCGPlayer (variante sélectionnée) | Colonne existante | Justification |
|---|---|---|
| `market` | `average_sell_price` | Le "juste prix" TCGPlayer — équivalent sémantique le plus proche d'un prix de vente moyen |
| `low` | `low_price` | Correspondance directe |
| `mid` | `trend_price` | **Changement de sémantique à documenter** : TCGPlayer n'a pas d'historique 1/7/30 jours comme CardMarket. `mid` (prix médian entre `low` et `high`) est utilisé ici comme approximation la plus proche disponible, pas une vraie tendance temporelle. |

## Sélection de la variante

Une carte peut avoir plusieurs variantes d'impression avec des prix différents. Ordre de priorité déterministe :

1. `normal`
2. `holofoil`
3. `reverseHolofoil`
4. `1stEditionHolofoil`
5. sinon : la première variante disponible dans le payload (clé arbitraire mais déterministe — `dict` préserve l'ordre d'insertion en Python 3.7+, donc l'ordre renvoyé par l'API)

**Hors scope explicite de cette v1 (décision utilisateur)** : les variantes "Pokeball Pattern" / "Masterball Pattern" (introduites sur des éditions récentes) ne sont pas traitées spécifiquement — elles tombent dans le cas de repli générique (règle 5) sans logique dédiée. Ce n'est pas un oubli : l'utilisateur a explicitement priorisé le traitement correct de la masse (normal/reverse/holo) et des raretés classiques (Art Rare, etc.) avant ces cas plus rares et récents. Une meilleure gestion de ces patterns spécifiques pourra faire l'objet d'une itération future si besoin.

## Schéma / migrations

Aucune colonne existante ne change (noms déjà génériques). Une nouvelle migration est nécessaire pour seeder la plateforme `tcgplayer` dans `prod.dim_platform` (même pattern que le seed `cardmarket` existant dans `migrations/003_create_star_schema.sql`) :

```sql
INSERT INTO prod.dim_platform (platform_name) VALUES ('tcgplayer')
ON CONFLICT (platform_name) DO NOTHING;
```

Migrations numérotées, jamais modifiées après merge (contrainte du projet) : ceci devient `migrations/005_seed_tcgplayer_platform.sql`, pas une édition de la 003.

`src/load/warehouse_loader.py` : le paramètre `platform_name` de `load_staging_to_warehouse()` passe de `"cardmarket"` à `"tcgplayer"` comme valeur par défaut.

## Données déjà en base (2026-08-07)

Le schéma interdit déjà tout `DELETE` sur `prod.fact_price_history` (immutabilité de l'historique des prix, contrainte actée dès le Mois 2). Les lignes déjà chargées avec `platform_name='cardmarket'` restent en l'état comme historique — rien n'est supprimé ni écrasé (la contrainte `UNIQUE (card_id, date_id, platform_id)` fait que `cardmarket` et `tcgplayer` coexistent comme deux `platform_id` distincts, sans conflit).

**Backfill** : re-jouer `clean_to_staging` + `load_staging_to_warehouse` contre le `raw.card_prices` déjà stocké pour `extracted_date=2026-08-07` — pas besoin de re-frapper l'API pokemontcg.io, le payload complet (bloc `tcgplayer` inclus) y est déjà. Ça peuple immédiatement les prix `tcgplayer` pour la journée déjà extraite, en plus de valider le nouveau mapping contre de vraies données.

## Autres changements

- **Devise** : les prix TCGPlayer sont en USD, contre EUR pour CardMarket — à documenter clairement (commentaire dans le code + README du Mois 3, Task 3).
- **Message de rejet** (`src/transform/validate.py`) : `"aucun prix cardmarket disponible"` devient `"aucun prix tcgplayer disponible"`.
- **Couverture** : probablement meilleure que CardMarket pour ce catalogue (TCGPlayer = marché domestique des cartes anglaises qu'on utilise déjà), mais pas garantie a priori — à confirmer une fois implémenté, sans en faire une hypothèse bloquante du design.

## Tests à adapter

`tests/test_transform.py` mocke actuellement des payloads en forme CardMarket (`cardmarket.prices.{averageSellPrice,trendPrice,lowPrice}`). Ces fixtures doivent être réécrites en forme TCGPlayer (`tcgplayer.prices.<variante>.{low,mid,high,market}`), y compris un cas couvrant explicitement la logique de sélection de variante (ex : une carte avec `holofoil` ET `reverseHolofoil` disponibles doit choisir `holofoil` en priorité).

## Hors scope (rappel)

- Aucun changement de source de données (toujours pokemontcg.io).
- Aucune gestion spéciale des variantes Pokeball/Masterball Pattern (repli générique accepté).
- Aucune suppression des données CardMarket déjà chargées.
