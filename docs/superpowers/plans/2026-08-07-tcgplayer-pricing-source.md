# Card Price Tracker — Bascule CardMarket → TCGPlayer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remplacer la source de prix CardMarket (agrégée toutes langues confondues, sans possibilité de filtrer sur l'anglais) par TCGPlayer, déjà présent dans le même payload pokemontcg.io déjà récupéré — sans changer de source de données ni de mécanisme d'extraction.

**Architecture:** Un seul point de lecture change (`src/transform/validate.py`, la fonction pure `validate_and_clean()`) : elle lit désormais `payload["tcgplayer"]["prices"]` au lieu de `payload["cardmarket"]["prices"]`, avec une logique de sélection de variante d'impression (normal/holofoil/reverseHolofoil/...). `src/load/warehouse_loader.py` change juste sa plateforme par défaut. Le schéma en étoile et ses colonnes (déjà génériques) ne changent pas.

**Tech Stack:** Identique à l'existant (Python 3.11, psycopg 3, PostgreSQL 16, pytest). Aucune nouvelle dépendance.

## Global Constraints

- Migrations SQL numérotées, jamais modifiées après merge (une correction = une nouvelle migration) — la 005 est nouvelle, les 001-004 restent intactes.
- Idempotence par UPSERT (`ON CONFLICT DO UPDATE`), jamais par delete-and-reload — déjà garanti par le code existant, ne pas y déroger.
- Aucun `DELETE` sur `prod.fact_price_history` : les données CardMarket déjà chargées restent en base, non supprimées, non écrasées.
- Tests unitaires sur la logique métier (validation, sans DB) + tests d'intégration contre la vraie base Postgres locale pour la persistance — même répartition que l'existant.
- Lint (`ruff`) et formatage (`black`) appliqués avant chaque commit.
- Logging structuré, zéro `print()`.

**Référence :** `docs/superpowers/specs/2026-08-07-tcgplayer-pricing-source-design.md` (spec approuvée, contient le raisonnement complet sur pourquoi TCGPlayer plutôt que CardMarket ou tcgdex.dev).

---

### Task 1: Seeder la plateforme TCGPlayer et en faire le défaut

**Files:**
- Create: `migrations/005_seed_tcgplayer_platform.sql`
- Modify: `src/load/warehouse_loader.py:110`

**Interfaces:**
- Consomme : `prod.dim_platform` (créée en migration 003).
- Produit : une ligne `platform_name = 'tcgplayer'` dans `prod.dim_platform`, et `load_staging_to_warehouse(conn, extracted_date, source="pokemontcg.io", platform_name="tcgplayer")` (signature inchangée, seule la valeur par défaut du 3e paramètre change).

- [ ] **Step 1: Créer `migrations/005_seed_tcgplayer_platform.sql`**

```sql
-- Migration 005 : ajoute "tcgplayer" comme plateforme de prix disponible dans
-- prod.dim_platform, en plus de "cardmarket" (seedée en migration 003).
--
-- Contexte (voir docs/superpowers/specs/2026-08-07-tcgplayer-pricing-source-design.md) :
-- les prix CardMarket relayés par pokemontcg.io sont agrégés toutes langues
-- confondues (anglais, allemand, japonais...), une limitation de Cardmarket
-- lui-même (leur "price guide" ne distingue ni langue ni état de la carte).
-- TCGPlayer, déjà présent dans le même payload pokemontcg.io, sépare les
-- cartes japonaises dans une ligne de produit distincte ("Pokemon Japan") :
-- les prix TCGPlayer pour un card_id pokemontcg.io (catalogue anglais
-- uniquement) représentent donc déjà spécifiquement le marché anglais.
--
-- On ne supprime PAS la ligne "cardmarket" ni les données déjà chargées sous
-- cette plateforme (prod.fact_price_history interdit tout DELETE, voir
-- migrations/003_create_star_schema.sql) : les deux plateformes coexistent,
-- "tcgplayer" devient simplement la nouvelle valeur par défaut côté pipeline
-- (voir src/load/warehouse_loader.py).

BEGIN;

INSERT INTO prod.dim_platform (platform_name) VALUES ('tcgplayer')
ON CONFLICT (platform_name) DO NOTHING;

COMMIT;
```

- [ ] **Step 2: Appliquer la migration en local**

Run: `./scripts/apply_migrations.sh`
Expected: `Applique : 005_seed_tcgplayer_platform.sql` s'affiche, sans erreur.

- [ ] **Step 3: Vérifier que la ligne existe**

Run:
```bash
docker compose exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "SELECT platform_name FROM prod.dim_platform ORDER BY platform_name;"
```
Expected: deux lignes, `cardmarket` et `tcgplayer`.

- [ ] **Step 4: Modifier `src/load/warehouse_loader.py`**

Ligne 110, dans la signature de `load_staging_to_warehouse` :
```python
def load_staging_to_warehouse(
    conn: Connection,
    extracted_date: date,
    source: str = "pokemontcg.io",
    platform_name: str = "tcgplayer",
) -> int:
```
(seule la valeur par défaut du 4e paramètre change, de `"cardmarket"` à `"tcgplayer"` — aucune autre ligne du fichier ne bouge).

- [ ] **Step 5: Relancer la suite de tests existante pour vérifier qu'elle passe toujours**

Run: `pytest tests/test_warehouse_loader.py -v`
Expected: 3 PASS. `test_load_staging_to_warehouse_inserts_fact` n'appelle `load_staging_to_warehouse()` sans préciser `platform_name` — ce test exerce donc maintenant implicitement la résolution de `"tcgplayer"` (nouveau défaut) au lieu de `"cardmarket"`. S'il échoue avec `RuntimeError: platform_name inconnu`, la migration du Step 1 n'a pas été appliquée (revenir au Step 2).

- [ ] **Step 6: Lint, format, commit**

```bash
ruff check . && black .
git add migrations/005_seed_tcgplayer_platform.sql src/load/warehouse_loader.py
git commit -m "feat: seed tcgplayer platform and make it the default for warehouse loading"
```

---

### Task 2: Lire les prix TCGPlayer dans validate_and_clean()

**Files:**
- Modify: `src/transform/validate.py`
- Modify: `tests/test_transform.py`

**Interfaces:**
- Consomme : rien de nouveau (même payload pokemontcg.io déjà stocké en `raw.card_prices.payload`, bloc `tcgplayer` au lieu de `cardmarket`).
- Produit : `validate_and_clean(payload: dict) -> ValidationResult` — signature et forme de `CleanedCard`/`ValidationResult` INCHANGÉES (Task 3 du Mois 2 et `staging_loader.py` n'ont rien à savoir de ce changement). Nouvelle fonction privée `_select_tcgplayer_variant(tcgplayer_prices: dict) -> dict`, interne à ce module, non exportée.

- [ ] **Step 1: Réécrire `tests/test_transform.py` (tests qui doivent échouer)**

Remplacer tout le contenu du fichier :
```python
# Tests unitaires de la couche de validation (src/transform/validate.py).
# Contrairement à tests/test_raw_loader.py, ces tests ne touchent PAS la base
# de données : validate_and_clean() est une fonction pure (payload dict ->
# ValidationResult), donc on peut la tester en mémoire, rapidement et sans
# dépendre d'un conteneur Postgres démarré. C'est précisément l'intérêt de
# séparer "valider/nettoyer" (pur) de "charger en base" (effet de bord) :
# on peut couvrir toutes les règles métier de validation par de simples
# assertions Python, sans fixture ni TRUNCATE.
from __future__ import annotations

from src.transform.validate import validate_and_clean


def _make_payload(**overrides) -> dict:
    # Construit un payload "carte pokemontcg.io" valide par défaut, avec la
    # forme exacte renvoyée par l'API (voir src/extract/pokemontcg_client.py) :
    # id/name au niveau racine, set imbriqué (id/name), prix imbriqués sous
    # tcgplayer.prices.<variante>. **overrides permet à chaque test de ne
    # modifier QUE le champ qui l'intéresse (ex: set=None) sans réécrire tout
    # le payload, ce qui rend chaque test lisible et concentré sur un seul cas.
    payload = {
        "id": "base1-1",
        "name": "Alakazam",
        "rarity": "Rare Holo",
        "set": {"id": "base1", "name": "Base"},
        "tcgplayer": {
            "prices": {
                "normal": {"low": 8.0, "mid": 13.0, "market": 12.5, "high": 20.0},
            }
        },
    }
    payload.update(overrides)
    return payload


def test_validate_and_clean_accepts_valid_payload() -> None:
    # Cas nominal : un payload complet et cohérent doit être accepté, et les
    # champs du CleanedCard doivent correspondre à ceux du payload d'origine.
    # market -> average_sell_price, low -> low_price, mid -> trend_price (voir
    # docs/superpowers/specs/2026-08-07-tcgplayer-pricing-source-design.md).
    result = validate_and_clean(_make_payload())

    assert result.is_valid
    assert result.cleaned.card_id == "base1-1"
    assert result.cleaned.average_sell_price == 12.5
    assert result.cleaned.low_price == 8.0
    assert result.cleaned.trend_price == 13.0


def test_validate_and_clean_rejects_missing_set() -> None:
    # Une carte sans information de set (id/name) n'est pas exploitable en
    # staging (set_id/set_name sont NOT NULL, voir migrations/002_...sql) :
    # elle doit être rejetée avec une raison mentionnant "set".
    result = validate_and_clean(_make_payload(set=None))

    assert not result.is_valid
    assert "set" in result.rejection_reason


def test_validate_and_clean_rejects_when_no_price_available() -> None:
    # Une carte sans AUCUN prix tcgplayer (prices vide) n'a pas d'intérêt pour
    # un pipeline de suivi de PRIX : mieux vaut la rejeter explicitement en
    # quarantaine (avec sa raison) plutôt que l'insérer en staging avec les 3
    # prix NULL, ce qui masquerait silencieusement un problème de données
    # source.
    result = validate_and_clean(_make_payload(tcgplayer={"prices": {}}))

    assert not result.is_valid
    assert "prix" in result.rejection_reason
    assert "tcgplayer" in result.rejection_reason


def test_validate_and_clean_rejects_negative_price() -> None:
    # Un prix négatif est physiquement impossible (une carte ne peut pas
    # avoir une valeur marchande négative) : c'est le signe d'une anomalie
    # de la source ou d'un bug, donc la carte est rejetée plutôt
    # qu'acceptée avec une donnée aberrante qui fausserait les analyses.
    result = validate_and_clean(
        _make_payload(tcgplayer={"prices": {"normal": {"market": -1.0}}})
    )

    assert not result.is_valid
    assert "négatif" in result.rejection_reason


def test_validate_and_clean_prefers_normal_over_holofoil_variant() -> None:
    # Si plusieurs variantes d'impression sont disponibles, "normal" doit être
    # choisie en priorité (ordre de priorité documenté dans le design spec) —
    # pas la première trouvée dans le dict ni la plus chère.
    result = validate_and_clean(
        _make_payload(
            tcgplayer={
                "prices": {
                    "holofoil": {"low": 6.0, "mid": 20.0, "market": 15.0},
                    "normal": {"low": 1.0, "mid": 2.0, "market": 1.5},
                }
            }
        )
    )

    assert result.is_valid
    assert result.cleaned.average_sell_price == 1.5


def test_validate_and_clean_prefers_holofoil_when_normal_absent() -> None:
    # Une carte Rare Holo n'a souvent AUCUNE variante "normal" (elle n'existe
    # qu'en holofoil) : la sélection doit alors retomber sur "holofoil",
    # deuxième priorité de la liste, pas rejeter la carte faute de "normal".
    result = validate_and_clean(
        _make_payload(
            tcgplayer={
                "prices": {
                    "reverseHolofoil": {"low": 4.0, "mid": 5.0, "market": 4.5},
                    "holofoil": {"low": 6.0, "mid": 9.0, "market": 7.5},
                }
            }
        )
    )

    assert result.is_valid
    assert result.cleaned.average_sell_price == 7.5


def test_validate_and_clean_falls_back_to_first_available_variant() -> None:
    # Si aucune variante de la liste de priorité n'est présente (ex: une
    # variante récente type "pokeBallPattern", non gérée spécifiquement en v1
    # — décision produit explicite, voir le design spec), on retombe sur la
    # première variante du payload plutôt que de rejeter la carte.
    result = validate_and_clean(
        _make_payload(
            tcgplayer={"prices": {"pokeBallPattern": {"low": 3.0, "mid": 4.0, "market": 3.5}}}
        )
    )

    assert result.is_valid
    assert result.cleaned.average_sell_price == 3.5
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `pytest tests/test_transform.py -v`
Expected: FAIL — les assertions sur `average_sell_price`/`low_price`/`trend_price` et sur `"tcgplayer" in result.rejection_reason` échouent, car `validate_and_clean` lit encore `cardmarket`.

- [ ] **Step 3: Modifier `src/transform/validate.py`**

Remplacer le commentaire de règle 3 et son bloc de code (lignes 105-124 actuelles) par :
```python
    # --- Règle 3 : au moins un prix tcgplayer disponible ---
    # TCGPlayer (contrairement à CardMarket) sépare les cartes japonaises
    # dans une ligne de produit distincte ("Pokemon Japan") : les prix
    # tcgplayer pour un card_id pokemontcg.io (catalogue anglais uniquement)
    # représentent donc déjà spécifiquement le marché anglais — voir
    # docs/superpowers/specs/2026-08-07-tcgplayer-pricing-source-design.md
    # pour le raisonnement complet (bascule depuis cardmarket, agrégé toutes
    # langues confondues par conception chez Cardmarket lui-même).
    #
    # TCGPlayer structure ses prix par VARIANTE D'IMPRESSION (normal,
    # holofoil, reverseHolofoil...), contrairement à cardmarket qui n'avait
    # qu'un seul jeu de prix par carte. _select_tcgplayer_variant() choisit
    # laquelle utiliser selon un ordre de priorité déterministe.
    tcgplayer_prices = (payload.get("tcgplayer") or {}).get("prices") or {}
    selected_variant = _select_tcgplayer_variant(tcgplayer_prices)
    average_sell_price = selected_variant.get("market")
    trend_price = selected_variant.get("mid")
    low_price = selected_variant.get("low")

    # Si les 3 prix sont absents, la carte n'apporte rien à un pipeline dont
    # le but est justement de SUIVRE DES PRIX : on la rejette explicitement
    # plutôt que de l'insérer en staging avec average_sell_price/trend_price/
    # low_price tous NULL. Insérer quand même produirait une ligne "muette"
    # en staging qui pollue silencieusement les agrégats du prod (moyennes,
    # tendances) sans qu'aucune alerte ne signale le problème de données
    # source. En la routant vers card_prices_quarantine avec une raison
    # explicite, le problème reste visible et traçable pour un audit manuel.
    if average_sell_price is None and trend_price is None and low_price is None:
        return ValidationResult(cleaned=None, rejection_reason="aucun prix tcgplayer disponible")
```

Puis remplacer les labels de la règle 4 (boucle de détection des prix négatifs, juste après) pour utiliser le vocabulaire TCGPlayer :
```python
    for label, value in [
        ("market", average_sell_price),
        ("mid", trend_price),
        ("low", low_price),
    ]:
        if value is not None and value < 0:
            return ValidationResult(
                cleaned=None, rejection_reason=f"prix négatif ({label}={value})"
            )
```

Enfin, ajouter la nouvelle fonction privée `_select_tcgplayer_variant`, juste avant `validate_and_clean` (après la définition de `ValidationResult`) :
```python
# Ordre de priorité des variantes d'impression TCGPlayer, du plus courant au
# moins courant. "normal" en tête car c'est la variante la plus représentative
# pour une carte qui en dispose ; les holo/reverseHolofoil ne sont utilisées
# que si "normal" n'existe pas (cas fréquent des cartes Rare Holo, qui
# n'existent QUE dans ces variantes).
_VARIANT_PRIORITY = ["normal", "holofoil", "reverseHolofoil", "1stEditionHolofoil"]


def _select_tcgplayer_variant(tcgplayer_prices: dict) -> dict:
    """Choisit quelle variante d'impression utiliser parmi celles disponibles
    dans tcgplayer.prices, selon _VARIANT_PRIORITY. Retombe sur la première
    variante disponible (ordre du payload d'origine, ex: "pokeBallPattern",
    "masterBallPattern") si aucune des priorités nommées n'est présente —
    ces variantes récentes ne sont volontairement pas traitées spécifiquement
    en v1 (décision produit explicite, voir le design spec). Renvoie {} si
    aucune variante du tout n'est disponible."""
    for variant in _VARIANT_PRIORITY:
        if variant in tcgplayer_prices:
            return tcgplayer_prices[variant]
    if tcgplayer_prices:
        return next(iter(tcgplayer_prices.values()))
    return {}
```

- [ ] **Step 4: Relancer les tests pour vérifier qu'ils passent**

Run: `pytest tests/test_transform.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Lancer la suite complète pour vérifier l'absence de régression**

Run: `pytest -v`
Expected: tous les tests passent, y compris `tests/test_staging_loader.py` et `tests/test_warehouse_loader.py` (qui construisent leurs propres `CleanedCard` directement, sans passer par `validate_and_clean` — donc non affectés par ce changement, mais à vérifier explicitement plutôt que supposé).

- [ ] **Step 6: Lint, format, commit**

```bash
ruff check . && black .
git add src/transform/validate.py tests/test_transform.py
git commit -m "feat: read tcgplayer prices instead of cardmarket, with variant-priority selection"
```

---

### Task 3: Déployer et backfiller les données déjà en prod

**Files:**
- (Aucun nouveau fichier — déploiement du code des Tasks 1-2 sur le VPS déjà provisionné, Mois 3.)

**Interfaces:**
- Consomme : `clean_raw_to_staging`/`load_staging_to_warehouse` (Tasks 1-2), le `raw.card_prices` déjà stocké en prod pour `extracted_date=2026-08-07` (Mois 3, Task 2).
- Produit : des lignes `platform_name='tcgplayer'` dans `prod.fact_price_history` pour les cartes déjà extraites, sans toucher aux lignes `cardmarket` existantes.

- [ ] **Step 1: Pousser les commits des Tasks 1-2**

```bash
git push
```

- [ ] **Step 2: Récupérer le code et appliquer la migration sur la VM**

```bash
ssh card-tracker-vm
cd ~/card-price-tracker
git pull
./scripts/apply_migrations.sh docker-compose.prod.yml
```
Expected: `Applique : 005_seed_tcgplayer_platform.sql`, sans erreur.

- [ ] **Step 3: Relancer le nettoyage + chargement warehouse contre le raw déjà stocké**

Sur la VM (le code est monté en volume dans les conteneurs Airflow déjà en cours d'exécution, voir `docker-compose.prod.yml` — pas besoin de rebuild, les fichiers `.py` modifiés sont immédiatement visibles par tout nouveau sous-processus `airflow tasks run`) :
```bash
docker compose -f docker-compose.prod.yml exec airflow-scheduler python -c "
from datetime import date
from src.common.config import load_db_config
from src.common.db import get_connection
from src.transform.clean import clean_raw_to_staging
from src.load.warehouse_loader import load_staging_to_warehouse

extracted_date = date(2026, 8, 7)
with get_connection(load_db_config()) as conn:
    valid, rejected = clean_raw_to_staging(conn, extracted_date)
    print(f'staging: {valid} valides, {rejected} rejetees')
with get_connection(load_db_config()) as conn:
    loaded = load_staging_to_warehouse(conn, extracted_date)
    print(f'warehouse: {loaded} faits charges (plateforme tcgplayer)')
"
```
Expected: deux lignes affichées, sans exception. Le nombre de "valides" en staging peut différer légèrement du run cardmarket précédent (couverture TCGPlayer probablement différente, voir le design spec) — ce n'est pas une erreur en soi.

- [ ] **Step 4: Vérifier les résultats en base**

```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "
SELECT dp.platform_name, count(*)
FROM prod.fact_price_history fph
JOIN prod.dim_platform dp ON dp.platform_id = fph.platform_id
GROUP BY dp.platform_name;
"
```
Expected: deux lignes, `cardmarket` (le compte inchangé d'avant ce plan) ET `tcgplayer` (nouveau), confirmant que les anciennes données n'ont pas été supprimées et que les nouvelles ont bien été ajoutées à côté.

- [ ] **Step 5: Vérifier un échantillon de prix concrets**

```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "
SELECT dc.name, dc.set_name, fph_cm.average_sell_price AS prix_cardmarket_eur, fph_tcg.average_sell_price AS prix_tcgplayer_usd
FROM prod.dim_card dc
JOIN prod.fact_price_history fph_cm ON fph_cm.card_id = dc.card_id
JOIN prod.dim_platform dp_cm ON dp_cm.platform_id = fph_cm.platform_id AND dp_cm.platform_name = 'cardmarket'
JOIN prod.fact_price_history fph_tcg ON fph_tcg.card_id = dc.card_id AND fph_tcg.date_id = fph_cm.date_id
JOIN prod.dim_platform dp_tcg ON dp_tcg.platform_id = fph_tcg.platform_id AND dp_tcg.platform_name = 'tcgplayer'
LIMIT 10;
"
```
Expected: 10 lignes montrant les deux prix côte à côte (devises différentes, EUR vs USD — écart de valeur normal et attendu, pas un signe d'erreur).

- [ ] **Step 6: Documenter dans le ledger de suivi du projet**

Aucun commit de code ici (Steps 1-2 ont déjà tout commité) — juste consigner dans `.superpowers/sdd/2026-08-05-card-price-tracker-month3/progress.md` (ou équivalent en cours) que le backfill a été fait, avec les comptes obtenus au Step 4, pour référence future.

---

## Self-Review Notes

- **Couverture du spec** : lecture tcgplayer + sélection de variante ✓ (Task 2), migration + défaut plateforme ✓ (Task 1), backfill sans réappel API ni suppression des données cardmarket ✓ (Task 3), devise USD documentée ✓ (Task 3 Step 5, commentaire migration), variantes Pokeball/Masterball explicitement hors scope ✓ (Task 2, docstring + test de repli générique).
- **Cohérence des types** : `_select_tcgplayer_variant(tcgplayer_prices: dict) -> dict` (Task 2) a la même signature partout où elle est utilisée (un seul appel, dans `validate_and_clean`). `CleanedCard`/`ValidationResult` restent inchangés — aucune tâche en aval (staging_loader, warehouse_loader) n'a besoin d'être modifiée pour cette raison, seul le défaut `platform_name` change (Task 1, indépendant du changement de Task 2).
- **Ordre des tasks** : Task 1 avant Task 2 n'est pas strictement requis techniquement (les deux sont indépendantes), mais Task 1 doit être fait avant Task 3 (le backfill a besoin que `tcgplayer` soit une plateforme connue en base). Task 2 doit aussi être fait avant Task 3 (le backfill doit lire le nouveau code, pas l'ancien). Task 3 dépend donc de Task 1 ET Task 2, qui n'ont pas de dépendance entre elles.
